#!/usr/bin/env python3
"""Check typed hygienic binding stacks, precedence and missing lookups."""

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
    expected = "".join(f"{index} true\n" for index in range(1, 33))
    with tempfile.TemporaryDirectory(prefix="axiom-expansion-bindings-") as directory:
        work = Path(directory)
        for level in ("0", "2"):
            executable = work / ("expansion-bindings-O" + level)
            build = subprocess.run(
                [compiler, "--diagnostic-format=ai", "build",
                 str(root / "tests/tools/expansion-option-bindings.axp"),
                 "--opt", level, "--alloc-poison", "-o", str(executable)],
                cwd=root, env=environment, capture_output=True, text=True, timeout=180)
            assert build.returncode == 0, (level, build.stdout, build.stderr)
            result = subprocess.run([str(executable)], cwd=root, capture_output=True,
                                    text=True, timeout=30)
            assert result.returncode == 0, (level, result.returncode, result.stderr)
            assert result.stdout == expected, (level, result.stdout, result.stderr)
        for function in ("expForLookup", "expForBindOf", "expScResolveTpl"):
            source = work / (function + ".ax")
            source.write_text(
                '(import expand)\n(:: erased (-> MEnv Int))\n'
                f'(fn (erased env) ({function} env "x"))\n'
                '(:: main Int)\n(fn (main) 0)\n')
            result = subprocess.run([compiler, "--diagnostic-format=ai", "check", str(source)],
                                    cwd=root, env=environment, capture_output=True,
                                    text=True, timeout=30)
            assert result.returncode == 1 and "E AX3004 " in result.stderr, (function, result.stderr)
    print("ok   expansion bindings: 32 scope/lookup/capture cases at O0/O2; 3 erased results refused")


if __name__ == "__main__":
    main()
