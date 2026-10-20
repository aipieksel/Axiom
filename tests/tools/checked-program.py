#!/usr/bin/env python3
"""Exercise the final-check handoff through the CLI's production path."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile


def main():
    compiler = str(Path(sys.argv[1]).resolve())
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    cases = {
        'parse': ('(:: main Int) (fn (main)', 'AX2001'),
        'binding': ('(:: main Int) (fn (main) missing)', 'AX3001'),
        'type': ('(:: main Int) (fn (main) true)', 'AX3004'),
        'effect': (
            '(import IO) (:: main ()) ;@axiom:effect(pure)\n'
            '(fn (main) (println "unsafe purity claim"))', 'AX3010'),
        'region': (
            '(import Str) (:: escaped (-> Int String)) '
            '(fn (escaped x) (region r (strAlloc x))) '
            '(:: main Int) (fn (main) 0)', 'AX3059'),
    }
    checks = 0
    with tempfile.TemporaryDirectory(prefix='axiom-checked-') as work:
        work = Path(work)
        for name, (source, code) in cases.items():
            path = work / (name + '.ax')
            path.write_text(source)
            for command in ('emit-llvm', 'build'):
                output = work / (name + '-' + command)
                sentinel = b'previous output must survive a refused check\n'
                output.write_bytes(sentinel)
                run = subprocess.run(
                    [compiler, '--diagnostic-format=ai', command, str(path),
                     '-o', str(output)], cwd=work, env=env,
                    capture_output=True, text=True, timeout=60)
                assert run.returncode == 1, (name, command, run.returncode, run.stderr)
                assert 'E ' + code + ' ' in run.stderr, (name, command, run.stderr)
                assert output.read_bytes() == sentinel, (name, command, 'output replaced')
                assert 'target triple' not in run.stdout, (name, command, 'IR escaped')
                checks += 1

        (work / 'Support.ax').write_text(
            '(pub :: identity (-> a a)) (pub fn (identity x) x)\n')
        valid = work / 'valid.ax'
        valid.write_text(
            '(import Support) (import Vec) (import Str)\n'
            '(:: main Int)\n;@axiom:restrict(no-unsafe)\n'
            '(fn (main) { (let values (vecPush vecNew "typed")) '
            '(let nested (vecPush vecNew values)) '
            '(if (strEq (vecGet (vecGet nested 0) 0) "typed") '
            '(identity 42) 1) })\n')
        executable = work / 'valid'
        built = subprocess.run(
            [compiler, '--diagnostic-format=ai', 'build', str(valid),
             '-o', str(executable)], cwd=work, env=env,
            capture_output=True, text=True, timeout=60)
        assert built.returncode == 0, built.stderr
        result = subprocess.run([str(executable)], cwd=work, timeout=10)
        assert result.returncode == 42, result.returncode
        checks += 1

        # Binding generation edits the checked tree. The final handoff must
        # follow the second check, including generated nominal accessors.
        archive = work / 'nominal.a'
        binding = work / 'nominal.rs'
        built = subprocess.run(
            [compiler, '--diagnostic-format=ai', 'build',
             str(root / 'tests/ffi/host/nominal.ax'), '--emit-staticlib',
             '--emit-rust-binding', str(binding), '-o', str(archive)],
            cwd=work, env=env, capture_output=True, text=True, timeout=60)
        assert built.returncode == 0, built.stderr
        assert archive.read_bytes().startswith(b'!<arch>\n'), 'missing native archive'
        assert 'makeThing' in binding.read_text(), 'missing generated binding'
        checks += 1

        archive_before, binding_before = archive.read_bytes(), binding.read_bytes()
        refused = subprocess.run(
            [compiler, '--diagnostic-format=ai', 'build',
             str(root / 'tests/ffi/host/mangle-collision.ax'), '--emit-staticlib',
             '--emit-rust-binding', str(binding), '-o', str(archive)],
            cwd=work, env=env, capture_output=True, text=True, timeout=60)
        assert refused.returncode == 1, refused.stderr
        assert 'E AX3006 ' in refused.stderr, refused.stderr
        assert archive.read_bytes() == archive_before, 'refused archive replaced output'
        assert binding.read_bytes() == binding_before, 'refused binding replaced output'
        checks += 1
    print(f'ok   checked compilation: {checks} refusal/output-preservation and native checks')


if __name__ == '__main__':
    main()
