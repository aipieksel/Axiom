#!/usr/bin/env python3
"""Check optional declaration-name ownership and refusal propagation."""

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
    expected = "".join(f"{index} true\n" for index in range(1, 27))
    golden = (root / "tests/tools/expansion-option-names.golden").read_text()
    with tempfile.TemporaryDirectory(prefix="axiom-expansion-name-options-") as directory:
        work = Path(directory)
        for level in ("0", "2"):
            for fixture, output in (("expansion-option-names", expected),
                                    ("expansion-option-name-diagnostics", golden)):
                executable = work / (fixture + "-O" + level)
                build = subprocess.run(
                    [compiler, "--diagnostic-format=ai", "build",
                     str(root / "tests/tools" / (fixture + ".axp")),
                     "--opt", level, "--alloc-poison", "-o", str(executable)],
                    cwd=root, env=environment, capture_output=True, text=True, timeout=180)
                assert build.returncode == 0, (level, fixture, build.stdout, build.stderr)
                result = subprocess.run([str(executable)], cwd=root, capture_output=True,
                                        text=True, timeout=30)
                assert result.returncode == 0, (level, fixture, result.returncode, result.stderr)
                assert not result.stderr, (level, fixture, result.stderr)
                assert result.stdout == output, (level, fixture, result.stdout)
        invalid = {
            "name-sentinel": '(:: erased (-> MEnv String)) (fn (erased env) (expSubstName "T" vecNew vecNew env 0))',
            "names-null": '(:: erased (-> MEnv Names)) (fn (erased env) (expSubstNames vecNew vecNew vecNew env 0))',
            "type-name-sentinel": '(:: erased (-> MEnv ASTNode String)) (fn (erased env source) (expSubstTyName source vecNew vecNew env 0))',
        }
        for name, declaration in invalid.items():
            source = work / (name + ".ax")
            source.write_text("(import core)\n(import expand)\n" + declaration + "\n(:: main Int) (fn (main) 0)\n")
            checked = subprocess.run([compiler, "--diagnostic-format=ai", "check", str(source)],
                                     cwd=root, env=environment, capture_output=True,
                                     text=True, timeout=30)
            assert checked.returncode == 1 and "E AX3004 " in checked.stderr, (name, checked.stderr)
    print("ok   expansion name Options: 26 ownership/empty/refusal cases at O0/O2; 5 diagnostic byte goldens; 3 erased results refused")


if __name__ == "__main__":
    main()
