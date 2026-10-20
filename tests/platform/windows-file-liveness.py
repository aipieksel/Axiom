#!/usr/bin/env python3
"""Exercise actual Windows File markers and table pollers on the host."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    axiom = str(Path(args.axiom).resolve())
    source = (root / "stdlib/IO.ax").read_text()
    overlay = """
(:: usesSyscallAbi Int)
(fn (usesSyscallAbi) 0)
(:: pollUsesKqueue Int)
(fn (pollUsesKqueue) 0)
(:: pollEventSize Int)
(fn (pollEventSize) 24)
(:: pollEventFdOffset Int)
(fn (pollEventFdOffset) 8)
(:: pollReadFilter Int)
(fn (pollReadFilter) 1)
(:: pollAddOp Int)
(fn (pollAddOp) 1)
(:: pollDelOp Int)
(fn (pollDelOp) 2)
(:: ttyUsesTermios Int)
(fn (ttyUsesTermios) 0)
(:: platformConsoleSetMode (-> Int Int Int))
;@axiom:effect(io)
;@axiom:effect(unsafe)
(fn (platformConsoleSetMode fd mode) (setModeRaw fd mode))
(extern "windows_liveness_test"
  (platformOpen :: (-> Int Int Int Int) (symbol "livenessOpen"))
  (platformClose :: (-> Int Int) (symbol "livenessClose"))
  (platformConsoleMode :: (-> Int Int) (symbol "livenessMode"))
  (setModeRaw :: (-> Int Int Int) (symbol "livenessSetMode"))
  (platformPoll :: (-> Int Int Int Int) (symbol "livenessPoll")))
"""
    source = source.replace("(import Fmt)", "(import Fmt)\n" + overlay, 1)
    bridge = """(import Internal.WindowsLiveness)
(pub :: markerDup (-> Int Int))
;@axiom:effect(io)
(pub fn (markerDup fd) (windowsConsoleDup fd))
"""
    with tempfile.TemporaryDirectory(prefix="axiom-windows-file-liveness-") as tmp:
        work = Path(tmp)
        lib = work / "stdlib"
        lib.mkdir()
        for child in (root / "stdlib").iterdir():
            if child.name == "Internal":
                internal = lib / "Internal"
                internal.mkdir()
                for helper in child.iterdir():
                    if helper.name != "WindowsLiveness.ax":
                        (internal / helper.name).symlink_to(helper, target_is_directory=helper.is_dir())
                # Keep the actual Internal import boundary while
                # selecting its Windows sibling on this POSIX host.
                (internal / "WindowsLiveness.ax").write_text(
                    (child / "WindowsLiveness.windows.ax").read_text())
            else:
                (lib / child.name).symlink_to(child, target_is_directory=child.is_dir())
        module = lib / "WindowsIO.ax"
        module.write_text(source)
        # Only the direct duplicate ABI assertion uses this temporary
        # public bridge; production applications still cannot import
        # Internal.WindowsLiveness.
        (lib / "WindowsMarker.ax").write_text(bridge)
        env = dict(os.environ, AXIOM_STDLIB=str(lib))
        run("cc", "-c", str(root / "tests/platform/windows-file-liveness.c"), "-o", str(work / "mock.o"))
        probe = root / "tests/platform/windows-file-liveness.ax"

        def check(level, expected=()):
            ir = work / "probe.ll"
            run(axiom, "--diagnostic-format=ai", "emit-llvm", str(probe),
                "-o", str(ir), env=env, stdout=subprocess.DEVNULL)
            if level:
                optimised = work / "probe.opt.ll"
                run("opt", f"-O{level}", str(ir), "-S", "-o", str(optimised))
                ir = optimised
            obj, binary = work / "probe.o", work / "probe"
            run("llc", f"-O{level}", "-filetype=obj", "-relocation-model=pic", str(ir), "-o", str(obj))
            entry = ["-e", "_main"] if os.uname().sysname == "Darwin" else []
            run("cc", *entry, str(obj), str(work / "mock.o"), "-o", str(binary))
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            lines = result.stdout.splitlines()
            failures = {i+1 for i, line in enumerate(lines) if line.startswith("FAIL ")}
            if (len(lines) != 37 or failures != set(expected) or result.returncode != len(failures)
                    or any(not line.startswith(("ok   ", "FAIL ")) for line in lines)):
                raise SystemExit(f"FAIL Windows File liveness -O{level}: exit {result.returncode}\n"
                                 f"{result.stdout}{result.stderr}")

        for level in (0, 2):
            check(level)
            print(f"ok   Windows File and terminal lifetime mocks: 37 cases at -O{level} on the host")
        # Target the liveness call after the wait. Its argument can be
        # a marker directly or a typed slot's marker; the same property
        # must hold for either counted representation.
        anchor = "; Recheck after the native wait:"
        assert source.count(anchor) == 1, "post-wait liveness ablation lost its anchor"
        begin = source.index("(fileMarkerAlive ", source.index(anchor))
        depth = 0
        for end in range(begin, len(source)):
            depth += (source[end] == "(") - (source[end] == ")")
            if depth == 0:
                break
        else:
            raise AssertionError("post-wait liveness call is unbalanced")
        module.write_text(source[:begin] + "true" + source[end+1:])
        check(2, (14,))
        print("ok   negative probe: omitting the post-wait marker check publishes a retired token")
        original = "(if (|| (&& (>= fd 0) (<= fd 2)) (fileMarkerAlive marker))"
        assert source.count(original) == 1, "terminal restore ablation lost its target"
        module.write_text(source.replace(original, "(if true"))
        check(2, (27,))
        print("ok   negative probe: restoring through a retired handle fails the reuse case")


if __name__ == "__main__":
    main()
