#!/usr/bin/env python3
"""Pin formatter query absence without changing layout or refusal output."""
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
    module = root / 'core/format.ax'
    source = module.read_text()
    fixture = root / 'tests/tools/formatter-structural-query-options.axp'
    golden_path = root / 'tests/tools/formatter-structural-query-options.golden'
    golden = json.loads(golden_path.read_text())
    fixture_text = fixture.read_text()
    for import_line in ('(import IO)', '(import Fmt)', '(import Sys)'):
        fixture_text = fixture_text.replace(import_line, '')
    test_imports = '(import IO)\n(import Fmt)\n(import Sys)\n'
    environment = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'),
                       AXIOM_PATH=str(root / 'core'))
    expected = ''.join(f'{i} true\n' for i in range(1, 41))
    report = {'compiler': str(compiler), 'compiler_sha256': digest(compiler),
              'source_sha256': digest(module), 'fixture_sha256': digest(fixture),
              'golden_sha256': digest(golden_path),
              'configurations': [], 'controls': [], 'refusals': [], 'format_cases': [],
              'remaining': ['flat-layout String failure protocols and FRes tagged status/text'],
              'limits': ['Host O0/O2 allocation-poison probes; CI runs the complete suite.',
                         'No raw AST, ownership inference or renderer algorithm changes.']}

    def build(label, text, level=0):
        path = work / (label + '.ax'); path.write_text(text)
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
        executable = build('query-O' + str(level), test_imports + source + '\n' + fixture_text, level)
        checked = subprocess.run([str(executable)], env=environment, capture_output=True,
                                 text=True, timeout=30)
        (work / ('query-O' + str(level) + '.stdout')).write_text(checked.stdout)
        assert checked.returncode == 0 and not checked.stderr and checked.stdout == expected, checked
        binaries.append(executable)
        report['configurations'].append({'optimization': level, 'outcomes': 40,
                                         'allocation_poisoning': True, 'status': 'passed'})
        print(f'ok   formatter queries: 40 outcomes O{level}', flush=True)

    def alter(name, old, new):
        start = source.index('(fn (' + name + ' ')
        match = re.search(r'^\((?:pub )?:: ', source[start:], re.M)
        end = start + match.start() if match else len(source)
        body = source[start:end]
        assert body.count(old) == 1, (name, old, body.count(old))
        return source[:start] + body.replace(old, new) + source[end:]

    controls = [
        ('lose-newline-zero', alter('strLastNewline', '(Some i)',
                                   '(if (== i 0) None (Some i))'), {4, 10, 11, 12}),
        ('invent-missing-newline', alter('strLastNewline', '\n    None',
                                        '\n    (Some 0)'), {1, 2, 3, 7, 9, 12}),
        ('discard-overflow', alter('fNestOverflow', '(set hit (Some i))',
                                  '(set hit None)'), {14, 16, 17, 38}),
        ('discard-binding-init', alter('fpBindInit', '(Some i1)', 'None'), {19, 20, 25, 26, 40}),
        ('invent-operator-spelling', alter('fpOperSpelling', 'None', '(Some "!")'), {34, 35}),
        ('ignore-binding-absence', alter('fpLetBinding', '((None) (fpBad p))',
                                        '((None) ())'), {27}),
    ]
    # Every returned spelling borrows its input; a retained child must retire
    # after the match. The observed input is dynamically allocated.
    start = source.index('(fn (fpOperSpelling ')
    end = source.index('\n(:: fpIntFits ', start)
    segment = source[start:end]
    assert segment.count('(Some t)') == 18
    controls.append(('extra-spelling-share', source[:start] + segment.replace(
        '(Some t)', '{ (__retainref t) (Some t) }') + source[end:], {37}))
    for label, changed, failures in controls:
        executable = build(label, test_imports + changed + '\n' + fixture_text)
        checked = subprocess.run([str(executable)], env=environment, capture_output=True,
                                 text=True, timeout=30)
        failed = {int(line.split()[0]) for line in checked.stdout.splitlines()
                  if line.endswith(' false')}
        assert checked.returncode == 0 and not checked.stderr and failed == failures, (
            label, failed, failures, checked.returncode, checked.stdout, checked.stderr)
        report['controls'].append({'name': label, 'failed_cases': sorted(failed)})
        print(f'ok   control {label}: {sorted(failed)}', flush=True)

    invalid = [
        ('newline-int-result', '(:: bad Int) (fn (bad) (strLastNewline "" -1))', 'AX3004'),
        ('overflow-int-result', '(:: bad Int) (fn (bad) (fNestOverflow vecNew 0))', 'AX3004'),
        ('binding-int-result', '(:: bad (-> FNode Int)) (fn (bad n) (fpBindInit "" n))', 'AX3004'),
        ('operator-string-result', '(:: bad String) (fn (bad) (fpOperSpelling "+"))', 'AX3004'),
        ('unchecked-newline-match', '(:: bad Int) (fn (bad) (match (strLastNewline "" -1) ((Some n) n)))', 'AX3005'),
    ]
    for label, declaration, code in invalid:
        path = work / (label + '.ax')
        path.write_text(source + '\n' + declaration + '\n(:: main Int) (fn (main) 0)\n')
        checked = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(path)],
                                 cwd=root, env=environment, capture_output=True, text=True, timeout=120)
        (work / (label + '.stderr')).write_text(checked.stderr)
        errors = [line for line in checked.stderr.splitlines() if line.startswith('E ')]
        assert checked.returncode == 1 and len(errors) == 1 and f'E {code} ' in errors[0], (label, checked)
        report['refusals'].append({'name': label, 'code': code})

    # The checked-in bytes were measured before this port. A current compiler
    # cannot become its own reference when CI builds this revision.
    numbers = (80, 141, 181, 244, 250, 252, 253, 254, 255, 256, 257, 258, 267)
    cases = [('empty-success', ''), ('operator-fallback', '(fn (f x) (~~ x))\n'),
             ('empty-string', '(fn (f) "")\n'), ('missing-binding', '(fn (f) (let ((x)) x))\n'),
             ('first-depth-overflow', '(' * 1030 + 'x' + ')' * 1030)]
    for number in numbers:
        paths = list((root / 'tests/fmt/parity').glob(f'{number:03d}-*.axp'))
        assert len(paths) == 1, (number, paths)
        cases.append((paths[0].name, paths[0].read_text()))
    cases.append(('syntax-zoo', (root / 'tests/fmt/syntax-zoo.ax').read_text()))
    assert [row['name'] for row in golden['cases']] == [row[0] for row in cases]
    for (label, text), want in zip(cases, golden['cases']):
        assert hashlib.sha256(text.encode()).hexdigest() == want['input_sha256'], label
        path = work / ('format-' + label + '.ax'); path.write_text(text)
        samples = []
        for executable in binaries:
            checked = subprocess.run([str(executable), 'format', text], env=environment,
                                     capture_output=True, text=True, timeout=30)
            assert checked.returncode == 0 and not checked.stderr, (label, checked)
            status, output = checked.stdout.split('\n', 1)
            assert status in ('0', '1'), (label, status)
            assert int(status) == want['ok'] and output == want['text'], (label, checked, want)
            if status == '1':
                fixed = subprocess.run([str(executable), 'format', output], env=environment,
                                       capture_output=True, text=True, timeout=30)
                assert fixed.returncode == 0 and fixed.stdout == checked.stdout, (label, 'not a fixed point', fixed)
            samples.append(checked.stdout)
        assert samples[0] == samples[1], label
        report['format_cases'].append({'name': label, 'ok': int(samples[0][0]),
                                       'bytes': len(samples[0].encode()),
                                       'sha256': hashlib.sha256(samples[0].encode()).hexdigest()})
    (work / 'formatter-query-options-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'ok   {len(cases)} byte-exact fmt cases and fixed points; 7 controls; 5 refusals', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args(); root = Path(__file__).resolve().parents[2]
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
        run(args.axiom.resolve(), root, args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-formatter-queries-') as folder:
            run(args.axiom.resolve(), root, Path(folder))


if __name__ == '__main__':
    main()
