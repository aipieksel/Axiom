#!/usr/bin/env python3
"""Exercise typed assembler locations, checked decimals and exact diagnostics."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

SUPPORT = runpy.run_path(str(Path(__file__).parents[1] / 'selfhost/emitter-line-buffer.py'))
replace_once = SUPPORT['replace_once']
check_output = SUPPORT['check_output']


def check(compiler, root, work):
    driver = (root / 'core/driver.ax').read_text()
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for folder in ('core', 'stdlib') for path in sorted((root / folder).rglob('*.ax'))}
    env = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    fixture = root / 'tests/tools/driver-asm-query-options.axp'
    diagnostics = root / 'tests/tools/driver-asm-diagnostics.axp'
    golden = diagnostics.with_suffix('.golden').read_bytes()
    expected = [f'{index} true' for index in range(1, 45)]
    data = work / 'data'
    data.mkdir()
    (data / 'present.ax').write_text('alpha\nbeta\n')
    (data / 'empty.ax').write_bytes(b'')
    assert not (data / 'missing.ax').exists()
    report = {'builder': str(compiler),
              'builder_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
              'source_sha256': hashes, 'configurations': [], 'controls': [], 'refusals': [],
              'limits': [
                  'Native host O0/O2 with allocation poisoning; no cross-target execution claim.',
                  'Decimal metadata accepts nonnegative signed-64 values and rejects overflow before arithmetic.',
                  'Malformed offsets retain spanless diagnostics; packed source-span layout is unchanged.',
                  'Empty and failed file reads retain the existing empty-source diagnostic fallback.',
                  'No held lifetime/escape inference is reviewed, changed or validated.',
              ]}

    def modules(label, source=driver):
        folder = work / (label + '-core')
        folder.mkdir()
        for path in (root / 'core').iterdir():
            target = folder / path.name
            if path.name == 'driver.ax':
                target.write_text(source)
            else:
                target.symlink_to(path, target_is_directory=path.is_dir())
        return dict(env, AXIOM_PATH=str(folder))

    def native(label, source, local, level, lines=None, failures=(), output=None, different=False):
        binary = work / label
        built = subprocess.run([str(compiler), '--diagnostic-format=ai', 'build', str(source),
                                '--opt', str(level), '--alloc-poison', '-o', str(binary)],
                               env=local, capture_output=True, timeout=240)
        (work / (label + '.build.stdout')).write_bytes(built.stdout)
        (work / (label + '.build.stderr')).write_bytes(built.stderr)
        if built.returncode or any(line.startswith((b'E ', b'W ')) for line in built.stderr.splitlines()):
            raise AssertionError((label, built.returncode, built.stderr.decode()))
        ran = subprocess.run([str(binary)], env=local, cwd=data, capture_output=True, timeout=30)
        out = work / (label + '.stdout')
        out.write_bytes(ran.stdout)
        (work / (label + '.stderr')).write_bytes(ran.stderr)
        if ran.returncode or ran.stderr:
            raise AssertionError((label, ran.returncode, ran.stderr.decode()))
        if lines is not None:
            return check_output(out, lines, required_failures=failures)
        if (ran.stdout == output) == different:
            raise AssertionError((label, 'unexpected exact-output comparison'))
        return set()

    local = modules('positive')
    for level in (0, 2):
        native('queries-O' + str(level), fixture, local, level, lines=expected)
        native('diagnostics-O' + str(level), diagnostics, local, level, output=golden)
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True,
                                         'cases': 44, 'diagnostic_sha256': hashlib.sha256(golden).hexdigest()})
        print(f'ok   assembler queries: 44 cases and exact diagnostics O{level}', flush=True)

    variants = [
        ('zero-seen-lost',
         '(pub fn (asmSeenAt v k i)\n  (if (>= i (vecLen v))\n    None\n    (if (strEq (vecGet v i) k)\n      (Some i)',
         '(pub fn (asmSeenAt v k i)\n  (if (>= i (vecLen v))\n    None\n    (if (strEq (vecGet v i) k)\n      (if (== i 0) None (Some i))', (12,)),
        ('empty-filename-lost', '(Some (asmUnquoteName (strSlice ir start (- end start))))',
         '{ (let name (asmUnquoteName (strSlice ir start (- end start)))) (if (== (strLen name) 0) None (Some name)) }', (24,)),
        ('raw-key-lost', '(concat file (concat ":" (concat start (concat ":" (concat end ":")))))',
         '(concat file ":0:5:")', (39,)),
    ]
    for label, before, after, failed in variants:
        changed = replace_once(driver, before, after, label)
        altered = modules(label, changed)
        observed = native(label, fixture, altered, 0, lines=expected, failures=failed)
        report['controls'].append({'name': label, 'failed_cases': sorted(observed)})
        if label == 'raw-key-lost':
            native(label + '-diagnostics', diagnostics, altered, 0, output=golden, different=True)
        print(f'ok   {label}: cases {sorted(observed)} fail', flush=True)

    # Observe overflow only through the decimal query, without feeding an
    # intentionally wrapped value into a location index or a source span.
    numeric = work / 'overflow.ax'
    numeric.write_text('(import driver)\n(import IO)\n(:: main Int)\n;@axiom:effect(io)\n'
                       '(fn (main) { (let a (isNone (asmNumAt "9223372036854775808" 0))) '
                       '(let b (isNone (asmNumAt "18446744073709551616" 0))) '
                       '(println "1 {a}") (println "2 {b}") 0 })\n')
    before = '(|| (> v 922337203685477580) (&& (== v 922337203685477580) (> digit 7)))'
    changed = replace_once(driver, before, 'false', 'overflow-check-removed')
    observed = native('overflow-check-removed', numeric, modules('overflow-check-removed', changed),
                      0, lines=['1 true', '2 true'], failures=(1, 2))
    report['controls'].append({'name': 'overflow-check-removed', 'failed_cases': sorted(observed)})
    print('ok   overflow-check-removed: both overflow refusals fail', flush=True)

    refusals = [
        ('erased-number', '(:: bad Int) (fn (bad) (asmNumAt "0" 0))', 'AX3004'),
        ('erased-cookie', '(:: bad Int) (fn (bad) (asmAtLine "at line 0"))', 'AX3004'),
        ('erased-filename', '(:: bad String) (fn (bad) (asmFileName "" "0"))', 'AX3004'),
        ('erased-site', '(:: bad AsmSite) (fn (bad) (asmSiteOf "" 0))', 'AX3004'),
        ('missing-site-none', '(:: bad AsmSite) (fn (bad) (match (asmSiteOf "" 0) ((Some site) site)))', 'AX3005'),
        ('missing-number-none', '(:: bad Int) (fn (bad) (match (asmNumAt "0" 0) ((Some value) value)))', 'AX3005'),
    ]
    for label, declaration, code in refusals:
        source = work / (label + '.ax')
        source.write_text('(import driver)\n' + declaration + '\n(:: main Int) (fn (main) 0)\n')
        checked = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(source)],
                                 env=local, capture_output=True, text=True, timeout=90)
        (work / (label + '.stderr')).write_text(checked.stderr)
        errors = [line for line in checked.stderr.splitlines() if line.startswith('E ')]
        if checked.returncode != 1 or len(errors) != 1 or f'E {code} ' not in errors[0]:
            raise AssertionError((label, checked.returncode, checked.stdout, checked.stderr))
        report['refusals'].append({'name': label, 'diagnostic': code})
        print(f'ok   {label}: {code}', flush=True)

    for name, before in hashes.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != before:
            raise AssertionError('source changed during probe: ' + name)
    (work / 'report.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', required=True, type=Path)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
        check(args.axiom.resolve(), root, args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-driver-asm-query-') as directory:
            check(args.axiom.resolve(), root, Path(directory))


if __name__ == '__main__':
    main()
