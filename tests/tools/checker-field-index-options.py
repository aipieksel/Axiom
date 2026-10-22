#!/usr/bin/env python3
"""Check optional field and lexical indexes and byte-identical diagnostics."""
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
    expected = "".join(f"{index} true\n" for index in range(1, 41))
    golden = (root / "tests/tools/checker-field-index-options.golden").read_text()
    with tempfile.TemporaryDirectory(prefix="axiom-field-index-options-") as directory:
        work = Path(directory)
        for level in ("0", "2"):
            for name, output in (("checker-field-index-options", expected),
                                 ("checker-field-index-diagnostics", golden)):
                executable = work / (name + "-O" + level)
                built = subprocess.run(
                    [compiler, "--diagnostic-format=ai", "build",
                     str(root / "tests/tools" / (name + ".axp")),
                     "--opt", level, "--alloc-poison", "-o", str(executable)],
                    cwd=root, env=env, capture_output=True, text=True, timeout=180)
                assert built.returncode == 0, (name, level, built.stdout, built.stderr)
                ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
                assert ran.returncode == 0 and not ran.stderr, (name, level, ran.returncode, ran.stderr)
                assert ran.stdout == output, (name, level, ran.stdout)
        invalid = {
            "erased-store": ('(:: bad (-> (Vec FieldStore) Int)) '
                             '(fn (bad stores) (fieldStoreFind stores "run" 0))', "AX3004"),
            "erased-local": ('(:: bad (-> FsStack Int)) '
                             '(fn (bad stack) (fsFind stack "x" 0))', "AX3004"),
            "erased-field": ('(:: bad (-> Names Int)) '
                             '(fn (bad names) (declaredFieldIndex names "x" 0))', "AX3004"),
            "erased-first-field": ('(:: bad (-> (Vec CtorEnt) Int)) '
                                   '(fn (bad ctors) (dataFirstDeclIdx ctors "x" 0))', "AX3004"),
            "missing-store-none": ('(:: bad (-> (Vec FieldStore) Int)) '
                                   '(fn (bad stores) (match (fieldStoreFind stores "run" 0) ((Some index) index)))', "AX3005"),
            "missing-local-none": ('(:: bad (-> FsStack Int)) '
                                   '(fn (bad stack) (match (fsFind stack "x" 0) ((Some index) index)))', "AX3005"),
            "missing-field-none": ('(:: bad (-> Names Int)) '
                                   '(fn (bad names) (match (declaredFieldIndex names "x" 0) ((Some index) index)))', "AX3005"),
            "missing-first-field-none": ('(:: bad (-> (Vec CtorEnt) Int)) '
                                         '(fn (bad ctors) (match (dataFirstDeclIdx ctors "x" 0) ((Some index) index)))', "AX3005"),
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
    print("ok   field index Options: 40 lookup/order/boundary cases at O0/O2 poison; 8 diagnostic byte goldens; 8 erased/missing-None controls")


if __name__ == "__main__":
    main()
