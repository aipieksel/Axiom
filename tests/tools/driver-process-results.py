#!/usr/bin/env python3
"""Check typed process outcomes and settling every child before cleanup."""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile

SUPPORT = runpy.run_path(str(Path(__file__).parents[1] / 'selfhost/emitter-line-buffer.py'))
replace_once = SUPPORT['replace_once']


def diagnostic(message, help_text):
    return f'E AX4003 <toolchain>:- toolchain-failure "{message}" ?"{help_text}"\n'.encode()


LLC_FAILURE = diagnostic('llc failed', 'one of the split parts was rejected by `llc`; set AXIOM_CODEGEN_JOBS=1 for the serial path')
SPLIT_FAILURE = diagnostic('llvm-split failed', '`llvm-split` partitions the module for parallel code generation; it ships with LLVM - or set AXIOM_CODEGEN_JOBS=1 for the serial path')
CC_FAILURE = diagnostic('cc failed', 'a C compiler drives the link step; put `cc` on PATH')
CC_MISSING = diagnostic('could not run `cc`', 'a C compiler drives the link step; put `cc` on PATH')


def supports_posix_process_probes(system):
    # These probes require signal wait status and wait-any-child reaping.
    return system in ('Darwin', 'Linux', 'FreeBSD')


def check(compiler, root, work):
    system = platform.system()
    if not supports_posix_process_probes(system):
        reason = ('This harness requires POSIX signals and wait-any-child reaping; '
                  'Windows process handles and termination need separate native coverage.')
        report = {'status': 'skipped', 'host': system, 'reason': reason,
                  'configurations': [], 'controls': [], 'refusals': []}
        (work / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(f'skip process results on {system}: {reason}', flush=True)
        return
    driver = (root / 'core/driver.ax').read_text()
    bridge = (root / 'tests/tools/driver-process-bridge.axp').read_text()
    fixture = root / 'tests/tools/driver-process-results.axp'
    child = root / 'tests/tools/driver-process-native-child.axp'
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for folder in ('core', 'stdlib') for path in sorted((root / folder).rglob('*.ax'))}
    env = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    report = {'status': 'passed', 'host': system, 'builder': str(compiler), 'builder_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
              'source_sha256': hashes, 'configurations': [], 'controls': [], 'refusals': [],
              'limits': [
                  'Native host O0/O2 with allocation poisoning; no cross-target execution claim.',
                  'OS spawn/wait/signal contracts are trusted boundaries; no inference or ARC algorithm is reviewed.',
                  'All successful launches receive a wait; a wait error is retained as failure, not represented as successful reaping.',
                  'The fixture drains any uncollected children after its observations, including negative controls.',
                  'The synchronous runTool result is separate from this split launch/wait closure.',
              ]}

    def modules(label, source=driver):
        folder = work / (label + '-core')
        folder.mkdir()
        for path in (root / 'core').iterdir():
            target = folder / path.name
            if path.name == 'driver.ax':
                target.write_text('(import IO (println))\n' + source + '\n' + bridge)
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
        outcomes = {}
        phase_cases = {
            'empty': 'complete', 'success': 'complete',
            'missing-first': 'failure 0 missing', 'spawn-first': 'failure 0 spawn 2',
            'exit-first': 'failure 0 exit 7', 'signal-first': 'failure 0 signal 9',
            'wait-first': 'failure 0 wait 10', 'two-fail': 'failure 0 exit 7',
            'later-missing': 'failure 1 missing',
        }
        split_cases = {
            'split-success': ('split', 'ok', None, True, True, b''),
            'split-unavailable': ('split', 'ok', 'llvm-split', False, False, b''),
            'split-failed': ('split', 'split-fail', None, False, False, SPLIT_FAILURE),
            'split-missing-llc': ('split', 'ok', 'llc', False, False, LLC_FAILURE),
            'split-unstartable-llc': ('split', 'ok', 'nonexec-llc', False, False, LLC_FAILURE),
            'split-llc-exit': ('split', 'llc-exit', None, False, True, LLC_FAILURE),
            'split-llc-signal': ('split', 'llc-signal', None, False, True, LLC_FAILURE),
            'split-link-failed': ('split', 'link-fail', None, True, True, CC_FAILURE),
            'split-link-missing': ('split', 'ok', 'cc', False, True, CC_MISSING),
            'pipeline-success': ('pipeline', 'ok', None, True, True, b''),
            'pipeline-fallback': ('pipeline', 'ok', 'llvm-split', True, True, b''),
            'pipeline-split-failed': ('pipeline', 'split-fail', None, False, False, SPLIT_FAILURE),
            'pipeline-llc-exit': ('pipeline', 'llc-exit', None, False, True, LLC_FAILURE),
        }

        def setup(name, omitted=None):
            folder = data / name
            folder.mkdir()
            tools = folder / 'bin'
            tools.mkdir()
            for tool in ('llvm-split', 'llc', 'cc'):
                if tool == omitted:
                    continue
                if tool == 'llc' and omitted == 'nonexec-llc':
                    (tools / tool).write_text('not executable\n')
                    (tools / tool).chmod(0o644)
                else:
                    shutil.copy2(child_binary, tools / tool)
            shutil.copy2(child_binary, folder / 'worker')
            return folder, tools

        def run_case(name, folder, tools, args, mode, stdout, stderr, files, absent=()):
            native_env = dict(local, PATH=str(tools), AXIOM_TEST_PROCESS_MODE=mode, AXIOM_CODEGEN_JOBS='2')
            ran = subprocess.run([str(binary), *args, '--diagnostic-format=ai'], cwd=folder,
                                 env=native_env, capture_output=True, timeout=45)
            (folder / 'observed.stdout').write_bytes(ran.stdout)
            (folder / 'observed.stderr').write_bytes(ran.stderr)
            mismatches = []
            if ran.returncode != 0 or ran.stdout != stdout or ran.stderr != stderr:
                mismatches.append('status/stdout/stderr')
            for file, contents in files.items():
                if not (folder / file).is_file() or (folder / file).read_bytes() != contents:
                    mismatches.append('file:' + file)
            for file in absent:
                if (folder / file).exists():
                    mismatches.append('unexpected:' + file)
            outcomes[name] = {'passed': not mismatches, 'mismatches': mismatches,
                              'stdout_sha256': hashlib.sha256(ran.stdout).hexdigest(),
                              'stderr_sha256': hashlib.sha256(ran.stderr).hexdigest()}
            if mismatches and not restricted:
                raise AssertionError((label, name, mismatches, ran.returncode, ran.stdout, stdout, ran.stderr, stderr))

        for name, first_line in phase_cases.items():
            case = 'phase-' + name
            if restricted and case not in restricted:
                continue
            folder, tools = setup(case)
            run_case(case, folder, tools, ('phase', name), 'ok',
                     (first_line + '\nready true\ndrained 0\n').encode(), b'', {})
        for name, (kind, mode, omitted, linked, children, stderr) in split_cases.items():
            if restricted and name not in restricted:
                continue
            folder, tools = setup(name, omitted)
            (folder / 'input.ll').write_text('; input\n')
            pipeline = kind == 'pipeline'
            if pipeline:
                (folder / 'large.ll').write_bytes(b';' + b' ' * 4194304 + b'\n')
            fallback = omitted == 'llvm-split'
            status = 4 if stderr else 0
            first_line = f'status {status}' if pipeline else ('unavailable' if fallback else ('failed 4' if stderr else 'success'))
            files = {}
            absent = []
            stem = 'output.split' if pipeline else 'split'
            input_path = 'output.ll' if pipeline else 'input.ll'
            if fallback:
                absent.append('splitter.argv')
            else:
                files['splitter.argv'] = argv('llvm-split', '-j=2', '-o', stem + '.part', input_path)
            if children:
                parts = ['output.ll'] if fallback else [stem + '.part0', stem + '.part1']
                for part in parts:
                    # Serial assembly uses runTool's argv[0]; split assembly uses the resolved path.
                    tool = 'llc' if fallback else str(tools / 'llc')
                    opt = '-O0' if pipeline else '-O2'
                    obj = 'output.o' if fallback else part + '.o'
                    files[part + '.argv'] = argv(tool, part, '-filetype=obj', '-o', obj, opt, '-relocation-model=pic')
                    files[part + '.settled'] = b'present\n'
            else:
                for part in (stem + '.part0', stem + '.part1'):
                    absent.extend((part + '.argv', part + '.settled'))
            if linked:
                objects = ['output.o'] if fallback else [stem + '.part0.o', stem + '.part1.o']
                files['link.argv'] = argv('cc', *objects, '-o', 'output')
            else:
                absent.append('link.argv')
            if status == 0 and linked:
                files['output'] = b'linked\n'
            else:
                absent.append('output')
            for part in (stem + '.part0', stem + '.part1'):
                absent.extend((part, part + '.o', part + '.opt', part + '.llc-err'))
            if pipeline:
                absent.extend(('output.ll', 'output.o', 'output.opt.ll'))
            run_case(name, folder, tools, (kind,), mode,
                     (first_line + '\ndrained 0\n').encode(), stderr, files, absent)
        return outcomes

    local = modules('positive')
    for level in (0, 2):
        child_binary = build('child-O' + str(level), child, local, level)
        binary = build('process-O' + str(level), fixture, local, level)
        outcomes = exercise('process-O' + str(level), binary, local, child_binary)
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True, 'cases': outcomes})
        print(f'ok   process results: {len(outcomes)} typed outcome, argv and settled-child cases O{level}', flush=True)

    variants = [
        ('launch-failure-skipped', '((Err e) (Some (PhaseFailure :part part :reason (PhaseLaunchFailure e))))',
         '((Err e) None)', ('phase-missing-first', 'split-missing-llc')),
        ('last-failure-wins', '(if (isNone first)\n          (set first failure))',
         '(if (isSome failure)\n          (set first failure))', ('phase-two-fail',)),
        ('signal-kind-erased', '(ToolSignalled (sysTermSignal st))',
         '(ToolExited (+ 128 (sysTermSignal st)))', ('phase-signal-first',)),
    ]
    for label, before, after, required in variants:
        altered = modules(label, replace_once(driver, before, after, label))
        binary = build(label, fixture, altered, 0)
        observed = exercise(label, binary, altered, work / 'child-O0', restricted=required)
        if any(observed[name]['passed'] for name in required):
            raise AssertionError((label, observed))
        report['controls'].append({'name': label, 'failed_cases': observed})
        print(f'ok   {label}: {", ".join(required)} fail', flush=True)

    label = 'later-waits-skipped'
    changed = replace_once(driver, '(for part in i..jobs\n      {', '(for part in i..jobs\n      (if (isNone first) {', label)
    changed = replace_once(changed, '(set first failure))\n      })\n    first', '(set first failure))\n      }))\n    first', label)
    altered = modules(label, changed)
    binary = build(label, fixture, altered, 0)
    required = ('phase-exit-first', 'split-llc-exit', 'pipeline-llc-exit')
    observed = exercise(label, binary, altered, work / 'child-O0', restricted=required)
    if any(observed[name]['passed'] for name in required):
        raise AssertionError((label, observed))
    report['controls'].append({'name': label, 'failed_cases': observed})
    print(f'ok   {label}: waits and cleanup ordering failures detected; harness drains children', flush=True)

    refusals = [
        ('erased-split', '(:: bad Int) (fn (bad) (buildSplit "" "in" "split" "out" 0 vecNew))', 'AX3004'),
        ('missing-fallback', '(:: bad Int) (fn (bad) (match (buildSplit "" "in" "split" "out" 0 vecNew) ((SplitFinished _) 0)))', 'AX3005'),
        ('missing-completion', '(:: bad Int) (fn (bad) (match (buildSplit "" "in" "split" "out" 0 vecNew) ((SplitUnavailable) 0)))', 'AX3005'),
    ]
    for label, declaration, code in refusals:
        source = work / (label + '.ax')
        annotated = declaration.replace(' (fn (bad)', '\n;@axiom:effect(io)\n;@axiom:effect(spawn)\n;@axiom:effect(block)\n(fn (bad)')
        source.write_text('(import core)\n(import driver)\n' + annotated + '\n(:: main Int) (fn (main) 0)\n')
        checked = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(source)],
                                 env=dict(local, AXIOM_PATH=str(root / 'core')), capture_output=True, text=True, timeout=90)
        (work / (label + '.stderr')).write_text(checked.stderr)
        errors = [line for line in checked.stderr.splitlines() if line.startswith('E ')]
        if checked.returncode != 1 or len(errors) != 1 or f'E {code} ' not in errors[0]:
            raise AssertionError((label, checked.returncode, checked.stdout, checked.stderr))
        report['refusals'].append({'name': label, 'diagnostic': code})
        print(f'ok   {label}: {code}', flush=True)
    report['interrupted_wait'] = interrupted_wait(compiler, root, work)
    for name, before in hashes.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != before:
            raise AssertionError('source changed during probe: ' + name)
    (work / 'report.json').write_text(json.dumps(report, indent=2) + '\n')



