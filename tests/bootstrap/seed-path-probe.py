#!/usr/bin/env python3
"""Check the historical/current Path provenance ablation on private copies."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
LEGACY = """(import Str)
(pub :: pathIsAbsolute (-> String Bool))
(pub fn (pathIsAbsolute p)
  (&& (> (strLen p) 0) (== (strByte p 0) 47)))
(pub :: anotherPredicate (-> String Bool))
(pub fn (anotherPredicate p)
  (&& (> (strLen p) 0) (== (strByte p 0) 47)))
"""


def mutate(path, good=True):
    p = subprocess.run(["bash", "-c", 'source "$1"; seed_probe_path "$2"', "probe",
                        str(ROOT / "scripts/lib/seed-emit.sh"), str(path)],
                       text=True, capture_output=True, timeout=10)
    assert (p.returncode == 0) == good, p.stderr


def exercise(work, name, source, axiom):
    path = work / "ProbePath.ax"
    path.write_text(source)
    before = path.read_bytes()
    mutate(path)
    after = path.read_bytes()
    assert len(before) == len(after)
    assert sum(a != b for a, b in zip(before, after)) == 1
    if name == "historical":
        assert after.count(b"(== (strByte p 0) 47)") == 1
    if axiom:
        probe = work / "probe.ax"
        probe.write_text("""(import ProbePath)
(:: main Int)
(fn (main)
  (if (ProbePath::pathIsAbsolute "/input") 0 7))
""")
        env = dict(os.environ, AXIOM_STDLIB=str(ROOT / "stdlib"), AXIOM_PATH=str(work))
        for level in (0, 2):
            for text, expected in ((before, 0), (after, 7)):
                path.write_bytes(text)
                binary = work / f"probe-O{level}"
                subprocess.run([axiom, "--diagnostic-format=ai", "build", str(probe),
                                "--opt", str(level), "-o", str(binary)],
                               env=env, check=True, stdout=subprocess.DEVNULL)
                result = subprocess.run([str(binary)], timeout=10)
                assert result.returncode == expected, (name, level, result.returncode, expected)
    print(f"ok   {name} Path provenance mutation changes exactly one byte" +
          (" and changes the executed predicate at O0/O2" if axiom else ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom", help="also compile and execute both source layouts")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="axiom-seed-path-probe-") as tmp:
        work = Path(tmp)
        exercise(work, "historical", LEGACY, args.axiom)
        exercise(work, "current", (ROOT / "stdlib/Path.ax").read_text(), args.axiom)
        missing = work / "Unknown.ax"
        missing.write_text("; no recognised separator predicate\n")
        before = missing.read_bytes()
        mutate(missing, good=False)
        assert missing.read_bytes() == before
        print("ok   an unknown Path layout is refused without changing its bytes")
