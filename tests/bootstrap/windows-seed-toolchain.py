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
shell_arch = os.environ.get('SEED_TEST_SHELL_ARCH', 'x86_64')
probe = any(pathlib.Path(x.removeprefix('-out:')).name.startswith('native-host.') for x in args)
if name == 'llvm-dlltool':
    probe = 'IsWow64Process2' in pathlib.Path(args[args.index('-d') + 1]).read_text()
if probe and name == os.environ.get('SEED_TEST_PROBE_FAILURE'):
    sys.exit('host probe tool failure: ' + name)
triple = 'x86_64-pc-windows-msvc' if arch == 'x86_64' else 'aarch64-pc-windows-msvc'
if name == 'uname':
    print('MINGW64_NT-10.0' if args == ['-s'] else shell_arch)
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
    if probe:
        result = os.environ.get('SEED_TEST_PROBE_STATUS', '10' if arch == 'x86_64' else '11')
        out.write_text('MZ(){ :; }\nexit ' + result + '\n')
    else:
        driver = os.environ['SEED_TEST_DRIVER']
        out.write_text('MZ(){ :; }\nexec python3 "' + driver + '" "$@"\n' + '# padding\n' * 11000)
    out.chmod(0o755)
elif name == 'llvm-readobj':
    mode = os.environ.get('SEED_TEST_FAILURE', '')
    if probe:
        mode = os.environ.get('SEED_TEST_PROBE_FAILURE', '')
    if args[0] == '--file-headers':
        machine = 'AMD64' if (shell_arch if probe else arch) == 'x86_64' else 'ARM64'
        if mode == 'machine': machine = 'I386'
        if mode == 'machine-suffix': machine = 'ARM64EC'
        print('  Machine: IMAGE_FILE_MACHINE_' + machine)
    else:
        dll = 'user32' if mode == 'dll' else 'kernel32'
        print('  Name: ' + dll + '.dll')
        symbols = ['GetCurrentProcess', 'IsWow64Process2', 'ExitProcess'] if probe else ['GetStdHandle', 'WriteFile', 'GetLastError', 'ExitProcess']
        for symbol in symbols:
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
    assert os.environ['AXIOM_PATH'] == 'core'
    assert os.environ['AXIOM_STDLIB'].startswith('C:/'), os.environ['AXIOM_STDLIB']
    assert args[-1].startswith('C:/'), args
    assert pathlib.Path('core/main.ax').is_file()
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
    (repo / "core").mkdir()
    (repo / "core/main.ax").write_text("; this is a mock compiler source\n")
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
    temporary_outputs = work / "temporary outputs"
    temporary_outputs.mkdir()
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               SEED_TEST_LOG=str(log), SEED_TEST_DRIVER=str(driver), TMPDIR=str(temporary_outputs))
    # macOS's Python launcher may cache xcrun_db in TMPDIR. Warm that
    # launcher before measuring the bootstrap's own temporary outputs.
    run(["python3", "-c", "pass"], env=env)
    temporary_baseline = set(temporary_outputs.iterdir())
    env.pop("PROCESSOR_ARCHITEW6432", None)
    for arch in TRIPLES:
        # Both variables lie: this reproduces the ARM64 CI shell and
        # also proves a bogus ARM64 environment cannot select an ARM seed.
        misleading = "ARM64" if arch == "x86_64" else "AMD64"
        env.update(SEED_TEST_ARCH=arch, PROCESSOR_ARCHITECTURE=misleading,
                   PROCESSOR_ARCHITEW6432=misleading)
        log.write_text("")
        install = work / f"installed {arch}"
        p = run(["bash", str(repo / "scripts/bootstrap-from-seed.sh"), "--install", str(install)], env=env)
        assert "stage2 and stage3 are byte-identical" in p.stdout
        assert set(temporary_outputs.iterdir()) == temporary_baseline, "successful bootstrap leaked temporary outputs"
        assert f"Windows native host: windows-{arch} (shell machine: x86_64)" in p.stderr
        assert (install / "axiom.exe").stat().st_size > 100000
        calls = [json.loads(x) for x in log.read_text().splitlines()]
        assert not any(name == "cc" for name, args in calls)
        probes = [args for name, args in calls if name == "lld-link" and any(x.endswith('/native-host.exe') for x in args)]
        assert len(probes) == 1 and '-machine:x64' in probes[0]
        links = [args for name, args in calls if name == "lld-link" and args not in probes]
        assert len(links) == 4
        assert all("-stack:8388608" in a and "-timestamp:0" in a and "-Brepro" in a for a in links)
        machine = "-machine:x64" if arch == "x86_64" else "-machine:arm64"
        assert all(machine in a for a in links)
        assert all("-subsystem:console" in a and "-entry:mainCRTStartup" in a for a in links)
        print(f"ok   mocked Windows {arch} seed ladder, fixpoint, program and .exe install (no native execution)")

    selector = '''set -euo pipefail
source "$1/scripts/lib/imports.sh"
source "$1/scripts/lib/seed-toolchain.sh"
seed_host_target "$2" "$3"
printf '%s\\n' "$seed_target"
'''
    for arch in TRIPLES:
        for shell_arch in TRIPLES:
            env.update(SEED_TEST_ARCH=arch, SEED_TEST_SHELL_ARCH=shell_arch)
            env.pop("PROCESSOR_ARCHITECTURE", None)
            env.pop("PROCESSOR_ARCHITEW6432", None)
            p = run(["bash", "-c", selector, "selector", str(repo), "MINGW64_NT-10.0", shell_arch], env=env)
            assert p.stdout.strip() == f"windows-{arch}", p.stdout
    print("ok   native API selects both Windows targets with x64/ARM64 shells and no processor variables")

    env.update(SEED_TEST_ARCH="aarch64", SEED_TEST_SHELL_ARCH="x86_64",
               PROCESSOR_ARCHITECTURE="AMD64", PROCESSOR_ARCHITEW6432="AMD64")
    for status, message in [("0", "probe failed"), ("12", "could not determine"),
                            ("13", "unsupported native"), ("127", "probe failed")]:
        env["SEED_TEST_PROBE_STATUS"] = status
        log.write_text("")
        p = run(["bash", str(repo / "scripts/bootstrap-from-seed.sh")], env=env, good=False)
        assert message in p.stderr, p.stderr
        assert set(temporary_outputs.iterdir()) == temporary_baseline, "failed host query leaked temporary outputs"
        calls = [json.loads(x) for x in log.read_text().splitlines()]
        assert not any(name == "lld-link" and not any(x.endswith('/native-host.exe') for x in args) for name, args in calls)
    env.pop("SEED_TEST_PROBE_STATUS")
    for failure in ["llc", "llvm-dlltool", "lld-link", "llvm-readobj", "machine", "machine-suffix", "import", "dll", "empty"]:
        env["SEED_TEST_PROBE_FAILURE"] = failure
        env["SEED_TEST_SHELL_ARCH"] = "aarch64" if failure == "machine-suffix" else "x86_64"
        log.write_text("")
        p = run(["bash", str(repo / "scripts/bootstrap-from-seed.sh")], env=env, good=False)
        assert "cannot select a native seed" in p.stderr, p.stderr
        assert set(temporary_outputs.iterdir()) == temporary_baseline, "failed host probe build leaked temporary outputs"
        calls = [json.loads(x) for x in log.read_text().splitlines()]
        assert not any(name == "lld-link" and not any(x.endswith('/native-host.exe') for x in args) for name, args in calls)
    env.pop("SEED_TEST_PROBE_FAILURE")
    for tool in ["llc", "llvm-dlltool", "lld-link", "llvm-readobj"]:
        missing = '''command() { if [[ "$*" == "-v $4" ]]; then return 1; fi; builtin command "$@"; }
'''.replace('$4', tool) + selector
        log.write_text("")
        p = run(["bash", "-c", missing, "selector", str(repo), "MINGW64_NT-10.0", "x86_64"], env=env, good=False)
        assert f"{tool} is not on PATH" in p.stderr, p.stderr
        assert not log.read_text(), "missing probe tools must be diagnosed before invoking any tool"
        assert set(temporary_outputs.iterdir()) == temporary_baseline, "missing probe tool leaked temporary outputs"
    print("ok   query errors, unsupported machines, execution, tools and malformed probe PE fail before seed linking")

    for system, target_os in [("Darwin", "darwin"), ("Linux", "linux"), ("FreeBSD", "freebsd")]:
        for machine, arch in [("arm64", "aarch64"), ("x86_64", "x86_64")]:
            log.write_text("")
            p = run(["bash", "-c", selector, "selector", str(repo), system, machine], env=env)
            assert p.stdout.strip() == f"{target_os}-{arch}" and not log.read_text()
    log.write_text("")
    p = run(["bash", "-c", selector, "selector", str(repo), "MINGW64_NT-10.0", "i686"], env=env, good=False)
    assert "unsupported Windows shell architecture" in p.stderr and not log.read_text()
    assert set(temporary_outputs.iterdir()) == temporary_baseline
    print("ok   all six POSIX targets still need no tools; unsupported Windows shell machines are refused")

    toolchain = repo / "scripts/lib/seed-toolchain.sh"
    original = toolchain.read_text()
    checked = 'machine="$(seed_windows_native_arch "$machine")" || return 1'
    assert original.count(checked) == 1
    toolchain.write_text(original.replace(checked, 'machine="${PROCESSOR_ARCHITEW6432:-${PROCESSOR_ARCHITECTURE:-$machine}}"'))
    p = run(["bash", "-c", selector, "selector", str(repo), "MINGW64_NT-10.0", "x86_64"], env=env)
    assert p.stdout.strip() != "windows-aarch64", "environment-only selection ablation survived"
    toolchain.write_text(original)
    print("ok   ablation: environment-only architecture selection disagrees on emulated ARM64")

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
        probe = '''set -euo pipefail
source "$1/scripts/lib/imports.sh"
source "$1/scripts/lib/seed-toolchain.sh"
seed_windows_build_host_probe "$2" "$3"
'''
        run(["bash", "-c", probe, "native-host", str(ROOT), arch, str(d / "native")])
        print(f"ok   real LLVM Windows {arch} PE linking, machine/import validation and reproducibility (not executed)")

    # Execute the production probe's IR on this host against WinAPI mocks.
    # This checks the BOOL/output ABI and native-vs-process selection,
    # separately from the shell mocks above and the unexecuted Windows PEs.
    ir = run(["bash", "-c", 'source "$1/scripts/lib/seed-toolchain.sh"; seed_windows_host_ir "$2"',
              "host-ir", str(ROOT), "x86_64-pc-windows-msvc"]).stdout
    native_ir = ir.replace('target triple = "x86_64-pc-windows-msvc"\n', "").replace("dllimport ", "")
    stub = work / "native-api.c"
    stub.write_text('''#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
void *GetCurrentProcess(void) { return (void *)(intptr_t)-1; }
int IsWow64Process2(void *handle, unsigned short *process, unsigned short *native) {
  assert(handle == GetCurrentProcess() && process != native);
  int ok = atoi(getenv("PROBE_OK"));
  if (ok) { *process = atoi(getenv("PROBE_PROCESS")); *native = atoi(getenv("PROBE_NATIVE")); }
  return ok;
}
_Noreturn void ExitProcess(unsigned code) { exit(code); }
void mainCRTStartup(void);
int main(void) { mainCRTStartup(); return 99; }
''')
    host_triple = run(["cc", "-dumpmachine"]).stdout.strip()
    cases = [(1, 0, 0x8664, 10), (1, 0x8664, 0xaa64, 11), (1, 0, 0xaa64, 11),
             (-1, 0xaa64, 0x8664, 10), (1, 0x8664, 0x14c, 13), (0, 0x8664, 0xaa64, 12)]
    def execute_probe(text, opt, label):
        path = work / f"query-{label}-{opt}"
        path.with_suffix(".ll").write_text(text)
        run(["llc", f"-O{opt}", f"-mtriple={host_triple}", "-filetype=obj", "-relocation-model=pic",
             str(path.with_suffix(".ll")), "-o", str(path.with_suffix(".o"))])
        run(["cc", str(stub), str(path.with_suffix(".o")), "-o", str(path)])
        results = []
        for ok, process, native, expected in cases:
            env = dict(os.environ, PROBE_OK=str(ok), PROBE_PROCESS=str(process), PROBE_NATIVE=str(native))
            p = subprocess.run([str(path)], env=env, capture_output=True, text=True, timeout=10)
            results.append(p.returncode == expected)
        return results
    for opt in [0, 2]:
        assert all(execute_probe(native_ir, opt, "production"))
    assert not all(execute_probe(native_ir.replace('load i16, ptr %native', 'load i16, ptr %process'), 2, "wrong-output"))
    print("ok   native-host query IR: 6 API cases at O0/O2; process-output ablation disagrees (host mocks, not Windows)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cross", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="axiom-windows-seed-") as tmp:
        work = Path(tmp)
        mock_cases(work)
        if args.cross:
            cross_cases(work)
