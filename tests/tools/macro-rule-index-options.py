#!/usr/bin/env python3
"""Check optional repetition/coverage indexes and first-witness diagnostics."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def check(compiler, root, work, modules):
    source = (root / "core/expand.ax").read_text()
    assert (modules / "expand.ax").read_text() == source, "validation module differs from the owned source"
    env = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"), AXIOM_PATH=str(modules))
    report = {"builder": compiler, "builder_sha256": hashlib.sha256(Path(compiler).read_bytes()).hexdigest(),
              "expand_sha256": hashlib.sha256(source.encode()).hexdigest(), "configurations": [],
              "controls": [], "refusals": [], "limits": ["Focused host O0/O2 probes with allocation poisoning.",
              "Some zero is a real index; required input cursor and target bounds retain their caller contract.",
              "The PResult/macro record dependency must be integrated before this increment compiles.",
              "No lifetime algorithms, held work, or overall memory/completion claim."]}
    def invoke(args, label, local=env, timeout=180):
        ran = subprocess.run(args, cwd=root, env=local, capture_output=True, text=True, timeout=timeout)
        (work / (label + ".out")).write_text(ran.stdout); (work / (label + ".err")).write_text(ran.stderr)
        return ran
    def build(name, level, local=env, suffix=""):
        binary = work / (name + "-O" + level + suffix)
        ran = invoke([compiler, "--diagnostic-format=ai", "build", str(root / "tests/tools" / (name + ".axp")),
                      "--opt", level, "--alloc-poison", "-o", str(binary)], binary.name + "-build", local)
        assert ran.returncode == 0, (name, ran.stdout, ran.stderr)
        return binary
    expected = "".join(f"{i} true\n" for i in range(1, 33))
    diagnostics = (root / "tests/tools/macro-record-diagnostics.golden").read_text()
    for level in ("0", "2"):
        for name, want in (("macro-rule-index-options", expected), ("macro-record-diagnostics", diagnostics)):
            binary = build(name, level)
            ran = invoke([str(binary)], binary.name + "-run", timeout=30)
            assert ran.returncode == 0 and not ran.stderr and ran.stdout == want, (name, level, ran.stdout, ran.stderr)
        report["configurations"].append({"opt": level, "allocation_poisoning": True, "outcomes": 32,
                                         "diagnostic_bytes": len(diagnostics.encode()), "status": "passed"})
        print(f"ok   macro index Options: 32 missing/zero/first-witness/matching outcomes and exact diagnostics O{level} poison", flush=True)
    repeat = "(Some i)\n      (expRepIndexFrom reps (+ i 1))"
    witness = "(Some j)\n        (expShadowingRule rules i (+ j 1))"
    assert source.count(repeat) == 1 and source.count(witness) == 1, "index enforcement seams changed"
    controls = {
        "drop-zero-repeat": (source.replace(repeat, repeat.replace("(Some i)", "(if (== i 0) None (Some i))")), {3, 5, 10}),
        "shift-first-witness": (source.replace(witness, witness.replace("(Some j)", "(Some (+ j 1))")), {13, 23, 24}),
    }
    for name, (changed, required) in controls.items():
        local_modules = work / (name + "-modules"); local_modules.mkdir(exist_ok=True)
        (local_modules / "expand.ax").write_text(changed)
        local = dict(env, AXIOM_PATH=os.pathsep.join((str(local_modules), str(modules))))
        binary = build("macro-rule-index-options", "0", local, "-" + name)
        ran = invoke([str(binary)], name + "-run", local, 30)
        assert ran.returncode == 0 and not ran.stderr, (name, ran.returncode, ran.stderr)
        failed = {int(line.split()[0]) for line in ran.stdout.splitlines() if line.endswith(" false")}
        assert required <= failed, (name, failed)
        report["controls"].append({"name": name, "failed_cases": sorted(failed)})
        print(f"ok   counterexample {name}: cases {sorted(failed)} fail", flush=True)
    refused = {
        "erased-repeat": ('(:: bad (-> MacroRule Int)) (fn (bad r) (expRepIndex r))', "AX3004"),
        "erased-shadow": ('(:: bad (-> MacroRules Int)) (fn (bad rules) (expShadowingRule rules 0 0))', "AX3004"),
        "missing-repeat-none": ('(:: bad (-> MacroRule Int)) (fn (bad r) (match (expRepIndex r) ((Some index) index)))', "AX3005"),
    }
    for name, (declaration, code) in refused.items():
        path = work / (name + ".ax"); path.write_text("(import core) (import expand)\n" + declaration + "\n(:: main Int) (fn (main) 0)\n")
        ran = invoke([compiler, "--diagnostic-format=ai", "check", str(path)], name)
        errors = [line for line in ran.stderr.splitlines() if line.startswith("E ")]
        assert ran.returncode == 1 and len(errors) == 1 and f"E {code} " in errors[0], (name, ran.stdout, ran.stderr)
        report["refusals"].append({"name": name, "code": code})
    print("ok   macro indexes: three erased/nonexhaustive contracts refused", flush=True)
    (work / "macro-index-report.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compiler"); parser.add_argument("--work", type=Path); parser.add_argument("--modules", type=Path)
    args = parser.parse_args(); root = Path(__file__).resolve().parents[2]
    modules = (args.modules or root / "core").resolve(); compiler = str(Path(args.compiler).resolve())
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True); check(compiler, root, args.work.resolve(), modules)
    else:
        with tempfile.TemporaryDirectory(prefix="axiom-macro-indexes-") as directory:
            check(compiler, root, Path(directory), modules)


if __name__ == "__main__":
    main()
