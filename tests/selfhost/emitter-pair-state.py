#!/usr/bin/env python3
"""Check typed auxiliary state, scoped ledger retirement and optional facts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

SUPPORT = runpy.run_path(str(Path(__file__).with_name('emitter-line-buffer.py')))
run_checked = SUPPORT['run_checked']
replace_once = SUPPORT['replace_once']
check_output = SUPPORT['check_output']
BRIDGES = '''
(pub :: probeLambdaType (-> CG ASTNode CG))
(pub fn (probeLambdaType cg ty) (cgSetLambdaType cg ty))
(pub :: probeFunctionTypes (-> CG FunctionTypes CG))
(pub fn (probeFunctionTypes cg types) (cgSetFunctionFloats cg (Some (functionFloatIndex types))))
(pub :: probeCheckedFlags (-> CG String (Option Ints)))
(pub fn (probeCheckedFlags cg name) (checkedFloatFlags cg name))
(pub :: probeTruncateSlots (-> CG Int Int))
(pub fn (probeTruncateSlots cg size) (truncatePendingSlots (pairOwningSlots cg) size))
(pub :: probePairMode (-> CG Int))
(pub fn (probePairMode cg) (cgPairMode cg))
'''


def check(axiom, repo, work):
    fixture = work / 'pair-state.ax'
    fixture.write_bytes((repo / 'tests/selfhost/emitter-pair-state.axp').read_bytes())
    expected = [f'{i} true' for i in range(1, 52)]
    original = (repo / 'core/codegen.ax').read_text()
    env = dict(os.environ, AXIOM_STDLIB=str(repo / 'stdlib'))
    report = {'source_sha256': hashlib.sha256(original.encode()).hexdigest(),
              'builder': str(axiom), 'configurations': [], 'counterexamples': [],
              'invariants': [
                  {'invariant': 'Auxiliary state owns and retires its nested references.',
                   'enforcement': 'PairState/PendingSlot typed fields, typed vectors and field stores; cases 10-13,17,19,22,32-35,42-49.',
                   'counterexample': 'unmapped-state-children'},
                  {'invariant': 'Owning slot scope truncation retires each removed descriptor.',
                   'enforcement': 'truncatePendingSlots vecPop loop; cases 36-41.',
                   'counterexample': 'raw-slot-truncation'},
                  {'invariant': 'Optional facts handle absence and preserve typed lookup results.',
                   'enforcement': 'Some/None branches and pairRetTyOf; cases 1-9,14-16,18-31.',
                   'counterexample': 'lost-float-index'},
              ],
              'known_limits': ['Host execution only; no Windows runtime evidence.',
                               'Numeric flags/counters and emitted LLVM words remain numeric intentionally.',
                               'AST nil is normalized at checked reader boundaries; this is not a general AST redesign.',
                               'Focused evidence does not establish global soundness, Rust parity or certification.']}
    run_checked([str(axiom), '--diagnostic-format=ai', 'fmt', '--check', str(fixture)],
                env=env, stdout=work / 'fmt.stdout', stderr=work / 'fmt.stderr')

    def modules_for(label, source):
        modules = work / f'{label}-modules'
        modules.mkdir(exist_ok=True)
        for path in (repo / 'core').iterdir():
            destination = modules / path.name
            if path.name == 'codegen.ax':
                destination.write_text(source + BRIDGES)
            elif not destination.exists():
                destination.symlink_to(path, target_is_directory=path.is_dir())
        return modules

    def build_run(label, level, source, negative=()):
        local = dict(env, AXIOM_PATH=str(modules_for(label, source)))
        binary = work / label
        run_checked([str(axiom), '--diagnostic-format=ai', 'build', str(fixture),
                     '--opt', str(level), '--alloc-poison', '-o', str(binary)],
                    env=local, stdout=work / f'{label}.build.stdout', stderr=work / f'{label}.build.stderr')
        output = work / f'{label}.stdout'
        run_checked([str(binary)], env=local, stdout=output, stderr=work / f'{label}.stderr', timeout=30)
        return check_output(output, expected, required_failures=negative)

    for level in (0, 2):
        build_run(f'positive-O{level}', level, original)
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True,
                                         'cases': 51, 'status': 'passed'})
        print(f'ok   PairState: 51 cases at -O{level} with allocation poisoning', flush=True)
    unmapped = replace_once(original, '      (CG\n        :lines vecNew',
                            '      (memMarkLeaf (as ps Int))\n      (CG\n        :lines vecNew',
                            'state child reference map')
    truncate = replace_once(original,
                            '    (while (> (vecLen slots) size)\n      {\n        (let retired (vecPop slots))\n        0\n      })',
                            '    (memSetWord (as slots Int) 0 size)', 'typed slot scope retirement')
    lost = replace_once(original, '(fn (cgFunctionFloats cg)\n  cg.pairState.functionFloats)',
                        '(fn (cgFunctionFloats cg)\n  (None))', 'checked float index lookup')
    for label, source, cases in [
        ('unmapped-state-children', unmapped, (11, 13, 33, 34, 35)),
        ('raw-slot-truncation', truncate, (38,)),
        ('lost-float-index', lost, (26,)),
    ]:
        failures = build_run(label, 0, source, cases)
        report['counterexamples'].append({'name': label, 'failed_cases': sorted(failures)})
        print(f'ok   counterexample {label}: cases {sorted(failures)} fail', flush=True)
    source = work / 'missing-pair-result.ax'
    source.write_text('''(import codegen)
(import parser)
(:: main Int)
;@axiom:effect(io)
(fn (main) { (emitPairWrapper (mkDFnAt "missing" vecNew (mkEInt 0) 0) newCG) 0 })
''')
    local = dict(env, AXIOM_PATH=str(modules_for('missing-result', original)))
    binary = work / 'missing-pair-result'
    run_checked([str(axiom), '--diagnostic-format=ai', 'build', str(source), '--opt', '0',
                 '--alloc-poison', '-o', str(binary)], env=local,
                stdout=work / 'missing-result.build.stdout', stderr=work / 'missing-result.build.stderr')
    result = subprocess.run([str(binary)], env=local, capture_output=True, text=True, timeout=30)
    (work / 'missing-result.stderr').write_text(result.stderr)
    if result.returncode != 3 or 'AX4002' not in result.stderr or 'pair wrapper has no checked result type' not in result.stderr:
        raise RuntimeError(f'missing checked pair result lost its error: {result}')
    report['required_missing_result'] = {'diagnostic': 'AX4002', 'exit': 3, 'status': 'passed'}
    print('ok   missing required pair result: AX4002 and exit 3', flush=True)
    (work / 'pair-state-report.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    if args.work:
        work = args.work.resolve()
        work.mkdir(parents=True, exist_ok=True)
        check(args.axiom.resolve(), repo, work)
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-pair-state-') as directory:
            check(args.axiom.resolve(), repo, Path(directory))


if __name__ == '__main__':
    main()
