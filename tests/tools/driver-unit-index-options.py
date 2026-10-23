#!/usr/bin/env python3
"""Check optional unit indexes, extern attribution and effective controls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

SUPPORT = runpy.run_path(str(Path(__file__).parents[1] / 'selfhost/emitter-line-buffer.py'))
run_checked = SUPPORT['run_checked']
replace_once = SUPPORT['replace_once']
check_output = SUPPORT['check_output']


def check(compiler, root, work):
    originals = {name: (root / 'core' / name).read_text()
                 for name in ('codegen.ax', 'driver.ax', 'lsp.ax')}
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for folder in ('core', 'stdlib') for path in sorted((root / folder).rglob('*.ax'))}
    environment = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    fixture = root / 'tests/tools/driver-unit-index-options.axp'
    diagnostic_fixture = root / 'tests/tools/driver-unit-index-diagnostics.axp'
    golden = (root / 'tests/tools/driver-unit-index-diagnostics.axdl').read_bytes()
    report = {'builder': str(compiler),
              'builder_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
              'source_sha256': hashes, 'configurations': [], 'counterexamples': [], 'refusals': [],
              'invariants': [
                  'Missing units and declarations return None; unit index zero remains present.',
                  'Empty module names remain named, and duplicate lookups select the first match.',
                  'An unresolved first extern declaration does not select a later declaration.',
                  'Queries retire their temporary owners and leave input owners unchanged.',
                  'Extern diagnostic attribution and message/help bytes remain unchanged.',
              ],
              'known_limits': [
                  'Host O0/O2 native execution with allocation poisoning; no Windows execution claim.',
                  'Parser module stamps and source spans retain their existing packed boundaries.',
                  'Missing required resolver units still take the existing index-trap refusal.',
                  'No held lifetime inference is inspected, changed or validated.',
              ]}

    def modules(label, changes=None):
        directory = work / (label + '-core')
        directory.mkdir()
        for path in (root / 'core').iterdir():
            destination = directory / path.name
            if changes and path.name in changes:
                destination.write_text(changes[path.name])
            else:
                destination.symlink_to(path, target_is_directory=path.is_dir())
        return dict(environment, AXIOM_PATH=str(directory))

    def build_run(label, source, local, level, expected=None, failures=(), stderr=b''):
        binary = work / label
        run_checked([str(compiler), '--diagnostic-format=ai', 'build', str(source),
                     '--opt', str(level), '--alloc-poison', '-o', str(binary)], env=local,
                    stdout=work / (label + '.build.stdout'), stderr=work / (label + '.build.stderr'),
                    timeout=240)
        output = work / (label + '.stdout')
        error = work / (label + '.stderr')
        run_checked([str(binary)], env=local, stdout=output, stderr=error, timeout=30)
        if error.read_bytes() != stderr:
            raise AssertionError((label, 'stderr mismatch', error.read_text()))
        if expected is None:
            if output.read_bytes():
                raise AssertionError((label, 'unexpected stdout', output.read_text()))
            return set()
        return check_output(output, expected, required_failures=failures)

    local = modules('positive')
    for source in (fixture, diagnostic_fixture):
        run_checked([str(compiler), '--diagnostic-format=ai', 'fmt', '--check', str(source)],
                    env=local, stdout=work / (source.stem + '.fmt.stdout'),
                    stderr=work / (source.stem + '.fmt.stderr'))
    for level in (0, 2):
        build_run('positive-O' + str(level), fixture, local, level,
                  [f'{index} true' for index in range(1, 32)])
        build_run('diagnostics-O' + str(level), diagnostic_fixture, local, level, stderr=golden)
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True,
                                         'cases': 31, 'diagnostic_sha256': hashlib.sha256(golden).hexdigest()})
        print(f'ok   unit indexes: 31 cases and exact extern diagnostics O{level} poison', flush=True)

    variants = [
        ('lost-zero-unit', 'codegen.ax',
         '(if (unitNameIs (vecGet units i) name)\n        (Some i)',
         '(if (unitNameIs (vecGet units i) name)\n        (if (== i 0) None (Some i))',
         (3, 7, 22, 23, 31)),
        ('lost-entry-unit', 'driver.ax', '((None) (Some 0))', '((None) None)', (13, 19)),
        ('missing-unit-is-zero', 'codegen.ax',
         '(pub fn (unitIndexOf units name i)\n  (if (>= i (vecLen units))\n    None',
         '(pub fn (unitIndexOf units name i)\n  (if (>= i (vecLen units))\n    (Some 0)',
         (1, 2, 4, 9, 10, 17, 18, 20, 24)),
    ]
    for label, name, before, after, failed in variants:
        altered = replace_once(originals[name], before, after, label)
        observed = build_run(label, fixture, modules(label, {name: altered}), 0,
                             [f'{index} true' for index in range(1, 32)], failed)
        report['counterexamples'].append({'name': label, 'failed_cases': sorted(observed)})
        print(f'ok   {label}: cases {sorted(observed)} fail', flush=True)

    refusals = [
        ('erased-unit-index', '(:: bad Int) (fn (bad) (unitIndexOf vecNew "Missing" 0))'),
        ('erased-extern-index', '(:: bad Int) (fn (bad) (findExternUnit vecNew "missing" vecNew))'),
        ('missing-none-unit', '(:: bad Int) (fn (bad) (match (unitIndexOf vecNew "Missing" 0) ((Some i) i)))'),
        ('missing-none-extern', '(:: bad Int) (fn (bad) (match (findExternUnit vecNew "missing" vecNew) ((Some i) i)))'),
    ]
    for label, declaration in refusals:
        source = work / (label + '.ax')
        source.write_text('(import core)\n(import codegen)\n(import driver)\n(import Vec)\n'
                          + declaration + '\n(:: main Int) (fn (main) 0)\n')
        result = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(source)],
                                env=local, capture_output=True, text=True, timeout=90)
        (work / (label + '.stderr')).write_text(result.stderr)
        errors = [line for line in result.stderr.splitlines() if line.startswith('E ')]
        accepted = ('AX3004', 'AX3089') if label.startswith('erased-') else ('AX3005',)
        if result.returncode != 1 or len(errors) != 1 or not any(f'E {code} ' in errors[0] for code in accepted):
            raise AssertionError((label, result.returncode, result.stdout, result.stderr))
        report['refusals'].append({'name': label, 'diagnostic': errors[0].split()[1]})
        print(f'ok   {label}: {errors[0].split()[1]}', flush=True)

    # The private recursion must carry the Option too, not reintroduce a bridge.
    old_signature = '(:: findExternUnitFrom (-> (Vec ASTNode) String Units Int (Option Int)))'
    altered = replace_once(originals['driver.ax'], old_signature,
                           '(:: findExternUnitFrom (-> (Vec ASTNode) String Units Int Int))',
                           'private helper result erasure')
    result = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(fixture)],
                            env=modules('erased-private-helper', {'driver.ax': altered}),
                            capture_output=True, text=True, timeout=90)
    (work / 'erased-private-helper.stderr').write_text(result.stderr)
    if result.returncode != 1 or not any(code in result.stderr for code in ('E AX3004 ', 'E AX3089 ')):
        raise AssertionError(('erased-private-helper', result.returncode, result.stdout, result.stderr))
    report['refusals'].append({'name': 'erased-private-helper', 'status': 'refused'})
    print('ok   erased-private-helper: refused', flush=True)

    for relative, before in hashes.items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != before:
            raise AssertionError('source changed during probe: ' + relative)
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
        with tempfile.TemporaryDirectory(prefix='axiom-driver-unit-index-') as directory:
            check(args.axiom.resolve(), root, Path(directory))


if __name__ == '__main__':
    main()
