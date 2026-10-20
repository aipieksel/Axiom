#!/usr/bin/env python3
"""Run the Windows time implementation on the host with WinAPI mocks.

This verifies arithmetic and API arguments. Native Windows execution and
its calling convention remain the Windows CI jobs' responsibility.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def check(root, axiom, work, env, levels, expected_failures=()):
    run(axiom, "--diagnostic-format=ai", "emit-llvm", str(work / "probe.ax"),
        "-o", str(work / "probe.ll"), env=env, stdout=subprocess.DEVNULL)
    for level in levels:
        ir = work / "probe.ll"
        if level:
            ir = work / "probe.opt.ll"
            run("opt", f"-O{level}", str(work / "probe.ll"), "-S", "-o", str(ir))
        obj = work / f"probe-O{level}.o"
        binary = work / f"probe-O{level}"
        run("llc", f"-O{level}", "-filetype=obj", "-relocation-model=pic", str(ir), "-o", str(obj))
        entry = ["-e", "_main"] if os.uname().sysname == "Darwin" else []
        run("cc", *entry, str(obj), str(work / "mock.o"), "-o", str(binary))
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
        lines = result.stdout.splitlines()
        failures = {i + 1 for i, line in enumerate(lines) if line.startswith("FAIL ")}
        if (len(lines) != 21 or failures != set(expected_failures)
                or result.returncode != len(expected_failures)
                or any(not line.startswith(("ok   ", "FAIL ")) for line in lines)):
            raise SystemExit(f"FAIL Windows time mocks -O{level}: exit {result.returncode}\n"
                             f"{result.stdout}{result.stderr}")
        if not expected_failures:
            print(f"ok   Windows time API mocks: 21 cases at -O{level} on the host")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    axiom = str(Path(args.axiom).resolve())
    env = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"))
    with tempfile.TemporaryDirectory(prefix="axiom-windows-time-") as tmp:
        work = Path(tmp)
        # A distinct module name selects the Windows implementation even
        # when this test is compiled for a POSIX host.
        platform = (root / "stdlib/Sys/Platform.windows.ax").read_text()
        (work / "WindowsPlatform.ax").write_text(platform)
        shutil.copyfile(root / "tests/platform/windows-time.ax", work / "probe.ax")
        env["AXIOM_PATH"] = str(work)
        run("cc", "-c", str(root / "tests/platform/windows-time.c"), "-o", str(work / "mock.o"))
        check(root, axiom, work, env, (0, 2))

        # Recreate each old implementation in the private module. Both
        # must build and reach the expected failures, so a compiler or
        # link failure cannot pass the negative half of this check.
        old_sleep = """(winSleepMillis
        (+ (/ nanos 1000000)
          (if (== (% nanos 1000000) 0)
            0
            1)))"""
        assert platform.count(old_sleep) == 1, "sleep ablation no longer names one call"
        ablated = platform.replace(old_sleep, "(winSleep (/ (+ nanos 999999) 1000000))")
        ablated = ablated.replace(
            ";@axiom:effect(io)\n(pub fn (platformSleepNanos nanos)",
            ";@axiom:effect(io)\n;@axiom:effect(unsafe)\n(pub fn (platformSleepNanos nanos)")
        (work / "WindowsPlatform.ax").write_text(ablated)
        check(root, axiom, work, env, (2,), (8, 9, 10, 11))
        print("ok   negative probe: unchecked Sleep arguments fail four duration cases")

        old_clock = """(withContext
        (andThen (subChecked (__load64 ft 0) 116444736000000000)
          (lambda (ticks) (mulChecked ticks 100)))
        "platformRealtimeNanos")"""
        assert platform.count(old_clock) == 1, "clock ablation no longer names one conversion"
        ablated = platform.replace(old_clock, "(Ok (* (- (__load64 ft 0) 116444736000000000) 100))")
        (work / "WindowsPlatform.ax").write_text(ablated)
        check(root, axiom, work, env, (2,), (17, 18, 19, 20, 21))
        print("ok   negative probe: unchecked FILETIME arithmetic fails five overflow cases")


if __name__ == "__main__":
    main()
