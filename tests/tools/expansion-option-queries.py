#!/usr/bin/env python3
"""Check typed optional query results, iteration and diagnostic compatibility."""

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
    expected = "".join(f"{index} true\n" for index in range(1, 55))
    golden = (root / "tests/tools/expansion-option-queries.golden").read_text()
    with tempfile.TemporaryDirectory(prefix="axiom-expansion-query-options-") as directory:
        work = Path(directory)
        for level in ("0", "2"):
            for fixture, output in (("expansion-option-queries", expected),
                                    ("expansion-option-query-diagnostics", golden)):
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
            "name-sentinel": '(:: erased (-> MEnv String)) (fn (erased env) (expResolveQueryName env "T" vecNew vecNew 0))',
            "query-erased-owner": '(:: erased (-> MEnv Ints)) (fn (erased env) (expFieldsOf env "S" 0))',
            "arity-sentinel": '(:: erased (-> MEnv Int)) (fn (erased env) (expCtorArityFor env "C" 0 "syntax/arity" "measure"))',
            "iterator-null": '(:: erased (-> MEnv ASTNode (Vec ExpIterBinding))) (fn (erased env source) (expIterPairs env source 0 "syntax/for"))',
            "node-sentinel": '(:: erased (-> MEnv ASTNode)) (fn (erased env) (expFindDeclNamed env "S" 1))',
            "sequence-null": '(:: erased (-> MEnv ASTNode (Vec ExpIterSequence))) (fn (erased env source) (expIterSeqsOf env source vecNew vecNew 0 0))',
        }
        for name, declaration in invalid.items():
            source = work / (name + ".ax")
            source.write_text("(import core)\n(import expand)\n" + declaration + "\n(:: main Int) (fn (main) 0)\n")
            checked = subprocess.run([compiler, "--diagnostic-format=ai", "check", str(source)],
                                     cwd=root, env=environment, capture_output=True,
                                     text=True, timeout=30)
            assert checked.returncode == 1 and "E AX3004 " in checked.stderr, (name, checked.stderr)
    print("ok   expansion query Options: 54 present/missing/empty/iteration cases at O0/O2; 15 diagnostic byte goldens; 6 erased results refused")


if __name__ == "__main__":
    main()
