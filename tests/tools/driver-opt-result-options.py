#!/usr/bin/env python3
"""Exercise typed optimiser outcomes, literal argv and build cleanup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile

SUPPORT = runpy.run_path(str(Path(__file__).parents[1] / 'selfhost/emitter-line-buffer.py'))
replace_once = SUPPORT['replace_once']
WARNING = b'warning: `opt` not found on PATH; building without mid-level optimisation (deeply recursive code may exhaust the stack)\n'
FAILURE = b'E AX4003 <toolchain>:- toolchain-failure "opt failed" ?"`opt` is optional - a PATH without it warns and builds - so this is a failure of the one that is installed"\n'
BRIDGE = '''
(pub :: probeRunOpt (-> String Int String (Result String Int)))
;@axiom:effect(io)
;@axiom:effect(spawn)
;@axiom:effect(block)
(pub fn (probeRunOpt input level output) (runOpt input level output))
'''


def check(compiler, root, work, baseline):
    driver = (baseline or root / 'core/driver.ax').read_text()
    bridge = BRIDGE
    if baseline:
        bridge = bridge.replace('(runOpt input level output))',
                                '{ (let path (runOpt input level output)) (if (== (strLen path) 0) (Err -1) (Ok path)) })')
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for folder in ('core', 'stdlib') for path in sorted((root / folder).rglob('*.ax'))}
    env = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    fixture = root / 'tests/tools/driver-opt-result-options.axp'
    child = root / 'tests/tools/driver-opt-native-tool.axp'
    report = {'builder': str(compiler), 'builder_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
              'driver_sha256': hashlib.sha256(driver.encode()).hexdigest(), 'baseline': bool(baseline),
              'source_sha256': hashes, 'configurations': [], 'controls': [], 'refusals': [],
              'limits': [
                  'Native host O0/O2 with allocation poisoning; no cross-target execution claim.',
                  'Negative spawn status retains warning and unoptimised fallback; positive child status is Err.',
                  'The old String helper erased failure status and empty successful paths; baseline adapter records Err -1 for those.',
                  'A zero tool exit still trusts its output path without verifying the output file, as before.',
                  'The broader PID/wait/process status migration remains separate.',
                  'No held lifetime/escape inference is reviewed, changed or validated.',
              ]}

    def modules(label, source=driver):
        folder = work / (label + '-core')
        folder.mkdir()
        for path in (root / 'core').iterdir():
            target = folder / path.name
            if path.name == 'driver.ax':
                target.write_text(source + bridge)
            else:
                target.symlink_to(path, target_is_directory=path.is_dir())
        return dict(env, AXIOM_PATH=str(folder))

    def build(label, source, local, level):
        binary = work / label
        built = subprocess.run([str(compiler), '--diagnostic-format=ai', 'build', str(source),
                                '--opt', str(level), '--alloc-poison', '-o', str(binary)],
                               env=local, capture_output=True, timeout=240)
        (work / (label + '.build.stdout')).write_bytes(built.stdout)
        (work / (label + '.build.stderr')).write_bytes(built.stderr)
        if built.returncode or any(line.startswith((b'E ', b'W ')) for line in built.stderr.splitlines()):
            raise AssertionError((label, built.returncode, built.stderr.decode()))
        return binary

    def argv(tool, *args):
        return ''.join(f'{index}:{arg}\n' for index, arg in enumerate((tool, *args))).encode()

    def exercise(label, binary, local, child_binary, restricted=None):
        data = work / (label + '-data')
        data.mkdir()
        for folder, names in (('all', ('opt', 'llc', 'cc')), ('no-opt', ('llc', 'cc'))):
            (data / folder).mkdir()
            for name in names:
                shutil.copy2(child_binary, data / folder / name)
        scenarios = []
        def helper(name, level, mode='ok', path='all', input='input.ll', output='optimised.ll'):
            attempted = level > 0
            missing = attempted and path == 'no-opt'
            failed = attempted and not missing and mode.startswith('fail')
            code = 19 if mode == 'fail19' else 7
            result = input if not attempted or missing else output
            stdout = f'err {(-1 if baseline else code)}\n' if failed else f'ok {result}\n'
            if baseline and not failed and result == '':
                stdout = 'err -1\n'
            log = b'' if not attempted or missing else argv('opt', '-O' + str(min(level, 3)), input, '-S', '-o', output)
            files = {} if not attempted or missing or failed or mode == 'no-output' or output == '' else {output: b'opt output\n'}
            scenarios.append((name, ('helper', input, str(level), output), path, mode,
                              stdout.encode(), WARNING if missing else b'', log, files))
        helper('disabled', 0, mode='fail7')
        helper('negative-level', -1, mode='fail7')
        helper('missing-optimiser', 1, path='no-opt')
        helper('optimised', 1)
        helper('clamped-level', 9)
        helper('failed7', 1, mode='fail7')
        helper('failed19', 2, mode='fail19')
        helper('literal-argv', 2, input='in ;$(touch forbidden).ll', output='out space.opt.ll')
        helper('empty-input-disabled', 0, input='')
        helper('empty-output-success', 1, output='')
        helper('trusted-output-path', 1, mode='no-output')
        for name, level, path, mode, keep in (
            ('build-disabled', 0, 'all', 'ok', False),
            ('build-fallback', 1, 'no-opt', 'ok', False),
            ('build-optimised', 2, 'all', 'ok', False),
            ('build-keep-ir', 2, 'all', 'ok', True),
            ('build-failed', 1, 'all', 'fail7', False),
            ('build-failed-keep', 1, 'all', 'fail7', True),
        ):
            failed = mode == 'fail7'
            optimised = level > 0 and path == 'all'
            used = 'output.opt.ll' if optimised else 'output.ll'
            log = argv('opt', '-O' + str(level), 'output.ll', '-S', '-o', 'output.opt.ll') if optimised else b''
            if not failed:
                log += argv('llc', used, '-filetype=obj', '-o', 'output.o', '-O' + str(level), '-relocation-model=pic')
                log += argv('cc', 'output.o', '-o', 'output')
            files = {} if failed else {'output': b'cc output\n'}
            if keep and not failed:
                files['output.ll'] = b'; optimiser outcome fixture\n'
            scenarios.append((name, ('build', 'output', str(level), 'keep' if keep else 'remove', '--diagnostic-format=ai'),
                              path, mode, b'status 4\n' if failed else b'status 0\n',
                              FAILURE if failed else (WARNING if path == 'no-opt' else b''), log, files))
        outcomes = {}
        for name, args, path, mode, stdout, stderr, expected_log, files in scenarios:
            if restricted and name not in restricted:
                continue
            folder = data / name
            folder.mkdir()
            native_env = dict(local, PATH=str(data / path), AXIOM_TEST_OPT_MODE=mode, AXIOM_CODEGEN_JOBS='1')
            ran = subprocess.run([str(binary), *args], cwd=folder, env=native_env,
                                 capture_output=True, timeout=30)
            (folder / 'observed.stdout').write_bytes(ran.stdout)
            (folder / 'observed.stderr').write_bytes(ran.stderr)
            log = (folder / 'tools.log').read_bytes() if (folder / 'tools.log').exists() else b''
            actual_files = {p.name: p.read_bytes() for p in folder.iterdir()
                            if p.name not in ('observed.stdout', 'observed.stderr', 'tools.log')}
            passed = ran.returncode == 0 and ran.stdout == stdout and ran.stderr == stderr and log == expected_log and actual_files == files
            outcomes[name] = {'passed': passed, 'stdout_sha256': hashlib.sha256(ran.stdout).hexdigest(),
                              'stderr_sha256': hashlib.sha256(ran.stderr).hexdigest(),
                              'argv_sha256': hashlib.sha256(log).hexdigest(), 'files': sorted(actual_files)}
            if not passed and not restricted:
                raise AssertionError((label, name, ran.returncode, ran.stdout, stdout, ran.stderr, stderr,
                                      log, expected_log, actual_files, files))
        return outcomes

    local = modules('positive')
    for level in (0, 2):
        child_binary = build('child-O' + str(level), child, local, level)
        binary = build('outcomes-O' + str(level), fixture, local, level)
        outcomes = exercise('outcomes-O' + str(level), binary, local, child_binary)
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True, 'cases': outcomes})
        print(f'ok   optimiser outcomes: {len(outcomes)} exact argv/status/output/cleanup cases O{level}', flush=True)
    if not baseline:
        variants = [
            ('failure-treated-as-success', '(Err code)\n                (Ok outPath)', '(Ok llPath)\n                (Ok outPath)', ('failed7', 'build-failed')),
            ('child-status-erased', '(Err code)\n                (Ok outPath)', '(Err 1)\n                (Ok outPath)', ('failed7', 'failed19')),
            ('missing-fallback-lost', '(Ok llPath)\n              }', '(Err 1)\n              }', ('missing-optimiser', 'build-fallback')),
        ]
        for label, before, after, required in variants:
            altered = modules(label, replace_once(driver, before, after, label))
            binary = build(label, fixture, altered, 0)
            observed = exercise(label, binary, altered, work / 'child-O0', restricted=required)
            if any(observed[name]['passed'] for name in required):
                raise AssertionError((label, observed))
            report['controls'].append({'name': label, 'failed_cases': list(required)})
            print(f'ok   {label}: {", ".join(required)} fail', flush=True)
        refusals = [
            ('erased-result', '(:: bad String) (fn (bad) (runOpt "input" 1 "output"))', 'AX3004'),
            ('missing-err', '(:: bad String) (fn (bad) (match (runOpt "input" 1 "output") ((Ok path) path)))', 'AX3005'),
            ('missing-ok', '(:: bad Int) (fn (bad) (match (runOpt "input" 1 "output") ((Err status) status)))', 'AX3005'),
        ]
        for label, declaration, code in refusals:
            source = work / (label + '.ax')
            annotated = declaration.replace(' (fn (bad)', '\n;@axiom:effect(io)\n;@axiom:effect(spawn)\n;@axiom:effect(block)\n(fn (bad)')
            source.write_text('(import driver)\n(import Err)\n' + annotated + '\n(:: main Int) (fn (main) 0)\n')
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
    parser.add_argument('--baseline-driver', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
        check(args.axiom.resolve(), root, args.work.resolve(), args.baseline_driver)
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-driver-opt-result-') as directory:
            check(args.axiom.resolve(), root, Path(directory), args.baseline_driver)


if __name__ == '__main__':
    main()
