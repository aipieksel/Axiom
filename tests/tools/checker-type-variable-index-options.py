#!/usr/bin/env python3
"""Check optional display-variable indexes, stable labels, and rejected erased results."""
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
    expected = "".join(f"{index} true\n" for index in range(1, 19))
    with tempfile.TemporaryDirectory(prefix="axiom-type-variable-index-options-") as directory:
        work = Path(directory)
        for level in ("0", "2"):
            executable = work / ("type-variable-index-O" + level)
            built = subprocess.run(
                [compiler, "--diagnostic-format=ai", "build",
                 str(root / "tests/tools/checker-type-variable-index-options.axp"),
                 "--opt", level, "--alloc-poison", "-o", str(executable)],
                cwd=root, env=env, capture_output=True, text=True, timeout=180)
            assert built.returncode == 0, (level, built.stdout, built.stderr)
            ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
            assert ran.returncode == 0 and not ran.stderr, (level, ran.returncode, ran.stderr)
            assert ran.stdout == expected, (level, ran.stdout)
        invalid = {
            "erased-index": ('(:: bad (-> Strings Int)) '
                             '(fn (bad names) (tyVarIndex names "_t0" 0))', "AX3004"),
            "missing-none": ('(:: bad (-> Strings Int)) '
                             '(fn (bad names) (match (tyVarIndex names "_t0" 0) '
                             '((Some index) index)))', "AX3005"),
        }
        for name, (declaration, code) in invalid.items():
            source = work / (name + ".ax")
            source.write_text("(import core)\n(import typecheck)\n" + declaration +
                              "\n(:: main Int) (fn (main) 0)\n")
            checked = subprocess.run([compiler, "--diagnostic-format=ai", "check", str(source)],
                                     cwd=root, env=env, capture_output=True, text=True, timeout=45)
            errors = [line for line in checked.stderr.splitlines() if line.startswith("E ")]
            assert checked.returncode == 1 and len(errors) == 1 and f"E {code} " in errors[0], (
                name, checked.returncode, checked.stdout, checked.stderr)
    print("ok   display-variable index Options: 18 lookup/label cases at O0/O2 poison; erased result and missing None refused")


if __name__ == "__main__":
    main()
