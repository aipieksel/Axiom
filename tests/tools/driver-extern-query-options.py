#!/usr/bin/env python3
"""Check extern query absence, owner retirement and exact grounding/binding output."""
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
    driver = (root / 'core/driver.ax').read_text()
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for folder in ('core', 'stdlib') for path in sorted((root / folder).rglob('*.ax'))}
    environment = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    fixtures = root / 'tests/tools'
    query = fixtures / 'driver-extern-query-options.axp'
    binding = fixtures / 'driver-extern-binding-output.axp'
    rust = binding.with_suffix('.golden').read_bytes() + b'\n'
    diagnostics = (fixtures / 'driver-unit-index-diagnostics.axdl').read_bytes()
    expected = [f'{index} true' for index in range(1, 41)]
    report = {'builder': str(compiler),
              'builder_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
              'source_sha256': hashes, 'configurations': [], 'controls': [], 'refusals': [],
              'limits': [
                  'Native host O0/O2 with allocation poisoning; no other-target execution claim.',
                  'Packed AST inputs normalise nil at query boundaries; their wire layout is unchanged.',
                  'Malformed arrow results preserve the existing word tag and Rust refusal note.',
                  'No held lifetime/escape inference is reviewed, changed or validated.',
              ]}

    def modules(label, altered=None):
        directory = work / (label + '-core')
        directory.mkdir()
        for path in (root / 'core').iterdir():
            target = directory / path.name
            if path.name == 'driver.ax' and altered is not None:
                target.write_text(altered)
            else:
                target.symlink_to(path, target_is_directory=path.is_dir())
        return dict(environment, AXIOM_PATH=str(directory))

    def native(label, source, env, level, stdout=None, stderr=b'', lines=None, failures=()):
        binary = work / label
        run_checked([str(compiler), '--diagnostic-format=ai', 'build', str(source),
                     '--opt', str(level), '--alloc-poison', '-o', str(binary)], env=env,
                    stdout=work / (label + '.build.stdout'), stderr=work / (label + '.build.stderr'),
                    timeout=240)
        out = work / (label + '.stdout')
        error = work / (label + '.stderr')
        run_checked([str(binary)], env=env, stdout=out, stderr=error, timeout=30)
        if error.read_bytes() != stderr:
            raise AssertionError((label, 'stderr mismatch', error.read_text()))
        if lines is not None:
            return check_output(out, lines, required_failures=failures)
        if out.read_bytes() != stdout:
            raise AssertionError((label, 'stdout mismatch'))
        return set()

    local = modules('positive')
    for source in (query, binding):
        run_checked([str(compiler), '--diagnostic-format=ai', 'fmt', '--check', str(source)],
                    env=local, stdout=work / (source.stem + '.fmt.stdout'),
                    stderr=work / (source.stem + '.fmt.stderr'))
    for level in (0, 2):
        native('queries-O' + str(level), query, local, level, lines=expected)
        native('binding-O' + str(level), binding, local, level, stdout=rust)
        native('diagnostics-O' + str(level), fixtures / 'driver-unit-index-diagnostics.axp',
               local, level, stdout=b'', stderr=diagnostics)
        native('unit-indexes-O' + str(level), fixtures / 'driver-unit-index-options.axp',
               local, level, lines=[f'{index} true' for index in range(1, 32)])
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True,
                                         'query_cases': 40, 'unit_index_cases': 31,
                                         'rust_sha256': hashlib.sha256(rust).hexdigest(),
                                         'diagnostic_sha256': hashlib.sha256(diagnostics).hexdigest()})
        print(f'ok   extern queries: 40 cases, 31 unit cases, exact Rust and diagnostics O{level}', flush=True)

    variants = [
        ('item-presence-lost', '(Some (vecGet items i))', 'None', (3, 4, 6, 7, 10, 12, 13, 15, 29, 34)),
        ('empty-library-lost', '(Some (nodeAName d))',
         '(if (== (strLen (nodeAName d)) 0) None (Some (nodeAName d)))', (15,)),
        ('nullary-signature-lost',
         '(pub fn (externSigTag ty)\n  (if (nodeSame ty nilNode)',
         '(pub fn (externSigTag ty)\n  (if (|| (nodeSame ty nilNode) (!= (nodeTag ty) TAG_T_ARR))',
         (23, 24)),
        ('missing-result-tag-changed',
         '; A malformed arrow\'s missing result keeps the legacy word tag.\n            ((None) "i")',
         '; A malformed arrow\'s missing result keeps the legacy word tag.\n            ((None) "s")',
         (28,)),
    ]
    for label, before, after, failed in variants:
        altered = replace_once(driver, before, after, label)
        observed = native(label, query, modules(label, altered), 0, lines=expected, failures=failed)
        report['controls'].append({'name': label, 'failed_cases': sorted(observed)})
        print(f'ok   {label}: cases {sorted(observed)} fail', flush=True)

    refusals = [
        ('erased-item', '(:: bad ASTNode) (fn (bad) (findExternItem vecNew "x" 0))', 'AX3004'),
        ('erased-library', '(:: bad String) (fn (bad) (findExternLib vecNew "x" 0))', 'AX3004'),
        ('erased-signature', '(:: bad String) (fn (bad) (externSigTag nilNode))', 'AX3004'),
        ('erased-result', '(:: bad ASTNode) (fn (bad) (sigResultOf nilNode))', 'AX3004'),
        ('missing-item-none', '(:: bad ASTNode) (fn (bad) (match (findExternItem vecNew "x" 0) ((Some item) item)))', 'AX3005'),
        ('missing-result-none', '(:: bad ASTNode) (fn (bad) (match (sigResultOf nilNode) ((Some result) result)))', 'AX3005'),
    ]
    for label, declaration, code in refusals:
        source = work / (label + '.ax')
        source.write_text('(import core)\n(import parser)\n(import driver)\n(import Vec)\n'
                          + declaration + '\n(:: main Int) (fn (main) 0)\n')
        result = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(source)],
                                env=local, capture_output=True, text=True, timeout=90)
        (work / (label + '.stderr')).write_text(result.stderr)
        errors = [line for line in result.stderr.splitlines() if line.startswith('E ')]
        if result.returncode != 1 or len(errors) != 1 or f'E {code} ' not in errors[0]:
            raise AssertionError((label, result.returncode, result.stdout, result.stderr))
        report['refusals'].append({'name': label, 'diagnostic': code})
        print(f'ok   {label}: {code}', flush=True)

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
        with tempfile.TemporaryDirectory(prefix='axiom-driver-extern-query-') as directory:
            check(args.axiom.resolve(), root, Path(directory))


if __name__ == '__main__':
    main()
