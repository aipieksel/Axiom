#!/usr/bin/env python3
"""Check lifetime refusals by source function and run independent controls."""
import argparse
from collections import Counter
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]

# These counts follow the stores and captures in the source, rather than a
# blessed diagnostic stream. An extra refusal in a silent control also fails.
CASES = {
    "diagnostics/1474-handler-keeps-argument.ax": {
        ("keptInCell", "AX3060"): 1,
        ("keptInVec", "AX3060"): 2,
        ("keptCopy", "AX3060"): 1,
        ("keptClosure", "AX3062"): 1,
        ("keptAcrossRecovery", "AX3060"): 1,
        ("namedKeeps", "AX3060"): 2,
        ("sideEffectScalar", "AX3060"): 1,
        ("keptThroughCallee", "AX3060"): 1,
        ("keptThroughUnknown", "AX3060"): 1,
        ("keptThroughInstalledHandler", "AX3060"): 1,
    },
    "diagnostics/1475-recover-store-shapes.ax": {
        ("setField", "AX3060"): 1,
        ("setSlot", "AX3060"): 1,
        ("pushNested", "AX3060"): 2,
        ("setDeep", "AX3060"): 1,
        ("setHook", "AX3062"): 1,
        ("putMap", "AX3060"): 2,
        ("setSlice", "AX3060"): 1,
        ("consBag", "AX3060"): 1,
        ("viaAlias", "AX3060"): 1,
        ("viaGet", "AX3060"): 1,
        ("viaMatch", "AX3060"): 1,
    },
    "diagnostics/1476-region-store-shapes.ax": {
        ("setField", "AX3059"): 1,
        ("setSlot", "AX3060"): 1,
        ("pushNested", "AX3060"): 2,
        ("setDeep", "AX3059"): 1,
        ("setHook", "AX3059"): 1,
        ("putMap", "AX3060"): 2,
        ("setSlice", "AX3059"): 1,
        ("consBag", "AX3059"): 1,
        ("viaAlias", "AX3060"): 1,
        ("viaGet", "AX3060"): 1,
        ("viaMatch", "AX3060"): 1,
        ("pushHook", "AX3060"): 1,
        ("pushHook", "AX3062"): 1,
    },
    "diagnostics/1477-parallel-capture-nested.ax": {
        ("viaData", "AX3064"): 1,
        ("viaOption", "AX3064"): 1,
        ("viaDeep", "AX3064"): 1,
        ("carryBack", "AX3004"): 1,
    },
    "region/handler-escape.ax": {("main", "AX3060"): 1},
    "region/handler-unseen.ax": {
        ("bound", "AX3060"): 1,
        ("parameter", "AX3060"): 1,
        ("field", "AX3060"): 1,
        ("returned", "AX3060"): 1,
        ("nullary", "AX3060"): 1,
        ("callerExtent", "AX3060"): 1,
    },
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("compiler", type=Path)
    parser.add_argument("--handler-ablated", action="store_true")
    args = parser.parse_args()
    compiler = args.compiler.resolve()
    env = dict(os.environ, AXIOM_STDLIB=str(ROOT / "stdlib"))

    def run(*argv):
        return subprocess.run(
            [str(compiler), "--diagnostic-format=ai", *map(str, argv)],
            cwd=ROOT, env=env, capture_output=True, timeout=120,
        )

    if args.handler_ablated:
        for name in ("handler-escape", "handler-unseen"):
            result = run("check", ROOT / f"tests/region/{name}.ax")
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            print(f"ok   handler ablation accepts {name}")
        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / "handler-escape"
            result = run("build", ROOT / "tests/region/handler-escape.ax", "-o", output)
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            result = subprocess.run([str(output)], capture_output=True, timeout=15)
            assert (result.returncode, result.stdout) != (0, b"hello\nfresh\n"), (
                "handler ablation preserved the reclaimed reference; runtime witness failed"
            )
            print(f"ok   handler ablation exposes reclaimed storage "
                  f"(exit {result.returncode}, output {result.stdout[:64]!r})")
        return

    for name, expected in CASES.items():
        source = ROOT / "tests" / name
        functions = [
            (i, match[1])
            for i, line in enumerate(source.read_text().splitlines(), 1)
            if (match := re.match(r"\(fn \((\w+)", line))
        ]
        result = run("emit-llvm", source)
        assert result.returncode == 1 and not result.stdout, (
            f"{name}: a refused program reached emission: {result.returncode}\n"
            + result.stderr.decode(errors="replace")
        )
        found = Counter()
        for line in result.stderr.decode().splitlines():
            if match := re.match(r"([EW]) (AX\d+) (.*?):(\d+):", line):
                assert match[1] == "E", f"{name}: unexpected warning: {line}"
                assert Path(match[3]).resolve() == source.resolve(), line
                function = next(n for i, n in reversed(functions) if i <= int(match[4]))
                found[function, match[2]] += 1
        assert found == Counter(expected), (
            f"{name}: expected {expected}, got {dict(found)}\n"
            + result.stderr.decode(errors="replace")
        )
        print(f"ok   {name}: {sum(found.values())} intended refusals; controls silent")

    source = ROOT / "tests/region/lifetime-controls.ax"
    for threads, levels in ((False, range(4)), (True, (0, 3))):
        for level in levels:
            flags = ["--opt", str(level)] + (["--threads"] if threads else [])
            result = run("run", source, *flags)
            assert result.returncode == 0 and not result.stdout and not result.stderr, (
                f"lifetime controls {flags}: exit {result.returncode}\n"
                + result.stderr.decode(errors="replace")
            )
            print(f"ok   lifetime controls: {'threads' if threads else 'processes'}, opt {level}")


if __name__ == "__main__":
    main()
