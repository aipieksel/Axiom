#!/usr/bin/env python3
"""Pin target-selected ownership probes with host execution and optional PE links.

The Windows helper is executed with WinAPI mocks at O0/O2. The real six
fixtures keep their checked-in goldens on this POSIX host; --cross also
links both Windows targets and checks their imports, without executing PE.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


CASES = ("698-file-lifetime", "699-net-lifetime", "708-mut-slot-reassign",
         "770-vec-files", "773-map-files", "786-poller-owned")
PROBE = '''(import IO)
(import Sys)
(import Str)
(import ResourceCount)
(extern "resource_count_mock"
  (mockMode :: (-> Int Int) (symbol "resourceCountMode"))
  (mockErrors :: Int (symbol "resourceCountErrors")))
(:: main ())
;@axiom:effect(io)
;@axiom:effect(unsafe)
(fn (main) {
  (mockMode (- (strByte (sysArg 1) 0) 48))
  (println openCount)
  (println mockErrors)
})
'''


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom", required=True)
    parser.add_argument("--cross", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    axiom = str(Path(args.axiom).resolve())
    helper = (root / "tests/stdlib/ResourceCount.windows.ax").read_text()
    env = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"))
    # An ambient search path must not shadow the counter under test.
    env.pop("AXIOM_PATH", None)
    with tempfile.TemporaryDirectory(prefix="axiom-resource-count-") as tmp:
        work = Path(tmp)
        module = work / "ResourceCount.ax"
        module.write_text(helper)
        probe = work / "probe.ax"
        probe.write_text(PROBE)
        run("cc", "-c", str(root / "tests/platform/resource-count.c"), "-o", str(work / "mock.o"))

        def mock_build(level):
            ir, obj, binary = work / "probe.ll", work / "probe.o", work / "probe"
            run(axiom, "--diagnostic-format=ai", "emit-llvm", str(probe),
                "-o", str(ir), env=env, stdout=subprocess.DEVNULL)
            if level:
                optimised = work / "probe.opt.ll"
                run("opt", f"-O{level}", str(ir), "-S", "-o", str(optimised))
                ir = optimised
            run("llc", f"-O{level}", "-filetype=obj", "-relocation-model=pic", str(ir), "-o", str(obj))
            entry = ["-e", "_main"] if os.uname().sysname == "Darwin" else []
            run("cc", *entry, str(obj), str(work / "mock.o"), "-o", str(binary))
            return binary

        def verdict(binary, mode):
            result = subprocess.run([str(binary), str(mode)], capture_output=True, timeout=10)
            if mode < 5:
                count = (43, 0, 4294967295, 7, 17)[mode]
                return (result.returncode == 0 and result.stdout == f"{count}\n0\n".encode()
                        and result.stderr == b"")
            code = 5 if mode == 5 else 6
            return (result.returncode == 1 and result.stdout == b""
                    and result.stderr == f"GetProcessHandleCount failed: {code}\n".encode())

        for level in (0, 2):
            binary = mock_build(level)
            if not all(verdict(binary, mode) for mode in range(7)):
                raise SystemExit(f"FAIL Windows resource counter API mocks -O{level}")
            print(f"ok   Windows resource counter: 7 API success/width/error cases at -O{level}")

        count_anchor = "(& (__load64 count 0) 4294967295)"
        assert helper.count(count_anchor) == 1
        module.write_text(helper.replace(count_anchor, "(& (__load64 count 0) 0)"))
        binary = mock_build(2)
        failed = {mode for mode in range(7) if not verdict(binary, mode)}
        if failed != {0, 2, 3, 4}:
            raise SystemExit(f"FAIL zero-count ablation: failed cases {failed}")
        print("ok   negative probe: a fabricated zero fails all four nonzero-count controls")

        failure_anchor = '(die (concat "GetProcessHandleCount failed: " (fmtInt (& lastError 4294967295))) 1)'
        assert helper.count(failure_anchor) == 1
        module.write_text(helper.replace(failure_anchor, "0"))
        binary = mock_build(2)
        failed = {mode for mode in range(7) if not verdict(binary, mode)}
        if failed != {5, 6}:
            raise SystemExit(f"FAIL swallowed-error ablation: failed cases {failed}")
        print("ok   negative probe: swallowing probe errors fails both error controls")

        # The POSIX probe must also stop on failure instead of answering 0.
        posix = (root / "tests/stdlib/ResourceCount.ax").read_text()
        missing = str(work / "absent-fd-directory")
        module.write_text(posix.replace('"/dev/fd"', f'"{missing}"'))
        probe.write_text("(import IO)\n(import ResourceCount)\n(:: main ())\n"
                         ";@axiom:effect(io)\n(fn (main) (println openCount))\n")
        binary = work / "missing"
        run(axiom, "--diagnostic-format=ai", "build", str(probe), "-o", str(binary),
            env=env, stdout=subprocess.DEVNULL)
        result = subprocess.run([str(binary)], capture_output=True, timeout=10)
        if result.returncode != 1 or result.stdout or not result.stderr:
            raise SystemExit("FAIL POSIX resource counter did not refuse an unreadable directory")
        print("ok   POSIX resource counter reports a failed directory probe")

        # Compile the originals: sibling resolution is the same path used
        # by both standard-library batteries and check-handles.sh.
        for name in CASES:
            source = root / "tests/stdlib" / f"{name}.ax"
            expected = (root / "tests/stdlib" / f"{name}.out").read_bytes()
            exit_file = root / "tests/stdlib" / f"{name}.exit"
            status = int(exit_file.read_text()) if exit_file.exists() else 0
            for level in (0, 2):
                binary = work / f"{name}-O{level}"
                run(axiom, "--diagnostic-format=ai", "build", str(source), "--opt", str(level),
                    "-o", str(binary), env=env, stdout=subprocess.DEVNULL)
                with open(os.devnull, "rb") as stdin:
                    result = subprocess.run([str(binary)], cwd=work, stdin=stdin,
                                            capture_output=True, timeout=30)
                if result.returncode != status or result.stdout != expected:
                    raise SystemExit(f"FAIL {name} -O{level}: exit {result.returncode}\n{result.stdout!r}\n{result.stderr!r}")
            print(f"ok   {name}: unchanged ownership golden at -O0/-O2 on the host")

        if args.cross:
            cross(root, axiom, work, env)


def cross(root, axiom, work, env):
    # Driver-generated import libraries use only actual declarations.
    # The explicit fixture allowance keeps these two query APIs out of
    # the production runtime/compiler allowlist.
    allow = work / "counter-allow.txt"
    allow.write_text((root / "scripts/platform-allow.windows.txt").read_text()
                     + (root / "tests/stdlib/resource-count-imports.windows.txt").read_text())
    imports_script = str(root / "scripts/lib/imports.sh")
    for target, machine in (("windows-x86_64", "IMAGE_FILE_MACHINE_AMD64"),
                            ("windows-aarch64", "IMAGE_FILE_MACHINE_ARM64")):
        for name in CASES:
            binary = work / f"{name}-{target}.exe"
            run(axiom, "--diagnostic-format=ai", f"--target={target}", "build",
                str(root / "tests/stdlib" / f"{name}.ax"), "-o", str(binary),
                env=env, stdout=subprocess.DEVNULL)
            headers = subprocess.check_output(["llvm-readobj", "--file-headers", str(binary)], text=True)
            if machine not in headers:
                raise SystemExit(f"FAIL resource fixture {name}: wrong PE machine for {target}")
            # Positional arguments keep every path out of shell source.
            check = subprocess.run(["bash", "-c", '''source "$1"
imports_of "$2" > "$4"
test -z "$(unpermitted_imports "$4" "$3")" && test -z "$(misplaced_imports "$2" "$3")"
''', "resource-counter", imports_script, str(binary), str(allow), str(work / "imports.txt")],
                capture_output=True, text=True)
            names = (work / "imports.txt").read_text().splitlines()
            if check.returncode or not {"GetProcessHandleCount", "GetCurrentProcess"} <= set(names):
                raise SystemExit(f"FAIL {name} [{target}]: counter API/import validation\n{check.stdout}{check.stderr}")
        print(f"ok   {target}: six ownership fixtures linked with target-selected counter; PE machines/imports checked, not executed")


if __name__ == "__main__":
    main()
