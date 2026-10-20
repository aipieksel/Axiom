#!/usr/bin/env python3
"""Keep unresolved callback aliases distinct from established effect bounds."""
import argparse
from pathlib import Path
import os
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
REFUSAL = ROOT / 'tests/diagnostics/1482-callback-alias-effects.ax'
POSITIVE = ROOT / 'tests/selfhost/1042-callback-alias-effects.ax'
UNKNOWN = ('fieldAlias', 'resultAlias', 'patternAlias', 'mutableAlias',
           'capturedAlias', 'direct')


def run(compiler, command, source, *arguments):
    return subprocess.run(
        [str(compiler), '--diagnostic-format=ai', command, str(source), *arguments],
        cwd=ROOT, env=dict(os.environ, AXIOM_STDLIB=str(ROOT / 'stdlib')),
        capture_output=True, text=True, timeout=120)


def rows(result):
    return {line.split()[1]: line for line in result.stdout.splitlines()
            if line.startswith('F ')}


def test(compiler, work, native=True):
    checks = 0
    result = run(compiler, 'symbols', REFUSAL)
    assert result.returncode == 1, result.stderr
    actual = rows(result)
    for name in UNKNOWN:
        assert '#effects-incomplete' in actual[name], (name, actual[name])
        assert any('E AX3057 ' in line and f'`{name}` claims' in line
                   for line in result.stderr.splitlines()), (name, result.stderr)
        checks += 1
    for name in ('known', 'forwarded'):
        assert '#effects-incomplete' not in actual[name], actual[name]
    assert '#effect-params=f' in actual['forwarded'], actual['forwarded']
    assert result.stderr.count('W AX3037 ') == 5, result.stderr
    assert result.stderr.count('E AX3057 ') == 6, result.stderr
    checks += 1

    # Without strict restrictions, uncertainty stays visible as a warning.
    source = work / 'warnings.ax'
    source.write_text(REFUSAL.read_text().replace(';@axiom:restrict(no-io, strict)\n', ''))
    result = run(compiler, 'check', source)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count('W AX3037 ') == 5, result.stderr
    assert not any(line.startswith('E ') for line in result.stderr.splitlines()), result.stderr
    checks += 1

    # No effect's absence is established from an unknown call result.
    template = '''
(:: invoke (-> (-> Int Int) Int Int))
;@axiom:effect(pure)
(fn (invoke f n) (f n))
(:: identity (-> (-> Int Int) (-> Int Int)))
(fn (identity f) f)
(:: unresolved (-> (-> Int Int) Int Int))
;@axiom:restrict(RESTRICTION, strict)
(fn (unresolved f n) { (let callback (identity f)) (invoke callback n) })
(:: main Int)
(fn (main) 0)
'''
    for restriction in ('no-io', 'no-alloc', 'no-unsafe', 'no-entropy',
                        'no-spawn', 'no-block'):
        source = work / (restriction + '.ax')
        source.write_text(template.replace('RESTRICTION', restriction))
        result = run(compiler, 'symbols', source)
        assert result.returncode == 1, (restriction, result.stderr)
        assert 'E AX3057 ' in result.stderr, (restriction, result.stderr)
        assert '#effects-incomplete' in rows(result)['unresolved'], result.stdout
        checks += 1

    # A callee that never calls its argument needs no callback effect bound.
    source = work / 'unused.ax'
    source.write_text('''
(struct Cell (run : (-> Int Int)))
(:: ignore (-> (-> Int Int) Int Int))
(fn (ignore f n) n)
(:: unused (-> Cell Int Int))
;@axiom:effect(pure)
;@axiom:restrict(no-io, no-alloc, strict)
(fn (unused cell n) { (let callback cell.run) (ignore callback n) })
(:: main Int)
(fn (main) 0)
''')
    result = run(compiler, 'symbols', source)
    assert result.returncode == 0 and not result.stderr, result.stderr
    assert '#effects-incomplete' not in rows(result)['unused'], result.stdout
    checks += 1

    for command in ('emit-llvm', 'build'):
        output = work / command
        sentinel = b'previous output\n'
        output.write_bytes(sentinel)
        result = run(compiler, command, REFUSAL, '-o', str(output))
        assert result.returncode == 1 and 'E AX3057 ' in result.stderr, result.stderr
        assert output.read_bytes() == sentinel, (command, 'replaced refused output')
        assert 'target triple' not in result.stdout, (command, 'emitted refused IR')
        checks += 1

    result = run(compiler, 'symbols', POSITIVE)
    assert result.returncode == 0 and not result.stderr, result.stderr
    actual = rows(result)
    for name in ('known', 'chosen', 'stored', 'forwarded', 'scalar'):
        assert '#effects-incomplete' not in actual[name], actual[name]
    for name in ('forwarded', 'scalar'):
        assert '#effect-params=f' in actual[name], actual[name]
    checks += 1
    if native:
        output = work / 'positive'
        result = run(compiler, 'build', POSITIVE, '-o', str(output))
        assert result.returncode == 0 and not result.stderr, result.stderr
        result = subprocess.run([str(output)], capture_output=True, timeout=15)
        assert result.returncode == 42, (result.returncode, result.stderr)
        checks += 1
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('compiler', type=Path)
    parser.add_argument('--control', type=Path,
                        help='compiler with the alias enforcement removed; must fail this test')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='axiom-callback-alias-') as directory:
        work = Path(directory)
        checks = test(args.compiler.resolve(), work)
        if args.control:
            result = run(args.control.resolve(), 'symbols', work / 'no-io.ax')
            assert result.returncode == 0 and not result.stderr, result.stderr
            row = rows(result)['unresolved']
            assert '#effects-incomplete' not in row, row
            assert '#restrict=no-io,' in row, row
            print('ok   negative control silently certifies the unresolved callback alias')
            source = work / 'observable.ax'
            source.write_text(
                '(import IO)\n' + (work / 'no-io.ax').read_text().replace(
                    '(fn (unresolved ', ';@axiom:effect(pure)\n(fn (unresolved ').replace(
                    '(:: main Int)\n(fn (main) 0)',
                    '(:: loud (-> Int Int))\n;@axiom:effect(io)\n'
                    '(fn (loud n) { (println "callback ran") n })\n'
                    '(:: main Int)\n;@axiom:effect(io)\n'
                    '(fn (main) (unresolved loud 42))'))
            result = run(args.compiler.resolve(), 'check', source)
            assert result.returncode == 1 and 'E AX3057 ' in result.stderr, result.stderr
            output = work / 'observable'
            result = run(args.control.resolve(), 'build', source, '-o', str(output))
            assert result.returncode == 0 and not result.stderr, result.stderr
            result = subprocess.run([str(output)], capture_output=True, timeout=15)
            assert result.returncode == 42 and result.stdout == b'callback ran\n', result
            print('ok   negative control performs IO behind its accepted pure and strict no-io claims')
        print(f'ok   callback alias effects: {checks} refusal, uncertainty and valid controls')


if __name__ == '__main__':
    main()
