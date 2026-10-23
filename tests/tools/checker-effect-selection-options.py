#!/usr/bin/env python3
"""Check optional restriction mappings and total field-effect suggestion choice."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def check(compiler, root, work):
    env = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"), AXIOM_PATH=str(root / "core"))
    expected = "".join(f"{index} true\n" for index in range(1, 41))
    golden = (root / "tests/tools/checker-effect-selection-options.golden").read_text()
    for level in ("0", "2"):
        for name, expected_output in (("checker-effect-selection-options", expected),
                                      ("checker-effect-selection-diagnostics", golden)):
            executable = work / (name + "-O" + level)
            built = subprocess.run(
                [compiler, "--diagnostic-format=ai", "build",
                 str(root / "tests/tools" / (name + ".axp")),
                 "--opt", level, "--alloc-poison", "-o", str(executable)],
                cwd=root, env=env, capture_output=True, text=True, timeout=180)
            (work / (name + "-O" + level + ".build.out")).write_text(built.stdout)
            (work / (name + "-O" + level + ".build.err")).write_text(built.stderr)
            assert built.returncode == 0, (name, level, built.returncode, built.stdout, built.stderr)
            ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
            (work / (name + "-O" + level + ".out")).write_text(ran.stdout)
            (work / (name + "-O" + level + ".err")).write_text(ran.stderr)
            assert ran.returncode == 0 and not ran.stderr and ran.stdout == expected_output, (
                name, level, ran.returncode, ran.stdout, ran.stderr)
    invalid = {
        "erased-mapping": ('(:: bad String) (fn (bad) (restrictionEffectName "no-io"))', "AX3004"),
        "missing-none": ('(:: bad String) (fn (bad) (match (restrictionEffectName "no-cast") '
                         '((Some effect) effect)))', "AX3005"),
    }
    for name, (declaration, code) in invalid.items():
        source = work / (name + ".ax")
        source.write_text("(import core)\n(import typecheck)\n" + declaration +
                          "\n(:: main Int) (fn (main) 0)\n")
        checked = subprocess.run([compiler, "--diagnostic-format=ai", "check", str(source)],
                                 cwd=root, env=env, capture_output=True, text=True, timeout=45)
        (work / (name + ".err")).write_text(checked.stderr)
        errors = [line for line in checked.stderr.splitlines() if line.startswith("E ")]
        assert checked.returncode == 1 and len(errors) == 1 and f"E {code} " in errors[0], (
            name, checked.returncode, checked.stdout, checked.stderr)
    print("ok   effect selection Options: 40 mapping/order/default/refusal cases at O0/O2 poison; "
          f"{len(golden.splitlines())} diagnostic byte goldens; erased mapping and missing None refused")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compiler")
    parser.add_argument("--work", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    compiler = str(Path(args.compiler).resolve())
    if args.work is not None:
        args.work.mkdir(parents=True, exist_ok=True)
        check(compiler, root, args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="axiom-effect-selection-options-") as directory:
            check(compiler, root, Path(directory))


if __name__ == "__main__":
    main()
