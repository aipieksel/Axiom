#!/usr/bin/env python3
"""Check optional symbol maps, ownership, fresh tag unions and stream bytes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

BRIDGE = '''
(pub :: symbolProbeVec (-> Strings (Vec Ints) String (Option Ints)))
(pub fn (symbolProbeVec names values key) (saGetVec names values key))
(pub :: symbolProbeParam (-> (Option Names) Int String))
(pub fn (symbolProbeParam names index) (smParamName names index))
'''

NAMES_GETTER = '''(pub fn (saGetNames names vals key)
  {
    (let i (saIdx names key))
    (if (< i 0)
      None
      (Some (vecGet vals i)))
  })'''


def check(compiler, root, work):
    source = (root / "core/symbols.ax").read_text()
    env = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"))
    expected = "".join(f"{index} true\n" for index in range(1, 41))
    golden = (root / "tests/tools/symbol-map-options.golden").read_text()
    report = {"builder": compiler, "builder_sha256": hashlib.sha256(Path(compiler).read_bytes()).hexdigest(),
              "symbols_sha256": hashlib.sha256(source.encode()).hexdigest(),
              "axir_sha256": hashlib.sha256((root / "core/axir.ax").read_bytes()).hexdigest(),
              "configurations": [], "counterexamples": [], "refusals": [],
              "limits": ["Host O0/O2 native execution with allocation poisoning; no Windows runtime claim.",
                         "ASTNode handles and parser-packed AXTAG Ints stay unchanged; this probe does not prove raw tag-entry ownership.",
                         "Getters retain stored vector aliases; only tag union constructs a fresh collection.",
                         "saIdx semantic negative-index API and unused saGet zero-return API remain separate inventory items.",
                         "No lifetime algorithms or held work are read, changed or validated."]}

    def modules_for(label, text):
        modules = work / (label + "-modules")
        modules.mkdir(exist_ok=True)
        (modules / "symbols.ax").write_text(text + BRIDGE)
        return dict(env, AXIOM_PATH=os.pathsep.join((str(modules), str(root / "core"))))

    def invoke(arguments, label, local, timeout=240):
        result = subprocess.run(arguments, cwd=root, env=local, capture_output=True,
                                text=True, timeout=timeout)
        (work / (label + ".out")).write_text(result.stdout)
        (work / (label + ".err")).write_text(result.stderr)
        return result

    local = modules_for("positive", source)
    for level in ("0", "2"):
        for name, output in (("symbol-map-options", expected), ("symbol-map-streams", golden)):
            executable = work / (name + "-O" + level)
            built = invoke([compiler, "--diagnostic-format=ai", "build",
                            str(root / "tests/tools" / (name + ".axp")),
                            "--opt", level, "--alloc-poison", "-o", str(executable)], name + "-build-O" + level, local)
            assert built.returncode == 0, (name, level, built.stdout, built.stderr)
            ran = invoke([str(executable)], name + "-run-O" + level, local, 60)
            assert ran.returncode == 0 and not ran.stderr, (name, level, ran.returncode, ran.stderr)
            assert ran.stdout == output, (name, level, ran.stdout)
        report["configurations"].append({"optimization": level, "allocation_poisoning": True,
                                         "cases": 40, "stream_bytes": len(golden.encode()), "status": "passed"})
        print(f"ok   symbol map Options: 40 outcomes and exact AXSYM/human/AXIR/diagnostic bytes at O{level} poison", flush=True)

    assert source.count(NAMES_GETTER) == 1, "names getter enforcement seam changed"
    missed = NAMES_GETTER.replace("(Some (vecGet vals i))", "None")
    unowned = NAMES_GETTER.replace("(Some (vecGet vals i))",
                                   "{ (let found (Some (vecGet vals i))) (memMarkLeaf (as found Int)) found }")
    union = "(saPut sm.tagN sm.tagV name (smTagsUnion have tags))"
    assert source.count(union) == 1, "tag union enforcement seam changed"
    mutants = {
        "discard-present-names": (source.replace(NAMES_GETTER, missed), {2, 3, 4, 5, 7, 32}),
        "unmapped-option-child": (source.replace(NAMES_GETTER, unowned), {6}),
        "discard-earlier-tags": (source.replace(union, "(saPut sm.tagN sm.tagV name tags)"), {20, 21}),
    }
    for name, (mutant, required) in mutants.items():
        mutant_env = modules_for(name, mutant)
        executable = work / name
        built = invoke([compiler, "--diagnostic-format=ai", "build",
                        str(root / "tests/tools/symbol-map-options.axp"),
                        "--opt", "0", "--alloc-poison", "-o", str(executable)], name + "-build", mutant_env)
        assert built.returncode == 0, (name, built.stdout, built.stderr)
        ran = invoke([str(executable)], name + "-run", mutant_env, 60)
        assert ran.returncode == 0 and not ran.stderr, (name, ran.returncode, ran.stderr)
        failed = {int(line.split()[0]) for line in ran.stdout.splitlines() if line.endswith(" false")}
        assert required <= failed, (name, failed)
        report["counterexamples"].append({"name": name, "failed_cases": sorted(failed)})
        print(f"ok   counterexample {name}: cases {sorted(failed)} fail", flush=True)

    invalid = {
        "erased-names": ('(:: bad (-> SymMaps Names)) (fn (bad map) (saGetNames map.prmN map.prmV "f"))', "AX3004"),
        "erased-node": ('(:: bad (-> SymMaps ASTNode)) (fn (bad map) (saGetNode map.prmN map.fnD "f"))', "AX3004"),
        "erased-vector": ('(:: bad (-> SymMaps Ints)) (fn (bad map) (symbolProbeVec map.tagN map.tagV "f"))', "AX3004"),
        "missing-names-none": ('(:: bad (-> SymMaps Names)) (fn (bad map) '
                               '(match (saGetNames map.prmN map.prmV "f") ((Some names) names)))', "AX3005"),
        "raw-projection-names": ('(:: bad (-> FnEnt Int)) (fn (bad entry) (symMirMetas entry vecNew 0 vecNew))', "AX3004"),
        "raw-lowering-node": ('(:: bad Int) (fn (bad) (axirLowered nilNode vecNew))', "AX3004"),
    }
    for name, (declaration, code) in invalid.items():
        refused = work / (name + ".ax")
        refused.write_text("(import core)\n(import parser)\n(import typecheck)\n(import symbols)\n(import axir)\n(import Vec)\n" +
                           declaration + "\n(:: main Int) (fn (main) 0)\n")
        checked = invoke([compiler, "--diagnostic-format=ai", "check", str(refused)], name, local)
        errors = [line for line in checked.stderr.splitlines() if line.startswith("E ")]
        assert checked.returncode == 1 and len(errors) == 1 and f"E {code} " in errors[0], (
            name, checked.returncode, checked.stdout, checked.stderr)
        report["refusals"].append({"name": name, "diagnostic": code})
        print(f"ok   refusal {name}: {code}", flush=True)
    (work / "symbol-map-report.json").write_text(json.dumps(report, indent=2) + "\n")


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
        with tempfile.TemporaryDirectory(prefix="axiom-symbol-maps-") as directory:
            check(compiler, root, Path(directory))


if __name__ == "__main__":
    main()