def interrupted_wait(compiler, root, work):
    driver = (root / 'core/driver.ax').read_text()
    bridge = (root / 'tests/tools/driver-process-bridge.axp').read_text()
    fixture = root / 'tests/tools/driver-process-results.axp'
    injected = replace_once(driver, '(match (sysWaitPid pid)', '(match (probeInterruptedWait pid)', 'inject-eintr')
    retry = '(if (== (errCode e) 4)\n        (waitTool pid)\n        (Err e))'
    variants = [('retry-O0', injected, 0, True), ('retry-O2', injected, 2, True),
                ('no-retry-control', replace_once(injected, retry, '(Err e)', 'remove-retry'), 0, False)]
    evidence = []
    for label, source, level, should_pass in variants:
        modules = work / (label + '-core')
        modules.mkdir()
        for path in (root / 'core').iterdir():
            target = modules / path.name
            if path.name == 'driver.ax':
                target.write_text('(import IO (println))\n' + source + '\n' + bridge)
            else:
                target.symlink_to(path, target_is_directory=path.is_dir())
        env = dict(os.environ, AXIOM_PATH=str(modules), AXIOM_STDLIB=str(root / 'stdlib'))
        binary = work / label
        built = subprocess.run([str(compiler), '--diagnostic-format=ai', 'build', str(fixture),
                                '--opt', str(level), '--alloc-poison', '-o', str(binary)],
                               env=env, capture_output=True, timeout=240)
        (work / (label + '.build.stderr')).write_bytes(built.stderr)
        if built.returncode or any(line.startswith((b'E ', b'W ')) for line in built.stderr.splitlines()):
            raise AssertionError((label, built.returncode, built.stderr.decode()))
        for mode, outcome in (('success', 'complete'), ('exit-first', 'failure 0 exit 7')):
            folder = work / (label + '-' + mode)
            folder.mkdir()
            shutil.copy2(work / ('child-O' + str(level)), folder / 'worker')
            ran = subprocess.run([str(binary), 'phase', mode, '--diagnostic-format=ai'],
                                 cwd=folder, env=dict(env, PATH=str(folder)), capture_output=True, timeout=30)
            (folder / 'observed.stdout').write_bytes(ran.stdout)
            (folder / 'observed.stderr').write_bytes(ran.stderr)
            expected = (outcome + '\nready true\ndrained 0\n').encode()
            passed = ran.returncode == 0 and ran.stdout == expected and not ran.stderr
            if passed != should_pass or (folder / 'interrupt.once').read_bytes() != b'interrupted\n':
                raise AssertionError((label, mode, ran.returncode, ran.stdout, expected, ran.stderr))
            evidence.append({'configuration': label, 'mode': mode, 'passed': passed,
                             'stdout_sha256': hashlib.sha256(ran.stdout).hexdigest()})
        print(f'ok   {label}: one-shot EINTR with actual child waits', flush=True)
    (work / 'interrupted-wait-report.json').write_text(json.dumps(evidence, indent=2) + '\n')
    return evidence


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
        with tempfile.TemporaryDirectory(prefix='axiom-driver-process-') as directory:
            check(args.axiom.resolve(), root, Path(directory))


if __name__ == '__main__':
    main()
