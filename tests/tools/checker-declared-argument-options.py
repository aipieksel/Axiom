#!/usr/bin/env python3
"""Check optional declarations/scopes, grounding, diagnostic bytes and refusals."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def check(compiler, root, work):
    env = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"), AXIOM_PATH=str(root / "core"))
    expected = "".join(f"{index} true\n" for index in range(1, 47))
    golden = (root / "tests/tools/checker-declared-argument-options.golden").read_text()
    report = {"cases_per_configuration": 46, "configurations": [], "counterexamples": [], "refusals": []}

    def invoke(arguments, label, local=env, timeout=240):
        result = subprocess.run(arguments, cwd=root, env=local, capture_output=True,
                                text=True, timeout=timeout)
        (work / (label + ".out")).write_text(result.stdout)
        (work / (label + ".err")).write_text(result.stderr)
        return result

    for level in ("0", "2"):
        for name, output in (("checker-declared-argument-options", expected),
                             ("checker-declared-argument-diagnostics", golden)):
            executable = work / (name + "-O" + level)
            built = invoke([compiler, "--diagnostic-format=ai", "build",
                            str(root / "tests/tools" / (name + ".axp")),
                            "--opt", level, "--alloc-poison", "-o", str(executable)], name + "-build-O" + level)
            assert built.returncode == 0, (name, level, built.stdout, built.stderr)
            ran = invoke([str(executable)], name + "-run-O" + level, timeout=60)
            assert ran.returncode == 0 and not ran.stderr, (name, level, ran.returncode, ran.stderr)
            assert ran.stdout == output, (name, level, ran.stdout)
        report["configurations"].append({"optimization": level, "allocation_poisoning": True,
                                         "diagnostic_bytes": len(golden.encode()), "status": "passed"})
        print(f"ok   declared argument/scope Options: 46 cases and diagnostic bytes at O{level} poison", flush=True)

    source = (root / "core/typecheck.ax").read_text()
    mutants = {
        "discard-declared-parameter": ("(let ans (checkLamAgainst tc argTy param))",
                                       "(let ans (checkLamAgainst tc argTy (mkTArr mkIntTy mkIntTy)))", {16, 17}),
        "invent-missing-scope-type": ("(bsMake vecNew params callable eparams fnty",
                                      "(bsMake vecNew params callable eparams (Some mkUnitTy)", {34, 35}),
    }
    for name, (old, new, required) in mutants.items():
        assert source.count(old) == 1, (name, "mutation seam changed")
        modules = work / (name + "-modules")
        modules.mkdir(exist_ok=True)
        (modules / "typecheck.ax").write_text(source.replace(old, new))
        local = dict(env, AXIOM_PATH=os.pathsep.join((str(modules), str(root / "core"))))
        executable = work / name
        built = invoke([compiler, "--diagnostic-format=ai", "build",
                        str(root / "tests/tools/checker-declared-argument-options.axp"),
                        "--opt", "0", "--alloc-poison", "-o", str(executable)], name + "-build", local)
        assert built.returncode == 0, (name, built.stdout, built.stderr)
        ran = invoke([str(executable)], name + "-run", local, timeout=60)
        assert ran.returncode == 0 and not ran.stderr, (name, ran.returncode, ran.stderr)
        failed = {int(line.split()[0]) for line in ran.stdout.splitlines() if line.endswith(" false")}
        assert required <= failed, (name, failed)
        report["counterexamples"].append({"name": name, "failed_cases": sorted(failed)})
        print(f"ok   counterexample {name}: cases {sorted(failed)} fail", flush=True)

    invalid = {
        "raw-declared-parameter": ('(:: bad (-> TC ASTNode ASTNode)) '
                                   '(fn (bad tc value) (groundArgByDecl tc value mkIntTy 0))', "AX3004"),
        "raw-scope-signature": ('(:: bad BScope) (fn (bad) (bsNew vecNew vecNew vecNew nilNode))', "AX3004"),
        "missing-scope-none": ('(:: bad (-> BScope ASTNode)) '
                               '(fn (bad scope) (match (bsFnTy scope) ((Some ty) ty)))', "AX3005"),
    }
    for name, (declaration, code) in invalid.items():
        refused = work / (name + ".ax")
        refused.write_text("(import core)\n(import parser)\n(import typecheck)\n(import Vec)\n" +
                           declaration + "\n(:: main Int) (fn (main) 0)\n")
        checked = invoke([compiler, "--diagnostic-format=ai", "check", str(refused)], name)
        errors = [line for line in checked.stderr.splitlines() if line.startswith("E ")]
        assert checked.returncode == 1 and len(errors) == 1 and f"E {code} " in errors[0], (
            name, checked.returncode, checked.stdout, checked.stderr)
        report["refusals"].append({"name": name, "diagnostic": code})
        print(f"ok   refusal {name}: {code}", flush=True)
    (work / "declared-argument-report.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compiler")
    parser.add_argument("--work", type=Path)
    args = parser.parse_args()
    compiler = str(Path(args.compiler).resolve())
    root = Path(__file__).resolve().parents[2]
    if args.work is not None:
        args.work.mkdir(parents=True, exist_ok=True)
        check(compiler, root, args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="axiom-declared-arguments-") as directory:
            check(compiler, root, Path(directory))


if __name__ == "__main__":
    main()
