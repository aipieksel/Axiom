#!/usr/bin/env python3
"""Check typed builder outcomes and explicit empty expansion."""

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
    expected = "".join(f"{index} true\n" for index in range(1, 35))
    golden = (root / "tests/tools/expansion-option-builders.golden").read_text()
    with tempfile.TemporaryDirectory(prefix="axiom-expansion-builder-options-") as directory:
        work = Path(directory)
        for level in ("0", "2"):
            for fixture, output in (("expansion-option-builders", expected),
                                    ("expansion-option-builder-diagnostics", golden)):
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
            "node-sentinel": '(:: erased (-> MEnv ASTNode ASTNode)) (fn (erased env source) (expBuildOneDecl source vecNew vecNew env 0))',
            "declarations-null": '(:: erased (-> MEnv Nodes)) (fn (erased env) (expBuildDecls vecNew vecNew vecNew env 0 vecNew 0))',
            "constructors-null": '(:: erased (-> MEnv Nodes)) (fn (erased env) (expBuildCtors vecNew vecNew vecNew env 0))',
            "fields-null": '(:: erased (-> MEnv Nodes)) (fn (erased env) (expBuildStructFields vecNew vecNew vecNew env 0))',
            "splice-null": '(:: erased (-> MEnv ASTNode Nodes)) (fn (erased env source) (expSpliceOneDecl source vecNew vecNew env 0))',
            "status-int": '(:: erased (-> MEnv Int)) (fn (erased env) (expBuildEffectOps vecNew vecNew vecNew vecNew env 0 vecNew vecNew 0))',
        }
        for name, declaration in invalid.items():
            source = work / (name + ".ax")
            source.write_text("(import core)\n(import expand)\n" + declaration + "\n(:: main Int) (fn (main) 0)\n")
            checked = subprocess.run([compiler, "--diagnostic-format=ai", "check", str(source)],
                                     cwd=root, env=environment, capture_output=True,
                                     text=True, timeout=30)
            assert checked.returncode == 1 and "E AX3004 " in checked.stderr, (name, checked.stderr)
    print("ok   expansion builder Options: 34 empty/success/refusal/restoration/defer cases at O0/O2; 5 diagnostic byte goldens; 6 erased results refused")


if __name__ == "__main__":
    main()
