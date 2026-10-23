#!/usr/bin/env python3
"""Distinguish present empty expansion values from refusal and absence."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compiler", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    compiler = str(args.compiler.resolve())
    env = dict(os.environ, AXIOM_PATH=str(root / "core"), AXIOM_STDLIB=str(root / "stdlib"))
    original = (root / "core/expand.ax").read_text()
    controls = (
        ("empty-fields-lost", "expFieldsOf", "queries", 54, 13,
         '(Some (ExpQuerySequence :key (concat "fields " s) :values names))',
         '(if (== (vecLen names) 0) None (Some (ExpQuerySequence :key (concat "fields " s) :values names)))'),
        ("empty-name-lost", "expSubstName", "names", 26, 2,
         "(Some s)", '(if (strEq s "") None (Some s))'),
        ("empty-build-lost", "expBuildDecls", "builders", 34, 1,
         "(Some acc)", '(if (== (vecLen acc) 0) None (Some acc))'),
    )

    def run(command, local=env):
        return subprocess.run(command, cwd=root, env=local, capture_output=True, text=True, timeout=240)

    with tempfile.TemporaryDirectory(prefix="axiom-expansion-option-controls-") as directory:
        work = Path(directory)
        for name, function, fixture, count, witness, old, new in controls:
            start = original.index("(pub fn (" + function + " ")
            end = original.index("\n\n", start)
            body = original[start:end]
            assert body.count(old) == 1, (name, body.count(old))
            overlay = work / (name + "-core")
            overlay.mkdir()
            for path in (root / "core").iterdir():
                if path.name == "expand.ax":
                    (overlay / path.name).write_text(original[:start] + body.replace(old, new) + original[end:])
                else:
                    (overlay / path.name).symlink_to(path, target_is_directory=path.is_dir())
            binary = work / name
            result = run([compiler, "--diagnostic-format=ai", "build",
                          str(root / "tests/tools" / ("expansion-option-" + fixture + ".axp")),
                          "--opt", "0", "--alloc-poison", "-o", str(binary)], dict(env, AXIOM_PATH=str(overlay)))
            assert result.returncode == 0, (name, result.returncode, result.stderr)
            result = run([str(binary)])
            assert result.returncode == 0 and not result.stderr, (name, result.returncode, result.stderr)
            rows = result.stdout.splitlines()
            assert len(rows) == count, (name, result.stdout)
            failed = [index for index, row in enumerate(rows, 1) if row != f"{index} true"]
            assert witness in failed, (name, failed)
            print(f"ok   {name}: cases {failed} detect the mutation", flush=True)
        for name, signature, expression in (
            ("query", "(-> MEnv Int)", '(match (expFieldsOf env "Empty" 0) ((Some value) (vecLen value.values)))'),
            ("name", "(-> MEnv String)", '(match (expSubstName "" vecNew vecNew env 0) ((Some value) value))'),
            ("builder", "(-> MEnv Int)", '(match (expBuildDecls vecNew vecNew vecNew env 0 vecNew 0) ((Some value) (vecLen value)))'),
        ):
            source = work / (name + "-nonexhaustive.ax")
            source.write_text('(import core)\n(import expand)\n(import Vec)\n' +
                              f'(:: bad {signature})\n(fn (bad env) {expression})\n(:: main Int) (fn (main) 0)\n')
            result = run([compiler, "--diagnostic-format=ai", "check", str(source)])
            errors = [line for line in result.stderr.splitlines() if line.startswith("E ")]
            assert result.returncode == 1 and len(errors) == 1 and "E AX3005 " in errors[0], (name, result.stderr)
            print(f"ok   {name}: omitted None arm refused", flush=True)


if __name__ == "__main__":
    main()
