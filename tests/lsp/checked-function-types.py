#!/usr/bin/env python3
"""Keep checked numeric types consistent across CLI and LSP assists."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workspace = load('workspace', 'workspace-overlays.py')
boundaries = load('boundaries', 'memory-boundaries.py')


def run(compiler):
    compiler = str(Path(compiler).resolve())
    root = Path(__file__).resolve().parents[2]
    os.environ.setdefault('AXIOM_STDLIB', str(root / 'stdlib'))
    with tempfile.TemporaryDirectory(prefix='axiom-checked-types-') as directory:
        path = Path(directory) / 'Numbers.ax'
        source = '''(fn (value) 2.5)
(fn (fraction n) (/ (as n Float) 2.0))
(:: main Int)
(fn (main) (as (+ value (fraction 5)) Int))
'''
        path.write_text(source)
        assert boundaries.terminal(compiler, path) == []
        symbols = subprocess.run([compiler, '--diagnostic-format=ai', 'symbols', str(path)],
                                 capture_output=True, text=True, timeout=60)
        assert symbols.returncode == 0, symbols.stderr
        for name, signature in (('value', '"Float"'), ('fraction', '"(Int -> Float)"')):
            line = next(line for line in symbols.stdout.splitlines()
                        if line.startswith('F ' + name + ' '))
            assert signature in line, line
        client = workspace.Client(compiler, cwd=path.parent)
        try:
            client.request('initialize', {})
            client.opening(path.as_uri(), source)
            assert client.latest(path.as_uri())['diagnostics'] == []
            actions = client.request('textDocument/codeAction', {
                'textDocument': {'uri': path.as_uri()},
                'range': {'start': boundaries.position(source, 0),
                          'end': boundaries.position(source, len(source))},
                'context': {'diagnostics': []}})
            for name, signature in (('value', '(:: value Float)\n'),
                                    ('fraction', '(:: fraction (-> Int Float))\n')):
                action = next(action for action in actions
                              if action['title'] == 'Add type signature for `' + name + '`')
                edits = action['edit']['changes'][path.as_uri()]
                assert len(edits) == 1 and edits[0]['newText'] == signature, edits

            # Hover quotes written signatures; apply the inferred signatures
            # and confirm they remain valid and visible through that surface.
            signed = source.replace('(fn (value)', '(:: value Float)\n(fn (value)')
            signed = signed.replace('(fn (fraction n)', '(:: fraction (-> Int Float))\n(fn (fraction n)')
            path.write_text(signed)
            assert boundaries.terminal(compiler, path) == []
            client.change(path.as_uri(), signed, 2)
            assert client.latest(path.as_uri())['diagnostics'] == []
            for name in ('value', 'fraction'):
                hover = client.request('textDocument/hover', {
                    'textDocument': {'uri': path.as_uri()},
                    'position': boundaries.position(signed, signed.index(name) + 1)})
                assert hover and '(:: ' + name + ' ' in hover['contents']['value'], (name, hover)
                assert 'Float' in hover['contents']['value'], (name, hover)
            refused = source.replace('(fraction 5)', '(fraction "wrong")')
            path.write_text(refused)
            cli = boundaries.terminal(compiler, path)
            assert [item['code'] for item in cli] == ['AX3004'], cli
            client.change(path.as_uri(), refused, 3)
            diagnostics = client.latest(path.as_uri())['diagnostics']
            assert [item['code'] for item in diagnostics] == ['AX3004'], diagnostics
            span = cli[0]['span']
            expected = {'start': boundaries.position(refused, span['char_start']),
                        'end': boundaries.position(refused, span['char_end'])}
            assert diagnostics[0]['range'] == expected, (diagnostics, cli)
            # Expansion failure carries diagnostics without constructing a
            # checker context. Ordinary source lint fixes still work, while
            # signature assists must wait for a successful checked context.
            expansion_refused = """(macro refuse ((refuse) (syntax/arity Ghost)))
(fn (value) (refuse))
(fn (choose yes) (if yes true false))
"""
            path.write_text(expansion_refused)
            cli = boundaries.terminal(compiler, path)
            assert [item['code'] for item in cli] == ['AX3028'], cli
            client.change(path.as_uri(), expansion_refused, 4)
            diagnostics = client.latest(path.as_uri())['diagnostics']
            errors = [item for item in diagnostics if item['severity'] == 1]
            assert [item['code'] for item in errors] == ['AX3028'], diagnostics
            span = cli[0]['span']
            expected = {'start': boundaries.position(expansion_refused, span['char_start']),
                        'end': boundaries.position(expansion_refused, span['char_end'])}
            assert errors[0]['range'] == expected, (errors, cli)
            assert cli[0]['message'] in errors[0]['message'], (errors, cli)
            actions = client.request('textDocument/codeAction', {
                'textDocument': {'uri': path.as_uri()},
                'range': {'start': boundaries.position(expansion_refused, 0),
                          'end': boundaries.position(expansion_refused, len(expansion_refused))},
                'context': {'diagnostics': diagnostics}})
            assert not [action for action in actions
                        if action['title'].startswith('Add type signature for `')], actions
            fixes = [action for action in actions if action.get('kind') == 'quickfix'
                     and [item['code'] for item in action.get('diagnostics', [])] == ['lint-bool-if']]
            assert len(fixes) == 1, actions
            edits = fixes[0]['edit']['changes'][path.as_uri()]
            insertion = boundaries.position(expansion_refused, expansion_refused.index('(fn (choose'))
            assert edits == [{'range': {'start': insertion, 'end': insertion},
                              'newText': ';@axiom:nolint(lint-bool-if)\n'}], edits
            lint_fixed = expansion_refused.replace('(fn (choose', edits[0]['newText'] + '(fn (choose')
            client.change(path.as_uri(), lint_fixed, 5)
            assert [item['code'] for item in client.latest(path.as_uri())['diagnostics']] == ['AX3028']

            path.write_text(source)
            client.change(path.as_uri(), source, 6)
            assert client.latest(path.as_uri())['diagnostics'] == []
            actions = client.request('textDocument/codeAction', {
                'textDocument': {'uri': path.as_uri()},
                'range': {'start': boundaries.position(source, 0),
                          'end': boundaries.position(source, len(source))},
                'context': {'diagnostics': []}})
            for name in ('value', 'fraction'):
                assert any(action['title'] == 'Add type signature for `' + name + '`'
                           for action in actions), actions
        finally:
            client.close()
    print('ok   checked function types: CLI/signature/hover agree; checker and expansion errors preserve diagnostics, lint fixes and recovery')


if __name__ == '__main__':
    run(sys.argv[1])
