#!/usr/bin/env python3
"""Exercise typed module rows and owning namespace indexes."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compiler")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    compiler = str(Path(arguments.compiler).resolve())
    environment = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"),
                       AXIOM_PATH=str(root / "core"))
    expected = "".join(f"{index} true\n" for index in range(1, 30))
    with tempfile.TemporaryDirectory(prefix="axiom-namespace-options-") as directory:
        work = Path(directory)
        for level in ("0", "2"):
            executable = work / ("namespace-options-O" + level)
            build = subprocess.run(
                [compiler, "--diagnostic-format=ai", "build",
                 str(root / "tests/tools/namespace-option-imports.axp"),
                 "--opt", level, "--alloc-poison", "-o", str(executable)],
                cwd=root, env=environment, capture_output=True, text=True, timeout=180)
            assert build.returncode == 0, (level, build.stdout, build.stderr)
            result = subprocess.run([str(executable)], cwd=root, capture_output=True,
                                    text=True, timeout=30)
            assert result.returncode == 0, (level, result.returncode, result.stderr)
            assert result.stdout == expected, (level, result.stdout, result.stderr)
    print("ok   namespace Options: 29 filter, visibility and index cases at O0/O2")


if __name__ == "__main__":
    main()
