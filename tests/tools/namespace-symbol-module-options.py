#!/usr/bin/env python3
"""Check optional namespace modules, symbol unit lookup and safe location text."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

STREAM = '''-- symbols --
F entry entry.ax:1:1-2 "Int"
F empty empty.ax:1:1-2 "Int"
F module module.ax:1:1-2 "Int"
F missing entry.ax:1:1-2 "Int"
F module - "Int"
-- names --
plain
$empty
Module$work
Module$private
Module$sig
api
'''


def check(compiler, root, work):
    source = {name: (root / 'core' / (name + '.ax')).read_text() for name in ('namespace', 'symbols')}
    fixture = root / 'tests/tools/namespace-symbol-module-options.axp'
    expected = ''.join(f'{i} true\n' for i in range(1, 36)) + STREAM
    environment = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    report = {'builder': str(compiler), 'builder_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
              'sources': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in source.items()},
              'fixture_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
              'stream_bytes': len(STREAM.encode()), 'stream_sha256': hashlib.sha256(STREAM.encode()).hexdigest(),
              'configurations': [], 'controls': [], 'refusals': [],
              'invariants': [
                  'None module input leaves entry declarations unchanged; a present empty module still mangles its declarations.',
                  'Unit lookup returns None for no row and Some0 only for a real entry row; named matches preserve first-match order.',
                  'smCollect retains its explicit entry-file rendering fallback for unknown provenance.',
                  'Location formatting omits a nonexistent unit row rather than indexing an empty or shorter unit table.',
                  'Lookup borrows its unit table and name, and mangling retains no extra share of its module argument.',
              ],
              'known_limits': [
                  'Host O0/O2 allocation-poison probes; CI owns the complete target and compiler suite.',
                  'mangleDecl retains its existing side-effect result protocol; its return zero is not absence.',
                  'Raw resolver/MEnv module caches and the separate SyntaxIdentity ownership experiment remain outside this bounded fact closure.',
                  'AST stamp conversion uses the existing parser nodeModuleOpt boundary; no lifetime or inference algorithm is read or changed.',
              ]}

    def modules(label, changed=None):
        folder = work / (label + '-modules'); folder.mkdir()
        for path in (root / 'core').iterdir():
            destination = folder / path.name
            if changed and path.name == changed[0] + '.ax':
                destination.write_text(changed[1])
            else:
                destination.symlink_to(path, target_is_directory=path.is_dir())
        return dict(environment, AXIOM_PATH=str(folder))

    def build_run(label, level, local, failures=(), trap=False):
        binary = work / label
        built = subprocess.run([str(compiler), '--diagnostic-format=ai', 'build', str(fixture),
                                '--opt', str(level), '--alloc-poison', '-o', str(binary)],
                               cwd=root, env=local, capture_output=True, text=True, timeout=240)
        (work / (label + '.build.stdout')).write_text(built.stdout)
        (work / (label + '.build.stderr')).write_text(built.stderr)
        assert built.returncode == 0, (label, built.returncode, built.stdout, built.stderr)
        ran = subprocess.run([str(binary)], env=local, capture_output=True, text=True, timeout=30)
        (work / (label + '.stdout')).write_text(ran.stdout)
        (work / (label + '.stderr')).write_text(ran.stderr)
        if trap:
            assert ran.returncode == 77 and 'index out of range' in ran.stderr, (label, ran)
            assert ran.stdout.endswith('33 true\n'), (label, ran.stdout)
            return {'exit': ran.returncode, 'before_empty_table_guard': 33}
        assert ran.returncode == 0 and not ran.stderr, (label, ran.returncode, ran.stderr)
        if failures:
            failed = {int(line.split()[0]) for line in ran.stdout.splitlines()
                      if line.endswith(' false') and line.split()[0].isdigit()}
            assert set(failures) <= failed, (label, failed, failures)
            return {'failed_cases': sorted(failed)}
        assert ran.stdout == expected, (label, ran.stdout)
        return {'outcomes': 35, 'stream_bytes': len(STREAM.encode())}

    local = modules('positive')
    for level in (0, 2):
        result = build_run('positive-O' + str(level), level, local)
        report['configurations'].append(dict(result, optimization=level, allocation_poisoning=True, status='passed'))
        print(f'ok   namespace/symbol Options: 35 cases and exact {len(STREAM.encode())} symbol/name bytes O{level} poison', flush=True)

    def altered(module, old, new):
        assert source[module].count(old) == 1, (module, old, source[module].count(old))
        return module, source[module].replace(old, new)

    controls = [
        ('discard-entry-zero', altered('symbols', '((None) (Some 0))', '((None) None)'), {1, 9}, False),
        ('invent-empty-table-row', altered('symbols', '(if (== (vecLen units) 0)\n    None',
                                          '(if (== (vecLen units) 0)\n    (Some 0)'), {2, 3}, False),
        ('discard-empty-module', altered('namespace', '(if (== (nodeTag node) TAG_D_FN)',
                                        '(if (&& (! (strEq modName "")) (== (nodeTag node) TAG_D_FN))'), {15, 16}, False),
        ('select-last-unit', altered('symbols', '(if (&& (isNone hit) (unitNameIs (vecGet units i) name))',
                                    '(if (unitNameIs (vecGet units i) name)'), {8}, False),
        ('lose-entry-render-fallback', altered('symbols', '(match (saUnitOf units (nodeModuleOpt d))\n          ((None) 0)',
                                              '(match (saUnitOf units (nodeModuleOpt d))\n          ((None) 2)'), {29, 33}, False),
        ('extra-lookup-name-share', altered('symbols', '          (let mut i 1)\n          (let mut hit',
                                            '          (__retainref name)\n          (let mut i 1)\n          (let mut hit'), {11}, False),
        ('remove-location-bounds', altered('symbols', '(if (|| (< index 0) (>= index (vecLen units)))',
                                          '(if false'), (), True),
    ]
    for label, change, failures, trap in controls:
        result = build_run(label, 0, modules(label, change), failures, trap)
        report['controls'].append(dict(result, name=label))
        print(f'ok   counterexample {label}: {result}', flush=True)

    imports = '(import core) (import parser) (import namespace) (import symbols) (import Vec)\n'
    invalid = [
        ('raw-module-name', '(:: bad Int) (fn (bad) (mangleDecl (mkEVar "x") "M" mangleIdxNew vecNew vecNew 1))', 'AX3004'),
        ('word-module-input', '(:: bad Int) (fn (bad) (mangleDecl (mkEVar "x") 0 mangleIdxNew vecNew vecNew 1))', 'AX3004'),
        ('erased-unit-input', '(:: bad (-> Units (Option Int))) (fn (bad units) (saUnitOf units 0))', 'AX3004'),
        ('erased-unit-result', '(:: bad (-> Units Int)) (fn (bad units) (saUnitOf units None))', 'AX3004'),
        ('missing-unit-none', '(:: bad (-> Units Int)) (fn (bad units) (match (saUnitOf units None) ((Some index) index)))', 'AX3005'),
    ]
    for label, declaration, code in invalid:
        path = work / (label + '.ax'); path.write_text(imports + declaration + '\n(:: main Int) (fn (main) 0)\n')
        ran = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(path)],
                             env=local, capture_output=True, text=True, timeout=180)
        (work / (label + '.stderr')).write_text(ran.stderr)
        errors = [line for line in ran.stderr.splitlines() if line.startswith('E ')]
        assert ran.returncode == 1 and len(errors) == 1 and f'E {code} ' in errors[0], (label, ran)
        report['refusals'].append({'name': label, 'code': code})
        print(f'ok   refusal {label}: {code}', flush=True)
    (work / 'namespace-symbol-options-report.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True); parser.add_argument('--work', type=Path)
    args = parser.parse_args(); root = Path(__file__).resolve().parents[2]
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True); check(args.axiom.resolve(), root, args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-namespace-symbol-options-') as directory:
            check(args.axiom.resolve(), root, Path(directory))


if __name__ == '__main__':
    main()
