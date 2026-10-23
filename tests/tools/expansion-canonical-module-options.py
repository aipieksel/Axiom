#!/usr/bin/env python3
"""Keep optional qualifiers, imported bindings and literal identity consistent."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

SUPPORT = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'selfhost/emitter-line-buffer.py'))
run_checked = SUPPORT['run_checked']
check_output = SUPPORT['check_output']


def check(compiler, root, work, dependencies, literal_compiler, query_only):
    source = (root / 'core/expand.ax').read_text()
    fixture = root / 'tests/tools/expansion-canonical-module-options.axp'
    expected = [f'{i} true' for i in range(1, 23)]
    environment = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    report = {'base': '4ee86a21aeb6ef739c3a6da9ae6d949f707e3def',
              'builder': str(compiler), 'builder_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
              'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
              'fixture_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
              'configurations': [], 'counterexamples': [], 'literal_dispatch': [],
              'invariants': [
                  'A missing qualifier uses the imported canonical binding, including its ambiguous state.',
                  'Qualified and unqualified spellings of one visible imported function or constructor agree.',
                  'Literal comparison refuses ambiguous and locally shadowed imported names.',
                  'The existing cross-module literal-dispatch fixture still exits 19 without fixture changes.',
              ],
              'known_limits': [
                  'This is a qualifier-consumer repair; module owner carriers and raw SyntaxIdentity metadata remain separate unfinished migration work.',
                  'Host O0/O2 allocation-poison probes do not establish whole-compiler or memory-model soundness.',
                  'No held lifetime work or inference algorithm is inspected or changed.',
              ]}

    def modules(label, contents):
        folder = work / (label + '-modules')
        folder.mkdir()
        for path in dependencies.iterdir():
            destination = folder / path.name
            if path.name == 'expand.ax':
                destination.write_text(contents)
            else:
                destination.symlink_to(path, target_is_directory=path.is_dir())
        return dict(environment, AXIOM_PATH=str(folder))

    def build_run(label, level, local, failures=()):
        binary = work / label
        run_checked([str(compiler), '--diagnostic-format=ai', 'build', str(fixture),
                     '--opt', str(level), '--alloc-poison', '-o', str(binary)],
                    env=local, stdout=work / (label + '.build.stdout'),
                    stderr=work / (label + '.build.stderr'), timeout=240)
        output = work / (label + '.stdout')
        run_checked([str(binary)], env=local, stdout=output,
                    stderr=work / (label + '.stderr'), timeout=30)
        return check_output(output, expected, required_failures=failures)

    local = modules('positive', source)
    for level in (0, 2):
        build_run('positive-O' + str(level), level, local)
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True,
                                         'cases': 22, 'status': 'passed'})
        print(f'ok   canonical module Options: 22 qualified/imported/ambiguous/literal cases O{level} poison', flush=True)

    repaired = '(if (isSome qual)'
    if source.count(repaired) != 1:
        raise AssertionError('expected exactly one optional-qualifier enforcement branch')
    previous = source.replace(repaired, '(if (!= (as qual Int) 0)')
    failed = build_run('old-option-pointer-test', 0, modules('old-option-pointer-test', previous),
                       {1, 7, 8, 9, 10, 13, 15, 20, 22})
    report['counterexamples'].append({'name': 'old-option-pointer-test', 'failed_cases': sorted(failed)})
    print(f'ok   old Option-pointer control: cases {sorted(failed)} fail', flush=True)

    if query_only:
        report['literal_dispatch_status'] = 'pending a compiler built with the repaired source'
        (work / 'canonical-module-options-report.json').write_text(json.dumps(report, indent=2) + '\n')
        return

    literal = root / 'tests/selfhost/402-literal-dispatch.ax'
    if not literal.read_text().startswith('; expect 19\n'):
        raise AssertionError('the literal-dispatch expectation must remain 19')
    report['literal_fixture_sha256'] = hashlib.sha256(literal.read_bytes()).hexdigest()
    report['literal_compiler'] = str(literal_compiler)
    report['literal_compiler_sha256'] = hashlib.sha256(literal_compiler.read_bytes()).hexdigest()
    for level in (0, 2):
        binary = work / ('literal-dispatch-O' + str(level))
        run_checked([str(literal_compiler), '--diagnostic-format=ai', 'build', str(literal),
                     '--opt', str(level), '--alloc-poison', '-o', str(binary)],
                    env=environment, stdout=work / (binary.name + '.build.stdout'),
                    stderr=work / (binary.name + '.build.stderr'), timeout=240)
        ran = subprocess.run([str(binary)], env=environment, capture_output=True, timeout=30)
        (work / (binary.name + '.stdout')).write_bytes(ran.stdout)
        (work / (binary.name + '.stderr')).write_bytes(ran.stderr)
        if ran.returncode != 19 or ran.stdout or ran.stderr:
            raise AssertionError(('literal dispatch', level, ran.returncode, ran.stdout, ran.stderr))
        report['literal_dispatch'].append({'optimization': level, 'expected_exit': 19, 'actual_exit': ran.returncode})
        print(f'ok   unchanged 402-literal-dispatch: freshly emitted O{level} poison executable exits 19', flush=True)
    (work / 'canonical-module-options-report.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True)
    parser.add_argument('--dependency-core', type=Path)
    parser.add_argument('--literal-compiler', type=Path)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--query-only', action='store_true',
                        help='check module functions while the repaired compiler is being built')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    dependencies = (args.dependency_core or root / 'core').resolve()
    compiler = args.axiom.resolve()
    literal_compiler = (args.literal_compiler or args.axiom).resolve()
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
        check(compiler, root, args.work.resolve(), dependencies, literal_compiler, args.query_only)
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-canonical-module-options-') as directory:
            check(compiler, root, Path(directory), dependencies, literal_compiler, args.query_only)


if __name__ == '__main__':
    main()
