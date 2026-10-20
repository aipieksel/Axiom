#!/usr/bin/env python3
"""Execute table-poller owner checks on the host, without Windows kernel calls."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    axiom = str(Path(args.axiom).resolve())
    source = (root / "stdlib/IO.ax").read_text()
    source = source.replace("(import Fmt)", "(import Fmt)\n"
                            "(:: usesSyscallAbi Int)\n(fn (usesSyscallAbi) 0)")
    fixture = (root / "tests/stdlib/981-poller-closed.ax").read_text()
    fixture = fixture.replace("(import IO)", "(import IO)\n(import WindowsIO)")
    for name in ("pollerNew", "pollerClose", "pollerWait"):
        fixture = fixture.replace(f"({name}", f"(WindowsIO::{name}")
    fixture = fixture.replace("(println", "(IO::println")
    stdout = (root / "tests/stdlib/981-poller-closed.out").read_bytes()
    stderr = (root / "tests/stdlib/981-poller-closed.err").read_bytes()
    with tempfile.TemporaryDirectory(prefix="axiom-windows-pollers-") as tmp:
        work = Path(tmp)
        lib = work / "stdlib"
        lib.mkdir()
        # Internal.Raw is restricted to modules inside the standard
        # library. The private copy keeps that boundary while selecting
        # the table-poller branch of the actual IO source on this host.
        for child in (root / "stdlib").iterdir():
            (lib / child.name).symlink_to(child, target_is_directory=child.is_dir())
        module = lib / "WindowsIO.ax"
        module.write_text(source)
        env = dict(os.environ, AXIOM_STDLIB=str(lib))

        def build(text, level, name="probe"):
            probe = work / "probe.ax"
            probe.write_text(text)
            binary = work / name
            subprocess.run([axiom, "--diagnostic-format=ai", "build", str(probe),
                            "--opt", str(level), "-o", str(binary)],
                           check=True, env=env, stdout=subprocess.DEVNULL)
            return subprocess.run([str(binary)], capture_output=True, timeout=10)

        original = "(WindowsIO::pollerWait p 0)"
        calls = {
            "wait": original,
            "token": "(WindowsIO::pollerToken p 0)",
            "signal": "(WindowsIO::pollerSignal p 0)",
            "signals": "(WindowsIO::pollerAddSignals p 0 1)",
            "add": "(WindowsIO::pollerAdd p WindowsIO::stdin 1)",
        }
        for level in (0, 2):
            for label, call in calls.items():
                result = build(fixture.replace(original, call), level)
                if (result.returncode != 85 or result.stdout != stdout
                        or not result.stderr.startswith(stderr)):
                    raise SystemExit(f"FAIL table poller {label} -O{level}: exit {result.returncode}\n"
                                     f"{result.stdout!r}\n{result.stderr!r}")
            print(f"ok   table poller owner checks: 5 operations at -O{level} on the host")

        # Recreate the Windows wait's missing owner read. The same
        # fixture must now reach its explicitly unreachable line.
        start = source.index("(pub fn (pollerWait p timeoutMs)")
        anchor = "(let pfd (pollerFd p))"
        position = source.index(anchor, start)
        module.write_text(source[:position] + source[position:].replace(anchor, "(let pfd 0)", 1))
        result = build(fixture, 2, "ablated")
        if result.returncode != 1 or result.stdout != stdout + b"UNREACHABLE\n":
            raise SystemExit("FAIL table poller negative probe did not expose the missing owner check")
        print("ok   ablating the table poller's owner check reaches the refused wait")


if __name__ == "__main__":
    main()
