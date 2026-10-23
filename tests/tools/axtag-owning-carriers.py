#!/usr/bin/env python3
"""Check owning annotation records, content/offset identity and the A packet codec."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile

BRIDGE = '''
(pub :: axtagProbeUnion (-> Axtags Axtags Axtags))
(pub fn (axtagProbeUnion first second) (smTagsUnion first second))
(pub :: axtagProbeLookup (-> SymMaps String (Option Axtags)))
(pub fn (axtagProbeLookup map key) (saGetVec map.tagN map.tagV key))
'''


def check(compiler, root, work, modules):
    owned = ("core", "lexer", "parser", "expand", "frontpack", "symbols", "nolint")
    for name in owned:
        assert (modules / (name + ".ax")).read_bytes() == (root / "core" / (name + ".ax")).read_bytes(), (name, "stale dependency module")
    source = {name: (root / "core" / (name + ".ax")).read_text() for name in owned}
    env = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"))
    report = {"builder": compiler, "builder_sha256": hashlib.sha256(Path(compiler).read_bytes()).hexdigest(),
              "sources": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in source.items()},
              "configurations": [], "malformed": [], "controls": [], "refusals": [],
              "limits": ["Focused host O0/O2 native execution with allocation poisoning; GitHub owns full suite and target validation.",
                         "Direct typed annotation copies and unions have strict ownership/cleanup controls. Packed AST projections retain their separately recorded conservative-retention limitation.",
                         "Packet schema is unchanged; malformed required text, offsets and collection lengths fail to serial-parser fallback.",
                         "No lifetime algorithms, held work, or global memory soundness/completion claim."]}
    positive = work / "positive-modules"; positive.mkdir(exist_ok=True)
    (positive / "symbols.ax").write_text(source["symbols"] + BRIDGE)
    env["AXIOM_PATH"] = os.pathsep.join((str(positive), str(modules)))
    def invoke(args, label, local=env, timeout=240):
        ran = subprocess.run(args, cwd=root, env=local, capture_output=True, text=True, timeout=timeout)
        (work / (label + ".out")).write_text(ran.stdout); (work / (label + ".err")).write_text(ran.stderr)
        return ran
    def build(name, level, local=env, suffix=""):
        binary = work / (name + "-O" + level + suffix)
        ran = invoke([compiler, "--diagnostic-format=ai", "build", str(root / "tests/tools" / (name + ".axp")),
                      "--opt", level, "--alloc-poison", "-o", str(binary)], binary.name + "-build", local)
        assert ran.returncode == 0, (name, level, ran.stdout, ran.stderr)
        return binary
    expected = "".join(f"{i} true\n" for i in range(1, 41))
    golden = (root / "tests/tools/axtag-carrier-packets.golden").read_text()
    for level in ("0", "2"):
        for name, want in (("axtag-owning-carriers", expected), ("axtag-carrier-packets", golden)):
            binary = build(name, level)
            ran = invoke([str(binary)], binary.name + "-run", timeout=60)
            assert ran.returncode == 0 and not ran.stderr and ran.stdout == want, (name, level, ran.returncode, ran.stdout, ran.stderr)
        report["configurations"].append({"opt": level, "allocation_poisoning": True, "outcomes": 40,
                                         "wire_bytes": sum(len(line) // 2 for line in golden.splitlines()), "status": "passed"})
        print(f"ok   owning annotations: 40 presence/empty/offset/copy/union/contract/nolint outcomes and exact packets O{level} poison", flush=True)

    # Derive offsets from the actual unchanged packet schema, including
    # nested macro rules and field declaration discriminators.
    shapes = {int(tag): shape for tag, shape in re.findall(r'\((\d+) "([wsnNSWP]+)"\)', source["frontpack"])}
    def tag(name):
        return int(re.search(r'\(pub fn \(' + re.escape(name) + r'\)\s+(\d+)\)', source["parser"])[1])
    dmacro, dsig, ddata, dstruct, ctor, field = map(tag, ("TAG_D_MACRO", "TAG_D_SIG", "TAG_D_DATA", "TAG_D_STRUCT", "TAG_CTOR", "TAG_FIELD"))
    mutants = []
    valid = []
    for packet_index, line in enumerate(golden.splitlines()):
        data = bytes.fromhex(line); position = 0; annotations = []; vectors = []
        def word():
            nonlocal position
            result = struct.unpack_from("<q", data, position)[0]; position += 8; return result
        def text():
            nonlocal position
            length = word()
            if length >= 0: position += (length + 7) // 8 * 8
        def vector(kind):
            count = word()
            for _ in range(max(0, count)): value(kind)
        def value(kind):
            if kind in "wp": word()
            elif kind == "s": text()
            elif kind in "NSWP": vector({"N": "n", "S": "s", "W": "w", "P": "p"}[kind])
            elif kind == "R": vector("r")
            elif kind == "r":
                for child in "SNNSWLn": value(child)
            elif kind == "L": value("S"); value("S")
            elif kind == "A":
                at = position; count = word(); vectors.append(at)
                for _ in range(max(0, count)):
                    off_at = position; word(); content_at = position; text()
                    annotations.append((off_at, content_at))
            elif kind == "n":
                header = word()
                if header == -1: return
                node_tag, mask = header & 127, header >> 7
                for slot in range(1, 10):
                    if not mask & (1 << (slot - 1)): continue
                    if node_tag == field and slot in (2, 3): child = chr(word())
                    elif slot <= 3: child = shapes[node_tag][slot - 1]
                    elif slot == 4: child = "p"
                    elif slot == 6: child = "S" if node_tag in (ddata, dstruct) else "N" if node_tag == ctor else "n" if node_tag in (dsig, field) else "w"
                    elif slot == 7: child = "A"
                    elif slot == 9: child = "R" if node_tag == dmacro else "S" if node_tag == ctor else "w"
                    else: child = "w"
                    value(child)
            else: raise AssertionError(kind)
        word(); source_bytes = struct.unpack_from("<q", data, position)[0]; text(); vector("n")
        assert position == len(data), (packet_index, position, len(data))
        valid.append((f"valid-{packet_index}", data))
        for vector_index, offset in enumerate(vectors):
            for label, number in (("null", -1), ("oversized", (1 << 63) - 1)):
                changed = bytearray(data); struct.pack_into("<q", changed, offset, number)
                mutants.append((f"packet{packet_index}-vector{vector_index}-{label}", bytes(changed)))
        for entry, (off_at, content_at) in enumerate(annotations):
            for label, offset, number in (("negative-offset", off_at, -1), ("outside-source", off_at, source_bytes + 1),
                                          ("null-content", content_at, -1), ("oversized-content", content_at, (1 << 63) - 1)):
                changed = bytearray(data); struct.pack_into("<q", changed, offset, number)
                mutants.append((f"packet{packet_index}-entry{entry}-{label}", bytes(changed)))
        for cut in range(0, len(data), 8): mutants.append((f"packet{packet_index}-cut{cut}", data[:cut]))
        mutants.append((f"packet{packet_index}-trailing", data + bytes(8)))
    decoder = build("macro-record-decode", "0")
    for label, data in valid + mutants:
        path = work / (label + ".bin"); path.write_bytes(data)
        ran = invoke([str(decoder), str(path)], label, timeout=30)
        want = "same\n" if label.startswith("valid-") else "fallback\n"
        assert ran.returncode == 0 and not ran.stderr and ran.stdout == want, (label, ran.returncode, ran.stdout, ran.stderr)
        if want == "fallback\n": report["malformed"].append(label)
    print(f"ok   A packets: {len(valid)} valid states round-trip; {len(mutants)} malformed/truncated packets return fallback", flush=True)

    ownership = "(axtagSliceInto v axtags from upto)\n      v"
    assert source["parser"].count(ownership) == 1, "copy ownership seam changed"
    empty = "(!= (as content Int) 0)"
    assert source["frontpack"].count(empty) == 1, "required text seam changed"
    absent = "((None) 0)\n    ((Some present) (expGiveTagsIn d made present 0))"
    assert source["expand"].count(absent) == 1, "invocation absence seam changed"
    controls = {
        "unmapped-copy-owner": ("parser", source["parser"].replace(ownership, ownership.replace("\n      v", "\n      (memMarkLeaf (as v Int))\n      v")), {13}),
        "reject-present-empty-text": ("frontpack", source["frontpack"].replace(empty, "(&& " + empty + " (> (strLen content) 0))"), {7}),
        "erase-invocation-absence": ("expand", source["expand"].replace(absent, absent.replace("((None) 0)", "((None) (expGiveTagsIn d made vecNew 0))")), {28}),
    }
    for name, (module, changed, required) in controls.items():
        local_modules = work / (name + "-modules"); local_modules.mkdir(exist_ok=True)
        (local_modules / (module + ".ax")).write_text(changed)
        local = dict(env, AXIOM_PATH=os.pathsep.join((str(local_modules), str(positive), str(modules))))
        binary = build("axtag-owning-carriers", "0", local, "-" + name)
        ran = invoke([str(binary)], name + "-run", local, 60)
        assert ran.returncode == 0 and not ran.stderr, (name, ran.returncode, ran.stderr)
        failed = {int(line.split()[0]) for line in ran.stdout.splitlines() if line.endswith(" false")}
        assert required <= failed, (name, failed)
        report["controls"].append({"name": name, "failed_cases": sorted(failed)})
        print(f"ok   counterexample {name}: cases {sorted(failed)} fail", flush=True)
    invalid = {
        "raw-entry": ('(:: bad (-> Int String)) (fn (bad tag) (contractKeyOf tag))', "AX3004"),
        "raw-collection": ('(:: bad (-> Ints Int)) (fn (bad tags) (contractTagsInto vecNew tags))', "AX3004"),
        "raw-node-payload": ('(:: bad (-> ASTNode Ints Int)) (fn (bad node tags) (writeNodeAxtags node tags))', "AX3004"),
        "missing-tags-none": ('(:: bad (-> ASTNode Axtags)) (fn (bad node) (match (nodeAxtagsOpt node) ((Some tags) tags)))', "AX3005"),
        "raw-signature-query": ('(:: bad (-> Nodes Axtags)) (fn (bad sigs) (contractSigTags sigs "f" 0))', "AX3004"),
        "scalar-content": ('(:: bad AxtagEnt) (fn (bad) (AxtagEnt :off 0 :content 7))', "AX3004"),
    }
    for name, (decl, code) in invalid.items():
        path = work / (name + ".ax"); path.write_text("(import core) (import parser)\n" + decl + "\n(:: main Int) (fn (main) 0)\n")
        ran = invoke([compiler, "--diagnostic-format=ai", "check", str(path)], name)
        errors = [line for line in ran.stderr.splitlines() if line.startswith("E ")]
        assert ran.returncode == 1 and len(errors) == 1 and f"E {code} " in errors[0], (name, ran.stdout, ran.stderr)
        report["refusals"].append({"name": name, "code": code})
    print("ok   annotation carriers: six erased/scalar/nonexhaustive contracts refused", flush=True)
    (work / "axtag-carrier-report.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("compiler")
    parser.add_argument("--work", type=Path); parser.add_argument("--dependency-core", type=Path)
    args = parser.parse_args(); root = Path(__file__).resolve().parents[2]
    compiler = str(Path(args.compiler).resolve()); modules = (args.dependency_core or root / "core").resolve()
    if args.work:
        args.work.mkdir(exist_ok=True, parents=True); check(compiler, root, args.work.resolve(), modules)
    else:
        with tempfile.TemporaryDirectory(prefix="axiom-axtag-carriers-") as directory:
            check(compiler, root, Path(directory), modules)


if __name__ == "__main__":
    main()
