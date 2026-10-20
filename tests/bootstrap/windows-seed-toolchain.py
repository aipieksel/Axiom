#!/usr/bin/env python3
"""Exercise the Windows bootstrap without generating a compiler seed.

The default uses mocked native tools for the complete shell ladder.
--cross also assembles and links small, independent PE fixtures with
real LLVM tools. Neither mode executes a Windows binary on the host.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
TRIPLES = {"x86_64": "x86_64-pc-windows-msvc", "aarch64": "aarch64-pc-windows-msvc"}


def run(argv, *, env=None, cwd=None, good=True):
    p = subprocess.run(argv, env=env, cwd=cwd, text=True, capture_output=True, timeout=60)
    if (p.returncode == 0) != good:
        raise AssertionError(f"{argv}: exit {p.returncode}\n{p.stdout}{p.stderr}")
    return p


def write_exe(path, text):
    path.write_text(text)
    path.chmod(0o755)


MOCK = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['SEED_TEST_LOG'], 'a') as log:
    log.write(json.dumps([name, args]) + '\n')
arch = os.environ['SEED_TEST_ARCH']
triple = 'x86_64-pc-windows-msvc' if arch == 'x86_64' else 'aarch64-pc-windows-msvc'
if name == 'uname':
    print('MINGW64_NT-10.0' if args == ['-s'] else 'x86_64')
elif name == 'cygpath':
    assert args[0] == '-m', args
    print('C:' + args[-1])
elif name == 'opt':
    shutil = __import__('shutil')
    shutil.copyfile(args[1], args[args.index('-o') + 1])
elif name == 'llc':
    pathlib.Path(args[args.index('-o') + 1]).write_bytes(b'COFF object, stable basename\n')
elif name == 'llvm-dlltool':
    pathlib.Path(args[args.index('-l') + 1]).write_bytes(b'import archive\n')
elif name == 'lld-link':
    out = pathlib.Path(next(x[5:] for x in args if x.startswith('-out:')))
    # MZ is both the PE-reader magic and a valid shell function name.
    # Bash runs this host mock after ENOEXEC; no PE is being executed.
    driver = os.environ['SEED_TEST_DRIVER']
    out.write_text('MZ(){ :; }\nexec python3 "' + driver + '" "$@"\n' + '# padding\n' * 11000)
    out.chmod(0o755)
elif name == 'llvm-readobj':
    mode = os.environ.get('SEED_TEST_FAILURE', '')
    if args[0] == '--file-headers':
        machine = 'AMD64' if arch == 'x86_64' else 'ARM64'
        if mode == 'machine': machine = 'I386'
        print('  Machine: IMAGE_FILE_MACHINE_' + machine)
    else:
        dll = 'user32' if mode == 'dll' else 'kernel32'
        print('  Name: ' + dll + '.dll')
        for symbol in ['GetStdHandle', 'WriteFile', 'GetLastError', 'ExitProcess']:
            print('  Symbol: ' + symbol + ' (0)')
        if mode == 'import': print('  Symbol: MessageBoxA (0)')
        if mode == 'empty': sys.exit(1)
elif name == 'cc':
    sys.exit('Windows bootstrap invoked cc')
else:
    sys.exit('unexpected mock tool ' + name)
'''

DRIVER = r'''import os, pathlib, sys
args = sys.argv[1:]
assert '--diagnostic-format=ai' in args, args
if 'emit-llvm' in args:
    assert os.environ['AXIOM_PATH'] == 'self_host'
    assert os.environ['AXIOM_STDLIB'].startswith('C:/'), os.environ['AXIOM_STDLIB']
    assert args[-1].startswith('C:/'), args
    assert pathlib.Path('self_host/main.ax').is_file()
    triple = 'x86_64-pc-windows-msvc' if os.environ['SEED_TEST_ARCH'] == 'x86_64' else 'aarch64-pc-windows-msvc'
    print('target triple = "' + triple + '"')
    print('; compiler mock line\n' * 10001, end='')
elif 'build' in args:
    p = pathlib.Path(args[args.index('--output') + 1])
    assert p.name == 'hello.exe', args
    p.write_text('MZ(){ :; }\nprintf "bootstrapped\\n"\nexit 42\n')
    p.chmod(0o755)
else:
    raise AssertionError(args)
'''


def mock_cases(work):
    repo, bin_dir = work / "checkout with spaces", work / "bin"
    bin_dir.mkdir()
    for name in ["bootstrap-from-seed.sh", "lib/seed-sums.sh", "lib/imports.sh", "lib/seed-toolchain.sh"]:
        dest = repo / "scripts" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "scripts" / name, dest)
    shutil.copyfile(ROOT / "scripts/platform-allow.windows.txt", repo / "scripts/platform-allow.windows.txt")
    (repo / "stdlib").mkdir()
    (repo / "self_host").mkdir()
    (repo / "self_host/main.ax").write_text("; this is a mock compiler source\n")
    seeds = repo / "bootstrap"
    seeds.mkdir()
    for arch, triple in TRIPLES.items():
        (seeds / f"axiom-windows-{arch}.ll").write_text(f'target triple = "{triple}"\n')

    def sums():
        (seeds / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in sorted(seeds.glob("*.ll"))))

    sums()
    driver = work / "driver.py"
    driver.write_text(DRIVER)
    for name in ["uname", "cygpath", "opt", "llc", "llvm-dlltool", "lld-link", "llvm-readobj", "cc"]:
        write_exe(bin_dir / name, MOCK)
    log = work / "calls.jsonl"
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               SEED_TEST_LOG=str(log), SEED_TEST_DRIVER=str(driver))
    env.pop("PROCESSOR_ARCHITEW6432", None)
    for arch in TRIPLES:
        env.update(SEED_TEST_ARCH=arch, PROCESSOR_ARCHITECTURE="AMD64")
        if arch == "aarch64":
            env["PROCESSOR_ARCHITEW6432"] = "ARM64"
        else:
            env.pop("PROCESSOR_ARCHITEW6432", None)
        log.write_text("")
        install = work / f"installed {arch}"
        p = run(["bash", str(repo / "scripts/bootstrap-from-seed.sh"), "--install", str(install)], env=env)
        assert "stage2 and stage3 are byte-identical" in p.stdout
        assert (install / "axiom.exe").stat().st_size > 100000
        calls = [json.loads(x) for x in log.read_text().splitlines()]
        assert not any(name == "cc" for name, args in calls)
        links = [args for name, args in calls if name == "lld-link"]
        assert len(links) == 4
        assert all("-stack:8388608" in a and "-timestamp:0" in a and "-Brepro" in a for a in links)
        machine = "-machine:x64" if arch == "x86_64" else "-machine:arm64"
        assert all(machine in a for a in links)
        assert all("-subsystem:console" in a and "-entry:mainCRTStartup" in a for a in links)
        print(f"ok   mocked Windows {arch} seed ladder, fixpoint, program and .exe install (no native execution)")

    for failure, message in [("machine", "not a IMAGE_FILE_MACHINE"), ("import", "unpermitted names"),
                             ("dll", "unpermitted DLL"), ("empty", "could not read the PE imports")]:
        env["SEED_TEST_FAILURE"] = failure
        p = run(["bash", str(repo / "scripts/bootstrap-from-seed.sh")], env=env, good=False)
        assert message in p.stderr, p.stderr
        print(f"ok   mocked bootstrap rejects {failure}")
    env.pop("SEED_TEST_FAILURE")
    seed = seeds / "axiom-windows-aarch64.ll"
    saved = seed.read_text()
    seed.unlink()
    p = run(["bash", str(repo / "scripts/bootstrap-from-seed.sh")], env=env, good=False)
    assert "no committed seed for windows-aarch64" in p.stderr
    seed.write_text(saved + "; changed\n")
    p = run(["bash", str(repo / "scripts/bootstrap-from-seed.sh")], env=env, good=False)
    assert "does not match its recorded hash" in p.stderr
    seed.write_text(saved.replace(TRIPLES["aarch64"], TRIPLES["x86_64"]))
    sums()
    p = run(["bash", str(repo / "scripts/bootstrap-from-seed.sh")], env=env, good=False)
    assert "does not name the native Windows triple" in p.stderr
    print("ok   missing, corrupt and wrong-target Windows seed inputs are refused")


def cross_cases(work):
    for tool in ["llc", "llvm-dlltool", "lld-link", "llvm-readobj"]:
        if not shutil.which(tool):
            raise SystemExit(f"--cross requires {tool} on PATH")
    for arch, triple in TRIPLES.items():
        d = work / arch
        d.mkdir()
        ir = d / "probe.ll"
        ir.write_text(f'''target triple = "{triple}"
@msg = private constant [1 x i8] c"x"
declare dllimport i64 @GetStdHandle(i64)
declare dllimport i64 @WriteFile(i64, ptr, i64, ptr, i64)
declare dllimport i64 @GetLastError()
declare dllimport void @ExitProcess(i64)
define void @mainCRTStartup() {{
  %count = alloca i32
  %h = call i64 @GetStdHandle(i64 -11)
  %e = call i64 @GetLastError()
  %w = call i64 @WriteFile(i64 %h, ptr @msg, i64 1, ptr %count, i64 0)
  call void @ExitProcess(i64 0)
  unreachable
}}
''')
        run(["llc", "-filetype=obj", "-relocation-model=pic", str(ir), "-o", str(d / "probe.obj")])
        script = '''set -euo pipefail
source "$1/scripts/lib/imports.sh"
source "$1/scripts/lib/seed-toolchain.sh"
seed_prepare_link "$2" "$3/imports" "$1/scripts/platform-allow.windows.txt"
seed_link "$2" "$3/probe.obj" "$3/first.exe"
seed_link "$2" "$3/probe.obj" "$3/second.exe"
cmp "$3/first.exe" "$3/second.exe"
'''
        run(["bash", "-c", script, "probe", str(ROOT), f"windows-{arch}", str(d)])
        print(f"ok   real LLVM Windows {arch} PE linking, machine/import validation and reproducibility (not executed)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cross", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="axiom-windows-seed-") as tmp:
        work = Path(tmp)
        mock_cases(work)
        if args.cross:
            cross_cases(work)
