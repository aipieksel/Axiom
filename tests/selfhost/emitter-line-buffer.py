#!/usr/bin/env python3
"""Exercise the actual emitter line-buffer ownership with focused ablations.

Only the copied codegen module changes in negative probes. Each probe is an
ordinary executable fixture, not a rebuilt compiler or bootstrap ladder.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile


BRIDGES = r'''
; Private-copy-only probes; production does not export raw CG setters.
(pub :: probeSetHoisted (-> Int (Vec String) Int))
(pub fn (probeSetHoisted cg value) (cgSetHoisted cg value))
(pub :: probeHoisted (-> Int (Vec String)))
(pub fn (probeHoisted cg) (cgHoisted cg))
(pub :: probeHoistedPayloadCount (-> Int Int Int))
;@axiom:effect(unsafe)
(pub fn (probeHoistedPayloadCount cg index)
  { (let line (vecGet (cgHoisted cg) index)) (__load64 (as line Int) -2) })
(pub :: probeSameHoistedGetter (-> Int String Bool))
;@axiom:effect(unsafe)
(pub fn (probeSameHoistedGetter cg expected)
  {
    (let value (cgHoisted cg))
    (let before (__load64 (as value Int) -2))
    (cgSetHoisted cg value)
    (&& (== (__load64 (as value Int) -2) before) (strEq (vecGet value 0) expected))
  })

; Mirror Vec's mapped header and raw data ownership, but return its word
; without a counted return cast. The real setter creates its sole share.
; This is only an ordering proof, never an ordinary Vec birth-share fix.
(:: probeRawVec Int)
;@axiom:effect(unsafe)
(fn (probeRawVec)
  {
    (let header (memAllocMapped 32 4))
    (let data (memAlloc 8))
    (__retain data)
    (memSetWord header 0 0)
    (memSetWord header 1 1)
    (memSetWord header 2 data)
    (memSetWord header 3 0)
    header
  })
(pub :: probeSoleHoistedSame Bool)
;@axiom:effect(unsafe)
(pub fn (probeSoleHoistedSame)
  {
    (let cg newCG)
    (let header (probeRawVec))
    (cgSetHoisted cg (as header (Vec String)))
    (let sole (== (__load64 header -2) 1))
    (let filed (__axiom_mem_stat 1))
    (cgSetHoisted cg (as header (Vec String)))
    (if (!= (__axiom_mem_stat 1) filed)
      false
      (&& sole (== (__load64 header -2) 1)))
  })
'''

def run_checked(command, *, env, stdout, stderr, timeout=180):
    with stdout.open("w") as out, stderr.open("w") as err:
        result = subprocess.run(command, env=env, stdout=out, stderr=err, timeout=timeout)
    if result.returncode:
        raise RuntimeError(
            f"command exited {result.returncode}: {' '.join(map(str, command))}\n"
            f"{stderr.read_text()[-6000:]}"
        )


def replace_once(source, before, after, description):
    if source.count(before) != 1:
        raise RuntimeError(f"{description}: expected one source anchor")
    return source.replace(before, after, 1)


def check_output(path, expected, *, required_failures=()):
    lines = path.read_text().splitlines()
    if len(lines) != len(expected):
        raise RuntimeError(f"{path}: expected {len(expected)} cases, got {lines!r}")
    failures = set()
    for index, (actual, wanted) in enumerate(zip(lines, expected), 1):
        if actual == f"{index} false":
            failures.add(index)
        elif actual != wanted:
            raise RuntimeError(f"{path}: malformed case {index}: {actual!r}")
    if required_failures:
        if not set(required_failures) <= failures:
            raise RuntimeError(f"{path}: expected failures {required_failures}, got {failures}")
    elif failures:
        raise RuntimeError(f"{path}: failed cases {sorted(failures)}")
    return failures


def emitted_function(module, name):
    match = re.search(r'^define [^\n]*@' + re.escape(name)
                      + r'\([^\n]*\) [^\n]*\{\n.*?^\}', module, re.M | re.S)
    if match is None:
        raise RuntimeError(f"missing actual emitted function {name}")
    return match.group()


def check_hoisted_routes(binary):
    module = Path(str(binary) + ".ll").read_text()
    setter = emitted_function(module, "codegen$cgSetHoisted")
    old = re.search(r'(%\S+) = call i64 @raw\$memGetWord\(i64 %cg, i64 44\)', setter)
    if old is None:
        raise RuntimeError("hoisted setter no longer reads its displaced vector slot")
    publish = "@raw$memSetWord(i64 %cg, i64 44, i64 %value, i64 1)"
    retire = f"call void @axiom_release(i64 {old[1]})"
    if setter.count(publish) != 1 or setter.count(retire) != 1:
        raise RuntimeError("hoisted setter must publish with witness 1 and retire once")
    if setter.index(publish) >= setter.index(retire):
        raise RuntimeError("hoisted setter retires before taking its replacement share")
    for name in ("openAllocaMark", "closeAllocaMark"):
        body = emitted_function(module, "codegen$" + name)
        if body.count("@codegen$cgSetHoisted(") != 1:
            raise RuntimeError(f"{name}: expected one typed hoisted-vector replacement")
        if re.search(r'@raw\$memSetWord\(i64 [^,]+, i64 44,', body):
            raise RuntimeError(f"{name}: unbalanced direct hoisted-slot writer remains")
    for name in ("emitAlloca", "emitRecoverCell", "emitRegionCell", "closeAllocaMark"):
        body = emitted_function(module, "codegen$" + name)
        getter = re.findall(r'(%\S+) = call i64 @codegen\$cgHoisted\(i64 %cg\)', body)
        if len(getter) != 1:
            raise RuntimeError(f"{name}: expected one hoisted-vector getter")
        release = f"call void @axiom_release(i64 {getter[0]})"
        if body.count(release) != 1:
            raise RuntimeError(f"{name}: temporary hoisted getter share must retire once")
        if name != "closeAllocaMark":
            pushes = re.findall(r'@Vec\$vecPush\(i64 [^,]+, i64 [^,]+, i64 ([01])\)', body)
            if pushes != ["1"]:
                raise RuntimeError(f"{name}: String payload appends must retain their elements")
            value = re.search(r'@Vec\$vecPush\(i64 [^,]+, i64 (%\S+), i64 1\)', body)[1]
            cleanup = f"call void @axiom_release(i64 {value})"
            if body.count(cleanup) != 1 or body.index(cleanup) < body.index("@Vec$vecPush("):
                raise RuntimeError(f"{name}: ordinary queued String birth must retire after its store")
    for name in ("emitRecoverCell", "emitRegionCell"):
        body = emitted_function(module, "codegen$" + name)
        direct = re.search(r'(%\S+) = (?P<tail>musttail )?call i64 '
                           r'@codegen\$emitLine\(i64 %cg, i64 (?P<line>%\S+)\)', body)
        if direct is None or direct["tail"] is not None:
            raise RuntimeError(f"{name}: fresh line requires a normal emitLine call before cleanup")
        cleanup = f'call void @axiom_release(i64 {direct["line"]})'
        if body.count(cleanup) != 1 or body.index(cleanup) <= direct.end():
            raise RuntimeError(f"{name}: direct line birth must retire after emitLine returns")
        if body.index(cleanup) >= body.rindex("ret i64 "):
            raise RuntimeError(f"{name}: direct line cleanup must precede return")
    # The String a typed getter reads is either borrowed, when nothing in
    # its scope can write (no share taken, none given back), or held by
    # one temporary share given back once. Either is balanced; a share
    # taken and never returned, or returned twice, is not.
    for name in ("joinAllocas", "joinAllocasSize"):
        body = emitted_function(module, "codegen$" + name)
        reads = re.findall(r'(%\S+) = call i64 @Vec\$vecGet\(i64 [^,]+, i64 [^,]+, i64 1\)', body)
        if len(reads) != 1:
            raise RuntimeError(f"{name}: expected one typed String getter")
        taken = body.count(f"call void @axiom_retain(i64 {reads[0]})")
        given = body.count(f"call void @axiom_release(i64 {reads[0]})")
        if taken != given or given > 1:
            raise RuntimeError(f"{name}: typed String getter takes {taken} share(s) and gives back {given}")


def check(axiom, repo, work):
    source = repo / "tests/selfhost/emitter-line-buffer.axp"
    fixture = work / source.with_suffix(".ax").name
    fixture.write_bytes(source.read_bytes())
    expected = source.with_suffix(".out").read_text().splitlines()
    if expected != [f"{i} true" for i in range(1, 58)]:
        raise RuntimeError("update the explicit 57-case assertion count")
    env = dict(os.environ, AXIOM_PATH=str(repo / "core"), AXIOM_STDLIB=str(repo / "stdlib"))

    run_checked([str(axiom), "--diagnostic-format=ai", "fmt", "--check", str(fixture)],
                env=env, stdout=work / "fixture.fmt.stdout", stderr=work / "fixture.fmt.stderr")

    def prepare_module(label, source):
        module_root = work / f"{label}-modules"
        module_root.mkdir(exist_ok=True)
        for path in (repo / "core").iterdir():
            destination = module_root / path.name
            if path.name == "codegen.ax":
                destination.write_text(source + BRIDGES)
            elif not destination.exists():
                destination.symlink_to(path, target_is_directory=path.is_dir())
        return module_root

    def build_run(label, level, module_root, negative=()):
        binary = work / label
        local_env = dict(env, AXIOM_PATH=str(module_root))
        run_checked(
            [str(axiom), "build", str(fixture), "--opt", str(level), "--alloc-poison",
             "--emit-llvm", "--diagnostic-format=ai", "-o", str(binary)],
            env=local_env, stdout=work / f"{label}.build.stdout", stderr=work / f"{label}.build.stderr",
        )
        output = work / f"{label}.stdout"
        run_checked([str(binary)], env=local_env, stdout=output, stderr=work / f"{label}.stderr", timeout=30)
        failures = check_output(output, expected, required_failures=negative)
        if not negative:
            check_hoisted_routes(binary)
        diagnostic = (work / f"{label}.stderr").read_text()
        growth = re.fullmatch(r"hoisted-soak-growth (-?\d+)\nhoisted-payload-soak-growth (-?\d+)\ndirect-payload-soak-growth (-?\d+)\n", diagnostic)
        if growth is None:
            raise RuntimeError(f"{label}: missing hoisted Vec ownership diagnostic: {diagnostic!r}")
        print(f"note fresh-Vec/hoisted/direct payload growth in {label}: {tuple(map(int, growth.groups()))} bytes", flush=True)
        return failures

    original = (repo / "core/codegen.ax").read_text()
    for level in (0, 2):
        label = f"positive-O{level}"
        build_run(label, level, prepare_module(label, original))
        print(f"ok   emitter line-buffer ownership: 57 cases at -O{level} with allocation poisoning", flush=True)

    variants = []
    variants.append(("header-share", replace_once(
        original,
        '    (vecPush (cgLines cg) (strDup line))\n    cg',
        '    (let updated (vecPush (cgLines cg) (strDup line)))\n'
        '    (memSetWord cg 0 updated)\n    cg',
        "same-header share ablation"), (2, 4)))
    variants.append(("mutable-snapshot", replace_once(
        original,
        '(vecPush (cgLines cg) (strDup line))',
        '(vecPush (cgLines cg) line)',
        "snapshot ablation"), (1, 5)))
    alloca = replace_once(
        original,
        '(vecSet (cgLines cg) mark (joinAllocas hoisted))',
        '(vecSet (memGetWordVec cg 0) mark (as (joinAllocas hoisted) Int))',
        "alloca element metadata ablation")
    alloca = replace_once(alloca, '(as 0 (Vec String)))\n          (cgSetAllocaMark cg',
                          '(as 0 (Vec Int)))\n          (cgSetAllocaMark cg', "alloca branch type")
    variants.append(("alloca-metadata", alloca, (11,)))
    displaced = replace_once(
        original, '    (memSetWord cg 0 lines)\n    (__release old)\n', '    (memSetWord cg 0 lines)\n',
        "displaced CG-slot share ablation")
    # This newly added tag describes the removed primitive. Keep the
    # private negative module truthful so the executable, not AX3010,
    # demonstrates that the fixture detects the retained slot share.
    displaced = replace_once(
        displaced,
        '(:: cgReplaceLines (-> Int Strings Int))\n;@axiom:effect(unsafe)\n',
        '(:: cgReplaceLines (-> Int Strings Int))\n',
        "displaced-slot primitive effect")
    variants.append(("displaced-slot", displaced, (14, 15)))

    name = "cgSetHoisted"
    signature = f"(:: {name} (-> Int (Vec String) Int))\n;@axiom:effect(unsafe)\n"
    begin = original.index(f"(fn ({name} cg value)\n")
    end = original.index("\n  })", begin) + len("\n  })")
    body = original[begin:end]
    absent = replace_once(body, "    (__release old)\n", "", "hoisted retirement ablation")
    no_retire = replace_once(original, body, absent, "hoisted setter body")
    # Removing this primitive also removes the copied helper's direct
    # unsafe claim, so runtime assertions demonstrate the negative control.
    no_retire = replace_once(no_retire, signature,
                             signature.replace(";@axiom:effect(unsafe)\n", ""),
                             "hoisted setter primitive effect")
    variants.append(("hoisted-no-retirement", no_retire, (24, 27, 28, 29, 30, 31, 37, 40, 42, 43)))
    ordered = replace_once(body,
        "    (memSetWord cg 44 value)\n    (__release old)\n",
        "    (__release old)\n    (memSetWord cg 44 value)\n",
        "hoisted retain order ablation")
    variants.append(("hoisted-release-first", replace_once(
        original, body, ordered, "hoisted setter order"), (43,)))

    # Keep the header's retain/publish/retire ledger intact while making
    # element storage plain. This deliberately recreates the old payload
    # leak direction; it never manually spends an ordinary String share.
    plain_payload = replace_once(body, "    (__release old)\n", """    (let h (as value Int))
    (if (> h 0)
      {
        (let data (memGetWord h 2))
        (if (> data 0)
          { (memMarkLeaf data) (memSetWord h 3 2) })
      })
    (__release old)
""", "hoisted payload metadata ablation")
    variants.append(("hoisted-plain-payload", replace_once(
        original, body, plain_payload, "hoisted payload map"), (42, 44, 51, 53)))

    for label, source, negative in variants:
        module_root = prepare_module(label, source)
        failures = build_run(label, 0, module_root, negative)
        if label == "hoisted-plain-payload":
            header_cases = set(range(18, 42)) | {43}
            if failures & header_cases:
                raise RuntimeError(f"payload-only control damaged header ledger: {sorted(failures & header_cases)}")
        print(f"ok   negative probe {label}: cases {','.join(map(str, sorted(failures)))} fail", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom", type=Path, required=True)
    parser.add_argument("--work", type=Path, help="retain logs and private ablation modules in this directory")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    axiom = args.axiom.resolve()
    if args.work:
        work = args.work.resolve()
        work.mkdir(parents=True, exist_ok=True)
        check(axiom, repo, work)
    else:
        with tempfile.TemporaryDirectory(prefix="axiom-emitter-lines-") as directory:
            check(axiom, repo, Path(directory))


if __name__ == "__main__":
    main()
