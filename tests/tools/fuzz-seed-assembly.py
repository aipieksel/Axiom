#!/usr/bin/env python3
"""Focused controls for the real fuzz harness's seed assembly boundary."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True)
    parser.add_argument('--modules', type=Path, required=True,
                        help='isolated self_host/stdlib source tree for the compiler')
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    if args.work:
        work = args.work.resolve()
        work.mkdir(parents=True, exist_ok=True)
        check(repo, args.axiom.resolve(), args.modules.resolve(), work)
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-fuzz-seed-') as directory:
            check(repo, args.axiom.resolve(), args.modules.resolve(), Path(directory))


def check(repo, axiom, modules, work):
    source = (repo / 'scripts/check-fuzz.sh').read_text()
    begin = source.index('describe_rc() {')
    end = source.index('# fmt_one ', begin)
    runner = work / 'runner.sh'
    runner.write_text('#!/usr/bin/env bash\nset -uo pipefail\n'
                      + 'source "$FUZZ_GATE_LIB"\n'
                      + source[begin:end]
                      + '\nfuzz_one "$1" "$2" "$3" 15 "${4:-}"\n'
                      + 'printf "RESULT\\t%s\\t%s\\n" "$f_verdict" "$f_stage"\n')
    seed = work / 'original-pressure.ax'
    seed.write_text((repo / 'tests/embedded/asm-link-register.ax').read_text())
    mutant = work / 'mutant-pressure.ax'
    original = seed.read_text()
    assert original.count('(in b (+ x 1))') == 1
    mutant.write_text(original.replace('(in b (+ x 1))', '(in b (& x 1))'))
    valid = work / 'valid.ax'
    valid.write_text('(:: main Int)\n(fn (main) 42)\n')
    refused = work / 'refused.ax'
    refused.write_text('(fn (main)\n')
    tool_dir = work / 'tools'
    tool_dir.mkdir(exist_ok=True)
    real_llc = shutil.which('llc')
    assert real_llc, 'llc is not on PATH'
    llc = tool_dir / 'llc'
    llc.write_text('''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
args=sys.argv[1:]
mode=os.environ.get('FUZZ_TEST_LLC','')
if mode=='crash': sys.exit(139)
if mode=='timeout': sys.exit(124)
if mode=='pressure':
    print('error: inline assembly requires more registers than available at line 1',file=sys.stderr)
    sys.exit(1)
if mode=='mixed':
    print('error: inline assembly requires more registers than available at line 1',file=sys.stderr)
    print('error: planted unrelated LLVM failure',file=sys.stderr)
    sys.exit(1)
if mode=='asmtext':
    print('<inline asm>:1:1: error: planted invalid mnemonic',file=sys.stderr)
    sys.exit(1)
sys.exit(subprocess.run([os.environ['FUZZ_TEST_REAL_LLC']]+args).returncode)
''')
    llc.chmod(0o755)
    wrapper = work / 'compiler'
    wrapper.write_text('''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
args=sys.argv[1:]
inp=next((pathlib.Path(a) for a in args if a.endswith('.ax')),None)
mode=os.environ.get('FUZZ_TEST_COMPILER','')
seed=inp and inp.name=='original-pressure.ax'
emit='emit-llvm' in args
if emit: args=['--target','darwin-aarch64']+args
if mode=='seed-crash' and seed: sys.exit(139)
if mode=='emit-crash' and emit: sys.exit(139)
result=subprocess.run([os.environ['FUZZ_TEST_REAL_COMPILER']]+args)
if result.returncode: sys.exit(result.returncode)
if emit:
    out=pathlib.Path(args[args.index('-o')+1])
    if mode=='empty': out.write_text('')
    if mode=='badir' or (mode=='seed-badir' and seed):
        with out.open('a') as stream: stream.write('this line is not LLVM IR\\n')
sys.exit(0)
''')
    wrapper.chmod(0o755)
    env = dict(os.environ, AXIOM_STDLIB=str(modules / 'stdlib'),
               FUZZ_GATE_LIB=str(repo / 'scripts/lib/gate.sh'),
               FUZZ_TEST_REAL_COMPILER=str(axiom), FUZZ_TEST_REAL_LLC=real_llc,
               PATH=str(tool_dir) + os.pathsep + os.environ['PATH'])
    cases = [
        ('inherited-pressure', mutant, seed, '', '', 'seedasm', 'llc'),
        ('new-pressure', mutant, valid, '', '', 'fail', 'llc'),
        ('valid', valid, valid, '', '', 'ok', ''),
        ('valid-mutant-of-pressure', valid, seed, '', '', 'ok', ''),
        ('missing-seed', mutant, work / 'absent.ax', '', '', 'fail', 'llc'),
        ('no-seed', mutant, None, '', '', 'fail', 'llc'),
        ('bad-ir', mutant, seed, 'badir', '', 'fail', 'llc'),
        ('bad-ir-with-pressure-message', mutant, seed, 'badir', 'pressure', 'fail', 'llc'),
        ('seed-bad-ir', mutant, seed, 'seed-badir', '', 'fail', 'llc'),
        ('seed-compiler-crash', mutant, seed, 'seed-crash', '', 'fail', 'llc'),
        ('compiler-emit-crash', mutant, seed, 'emit-crash', '', 'fail', 'emit'),
        ('compiler-empty-emit', mutant, seed, 'empty', '', 'fail', 'emit'),
        ('llc-crash', mutant, seed, '', 'crash', 'fail', 'llc'),
        ('llc-timeout', mutant, seed, '', 'timeout', 'fail', 'llc'),
        ('mixed-llvm-errors', mutant, seed, '', 'mixed', 'fail', 'llc'),
        ('existing-asm-text', mutant, seed, '', 'asmtext', 'asmtext', 'llc'),
        ('existing-check-refusal', refused, seed, '', '', 'refused', ''),
    ]
    results = []
    for name, input_, baseline, compiler_mode, llc_mode, verdict, stage in cases:
        case_env = dict(env, FUZZ_TEST_COMPILER=compiler_mode, FUZZ_TEST_LLC=llc_mode)
        result = subprocess.run(['/bin/bash', str(runner), str(wrapper), str(input_),
                                 str(modules / 'self_host'), str(baseline) if baseline else ''],
                                env=case_env, capture_output=True, text=True, timeout=60)
        (work / f'{name}.stdout').write_text(result.stdout)
        (work / f'{name}.stderr').write_text(result.stderr)
        expected = f'RESULT\t{verdict}\t{stage}'
        assert result.returncode == 0 and result.stdout.splitlines()[-1] == expected, (
            name, result.returncode, result.stdout, result.stderr)
        results.append({'case': name, 'verdict': verdict, 'stage': stage})
        print(f'ok   {name}: {verdict}{" at " + stage if stage else ""}', flush=True)
    (work / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(f'fuzz seed assembly: {len(results)} focused oracle checks passed (Darwin AArch64 cross-assembly; no native execution)')


if __name__ == '__main__':
    main()
