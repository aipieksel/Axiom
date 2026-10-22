#!/usr/bin/env python3
"""Check optional arities, checked emission, wire compatibility and refusals."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def run_checks(compiler, root, work):
    environment = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"))
    probes = work / "probes"
    probes.mkdir(exist_ok=True)
    # These expose existing private operations only in copied test modules.
    # Production visibility and the sealed handoff API stay unchanged.
    checked = (root / "core/checked.ax").read_text()
    (probes / "checked.ax").write_text(checked + '''
(pub :: arityProbeTypes (-> (Vec FnEnt) FunctionTypes))
(pub fn (arityProbeTypes functions) (checkedFunctionTypes functions))
''')
    codegen = (root / "core/codegen.ax").read_text()
    (probes / "codegen.ax").write_text(codegen + '''
(pub :: arityProbeFlags (-> ASTNode (Option Int) Ints))
(pub fn (arityProbeFlags ty arity) (functionFloatFlags ty arity vecNew))
''')
    env = dict(environment, AXIOM_PATH=os.pathsep.join((str(probes), str(root / "core"))))
    expected = "".join(f"{index} true\n" for index in range(1, 61))
    golden = (root / "tests/tools/checker-function-arity-options.golden").read_text()

    def invoke(arguments, label, timeout=240):
        result = subprocess.run(arguments, cwd=root, env=env, capture_output=True,
                                text=True, timeout=timeout)
        (work / (label + ".out")).write_text(result.stdout)
        (work / (label + ".err")).write_text(result.stderr)
        assert result.returncode == 0, (label, result.returncode, result.stdout, result.stderr)
        return result

    for level in ("0", "2"):
        executable = work / ("arities-O" + level)
        invoke([compiler, "--diagnostic-format=ai", "build",
                str(root / "tests/tools/checker-function-arity-options.axp"),
                "--opt", level, "--alloc-poison", "-o", str(executable)], "build-O" + level)
        ran = invoke([str(executable)], "run-O" + level, 60)
        assert not ran.stderr, (level, ran.stderr)
        facts, marker, llvm = ran.stdout.partition("-- emitted llvm --\n")
        assert facts == expected and marker and "target triple" in llvm, (level, facts, llvm[:100])
        emitted = work / ("numeric-O" + level + ".ll")
        emitted.write_text(llvm)
        optimized = work / ("numeric-opt-O" + level + ".ll")
        invoke(["opt", "-S", "-passes=default<O" + level + ">", str(emitted),
                "-o", str(optimized)], "opt-O" + level)
        obj = work / ("numeric-O" + level + ".o")
        invoke(["llc", str(optimized), "-filetype=obj", "-relocation-model=pic",
                "-O" + level, "-o", str(obj)], "llc-O" + level)
        native = work / ("numeric-O" + level)
        invoke(["cc", str(obj), "-o", str(native)], "link-O" + level)
        result = subprocess.run([str(native)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 42 and not result.stdout and not result.stderr, (
            level, result.returncode, result.stdout, result.stderr)

        diagnostics = work / ("diagnostics-O" + level)
        invoke([compiler, "--diagnostic-format=ai", "build",
                str(root / "tests/tools/checker-function-arity-diagnostics.axp"),
                "--opt", level, "--alloc-poison", "-o", str(diagnostics)], "diagnostic-build-O" + level)
        ran = invoke([str(diagnostics)], "diagnostics-O" + level, 60)
        assert not ran.stderr and ran.stdout == golden, (level, ran.stdout, ran.stderr)

    invalid = {
        "erased-declared": ("(:: bad (-> FnEnt Int)) (fn (bad entry) (fnEntParamCount entry))", "AX3004"),
        "erased-representation": ("(:: bad (-> FnEnt Int)) (fn (bad entry) (repArity entry))", "AX3004"),
        "missing-none": ("(:: bad (-> FnEnt Int)) (fn (bad entry) (match (repArity entry) ((Some count) count)))", "AX3005"),
        "raw-handoff": ("(:: bad FunctionType) (fn (bad) (FunctionType :name \"f\" :ty nilNode :arity 0))", "AX3004"),
        "raw-constructor": ("(:: bad FnEnt) (fn (bad) (FnEnt :name \"f\" :ty nilNode :paramCount 0 :isBuiltin 0 :isEffectOp 0 :effects vecNew :eparams vecNew :calls vecNew :rgn None :unsafe 0 :uses vecNew :mayCalls vecNew :pending 0 :scoped vecNew :compute vecNew :restrictions vecNew :lambdaCast NoLambdaCast))", "AX3004"),
    }
    for name, (declaration, code) in invalid.items():
        source = work / (name + ".ax")
        source.write_text("(import core)\n(import parser)\n(import typecheck)\n(import Vec)\n" +
                          declaration + "\n(:: main Int) (fn (main) 0)\n")
        result = subprocess.run([compiler, "--diagnostic-format=ai", "check", str(source)],
                                cwd=root, env=env, capture_output=True, text=True, timeout=60)
        (work / (name + ".err")).write_text(result.stderr)
        errors = [line for line in result.stderr.splitlines() if line.startswith("E ")]
        assert result.returncode == 1 and len(errors) == 1 and f"E {code} " in errors[0], (
            name, result.returncode, result.stdout, result.stderr)
    print("ok   function arity Options: 60 outcomes and checked Float/closure emission at O0/O2 poison; "
          f"{len(golden.splitlines())} diagnostic byte goldens; 5 erased/missing-case refusals")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compiler")
    parser.add_argument("--work", type=Path, help="retain focused probe artifacts")
    args = parser.parse_args()
    compiler = str(Path(args.compiler).resolve())
    root = Path(__file__).resolve().parents[2]
    if args.work is not None:
        args.work.mkdir(parents=True, exist_ok=True)
        run_checks(compiler, root, args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="axiom-function-arity-options-") as directory:
            run_checks(compiler, root, Path(directory))


if __name__ == "__main__":
    main()
