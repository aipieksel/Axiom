#!/usr/bin/env python3
"""Check typed registry lookup and refusal with the selected compiler."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def witness_negative_control(compiler, repo, env, work):
    """Prove that suppressing an invalid cast witness fails the runtime probe."""
    overlay = work / "negative-core"
    overlay.mkdir()
    for source in (repo / "core").iterdir():
        if source.is_file() and source.name != "typecheck.ax":
            (overlay / source.name).symlink_to(source)
    source = (repo / "core/typecheck.ax").read_text()
    old = """              (castTypeBadArgs (spineArgVec e vecNew) 0))
          }
          (Some e)))"""
    if source.count(old) != 1:
        raise SystemExit("cast witness control no longer matches its single implementation site")
    (overlay / "typecheck.ax").write_text(source.replace(old, old.replace("(Some e)", "None")))
    executable = work / "witness-negative"
    built = subprocess.run(
        [str(compiler), "--diagnostic-format=ai", "build",
         str(repo / "tests/selfhost/typecheck-witness-options.ax"),
         "--opt", "0", "--output", str(executable)],
        env=dict(env, AXIOM_PATH=str(overlay)), capture_output=True, text=True,
    )
    if built.returncode:
        raise SystemExit(built.stdout + built.stderr)
    ran = subprocess.run([str(executable)], capture_output=True, text=True)
    if ran.returncode != 3 or ran.stdout or ran.stderr:
        raise SystemExit(f"cast witness control: status={ran.returncode}, stdout={ran.stdout!r}, stderr={ran.stderr!r}")
    print("cast witness negative control rejected the missing-witness mutation (status 3)")


def field_negative_control(compiler, repo, env, work):
    """Prove that a found poison field cannot silently become a wildcard."""
    overlay = work / "negative-field-core"
    overlay.mkdir()
    for source in (repo / "core").iterdir():
        if source.is_file() and source.name != "typecheck.ax":
            (overlay / source.name).symlink_to(source)
    source = (repo / "core/typecheck.ax").read_text()
    old = """                (Some
                  (if (< j (vecLen tys))
                    (vecGet tys j)
                    mkTErr))"""
    if source.count(old) != 1:
        raise SystemExit("field poison control no longer matches its single implementation site")
    (overlay / "typecheck.ax").write_text(source.replace(old, old.replace("mkTErr", "mkSilentWild")))
    executable = work / "field-negative"
    built = subprocess.run(
        [str(compiler), "--diagnostic-format=ai", "build",
         str(repo / "tests/selfhost/typecheck-field-options.ax"),
         "--opt", "0", "--output", str(executable)],
        env=dict(env, AXIOM_PATH=str(overlay)), capture_output=True, text=True,
    )
    if built.returncode:
        raise SystemExit(built.stdout + built.stderr)
    ran = subprocess.run([str(executable)], capture_output=True, text=True)
    if ran.returncode != 2 or ran.stdout or ran.stderr:
        raise SystemExit(f"field poison control: status={ran.returncode}, stdout={ran.stdout!r}, stderr={ran.stderr!r}")
    print("field poison negative control rejected the wildcard mutation (status 2)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom", required=True, type=Path)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    compiler = args.axiom.resolve()
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ, AXIOM_PATH=str(repo / "core"), AXIOM_STDLIB=str(repo / "stdlib"))
    fixtures = [repo / "tests/selfhost" / name for name in [
        "typecheck-type-registry.ax", "typecheck-module-options.ax",
        "typecheck-function-registry.ax", "typecheck-private-state.ax",
        "typecheck-witness-options.ax", "typecheck-field-options.ax",
    ]]
    refusals = [
        ("typecheck-type-registry-payload.axp", "AX3004", ("DataEnt", "StructEnt")),
        ("typecheck-graph-options-exhaustive.axp", "AX3005", ("None",)),
        ("typecheck-context-refusal.axp", "AX3004", ("TC", "Int")),
        ("typecheck-private-context-refusal.axp", "AX3004", ("ContractContext", "Int")),
        ("typecheck-witness-exhaustive.axp", "AX3005", ("None",)),
        ("typecheck-field-exhaustive.axp", "AX3005", ("None",)),
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
        if args.negative_controls:
            witness_negative_control(compiler, repo, env, Path(work))
            field_negative_control(compiler, repo, env, Path(work))
    print(f"typed checker: {len(fixtures)} runtime fixtures returned 42; {len(refusals)} typed/exhaustiveness refusals passed")


if __name__ == "__main__":
    main()
