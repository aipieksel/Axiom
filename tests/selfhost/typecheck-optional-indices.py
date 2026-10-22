#!/usr/bin/env python3
"""Check optional checker indices with focused native, refusal and mutation probes."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


CASES = [
    {
        "name": "first-definer",
        "function": "firstDefinerScan",
        "old": "(Some at)",
        "new": "None",
        "negative_status": 1,
    },
]


def run_case(compiler, repo, env, work, case, negative):
    name = case["name"]
    fixture = repo / "tests/selfhost" / f"typecheck-{name}-options.ax"
    executable = work / name
    built = subprocess.run(
        [str(compiler), "--diagnostic-format=ai", "build", str(fixture),
         "--opt", "0", "--output", str(executable)],
        env=env, capture_output=True, text=True,
    )
    if built.returncode:
        raise SystemExit(built.stdout + built.stderr)
    ran = subprocess.run([str(executable)], capture_output=True, text=True)
    if ran.returncode != 42 or ran.stdout or ran.stderr:
        raise SystemExit(f"{name}: status={ran.returncode}, stdout={ran.stdout!r}, stderr={ran.stderr!r}")
    refusal = repo / "tests/selfhost" / f"typecheck-{name}-refusal.axp"
    checked = subprocess.run(
        [str(compiler), "--diagnostic-format=ai", "check", str(refusal)],
        env=env, capture_output=True, text=True,
    )
    errors = [line for line in checked.stderr.splitlines() if line.startswith("E ")]
    if checked.returncode != 1 or len(errors) != 1 or "E AX3005 " not in errors[0] or "None" not in errors[0]:
        raise SystemExit(f"{name} refusal: status={checked.returncode}\n{checked.stdout}{checked.stderr}")
    if not negative:
        return
    overlay = work / f"negative-{name}-core"
    overlay.mkdir()
    for source in (repo / "core").iterdir():
        if source.is_file() and source.name != "typecheck.ax":
            (overlay / source.name).symlink_to(source)
    source = (repo / "core/typecheck.ax").read_text()
    start = source.index(f"(pub fn ({case['function']} ")
    end = source.index("\n\n", start)
    body = source[start:end]
    if body.count(case["old"]) != 1:
        raise SystemExit(f"{name} mutation no longer matches its unique target")
    mutated = source[:start] + body.replace(case["old"], case["new"]) + source[end:]
    (overlay / "typecheck.ax").write_text(mutated)
    built = subprocess.run(
        [str(compiler), "--diagnostic-format=ai", "build", str(fixture),
         "--opt", "0", "--output", str(executable)],
        env=dict(env, AXIOM_PATH=str(overlay)), capture_output=True, text=True,
    )
    if built.returncode:
        raise SystemExit(built.stdout + built.stderr)
    ran = subprocess.run([str(executable)], capture_output=True, text=True)
    if ran.returncode != case["negative_status"] or ran.stdout or ran.stderr:
        raise SystemExit(f"{name} mutation: status={ran.returncode}, stdout={ran.stdout!r}, stderr={ran.stderr!r}")
    print(f"{name}: effective mutation failed with status {ran.returncode}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom", required=True, type=Path)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ, AXIOM_PATH=str(repo / "core"), AXIOM_STDLIB=str(repo / "stdlib"))
    with tempfile.TemporaryDirectory(prefix="axiom-optional-indices-") as work:
        for case in CASES:
            run_case(args.axiom.resolve(), repo, env, Path(work), case, args.negative_controls)
    print(f"checker optional indices: {len(CASES)} native and exhaustive-refusal cases passed")


if __name__ == "__main__":
    main()
