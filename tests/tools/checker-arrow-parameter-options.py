#!/usr/bin/env python3
"""Check shared optional argument types, conservative consumers and native code."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

BRIDGE = '''
(pub :: arrowProbeParks (-> CG ASTNode ASTNode Names Int))
(pub fn (arrowProbeParks cg call signature params)
  {
    (let cell (memAlloc 8))
    (memSetWord cell 0 0)
    (selfSlotParksFrom cg call signature (spineArity call) params vecNew cell 0)
    (memGetWord cell 0)
  })
'''


def check(compiler, root, work):
    probes = work / "modules"
    probes.mkdir(exist_ok=True)
    (probes / "codegen.ax").write_text((root / "core/codegen.ax").read_text() + BRIDGE)
    env = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"),
               AXIOM_PATH=os.pathsep.join((str(probes), str(root / "core"))))
    expected = "".join(f"{index} true\n" for index in range(1, 74))
    golden = (root / "tests/tools/checker-arrow-parameter-options.golden").read_text()

    def run(arguments, label, timeout=240):
        result = subprocess.run(arguments, cwd=root, env=env, capture_output=True,
                                text=True, timeout=timeout)
        (work / (label + ".out")).write_text(result.stdout)
        (work / (label + ".err")).write_text(result.stderr)
        assert result.returncode == 0, (label, result.returncode, result.stdout, result.stderr)
        return result

    for level in ("0", "2"):
        executable = work / ("arrow-parameters-O" + level)
        run([compiler, "--diagnostic-format=ai", "build",
             str(root / "tests/tools/checker-arrow-parameter-options.axp"),
             "--opt", level, "--alloc-poison", "-o", str(executable)], "build-O" + level)
        ran = run([str(executable)], "run-O" + level, 90)
        assert not ran.stderr, (level, ran.stderr)
        facts, marker, llvm = ran.stdout.partition("-- emitted llvm --\n")
        assert facts == expected and marker and "target triple" in llvm, (level, facts, llvm[:100])
        emitted = work / ("ownership-O" + level + ".ll")
        emitted.write_text(llvm)
        optimized = work / ("ownership-opt-O" + level + ".ll")
        run(["opt", "-S", "-passes=default<O" + level + ">", str(emitted),
             "-o", str(optimized)], "opt-O" + level)
        obj = work / ("ownership-O" + level + ".o")
        run(["llc", str(optimized), "-filetype=obj", "-relocation-model=pic", "-O" + level,
             "-o", str(obj)], "llc-O" + level)
        native = work / ("ownership-O" + level)
        run(["cc", str(obj), "-o", str(native)], "link-O" + level)
        result = subprocess.run([str(native)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 42 and not result.stdout and not result.stderr, (
            level, result.returncode, result.stdout, result.stderr)

        diagnostics = work / ("diagnostics-O" + level)
        run([compiler, "--diagnostic-format=ai", "build",
             str(root / "tests/tools/checker-arrow-parameter-diagnostics.axp"),
             "--opt", level, "--alloc-poison", "-o", str(diagnostics)], "diagnostic-build-O" + level)
        diagnosed = run([str(diagnostics)], "diagnostics-O" + level, 90)
        assert not diagnosed.stderr and diagnosed.stdout == golden, (level, diagnosed.stdout, diagnosed.stderr)

    invalid = {
        "erased-query": ('(:: bad (-> ASTNode ASTNode)) (fn (bad ty) (arrowParamTy ty 0))', "AX3004"),
        "missing-none": ('(:: bad (-> ASTNode ASTNode)) (fn (bad ty) (match (arrowParamTy ty 0) '
                         '((Some parameter) parameter)))', "AX3005"),
        "raw-callability-input": ('(:: bad Ints) (fn (bad) (paramCallablesOf nilNode 1))', "AX3004"),
        "raw-escape-input": ('(:: bad (-> BScope Int)) (fn (bad scope) '
                             '(escapeArgs scope vecNew 0 vecNew nilNode))', "AX3004"),
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

    # The existing effect-position gate must still remove exactly the real
    # enforcement condition, and its separate alias control must type-check.
    script = (root / "scripts/check-effect-argpos.sh").read_text()
    seam = script.split("seam_old='", 1)[1].split("'\nseam_new=", 1)[0]
    source = (root / "core/typecheck.ax").read_text()
    assert source.count(seam) == 1, "effect-position ablation no longer targets the real guard"
    alias = script.split("alias_old = '''", 1)[1].split("'''", 1)[0]
    assert source.count(alias) == 1, "callback alias ablation no longer targets the real guard"
    replacement = script.split("seam_new='", 1)[1].split("'\nn_seam=", 1)[0]
    alias_replacement = script.split("alias_new = " + chr(39) * 3, 1)[1].split(chr(39) * 3, 1)[0]
    mutant = work / "effect-ablation-typecheck.ax"
    mutant.write_text(source.replace(seam, replacement).replace(alias, alias_replacement))
    run([compiler, "--diagnostic-format=ai", "check", str(mutant)], "ablation-source-check", 90)
    print("ok   arrow parameter Options: 73 query/ownership/effect/class cases at O0/O2 poison; "
          "new emitted reference/generic/callback code exits42; "
          f"{len(golden.splitlines())} diagnostic byte goldens; 4 erased/missing-case refusals")


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
        with tempfile.TemporaryDirectory(prefix="axiom-arrow-parameters-") as directory:
            check(compiler, root, Path(directory))


if __name__ == "__main__":
    main()
