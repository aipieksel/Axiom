#!/usr/bin/env python3
"""Check binding lookup Options and exact generated Rust, including refusal notes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

BRIDGE = r'''(pub :: bindingProbeData (-> Nodes String (Option ASTNode)))
(pub fn (bindingProbeData nodes name) (rbDataDecl nodes name))
(pub :: bindingProbeSig (-> Nodes String Int (Option ASTNode)))
(pub fn (bindingProbeSig nodes name start) (rbFindSig nodes name start))
(pub :: bindingProbeIsData (-> Nodes ASTNode Int))
(pub fn (bindingProbeIsData nodes ty) (rbIsData nodes ty))
(pub :: bindingProbeIsStruct (-> Nodes ASTNode Int))
(pub fn (bindingProbeIsStruct nodes ty) (rbIsStructType nodes ty))
(pub :: bindingProbeCtorCount (-> Nodes ASTNode Int))
(pub fn (bindingProbeCtorCount nodes ty) (vecLen (rbCtorsOf nodes ty)))
(pub :: bindingProbeNamedEmpty (-> Nodes ASTNode Bool))
(pub fn (bindingProbeNamedEmpty nodes ty)
  { (let ctors (rbCtorsOf nodes ty))
  (if (== (vecLen ctors) 1)
    { (let ctor (vecGet ctors 0))
    (match ctor.fields ((None) false) ((Some names) (== (vecLen names) 0))) }
    false) })
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('compiler', type=Path)
    parser.add_argument('--baseline-rustbind', type=Path)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    compiler = str(args.compiler.resolve())
    source = (root / 'core/rustbind.ax').read_text()
    fixture = root / 'tests/tools/rustbind-query-options.axp'
    golden = fixture.with_suffix('.golden')
    temporary = None
    if args.work:
        work = args.work.resolve()
        work.mkdir(exist_ok=False)
    else:
        temporary = tempfile.TemporaryDirectory(prefix='axiom-rustbind-query-options-')
        work = Path(temporary.name)
    report = {'cases': 20, 'configurations': [], 'controls': [], 'refusals': [],
              'limits': ['Entry data lookup preserves last match, signature lookup preserves first match from its supplied index.',
                         'Packed AST type-child nil is normalised at the signature boundary; other type-walker input contracts are unchanged.',
                         'No Rust runtime/FFI ownership redesign or held M5 work.']}
    env = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))

    def environment(label, text, bridge=BRIDGE):
        modules = work / (label + '-core')
        modules.mkdir()
        for path in (root / 'core').iterdir():
            out = modules / path.name
            if path.name == 'rustbind.ax':
                out.write_text(text + '\n' + bridge)
            else:
                out.symlink_to(path, target_is_directory=path.is_dir())
        return dict(env, AXIOM_PATH=str(modules))

    def native(label, text, level, expected=None, failed=None, bridge=BRIDGE):
        local = environment(label, text, bridge)
        out = work / label
        built = subprocess.run([compiler, '--diagnostic-format=ai', 'build', str(fixture),
                                '--opt', str(level), '--alloc-poison', '-o', str(out)],
                               env=local, capture_output=True, text=True, timeout=240)
        assert built.returncode == 0, (label, built.stdout, built.stderr)
        ran = subprocess.run([str(out)], capture_output=True, timeout=30)
        assert ran.returncode == 0 and not ran.stderr, (label, ran)
        lines = ran.stdout.decode().splitlines()
        assert len(lines) > 21 and lines[20] == 'BINDING', (label, ran.stdout)
        if failed is None:
            assert lines[:20] == [f'{i} true' for i in range(1, 21)], (label, lines[:20])
            if expected is not None:
                assert ran.stdout == expected, (label, 'generated binding differs')
        else:
            assert lines[failed-1] == f'{failed} false' and all(
                line in (f'{i} true', f'{i} false') for i, line in enumerate(lines[:20], 1)), (label, lines[:20])
        (work / (label + '.stdout')).write_bytes(ran.stdout)
        print('ok   ' + label, flush=True)
        return ran.stdout, local

    try:
        if args.baseline_rustbind:
            original = args.baseline_rustbind.read_text()
            bridge = BRIDGE.replace('(rbDataDecl nodes name)',
                                    '{ (let node (rbDataDecl nodes name)) (if (nodeSame node nilNode) None (Some node)) }')
            bridge = bridge.replace('(rbFindSig nodes name start)',
                                    '{ (let node (rbFindSig nodes name start)) (if (nodeSame node nilNode) None (Some node)) }')
            baseline, _ = native('unchanged-baseline', original, 0, bridge=bridge)
            assert baseline.endswith(b"\n\n")
            golden.write_bytes(baseline[:-1])
        expected = golden.read_bytes() + b"\n"
        assert len(expected) > 1000
        for level in (0, 2):
            _, local = native('positive-O' + str(level), source, level, expected)
            report['configurations'].append({'optimization': level, 'poison': True})
        # An early data hit must not hide a later entry declaration.
        old = '(set found (Some d))'
        assert source.count(old) == 1
        native('first-data-wins', source.replace(old, '(if (isNone found) (set found (Some d)))'), 0, failed=2)
        report['controls'].append({'mutation': 'first-data-wins', 'fails': 2})
        start = source.index('(fn (rbFindSig ')
        end = source.index('\n\n', start)
        part = source[start:end]
        assert part.count('(Some d)') == 1
        mutated = source[:start] + part.replace('(Some d)', 'None') + source[end:]
        native('signature-presence-lost', mutated, 0, failed=5)
        report['controls'].append({'mutation': 'signature-presence-lost', 'fails': 5})
        local = environment('refusals', source)
        for label, declaration, code in [
                ('unchecked-data', '(:: bad (-> Nodes Int)) (fn (bad nodes) (nodeTag (bindingProbeData nodes "x")))', 'AX3004'),
                ('unchecked-signature', '(:: bad (-> Nodes ASTNode)) (fn (bad nodes) (bindingProbeSig nodes "x" 0))', 'AX3004'),
                ('missing-none', '(:: bad (-> Nodes ASTNode)) (fn (bad nodes) (match (bindingProbeData nodes "x") ((Some node) node)))', 'AX3005')]:
            path = work / (label + '.ax')
            path.write_text('(import core)\n(import parser)\n(import rustbind)\n' + declaration + '\n(:: main Int) (fn (main) 0)\n')
            checked = subprocess.run([compiler, '--diagnostic-format=ai', 'check', str(path)],
                                     env=local, capture_output=True, text=True, timeout=60)
            errors = [line for line in checked.stderr.splitlines() if line.startswith('E ')]
            assert checked.returncode == 1 and len(errors) == 1 and f'E {code} ' in errors[0], (label, checked)
            report['refusals'].append({'case': label, 'code': code})
            print('ok   ' + label + ': ' + code, flush=True)
        report['exact_output_bytes'] = len(expected)
        (work / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(f'ok   binding queries: 20 cases O0/O2 poison, {len(expected)} exact output bytes, two effective controls, three refusals', flush=True)
    finally:
        if temporary:
            temporary.cleanup()


if __name__ == '__main__':
    main()
