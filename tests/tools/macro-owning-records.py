#!/usr/bin/env python3
"""Check owning macro products, exact packet/render bytes and malformed fallback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile


BRIDGE = r"""
(pub :: macroPacketDepthProbe Bool)
(pub fn (macroPacketDepthProbe)
  {
    (let out (frontWriter 0))
    (frontPutMacroRules out vecNew 513)
    (let packet (frontWriter 0))
    (frontPutWord packet 0)
    (let input (FrontReader :bytes (strSlice packet.bytes 0 packet.pos) :pos 0 :good true :sourceBytes 0))
    (&& (! out.good)
      (&& (isNone (frontGetMacroNames input 513))
        (&& (isNone (frontGetMacroNodes input 513))
          (isNone (frontGetMacroRules input 513)))))
  })
"""

def check(compiler, root, work):
    env = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"), AXIOM_PATH=str(root / "core"))
    report = {"builder": compiler, "builder_sha256": hashlib.sha256(Path(compiler).read_bytes()).hexdigest(),
              "sources": {name: hashlib.sha256((root / "core" / (name + ".ax")).read_bytes()).hexdigest()
                          for name in ("core", "parser", "expand", "frontpack", "lsp")},
              "configurations": [], "malformed": [], "controls": [], "refusals": [], "observations": [],
              "limits": ["Host O0/O2 allocation-poisoned native probes; GitHub owns the full suite and target coverage.",
                         "Packed AST read projections can conservatively retain an original node graph; this gate observes that boundary separately from strict typed-product cleanup.",
                         "Renderer compatibility preserves its existing source spelling, including its lossy rendering of literal/repeated pattern shapes.",
                         "No lifetime algorithms, held work, or global memory soundness claim."]}

    def invoke(args, label, local=env, timeout=240):
        result = subprocess.run(args, cwd=root, env=local, capture_output=True, text=True, timeout=timeout)
        (work / (label + ".out")).write_text(result.stdout)
        (work / (label + ".err")).write_text(result.stderr)
        return result

    def build(name, level, local=env, suffix=""):
        executable = work / (name + "-O" + level + suffix)
        result = invoke([compiler, "--diagnostic-format=ai", "build", str(root / "tests/tools" / (name + ".axp")),
                         "--opt", level, "--alloc-poison", "-o", str(executable)], executable.name + "-build", local)
        assert result.returncode == 0, (name, level, result.stdout, result.stderr)
        return executable

    positive = work / "positive-modules"; positive.mkdir(exist_ok=True)
    (positive / "frontpack.ax").write_text((root / "core/frontpack.ax").read_text() + BRIDGE)
    env["AXIOM_PATH"] = str(positive) + os.pathsep + str(root / "core")
    expected = "".join(f"{i} true\n" for i in range(1, 38))
    packet_lines = (root / "tests/tools/macro-record-packets.golden").read_text()
    render_lines = (root / "tests/tools/macro-record-render.golden").read_text()
    diagnostic_lines = (root / "tests/tools/macro-record-diagnostics.golden").read_text()
    for level in ("0", "2"):
        for name, want in (("macro-owning-records", expected), ("macro-record-packets", packet_lines),
                           ("macro-record-render", render_lines), ("macro-record-diagnostics", diagnostic_lines)):
            binary = build(name, level)
            ran = invoke([str(binary)], binary.name + "-run", timeout=60)
            assert ran.returncode == 0 and not ran.stderr and ran.stdout == want, (
                name, level, ran.returncode, ran.stdout, ran.stderr)
        observed = build("macro-record-retention", level)
        ran = invoke([str(observed)], observed.name + "-run", timeout=60)
        assert ran.returncode == 0 and not ran.stderr, (ran.returncode, ran.stderr)
        counts = [int(line) for line in ran.stdout.splitlines()]
        assert len(counts) == 3 and counts[0] >= 1 and counts[1] >= counts[0] and counts[2] >= counts[1], counts
        report["observations"].append({"name": "existing-packed-name-getter", "opt": level, "counts": counts,
                                        "returned_to_baseline": counts[-1] == counts[0],
                                        "status": "observed; cleanup not certified"})
        report["configurations"].append({"opt": level, "allocation_poisoning": True, "product_outcomes": 37,
                                         "packet_wire_bytes": sum(len(line) // 2 for line in packet_lines.splitlines()),
                                         "renderer_bytes": len(render_lines.encode()), "diagnostic_bytes": len(diagnostic_lines.encode()), "status": "passed"})
        print(f"ok   macro products: 37 outcomes and exact packet/renderer bytes at O{level} poison", flush=True)
        print(f"obs  packed getter retention O{level}: {counts}; separate memory gate remains open", flush=True)

    # Walk the actual packet schema to mutate required R/L vector counts.
    # Offsets are derived from the golden bytes rather than baked into fixtures.
    shapes = {int(tag): shape for tag, shape in re.findall(r'\((\d+) "([wsnNSWP]+)"\)', (root / "core/frontpack.ax").read_text())}
    source = (root / "core/parser.ax").read_text()
    def tag(name):
        return int(re.search(r'\(pub fn \(' + re.escape(name) + r'\)\s+(\d+)\)', source)[1])
    dmacro, dsig, ddata, dstruct, ctor, field = map(tag, ("TAG_D_MACRO", "TAG_D_SIG", "TAG_D_DATA", "TAG_D_STRUCT", "TAG_CTOR", "TAG_FIELD"))
    mutated = []
    for index, line in enumerate(packet_lines.splitlines()):
        data = bytes.fromhex(line)
        position = 0
        counts = []
        def word():
            nonlocal position
            value = struct.unpack_from("<q", data, position)[0]
            position += 8
            return value
        def text():
            nonlocal position
            length = word()
            if length >= 0:
                position += (length + 7) // 8 * 8
        def vector(kind, label=None):
            at = position
            count = word()
            if label:
                counts.append((label, at))
            for _ in range(max(0, count)):
                value(kind)
        def rule():
            for kind, label in (("S", "params"), ("N", "declarations"), ("N", "patterns"),
                                ("S", "spellings"), ("W", "repeats"), ("S", "declared-literals"), ("S", "used-literals")):
                vector(kind.lower() if kind in "NS" else "w", label)
            value("n")
        def value(kind):
            if kind in "wp":
                word()
            elif kind == "s":
                text()
            elif kind == "R":
                count = word()
                for _ in range(max(0, count)):
                    rule()
            elif kind in "NSWP":
                vector({"N": "n", "S": "s", "W": "w", "P": "p"}[kind])
            elif kind == "n":
                header = word()
                if header == -1:
                    return
                node_tag, mask = header & 127, header >> 7
                for slot in range(1, 10):
                    if not mask & (1 << (slot - 1)):
                        continue
                    if slot <= 3:
                        child = shapes[node_tag][slot - 1]
                    elif slot == 4:
                        child = "p"
                    elif slot == 6:
                        child = "S" if node_tag in (ddata, dstruct) else "N" if node_tag == ctor else "n" if node_tag in (dsig, field) else "w"
                    elif slot == 9:
                        child = "R" if node_tag == dmacro else "S" if node_tag == ctor else "w"
                    else:
                        child = "w"
                    value(child)
            else:
                raise AssertionError(kind)
        word(); text(); vector("n")
        assert position == len(data), (position, len(data))
        for label, offset in counts:
            changed = bytearray(data)
            struct.pack_into("<q", changed, offset, -1)
            mutated.append((f"packet{index}-{label}-null", bytes(changed)))
            oversized = bytearray(data)
            struct.pack_into("<q", oversized, offset, (1 << 63) - 1)
            mutated.append((f"packet{index}-{label}-oversized", bytes(oversized)))
    decoder = build("macro-record-decode", "0")
    for index, line in enumerate(packet_lines.splitlines()):
        path = work / f"valid-packet{index}.bin"; path.write_bytes(bytes.fromhex(line))
        ran = invoke([str(decoder), str(path)], path.stem)
        assert ran.returncode == 0 and ran.stdout == "same\n" and not ran.stderr, (path, ran.stdout, ran.stderr)
    for name, data in mutated:
        path = work / (name + ".bin"); path.write_bytes(data)
        ran = invoke([str(decoder), str(path)], name)
        assert ran.returncode == 0 and ran.stdout == "fallback\n" and not ran.stderr, (name, ran.returncode, ran.stdout, ran.stderr)
        report["malformed"].append(name)
    print(f"ok   malformed R/L packets: {len(mutated)} null/oversized required vectors refuse without a crash", flush=True)

    codec = (root / "core/frontpack.ax").read_text()
    expression = re.compile(r":expression\s+\(if \(nodeSame expression nilNode\)\s+None\s+\(Some expression\)\)")
    assert len(expression.findall(codec)) == 1, "expression decoding seam changed"
    mutants = {"discard-decoded-expression": ("frontpack", expression.sub(":expression None", codec), {26, 27, 29})}
    parser = (root / "core/parser.ax").read_text()
    rewrite = "(MacroPatternRewrite :patterns pats2 :names names2 :used used)"
    assert parser.count(rewrite) == 1, "literal rewrite seam changed"
    mutants["discard-used-literals"] = ("parser", parser.replace(rewrite, rewrite.replace(":used used", ":used vecNew")), {15})
    for name, (module, changed, required) in mutants.items():
        modules = work / (name + "-modules"); modules.mkdir(exist_ok=True)
        (modules / (module + ".ax")).write_text(changed + (BRIDGE if module == "frontpack" else ""))
        local = dict(env, AXIOM_PATH=os.pathsep.join((str(modules), str(positive), str(root / "core"))))
        binary = build("macro-owning-records", "0", local, "-" + name)
        ran = invoke([str(binary)], name + "-run", local, 60)
        assert ran.returncode == 0 and not ran.stderr, (name, ran.returncode, ran.stderr)
        failed = {int(line.split()[0]) for line in ran.stdout.splitlines() if line.endswith(" false")}
        assert required <= failed, (name, failed)
        report["controls"].append({"name": name, "failed_cases": sorted(failed)})
        print(f"ok   counterexample {name}: cases {sorted(failed)} fail", flush=True)

    invalid = {
        "mixed-rule-row": ('(:: bad (-> Ints Nodes)) (fn (bad rule) (expRuleTpl rule))', "AX3004"),
        "mixed-rule-list": ('(:: bad (-> IntRows Nodes ExpMatchContext (Option ExpMatch))) (fn (bad rules args context) (expRuleForForms rules args 0 context))', "AX3004"),
        "erased-rule-option": ('(:: bad (-> ASTNode MacroRules)) (fn (bad node) (nodeMacroRules node))', "AX3004"),
        "missing-rule-none": ('(:: bad (-> ASTNode MacroRules)) (fn (bad node) (match (nodeMacroRules node) ((Some rules) rules)))', "AX3005"),
        "raw-expression": ('(:: bad (-> MacroRule ASTNode)) (fn (bad rule) (expRuleExpr rule))', "AX3004"),
        "mixed-rewrite": ('(:: bad (-> Nodes Names Ints)) (fn (bad patterns names) (rewriteRuleLits patterns names vecNew))', "AX3004"),
    }
    for name, (decl, code) in invalid.items():
        path = work / (name + ".ax")
        path.write_text("(import core) (import parser) (import expand) (import Vec)\n" + decl + "\n(:: main Int) (fn (main) 0)\n")
        ran = invoke([compiler, "--diagnostic-format=ai", "check", str(path)], name)
        errors = [line for line in ran.stderr.splitlines() if line.startswith("E ")]
        assert ran.returncode == 1 and len(errors) == 1 and f"E {code} " in errors[0], (name, ran.stdout, ran.stderr)
        report["refusals"].append({"name": name, "code": code})
    print("ok   macro typing: 6 mixed/erased/nonexhaustive contracts refused", flush=True)
    (work / "macro-record-report.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compiler")
    parser.add_argument("--work", type=Path)
    args = parser.parse_args()
    compiler = str(Path(args.compiler).resolve())
    root = Path(__file__).resolve().parents[2]
    if args.work:
        args.work.mkdir(exist_ok=True, parents=True)
        check(compiler, root, args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="axiom-macro-records-") as directory:
            check(compiler, root, Path(directory))


if __name__ == "__main__":
    main()
