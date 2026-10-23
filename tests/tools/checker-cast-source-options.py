#!/usr/bin/env python3
"""Check optional cast sources, numeric recovery, proofs and diagnostic bytes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

BRIDGE = '''
(pub :: castProbeCollectors (-> TC ()))
(pub fn (castProbeCollectors tc)
  {
    (tcSetForgeSites tc (Some nodesNew))
    (tcSetEraseSites tc (Some nodesNew))
    ()
  })
(pub :: castProbeForgeCount (-> TC Int))
(pub fn (castProbeForgeCount tc)
  (match (tcForgeSites tc)
    ((None) -1)
    ((Some sites) (vecLen sites))))
'''


def check(compiler, root, work):
    source = (root / "core/typecheck.ax").read_text()
    env = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"))
    expected = "".join(f"{index} true\n" for index in range(1, 49))
    golden = (root / "tests/tools/checker-cast-source-options.golden").read_text()
    report = {"builder": compiler, "builder_sha256": hashlib.sha256(Path(compiler).read_bytes()).hexdigest(),
              "typecheck_sha256": hashlib.sha256(source.encode()).hexdigest(),
              "cases_per_configuration": 48, "configurations": [], "counterexamples": [], "refusals": []}

    def modules_for(label, text):
        modules = work / (label + "-modules")
        modules.mkdir(exist_ok=True)
        (modules / "typecheck.ax").write_text(text + BRIDGE)
        return dict(env, AXIOM_PATH=os.pathsep.join((str(modules), str(root / "core"))))

    def invoke(arguments, label, local, timeout=240):
        result = subprocess.run(arguments, cwd=root, env=local, capture_output=True,
                                text=True, timeout=timeout)
        (work / (label + ".out")).write_text(result.stdout)
        (work / (label + ".err")).write_text(result.stderr)
        return result

    local = modules_for("positive", source)
    for level in ("0", "2"):
        for name, output in (("checker-cast-source-options", expected),
                             ("checker-cast-source-diagnostics", golden)):
            executable = work / (name + "-O" + level)
            built = invoke([compiler, "--diagnostic-format=ai", "build",
                            str(root / "tests/tools" / (name + ".axp")),
                            "--opt", level, "--alloc-poison", "-o", str(executable)], name + "-build-O" + level, local)
            assert built.returncode == 0, (name, level, built.stdout, built.stderr)
            ran = invoke([str(executable)], name + "-run-O" + level, local, 60)
            assert ran.returncode == 0 and not ran.stderr, (name, level, ran.returncode, ran.stderr)
            assert ran.stdout == output, (name, level, ran.stdout)
        report["configurations"].append({"optimization": level, "allocation_poisoning": True,
                                         "diagnostic_bytes": len(golden.encode()), "status": "passed"})
        print(f"ok   cast source Options: 48 outcomes and diagnostic bytes at O{level} poison", flush=True)

    mutants = {
        "discard-present-source": ('(:: (if (>= (vecLen args) 2)\n                (Some',
                                    '(:: (if false\n                (Some', {14, 16, 32}),
        "discard-missing-numeric-refusal": ('((None) (emitMismatch tc "Float" "{unknown}" (spanOf (spineArgAt e 1))))',
                                            '((None) 0)', {25, 42}),
    }
    for name, (old, new, required) in mutants.items():
        assert source.count(old) == 1, (name, "enforcement seam changed")
        mutant_env = modules_for(name, source.replace(old, new))
        executable = work / name
        built = invoke([compiler, "--diagnostic-format=ai", "build",
                        str(root / "tests/tools/checker-cast-source-options.axp"),
                        "--opt", "0", "--alloc-poison", "-o", str(executable)], name + "-build", mutant_env)
        assert built.returncode == 0, (name, built.stdout, built.stderr)
        ran = invoke([str(executable)], name + "-run", mutant_env, 60)
        assert ran.returncode == 0 and not ran.stderr, (name, ran.returncode, ran.stderr)
        failed = {int(line.split()[0]) for line in ran.stdout.splitlines() if line.endswith(" false")}
        assert required <= failed, (name, failed)
        report["counterexamples"].append({"name": name, "failed_cases": sorted(failed)})
        print(f"ok   counterexample {name}: cases {sorted(failed)} fail", flush=True)

    invalid = {
        "raw-numeric-source": ('(:: bad (-> TC ASTNode ())) '
                               '(fn (bad tc node) (rewriteNumericAs tc node mkFloatTy mkIntTy))', "AX3004"),
        "word-numeric-source": ('(:: bad (-> TC ASTNode ())) '
                                '(fn (bad tc node) (rewriteNumericAs tc node mkFloatTy (Some 0)))', "AX3004"),
        "missing-source-none": ('(:: bad (-> (Option ASTNode) ASTNode)) '
                                '(fn (bad source) (match source ((Some ty) ty)))', "AX3005"),
    }
    for name, (declaration, code) in invalid.items():
        refused = work / (name + ".ax")
        refused.write_text("(import core)\n(import parser)\n(import typecheck)\n(import Vec)\n" +
                           declaration + "\n(:: main Int) (fn (main) 0)\n")
        checked = invoke([compiler, "--diagnostic-format=ai", "check", str(refused)], name, local)
        errors = [line for line in checked.stderr.splitlines() if line.startswith("E ")]
        assert checked.returncode == 1 and len(errors) == 1 and f"E {code} " in errors[0], (
            name, checked.returncode, checked.stdout, checked.stderr)
        report["refusals"].append({"name": name, "diagnostic": code})
        print(f"ok   refusal {name}: {code}", flush=True)
    (work / "cast-source-report.json").write_text(json.dumps(report, indent=2) + "\n")


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
        with tempfile.TemporaryDirectory(prefix="axiom-cast-sources-") as directory:
            check(compiler, root, Path(directory))


if __name__ == "__main__":
    main()
