#!/usr/bin/env python3
"""Check specialised loop help, its absent fallbacks and exact diagnostic bytes."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compiler")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    compiler = str(Path(args.compiler).resolve())
    env = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"), AXIOM_PATH=str(root / "core"))
    expected = (root / "tests/tools/checker-for-help-options.golden").read_text()
    assert expected.startswith("".join(f"{index} true\n" for index in range(1, 14)))
    with tempfile.TemporaryDirectory(prefix="axiom-for-help-options-") as directory:
        for level in ("0", "2"):
            executable = Path(directory) / ("for-help-O" + level)
            built = subprocess.run(
                [compiler, "--diagnostic-format=ai", "build",
                 str(root / "tests/tools/checker-for-help-options.axp"),
                 "--opt", level, "--alloc-poison", "-o", str(executable)],
                cwd=root, env=env, capture_output=True, text=True, timeout=180)
            assert built.returncode == 0, (level, built.stdout, built.stderr)
            ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
            assert ran.returncode == 0 and not ran.stderr, (level, ran.returncode, ran.stderr)
            assert ran.stdout == expected, (level, ran.stdout)
    print("ok   for-help Options: 13 specialised/fallback cases at O0/O2 poison; 4 diagnostic byte goldens")


if __name__ == "__main__":
    main()
