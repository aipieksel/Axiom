#!/usr/bin/env python3
"""Run compiler representation probes outside the numbered language corpus."""

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
    environment = dict(os.environ, AXIOM_PATH=str(root / "core"),
                       AXIOM_STDLIB=str(root / "stdlib"))
    cases = (
        "typecheck-suggest-options",
        "typecheck-scope-options",
        "typecheck-option-field-resolution",
        "typecheck-if-let-control-flow",
        "typecheck-function-registry",
        "render-typed-rows",
        "lsp-function-entry-options",
    )
    with tempfile.TemporaryDirectory(prefix="axiom-compiler-representation-") as directory:
        work = Path(directory)
        for case in cases:
            source = root / "tests/selfhost" / (case + ".ax")
            assert source.read_text().splitlines()[0] == "; expect 42", case
            for level in ("0", "2"):
                binary = work / (case + "-O" + level)
                build = subprocess.run(
                    [compiler, "--diagnostic-format=ai", "build", str(source),
                     "--opt", level, "--alloc-poison", "-o", str(binary)],
                    cwd=root, env=environment, capture_output=True, text=True, timeout=180)
                assert build.returncode == 0, (case, level, build.stdout, build.stderr)
                result = subprocess.run([str(binary)], cwd=root, env=environment,
                                        capture_output=True, text=True, timeout=30)
                assert result.returncode == 42, (case, level, result.returncode,
                                                  result.stdout, result.stderr)
                assert result.stdout == "" and result.stderr == "", (case, level, result)
            print("ok   " + case + ": O0/O2 with allocation poisoning", flush=True)


if __name__ == "__main__":
    main()
