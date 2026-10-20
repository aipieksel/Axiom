#!/usr/bin/env python3
"""Exercise the actual private CG String-slot setters and their alias ledger."""
import argparse
import os
from pathlib import Path
import re
import runpy
import tempfile


SUPPORT = runpy.run_path(str(Path(__file__).with_name("emitter-line-buffer.py")))
run_checked = SUPPORT["run_checked"]
replace_once = SUPPORT["replace_once"]
check_output = SUPPORT["check_output"]

BRIDGES = r'''
; Private-copy-only probes: no production raw setter is exported.
(pub :: probeSetResult (-> Int String Int))
(pub fn (probeSetResult cg value) (cgSetResult cg value))
(pub :: probeSetBlock (-> Int String Int))
(pub fn (probeSetBlock cg value) (cgSetBlock cg value))
(pub :: probeSetRawResult (-> Int Int Int))
;@axiom:effect(unsafe)
(pub fn (probeSetRawResult cg value) (cgSetResult cg (as value String)))
(pub :: probeGetterCount (-> Int Int Int))
;@axiom:effect(unsafe)
(pub fn (probeGetterCount cg index)
  { (let value (memGetWordStr cg index)) (__load64 (as value Int) -2) })
(pub :: probeSameResultGetter (-> Int String Bool))
(pub fn (probeSameResultGetter cg expected)
  { (let value (memGetWordStr cg 2)) (cgSetResult cg value) (strEq value expected) })

; This raw leaf descriptor has count0 and static bytes. The actual typed
; setter creates its sole managed share; no ordinary String share is released
; by a test helper. It is used only for retain-before-release ordering.
(:: probeStaticString Int)
(fn (probeStaticString)
  {
    (let header (memAlloc 24))
    (memSetWord header 0 8)
    (memSetWord header 1 (strData "raw-slot"))
    (memSetWord header 2 0)
    header
  })
(pub :: probeSoleResultSame Bool)
;@axiom:effect(unsafe)
(pub fn (probeSoleResultSame)
  {
    (let cg newCG)
    (let header (probeStaticString))
    (cgSetResult cg (as header String))
    (let sole (== (__load64 header -2) 1))
    (let filed (__axiom_mem_stat 1))
    (cgSetResult cg (as header String))
    (if (!= (__axiom_mem_stat 1) filed)
      false
      (&& sole (== (__load64 header -2) 1)))
  })
(pub :: probeSoleBlockSame Bool)
;@axiom:effect(unsafe)
(pub fn (probeSoleBlockSame)
  {
    (let cg newCG)
    (let header (probeStaticString))
    (cgSetBlock cg (as header String))
    (let sole (== (__load64 header -2) 1))
    (let filed (__axiom_mem_stat 1))
    (cgSetBlock cg (as header String))
    (if (!= (__axiom_mem_stat 1) filed)
      false
      (&& sole (== (__load64 header -2) 1)))
  })
'''


def check(axiom, repo, work):
    fixture = repo / "tests/selfhost/emitter-string-slots.ax"
    expected = fixture.with_suffix(".out").read_text().splitlines()
    if expected != [f"{i} true" for i in range(1, 33)]:
        raise RuntimeError("update the explicit 32-case assertion count")
    env = dict(os.environ, AXIOM_PATH=str(repo / "self_host"), AXIOM_STDLIB=str(repo / "stdlib"))
    original = (repo / "self_host/codegen.ax").read_text()

    def build_run(label, level, source, negative=()):
        modules = work / f"{label}-modules"
        modules.mkdir(exist_ok=True)
        for path in (repo / "self_host").iterdir():
            destination = modules / path.name
            if path.name == "codegen.ax":
                destination.write_text(source + BRIDGES)
            elif not destination.exists():
                destination.symlink_to(path, target_is_directory=path.is_dir())
        binary = work / label
        local = dict(env, AXIOM_PATH=str(modules))
        run_checked(
            [str(axiom), "build", str(fixture), "--opt", str(level), "--alloc-poison",
             "--emit-llvm", "--diagnostic-format=ai", "-o", str(binary)],
            env=local, stdout=work / f"{label}.build.stdout", stderr=work / f"{label}.build.stderr",
        )
        output = work / f"{label}.stdout"
        error = work / f"{label}.stderr"
        run_checked([str(binary)], env=local, stdout=output, stderr=error, timeout=30)
        failures = check_output(output, expected, required_failures=negative)
        growth = re.fullmatch(r"ordinary-soak-growth (-?\d+)\n", error.read_text())
        if growth is None:
            raise RuntimeError(f"{label}: missing ordinary String ownership diagnostic: {error.read_text()!r}")
        return failures, int(growth[1])

    for level in (0, 2):
        _, growth = build_run(f"positive-O{level}", level, original)
        print(f"ok   emitter String slots: 32 cases at -O{level} with allocation poisoning", flush=True)
        print(f"note ordinary String overwrite live-storage growth at -O{level}: {growth} bytes", flush=True)

    no_retire = original
    release_first = original
    for name, slot in (("cgSetResult", 2), ("cgSetBlock", 11)):
        signature = f"(:: {name} (-> Int String Int))\n;@axiom:effect(unsafe)\n"
        begin = original.index(f"(fn ({name} cg value)\n")
        end = original.index("\n  })", begin) + len("\n  })")
        body = original[begin:end]
        absent = replace_once(body, "    (__release old)\n", "", f"{name} retirement ablation")
        no_retire = replace_once(no_retire, body, absent, f"{name} function body")
        # The private module has no raw primitive after its release is removed.
        no_retire = replace_once(no_retire, signature,
                                 signature.replace(";@axiom:effect(unsafe)\n", ""),
                                 f"{name} primitive effect")
        ordered = replace_once(
            body, f"    (memSetWord cg {slot} value)\n    (__release old)\n",
            f"    (__release old)\n    (memSetWord cg {slot} value)\n",
            f"{name} retain order ablation")
        release_first = replace_once(release_first, body, ordered, f"{name} function order")

    for label, source, negative in [
        ("no-retirement", no_retire, (9, 14, 24, 25, 32)),
        ("release-first", release_first, (30, 31)),
    ]:
        failures, growth = build_run(label, 0, source, negative)
        print(f"ok   negative probe {label}: cases {','.join(map(str, sorted(failures)))} fail", flush=True)
        print(f"note ordinary String overwrite live-storage growth in {label}: {growth} bytes", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom", type=Path, required=True)
    parser.add_argument("--work", type=Path, help="retain logs and private probe modules")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    if args.work:
        work = args.work.resolve()
        work.mkdir(parents=True, exist_ok=True)
        check(args.axiom.resolve(), repo, work)
    else:
        with tempfile.TemporaryDirectory(prefix="axiom-string-slots-") as directory:
            check(args.axiom.resolve(), repo, Path(directory))


if __name__ == "__main__":
    main()
