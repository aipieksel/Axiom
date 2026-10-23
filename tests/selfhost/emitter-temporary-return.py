#!/usr/bin/env python3
"""Check the ownership handoff when a call returns a temporary container's child."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import tempfile

SUPPORT = runpy.run_path(str(Path(__file__).with_name('emitter-line-buffer.py')))
run_checked = SUPPORT['run_checked']
check_output = SUPPORT['check_output']
emitted_function = SUPPORT['emitted_function']
EXPECTED = [f'{i} true' for i in range(1, 28)]
GET = re.compile(r'(%[^ ]+) = call i64 @Vec\$vecGet\(i64 (%[^,]+),[^\n]+\)')


def protected_calls(body, expected, generic=False):
    calls = list(GET.finditer(body))
    if len(calls) != expected:
        raise AssertionError('unexpected number of borrowed calls')
    for call in calls:
        result, owner = call.groups()
        retain = f'call void @axiom_retain(i64 {result})'
        release = f'call void @axiom_release(i64 {owner})'
        if body.count(retain) != 1 or body.count(release) != 1:
            raise AssertionError('borrowed result and temporary holder must each balance once')
        if not call.end() < body.index(retain) < body.index(release):
            raise AssertionError('the child must be protected before its holder retires')
        if generic and 'icmp ne i64' not in body[call.end():body.index(retain)]:
            raise AssertionError('generic protection needs runtime reference evidence')
    return calls


def borrowed_parameter(body):
    calls = list(GET.finditer(body))
    if len(calls) != 1 or calls[0][2] != '%rows':
        raise AssertionError('expected a borrowed parameter call')
    result = calls[0][1]
    retain = f'call void @axiom_retain(i64 {result})'
    if body.count(retain) != 1 or f'call void @axiom_release(i64 %rows)' in body:
        raise AssertionError('return must acquire its own share without spending the parameter share')
    if not calls[0].end() < body.index(retain) < body.index(f'ret i64 {result}'):
        raise AssertionError('borrowed return must be retained before returning')


def check(compiler, root, work, baseline=None):
    fixture = root / 'tests/selfhost/emitter-temporary-return.axp'
    program = work / 'temporary-return.ax'
    program.write_bytes(fixture.read_bytes())
    env = dict(os.environ, AXIOM_PATH=str(root / 'core'), AXIOM_STDLIB=str(root / 'stdlib'))
    report = {
        'builder': str(compiler), 'builder_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
        'fixture_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
        'configurations': [], 'counterexamples': [], 'baseline': [],
        'invariants': [
            'A returned child has one protection share before its temporary container is released, with no second tail retain.',
            'Nested temporary containers, conditional and match returns preserve that handoff.',
            'Borrowed parameters still acquire a return share; non-tail use retains and retires its child.',
            'Generic reference evidence guards the protection retain; scalar values retain their numeric meaning.',
        ],
        'known_limits': [
            'These are focused native host tests with allocation poisoning, not global memory-soundness evidence.',
            'General generic-call retirement and an owned-pop wrapper have separate pre-existing retained shares; cases 22-24 check value validity only.',
            'The independent strict comparison probe preserves those failing retirement cases; this test does not bless retained shares as intended behaviour.',
            'Named-container retention and scalar temporary-container retirement are outside this call-result fix.',
            'No held M5 code or lifetime traversal, fixpoint or enforcement algorithm is inspected or changed.',
            'The generated-IR runtime counterexample links through cc on POSIX; Windows runs positive native and structural counterexamples only.',
        ],
    }

    def native(builder, label, level, required_failures=()):
        binary = work / label
        run_checked([str(builder), '--diagnostic-format=ai', 'build', str(program), '--opt', str(level),
                     '--alloc-poison', '-o', str(binary)], env=env,
                    stdout=work / (label + '.build.stdout'), stderr=work / (label + '.build.stderr'))
        output = work / (label + '.stdout')
        run_checked([str(binary)], env=env, stdout=output, stderr=work / (label + '.stderr'), timeout=30)
        return check_output(output, EXPECTED, required_failures=required_failures)

    run_checked([str(compiler), '--diagnostic-format=ai', 'fmt', '--check', str(program)], env=env,
                stdout=work / 'fmt.stdout', stderr=work / 'fmt.stderr')
    for level in (0, 2):
        native(compiler, f'positive-O{level}', level)
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True, 'cases': 27})
        print(f'ok   temporary child return: 27 outcomes O{level} poison', flush=True)
        if baseline:
            failures = native(baseline, f'baseline-O{level}', level, {4, 6, 8, 12})
            report['baseline'].append({'optimization': level, 'failed_cases': sorted(failures),
                                       'builder': str(baseline)})
            print(f'ok   unchanged baseline O{level} exposes cases {sorted(failures)}', flush=True)

    ir = work / 'positive.ll'
    run_checked([str(compiler), '--diagnostic-format=ai', 'emit-llvm', '--alloc-poison', str(program), '-o', str(ir)],
                env=env, stdout=work / 'emit.stdout', stderr=work / 'emit.stderr')
    module = ir.read_text()
    report['ir_sha256'] = hashlib.sha256(ir.read_bytes()).hexdigest()
    for name, calls in [('temporary', 1), ('nestedTemporary', 2), ('conditionalTemporary', 1),
                        ('matchTemporary', 1), ('stringTemporary', 1), ('genericTemporary', 1), ('nonTail', 1)]:
        protected_calls(emitted_function(module, name), calls, name == 'genericTemporary')
    borrowed_parameter(emitted_function(module, 'borrowed'))
    scalar = emitted_function(module, 'scalarTemporary')
    call = list(GET.finditer(scalar))
    if len(call) != 1 or f'@axiom_retain(i64 {call[0][1]})' in scalar:
        raise AssertionError('a scalar result must not acquire a reference share')
    print('ok   emitted handoff: child retain precedes holder release; one result share; borrowed and scalar contracts', flush=True)

    def rejected(name, validator, body):
        try:
            validator(body)
        except AssertionError:
            report['counterexamples'].append({'name': name, 'structural_check': 'rejected'})
            print(f'ok   counterexample {name}: rejected', flush=True)
        else:
            raise AssertionError(f'ineffective structural control {name}')

    body = emitted_function(module, 'temporary')
    result, owner = list(GET.finditer(body))[0].groups()
    retain = f'  call void @axiom_retain(i64 {result})\n'
    release = f'  call void @axiom_release(i64 {owner})\n'
    duplicate = body.replace(f'  ret i64 {result}', retain + f'  ret i64 {result}', 1)
    rejected('duplicate-tail-retain', lambda b: protected_calls(b, 1), duplicate)
    rejected('missing-protection-retain', lambda b: protected_calls(b, 1), body.replace(retain, '', 1))
    rejected('protection-after-holder-release', lambda b: protected_calls(b, 1),
             body.replace(retain + release, release + retain, 1))
    borrowed = emitted_function(module, 'borrowed')
    returned = list(GET.finditer(borrowed))[0][1]
    rejected('blanket-suppressed-tail-retain', borrowed_parameter,
             borrowed.replace(f'  call void @axiom_retain(i64 {returned})\n', '', 1))

    # Only the extra-share mutation is executed. The missing/misordered
    # protection controls are rejected structurally before native execution.
    if sys.platform != 'win32':
        mutated = work / 'duplicate-tail-retain.ll'
        mutated.write_text(module.replace(body, duplicate, 1))
        run_checked(['opt', '-passes=verify', '-disable-output', str(mutated)], env=env,
                    stdout=work / 'mutant.verify.stdout', stderr=work / 'mutant.verify.stderr')
        obj, binary = work / 'mutant.o', work / 'mutant'
        run_checked(['llc', '-filetype=obj', '-relocation-model=pic', str(mutated), '-o', str(obj)], env=env,
                    stdout=work / 'mutant.llc.stdout', stderr=work / 'mutant.llc.stderr')
        run_checked(['cc', str(obj), '-o', str(binary)], env=env,
                    stdout=work / 'mutant.link.stdout', stderr=work / 'mutant.link.stderr')
        output = work / 'mutant.stdout'
        run_checked([str(binary)], env=env, stdout=output, stderr=work / 'mutant.stderr', timeout=30)
        failures = check_output(output, EXPECTED, required_failures={4})
        report['counterexamples'][0]['native_failed_cases'] = sorted(failures)
        print(f'ok   duplicate-tail-retain native counterexample: cases {sorted(failures)} fail', flush=True)
    (work / 'temporary-return-report.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    baseline = args.baseline.resolve() if args.baseline else None
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
        check(args.axiom.resolve(), root, args.work.resolve(), baseline)
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-temporary-return-') as directory:
            check(args.axiom.resolve(), root, Path(directory), baseline)


if __name__ == '__main__':
    main()
