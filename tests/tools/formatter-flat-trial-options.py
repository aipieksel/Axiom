#!/usr/bin/env python3
"""Check flat trial failure separately from successful empty formatter output."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(compiler, root, work):
    module = root / 'core/format.ax'; source = module.read_text()
    fixture = root / 'tests/tools/formatter-flat-trial-options.axp'
    golden_path = root / 'tests/tools/formatter-flat-trial-options.golden'
    golden = json.loads(golden_path.read_text())
    extra = fixture.read_text()
    for import_line in ('(import IO)', '(import Fmt)', '(import Sys)'):
        extra = extra.replace(import_line, '')
    imports = '(import IO)\n(import Fmt)\n(import Sys)\n'
    environment = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'), AXIOM_PATH=str(root / 'core'))
    expected = ''.join(f'{i} true\n' for i in range(1, 41))
    report = {'compiler': str(compiler), 'compiler_sha256': digest(compiler),
              'source_sha256': digest(module), 'fixture_sha256': digest(fixture),
              'golden_sha256': digest(golden_path), 'configurations': [], 'controls': [],
              'refusals': [], 'formats': [],
              'invariants': ['Trial failure is None; a completed empty output is Some empty.',
                             'Consumers retain their nonempty/width policy inside Some and their exact None fallback.',
                             'Trials do not change the original output flags, comment cursor or suffix width.',
                             'Returned trial text owns its buffer; trials and actual formatting borrow source and nodes.'],
              'remaining': ['FRes status/text record requires separate semantic classification'],
              'limits': ['Host O0/O2 allocation-poison probes; CI runs complete target and formatter suites.',
                         'FRes and renderer algorithms remain unchanged; no call emission or ownership inference work.',
                         'Input borrow counts do not establish global allocation or destructor completeness.']}

    def build(label, text, level=0):
        path = work / (label + '.ax'); path.write_text(imports + text + '\n' + extra)
        executable = work / label
        built = subprocess.run([str(compiler), '--diagnostic-format=ai', 'build', str(path),
                                '--opt', str(level), '--alloc-poison', '-o', str(executable)],
                               cwd=root, env=environment, capture_output=True, text=True, timeout=240)
        (work / (label + '.build.stdout')).write_text(built.stdout)
        (work / (label + '.build.stderr')).write_text(built.stderr)
        assert built.returncode == 0, (label, built.returncode, built.stdout, built.stderr)
        return executable

    binaries = []
    for level in (0, 2):
        executable = build('trials-O' + str(level), source, level)
        result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True, timeout=30)
        (work / ('trials-O' + str(level) + '.stdout')).write_text(result.stdout)
        assert result.returncode == 0 and not result.stderr and result.stdout == expected, result
        binaries.append(executable)
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True,
                                         'outcomes': 40, 'status': 'passed'})
        print(f'ok   flat trials: 40 outcomes O{level}', flush=True)

    def alter(name, old, new, count=1):
        definition = re.search(r'\((?:pub )?fn \(' + re.escape(name) + r'\s', source)
        assert definition, name
        start = definition.start()
        next_signature = re.search(r'^\((?:pub )?:: ', source[start:], re.M)
        end = start + next_signature.start() if next_signature else len(source)
        body = source[start:end]
        assert body.count(old) == count, (name, old, body.count(old))
        return source[:start] + body.replace(old, new) + source[end:]

    controls = [
        ('discard-empty-item', alter('fpListItemFlat', '(Some (foFinish out))',
                                     '(if (== (strLen (foFinish out)) 0) None (Some (foFinish out)))'), {21}),
        ('discard-empty-list', alter('fpListFlat', '(Some (foFinish out))',
                                     '(if (== (strLen (foFinish out)) 0) None (Some (foFinish out)))'), {22, 24}),
        ('invent-failed-expression', alter('fpExprFlat', 'None', '(Some "")'), {7, 17, 25}),
        ('ignore-app-comments', alter('fpAppFlat', '(if (fqAnyWithin q (fnStart n) (fnEnd n))',
                                     '(if false'), {13}),
        ('change-empty-inline-policy', alter('fpTypeAfter', '(> (strLen flat) 0)', 'true'), {33}),
    ]
    for label, value, failures in [('retain-trial-source', 'p.src', {29}),
                                    ('retain-trial-node', 'n', {30})]:
        controls.append((label, alter('fpExprFlat', '{\n    (let out',
                                      '{\n    (__retainref ' + value + ')\n    (let out'), failures))
    for label, changed, failures in controls:
        executable = build(label, changed)
        result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True, timeout=30)
        failed = {int(line.split()[0]) for line in result.stdout.splitlines() if line.endswith(' false')}
        assert result.returncode == 0 and not result.stderr and failed == failures, (
            label, failed, failures, result.returncode, result.stdout, result.stderr)
        report['controls'].append({'name': label, 'failed_cases': sorted(failed)})
        print(f'ok   control {label}: {sorted(failed)}', flush=True)

    invalid = [
        ('app-string-result', '(:: bad (-> FPr FNode (Vec FNode) String)) (fn (bad p n xs) (fpAppFlat p n xs))', 'AX3004'),
        ('expr-string-result', '(:: bad (-> FPr FNode String)) (fn (bad p n) (fpExprFlat p n))', 'AX3004'),
        ('named-string-result', '(:: bad (-> FPr FNode (Vec FNode) String)) (fn (bad p n xs) (fpNamedFlat p n xs))', 'AX3004'),
        ('item-string-result', '(:: bad (-> FPr FNode String)) (fn (bad p n) (fpListItemFlat p n 0))', 'AX3004'),
        ('list-string-result', '(:: bad (-> FPr FNode (Vec FNode) String)) (fn (bad p n xs) (fpListFlat p n xs 2 "" ""))', 'AX3004'),
        ('extern-string-result', '(:: bad (-> FPr FNode String)) (fn (bad p n) (fpExternFlat p n))', 'AX3004'),
        ('unchecked-trial-match', '(:: bad (-> FPr FNode String)) (fn (bad p n) (match (fpExprFlat p n) ((Some text) text)))', 'AX3005'),
    ]
    for label, declaration, code in invalid:
        path = work / (label + '.ax'); path.write_text(source + '\n' + declaration + '\n(:: main Int) (fn (main) 0)\n')
        result = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(path)],
                                cwd=root, env=environment, capture_output=True, text=True, timeout=120)
        (work / (label + '.stderr')).write_text(result.stderr)
        errors = [line for line in result.stderr.splitlines() if line.startswith('E ')]
        assert result.returncode == 1 and len(errors) == 1 and f'E {code} ' in errors[0], (label, result)
        report['refusals'].append({'name': label, 'code': code})

    assert len(golden['cases']) == 5
    prior = []
    for name in ['formatter-declaration-text-options', 'formatter-atom-text-options']:
        prior += json.loads((root / ('tests/tools/' + name + '.golden')).read_text())['cases']
    structural = json.loads((root / 'tests/tools/formatter-structural-query-options.golden').read_text())
    special = {'empty-success': '', 'operator-fallback': '(fn (f x) (~~ x))\n',
               'empty-string': '(fn (f) "")\n', 'missing-binding': '(fn (f) (let ((x)) x))\n',
               'first-depth-overflow': '(' * 1030 + 'x' + ')' * 1030}
    for old in structural['cases']:
        row = dict(old)
        if row['name'] in special:
            row['source'] = special[row['name']]
        elif row['name'] == 'syntax-zoo':
            row['source'] = (root / 'tests/fmt/syntax-zoo.ax').read_text()
        else:
            row['source'] = (root / 'tests/fmt/parity' / row['name']).read_text()
        assert hashlib.sha256(row['source'].encode()).hexdigest() == row['input_sha256']
        prior.append(row)
    unique = {}
    for row in golden['cases'] + prior:
        if row['source'] in unique:
            previous = unique[row['source']]
            assert previous['ok'] == row['ok'] and previous['text'] == row['text']
        else:
            unique[row['source']] = row
    formats = list(unique.values())
    assert len(formats) >= 40
    for row in formats:
        outputs = []
        for executable in binaries:
            result = subprocess.run([str(executable), 'format', row['source']], env=environment,
                                    capture_output=True, text=True, timeout=30)
            assert result.returncode == 0 and not result.stderr, (row['name'], result)
            assert result.stdout == str(row['ok']) + '\n' + row['text'], (row, result.stdout)
            outputs.append(result.stdout)
            if row['ok']:
                fixed = subprocess.run([str(executable), 'format', row['text']], env=environment,
                                       capture_output=True, text=True, timeout=30)
                assert fixed.returncode == 0 and fixed.stdout == result.stdout, (row['name'], 'not fixed', fixed)
        assert outputs[0] == outputs[1], row['name']
        report['formats'].append({'name': row['name'], 'ok': row['ok'], 'bytes': len(row['text'].encode()),
                                  'sha256': hashlib.sha256(row['text'].encode()).hexdigest()})
    (work / 'formatter-flat-options-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'ok   {len(formats)} byte-exact format cases and fixedpoints; 7 controls; 7 refusals', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True); parser.add_argument('--work', type=Path)
    args = parser.parse_args(); root = Path(__file__).resolve().parents[2]
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True); run(args.axiom.resolve(), root, args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-formatter-flat-') as directory:
            run(args.axiom.resolve(), root, Path(directory))


if __name__ == '__main__':
    main()
