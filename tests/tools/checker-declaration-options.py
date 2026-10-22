#!/usr/bin/env python3
"""Check optional function declarations and byte-identical diagnostics."""
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
    expected = "".join(f"{index} true\n" for index in range(1, 32))
    golden = (root / "tests/tools/checker-declaration-options.golden").read_text()
    with tempfile.TemporaryDirectory(prefix="axiom-declaration-options-") as directory:
        work = Path(directory)
        for level in ("0", "2"):
            for name, output in (("checker-declaration-options", expected),
                                 ("checker-declaration-diagnostics", golden)):
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
            "erased-declaration": ('(:: bad (-> Nodes ASTNode)) '
                                   '(fn (bad nodes) (declByName nodes "f" 0))', "AX3004"),
            "erased-hop": ('(:: bad (-> TC FnEnt ASTNode)) '
                           '(fn (bad tc entry) (restrictHopDecl tc entry))', "AX3004"),
            "erased-main": ('(:: bad (-> Nodes ASTNode)) '
                            '(fn (bad nodes) (entryMainDecl nodes 0))', "AX3004"),
            "missing-none": ('(:: bad (-> Nodes ASTNode)) '
                             '(fn (bad nodes) (match (entryMainDecl nodes 0) '
                             '((Some declaration) declaration)))', "AX3005"),
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
    print("ok   declaration Options: 31 first-match/main/builtin/extern/hop cases at O0/O2 poison; diagnostic byte goldens; 4 erased/missing-case refusals")


if __name__ == "__main__":
    main()
