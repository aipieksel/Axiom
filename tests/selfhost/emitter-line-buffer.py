#!/usr/bin/env python3
"""Exercise the actual emitter line-buffer ownership with focused ablations.

Only the copied codegen module changes in negative probes. Each probe is an
ordinary executable fixture, not a rebuilt compiler or bootstrap ladder.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


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


def check(axiom, repo, work):
    fixture = repo / "tests/selfhost/emitter-line-buffer.ax"
    expected = (repo / "tests/selfhost/emitter-line-buffer.out").read_text().splitlines()
    if expected != [f"{i} true" for i in range(1, 18)]:
        raise RuntimeError("update the explicit 17-case assertion count")
    env = dict(os.environ, AXIOM_PATH=str(repo / "self_host"), AXIOM_STDLIB=str(repo / "stdlib"))

    def build_run(label, level, module_root, negative=()):
        binary = work / label
        local_env = dict(env, AXIOM_PATH=str(module_root))
        run_checked(
            [str(axiom), "build", str(fixture), "--opt", str(level), "--alloc-poison",
             "--diagnostic-format=ai", "-o", str(binary)],
            env=local_env, stdout=work / f"{label}.build.stdout", stderr=work / f"{label}.build.stderr",
        )
        output = work / f"{label}.stdout"
        run_checked([str(binary)], env=local_env, stdout=output, stderr=work / f"{label}.stderr", timeout=30)
        return check_output(output, expected, required_failures=negative)

    for level in (0, 2):
        build_run(f"positive-O{level}", level, repo / "self_host")
        print(f"ok   emitter line-buffer ownership: 17 cases at -O{level} with allocation poisoning", flush=True)

    original = (repo / "self_host/codegen.ax").read_text()
    variants = []
    variants.append(("header-share", replace_once(
        original,
        '    (vecPush (memGetWordVec cg 0) (strDup line))\n    cg',
        '    (let updated (vecPush (memGetWordVec cg 0) (strDup line)))\n'
        '    (memSetWord cg 0 updated)\n    cg',
        "same-header share ablation"), (2, 4)))
    variants.append(("mutable-snapshot", replace_once(
        original,
        '(vecPush (memGetWordVec cg 0) (strDup line))',
        '(vecPush (memGetWordVec cg 0) line)',
        "snapshot ablation"), (1, 5)))
    alloca = replace_once(
        original,
        '(vecSet (memGetWordVec cg 0) mark (joinAllocas hoisted))',
        '(vecSet (memGetWordVec cg 0) mark (as (joinAllocas hoisted) Int))',
        "alloca element metadata ablation")
    alloca = replace_once(alloca, '(as 0 (Vec String)))\n          (memSetWord cg 43',
                          '(as 0 (Vec Int)))\n          (memSetWord cg 43', "alloca branch type")
    variants.append(("alloca-metadata", alloca, (11,)))
    displaced = replace_once(
        original, '          (__release oldLines)\n', '', "displaced CG-slot share ablation")
    # This newly added tag describes the removed primitive. Keep the
    # private negative module truthful so the executable, not AX3010,
    # demonstrates that the fixture detects the retained slot share.
    displaced = replace_once(
        displaced,
        '(pub :: pruneDeadDefs (-> Int Int))\n;@axiom:effect(io)\n;@axiom:effect(unsafe)\n',
        '(pub :: pruneDeadDefs (-> Int Int))\n;@axiom:effect(io)\n',
        "displaced-slot primitive effect")
    variants.append(("displaced-slot", displaced, (14, 15)))

    for label, source, negative in variants:
        module_root = work / f"{label}-modules"
        module_root.mkdir(exist_ok=True)
        for path in (repo / "self_host").iterdir():
            destination = module_root / path.name
            if path.name == "codegen.ax":
                destination.write_text(source)
            elif not destination.exists():
                destination.symlink_to(path, target_is_directory=path.is_dir())
        failures = build_run(label, 0, module_root, negative)
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
