#!/usr/bin/env python3
"""Check typed expansion-frame module joins, limits and missing identities."""

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
    expected = "".join(f"{index} true\n" for index in range(1, 24))
    with tempfile.TemporaryDirectory(prefix="axiom-expansion-frames-") as directory:
        work = Path(directory)
        for level in ("0", "2"):
            executable = work / ("expansion-frames-O" + level)
            build = subprocess.run(
                [compiler, "--diagnostic-format=ai", "build",
                 str(root / "tests/tools/expansion-option-frames.axp"),
                 "--opt", level, "--alloc-poison", "-o", str(executable)],
                cwd=root, env=environment, capture_output=True, text=True, timeout=180)
            assert build.returncode == 0, (level, build.stdout, build.stderr)
            result = subprocess.run([str(executable)], cwd=root, capture_output=True,
                                    text=True, timeout=30)
            assert result.returncode == 0, (level, result.returncode, result.stderr)
            assert result.stdout == expected, (level, result.stdout, result.stderr)
        for name, declaration in {
            "raw-site": '(ExpFrame :site 0 :span 0 :name "x" :defSpan 0 :defMod None)',
            "raw-definition": '(ExpFrame :site None :span 0 :name "x" :defSpan 0 :defMod 0)',
        }.items():
            source = work / (name + ".ax")
            source.write_text("(import core)\n(:: main Int)\n(fn (main) { " + declaration + " 0 })\n")
            result = subprocess.run([compiler, "--diagnostic-format=ai", "check", str(source)],
                                    cwd=root, env=environment, capture_output=True,
                                    text=True, timeout=30)
            assert result.returncode == 1 and "E AX3004 " in result.stderr, (name, result.stderr)
    print("ok   expansion frames: 23 module, span, fix and cap cases at O0/O2; 2 raw modules refused")


if __name__ == "__main__":
    main()
