#!/usr/bin/env python3
"""Check typed registry lookup and refusal with the selected compiler."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom", required=True, type=Path)
    args = parser.parse_args()
    compiler = args.axiom.resolve()
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ, AXIOM_PATH=str(repo / "core"), AXIOM_STDLIB=str(repo / "stdlib"))
    fixtures = [repo / "tests/selfhost" / name for name in [
        "typecheck-type-registry.ax", "typecheck-module-options.ax",
        "typecheck-function-registry.ax", "typecheck-private-state.ax",
    ]]
    refusals = [
        ("typecheck-type-registry-payload.axp", "AX3004", ("DataEnt", "StructEnt")),
        ("typecheck-graph-options-exhaustive.axp", "AX3005", ("None",)),
        ("typecheck-context-refusal.axp", "AX3004", ("TC", "Int")),
        ("typecheck-private-context-refusal.axp", "AX3004", ("ContractContext", "Int")),
    ]
    with tempfile.TemporaryDirectory(prefix="axiom-type-registry-") as work:
        for fixture in fixtures:
            executable = Path(work) / "registry"
            built = subprocess.run(
                [str(compiler), "--diagnostic-format=ai", "build", str(fixture),
                 "--opt", "0", "--output", str(executable)],
                env=env, capture_output=True, text=True,
            )
            if built.returncode:
                raise SystemExit(built.stdout + built.stderr)
            ran = subprocess.run([str(executable)], capture_output=True, text=True)
            if ran.returncode != 42 or ran.stdout or ran.stderr:
                raise SystemExit(f"registry fixture: status={ran.returncode}, stdout={ran.stdout!r}, stderr={ran.stderr!r}")
        for name, code, expected_names in refusals:
            refusal = repo / "tests/selfhost" / name
            checked = subprocess.run(
                [str(compiler), "--diagnostic-format=ai", "check", str(refusal)],
                env=env, capture_output=True, text=True,
            )
            errors = [line for line in checked.stderr.splitlines() if line.startswith("E ")]
            if checked.returncode != 1 or len(errors) != 1 or f"E {code} " not in errors[0]:
                raise SystemExit(f"{name}: status={checked.returncode}\n{checked.stdout}{checked.stderr}")
            if any(expected not in errors[0] for expected in expected_names):
                raise SystemExit(f"{name}: unexpected refusal: {errors[0]}")
    print(f"typed checker: {len(fixtures)} runtime fixtures returned 42; {len(refusals)} typed/exhaustiveness refusals passed")


if __name__ == "__main__":
    main()
