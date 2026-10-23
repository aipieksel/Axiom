#!/usr/bin/env python3
"""Check compilation-unit module identity, ownership and editor displays."""
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
    source = (root / 'core/core.ax').read_text()
    fixture = work / 'unit-name-options.ax'
    fixture.write_bytes((root / 'tests/tools/compiler-unit-name-options.axp').read_bytes())
    environment = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    expected = [f'{i} true' for i in range(1, 49)]
    empty = replace_once(source, '(pub fn (unitIsEntry u)\n  (match (unitName u)\n'
                         '    ((None) true)\n    ((Some _) false)))',
                         '(pub fn (unitIsEntry u)\n'
                         '  (match (unitName u) ((None) true) ((Some name) (== (strLen name) 0))))',
                         'empty name stays named')
    lookup = replace_once(source, '(strEq declared name)', 'false', 'present name participates in lookup')
    ownership = replace_once(source, '(pub fn (unitName u)\n  u.name)',
                             '(pub fn (unitName u)\n'
                             '  { (memMarkLeaf (as u Int)) u.name })', 'unit child ownership map')
    variants = [('empty-is-entry', empty, (4, 18, 22)),
                ('lost-name-lookup', lookup, (6, 8, 18, 19, 21, 22, 33, 35, 36, 37, 39)),
                ('unmapped-unit-children', ownership, (14,))]
    report = {'builder': str(compiler),
              'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted((root / 'core').glob('*.ax'))},
              'configurations': [], 'counterexamples': [], 'refusals': [],
              'invariants': [
                  {'invariant': 'Only None denotes an unnamed unit; Some empty remains named and owned.',
                   'enforcement': 'Option String field, unitName, unitNameIs and unitIsEntry; cases1-16,18,22,25,37.',
                   'counterexample': 'empty-is-entry'},
                  {'invariant': 'Lookup identity reaches every compiler consumer without erasing a name to Int.',
                   'enforcement': 'Checker, import resolver, expansion frames, symbols and LSP cases17-39; unitModule removed.',
                   'counterexample': 'lost-name-lookup'},
                  {'invariant': 'Unit and vector owners retain module text, and retiring units release their option child.',
                   'enforcement': 'Typed owning field and escaping/retiring getter controls12-16.',
                   'counterexample': 'unmapped-unit-children'},
                  {'invariant': 'Editor provenance is optional, while empty display text retains its existing omission rule.',
                   'enforcement': 'Typed hover/signature/symbol origin parameters with explicit rendering branches, cases40-48.',
                   'counterexample': 'empty-is-entry affects identity rather than conflating display with it.'},
              ],
              'known_limits': ['Host O0/O2 native execution with allocation poisoning; no Windows runtime claim.',
                               'Some empty and None intentionally render alike where the UI omits empty provenance; unit identity keeps them distinct.',
                               'This runner checks actual shared consumer functions; final compiler CLI/REPL and complete LSP protocol gates remain integration/CI checks.',
                               'Parser AST module stamps and source Span handles retain their existing wire boundaries.',
                               'No held M5 work or lifetime algorithm is read, changed or validated; no global soundness or Rust parity claim follows.']}
    run_checked([str(compiler), '--diagnostic-format=ai', 'fmt', '--check', str(fixture)], env=environment,
                stdout=work / 'fmt.stdout', stderr=work / 'fmt.stderr')

    def modules(label, content):
        directory = work / (label + '-modules')
        directory.mkdir()
        for path in (root / 'core').iterdir():
            destination = directory / path.name
            if path.name == 'core.ax':
                destination.write_text(content)
            else:
                destination.symlink_to(path, target_is_directory=path.is_dir())
        return dict(environment, AXIOM_PATH=str(directory))

    def build_run(label, content, optimization, failures=()):
        local = modules(label, content)
        binary = work / label
        run_checked([str(compiler), '--diagnostic-format=ai', 'build', str(fixture),
                     '--opt', str(optimization), '--alloc-poison', '-o', str(binary)],
                    env=local, stdout=work / (label + '.build.stdout'), stderr=work / (label + '.build.stderr'),
                    timeout=240)
        output = work / (label + '.stdout')
        run_checked([str(binary)], env=local, stdout=output, stderr=work / (label + '.stderr'), timeout=30)
        observed = check_output(output, expected, required_failures=failures)
        print('ok   ' + label + ': ' + str(sorted(observed)), flush=True)
        return observed

    for level in (0, 2):
        build_run('positive-O' + str(level), source, level)
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True,
                                         'cases': 48, 'status': 'passed'})
    for label, changed, cases in variants:
        observed = build_run(label, changed, 0, cases)
        report['counterexamples'].append({'name': label, 'failed_cases': sorted(observed)})
    local = modules('refusals', source)
    for name, expression, code in [
        ('unwrapped-name', '(CUnit2 :file "x" :src "" :name "Module" :imports vecNew :library false)', 'AX3004'),
        ('unchecked-name', '(strLen (unitName (CUnit2 :file "x" :src "" :name None :imports vecNew :library false)))', 'AX3004'),
        ('missing-name-none', '(match (unitName (CUnit2 :file "x" :src "" :name None :imports vecNew :library false)) ((Some name) (strLen name)))', 'AX3005'),
    ]:
        refused = work / (name + '.ax')
        refused.write_text('(import core)\n(import Str)\n(import Vec)\n(:: main Int)\n(fn (main) { '
                           + expression + ' 0 })\n')
        result = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(refused)],
                                env=local, capture_output=True, text=True, timeout=60)
        (work / (name + '.stderr')).write_text(result.stderr)
        errors = [line for line in result.stderr.splitlines() if line.startswith('E ')]
        if result.returncode != 1 or len(errors) != 1 or f'E {code} ' not in errors[0]:
            raise RuntimeError((name, result))
        report['refusals'].append({'name': name, 'diagnostic': code, 'status': 'passed'})
        print('ok   ' + name + ': ' + code, flush=True)
    for name, expected_hash in report['source_sha256'].items():
        if hashlib.sha256((root / 'core' / name).read_bytes()).hexdigest() != expected_hash:
            raise RuntimeError('core source changed during probe: ' + name)
    (work / 'unit-name-option-report.json').write_text(json.dumps(report, indent=2) + '\n')


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
        with tempfile.TemporaryDirectory(prefix='axiom-unit-name-options-') as directory:
            check(args.axiom.resolve(), root, Path(directory))


if __name__ == '__main__':
    main()
