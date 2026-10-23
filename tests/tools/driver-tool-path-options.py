#!/usr/bin/env python3
"""Exercise optional native-tool paths without changing process status policy."""
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
check_output = SUPPORT['check_output']

BRIDGE = '''
(pub :: probeResolve (-> String (Option String)))
;@axiom:effect(io)
(pub fn (probeResolve name) (resolveTool name))
(pub :: probeResolveIn (-> String String Int (Option String)))
;@axiom:effect(io)
(pub fn (probeResolveIn name path from) (resolveToolIn name path from))
(pub :: probeSpawnWait (-> String Strings Int))
;@axiom:effect(io)
;@axiom:effect(spawn)
;@axiom:effect(block)
(pub fn (probeSpawnWait tool args)
  (match (spawnTool tool args)
    ((Err _) -1)
    ((Ok pid)
      (match (waitTool pid)
        ((Err _) -1)
        ((Ok result)
          (match result
            ((ToolExited code) code)
            ((ToolSignalled signal) (+ 128 signal))))))))
'''


def check(compiler, root, work, baseline):
    host = platform.system()
    if host not in ('Darwin', 'Linux', 'FreeBSD'):
        raise AssertionError('native process fixture needs a reviewed spawn policy for ' + host)
    # posix_spawn rejects these in the parent; fork/exec reports child exit 127.
    failed_exec = -1 if host == 'Darwin' else 127
    driver = (baseline or (root / 'core/driver.ax')).read_text()
    bridge = BRIDGE
    if baseline:
        bridge = bridge.replace('''(pub fn (probeSpawnWait tool args)
  (match (spawnTool tool args)
    ((Err _) -1)
    ((Ok pid)
      (match (waitTool pid)
        ((Err _) -1)
        ((Ok result)
          (match result
            ((ToolExited code) code)
            ((ToolSignalled signal) (+ 128 signal))))))))''', '''(pub fn (probeSpawnWait tool args)
  { (let pid (spawnTool tool args)) (if (< pid 0) pid (waitTool pid)) })''')
        bridge = bridge.replace('(resolveTool name))',
                                '{ (let path (resolveTool name)) (if (== (strLen path) 0) None (Some path)) })')
        bridge = bridge.replace('(resolveToolIn name path from))',
                                '{ (let found (resolveToolIn name path from)) (if (== (strLen found) 0) None (Some found)) })')
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for folder in ('core', 'stdlib') for path in sorted((root / folder).rglob('*.ax'))}
    env = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    fixture = root / 'tests/tools/driver-tool-path-options.axp'
    child = root / 'tests/tools/driver-tool-argv-child.axp'
    expected = [f'{index} true' for index in range(1, 25)]
    data = work / 'data'
    data.mkdir()
    for name in ('first', 'second'):
        (data / name).mkdir()
    (data / 'first/nonexec').write_text('not executable\n')
    (data / 'first/nonexec').chmod(0o644)
    report = {'builder': str(compiler),
              'builder_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
              'driver_sha256': hashlib.sha256(driver.encode()).hexdigest(),
              'baseline': bool(baseline), 'host': host, 'source_sha256': hashes,
              'configurations': [], 'controls': [], 'refusals': [],
              'limits': [
                  'Native host O0/O2 with allocation poisoning; no cross-target execution claim.',
                  'Direct paths are returned without an existence check; PATH checks existence, not executability.',
                  'Empty/interior PATH components retain current-directory lookup; empty PATH and trailing components are skipped.',
                  'The bridge renders typed launch/wait outcomes as legacy status for comparison; phase behavior is tested separately.',
                  'Child argv and error status are checked without shell invocation.',
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

    def run(label, binary, local, args=(), path='first:second', failures=(), output=None):
        native_env = dict(local, PATH=path, AXIOM_TEST_TOOL=str(data / 'tool'))
        ran = subprocess.run([str(binary), *args], env=native_env, cwd=data,
                             capture_output=True, timeout=30)
        out = work / (label + '.stdout')
        out.write_bytes(ran.stdout)
        (work / (label + '.stderr')).write_bytes(ran.stderr)
        if ran.returncode or ran.stderr:
            raise AssertionError((label, ran.returncode, ran.stderr.decode()))
        if output is None:
            return check_output(out, expected, required_failures=failures)
        if ran.stdout != output:
            raise AssertionError((label, ran.stdout, output))
        return hashlib.sha256(ran.stdout).hexdigest()

    local = modules('positive')
    spawned = [
        ('direct-relative', './tool', 'first:second', ('plain',), './tool', 0),
        ('direct-absolute', str(data / 'tool'), 'first:second', ('plain',), str(data / 'tool'), 0),
        ('path-first', 'tool', 'first:second', ('plain',), 'first/tool', 0),
        ('path-second', 'tool', 'missing:second', ('plain',), 'second/tool', 0),
        ('path-empty-component', 'tool', ':second', ('plain',), 'tool', 0),
        ('literal-argv', './tool', 'first:second', ('a b', '', '$(touch forbidden)', ';|&*', 'quote"back\\slash'), './tool', 0),
        ('child-failed', './tool', 'first:second', ('exit7',), './tool', 7),
        ('missing-direct', './missing', 'first:second', (), None, -1),
        ('missing-path', 'absent', 'first:second', (), None, -1),
        ('empty-path', 'tool', '', (), None, -1),
        ('trailing-empty', 'tool', 'missing:', (), None, -1),
        ('non-executable', 'nonexec', 'first', (), None, failed_exec),
        ('directory', './first', 'first:second', (), None, failed_exec),
    ]
    for level in (0, 2):
        child_binary = build('child-O' + str(level), child, local, level)
        for destination in ('tool', 'first/tool', 'second/tool'):
            shutil.copy2(child_binary, data / destination)
        binary = build('queries-O' + str(level), fixture, local, level)
        run('queries-O' + str(level), binary, local)
        outputs = {}
        for label, tool, path, args, argv0, status in spawned:
            lines = [] if argv0 is None else [f'arg {index} {arg}\n' for index, arg in enumerate((argv0, *args))]
            output = (''.join(lines) + f'status {status}\n').encode()
            digest = run(f'{label}-O{level}', binary, local, ('spawn', tool, *args), path=path, output=output)
            outputs[label] = digest
        if (data / 'forbidden').exists():
            raise AssertionError('literal argv was interpreted as shell input')
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True,
                                         'query_cases': 24, 'spawn_cases': len(spawned),
                                         'exact_output_sha256': outputs})
        print(f'ok   tool paths: 24 queries and {len(spawned)} exact spawn outputs O{level}', flush=True)

    if not baseline:
        variants = [
            ('present-path-lost', '(if (fileExists cand)\n          (Some cand)',
             '(if (fileExists cand)\n          None', (7, 8, 9)),
            ('direct-path-lost', '(if (isSome (strFind name "/" 0))\n    (Some name)',
             '(if (isSome (strFind name "/" 0))\n    None', (1, 2, 3, 4, 21, 23)),
            ('empty-component-lost', '(if (== (strLen dir) 0)\n            name',
             '(if (== (strLen dir) 0)\n            (concat "missing/" name)', (10, 11, 13)),
        ]
        for label, before, after, failures in variants:
            altered = modules(label, replace_once(driver, before, after, label))
            binary = build(label, fixture, altered, 0)
            observed = run(label, binary, altered, failures=failures)
            report['controls'].append({'name': label, 'failed_cases': sorted(observed)})
            print(f'ok   {label}: cases {sorted(observed)} fail', flush=True)
        refusals = [
            ('erased-resolve', '(:: bad String) (fn (bad) (probeResolve "tool"))', 'AX3004'),
            ('erased-path-query', '(:: bad String) (fn (bad) (probeResolveIn "tool" "" 0))', 'AX3004'),
            ('missing-resolve-none', '(:: bad String) (fn (bad) (match (probeResolve "tool") ((Some path) path)))', 'AX3005'),
            ('missing-path-none', '(:: bad String) (fn (bad) (match (probeResolveIn "tool" "" 0) ((Some path) path)))', 'AX3005'),
        ]
        for label, declaration, code in refusals:
            source = work / (label + '.ax')
            source.write_text('(import driver)\n' + declaration.replace(' (fn (bad)', '\n;@axiom:effect(io)\n(fn (bad)') + '\n(:: main Int) (fn (main) 0)\n')
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
        with tempfile.TemporaryDirectory(prefix='axiom-driver-tool-path-') as directory:
            check(args.axiom.resolve(), root, Path(directory), args.baseline_driver)


if __name__ == '__main__':
    main()
