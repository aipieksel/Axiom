#!/usr/bin/env python3
"""Pin present, absent and invalid compiler format-specifier results."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


CASES = ("", ".0", ">8", "^08.2X", "<20x", "q", ".", ".x", "8x.2",
         "99999999999999999999", ".99999999999999999999", "4.1.2")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compiler")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    compiler = str(Path(arguments.compiler).resolve())
    environment = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"),
                       AXIOM_PATH=str(root / "core"))
    golden = (root / "tests/tools/format-option-specifiers.golden").read_text()
    with tempfile.TemporaryDirectory(prefix="axiom-format-options-") as directory:
        work = Path(directory)
        for level in ("0", "2"):
            executable = work / ("format-options-O" + level)
            build = subprocess.run(
                [compiler, "--diagnostic-format=ai", "build",
                 str(root / "tests/tools/format-option-specifiers.axp"),
                 "--opt", level, "--alloc-poison", "-o", str(executable)],
                cwd=root, env=environment, capture_output=True, text=True, timeout=180)
            assert build.returncode == 0, (level, build.stdout, build.stderr)
            output = ""
            for index, case in enumerate(CASES, 1):
                result = subprocess.run([str(executable), case], cwd=root,
                                        capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, (level, case, result.returncode, result.stderr)
                assert not result.stderr, (level, case, result.stderr)
                output += f"case {index}\n" + result.stdout
            assert output == golden, (level, output)
        invalid = {
            "precision-sentinel": "(mkFmtSpec 62 0 0 -1 None)",
            "radix-sentinel": "(mkFmtSpec 62 0 0 None 0)",
            "radix-int-payload": "(mkFmtSpec 62 0 0 None (Some 120))",
            "erased-owner": "(fmtSpecWidth 0)",
        }
        for name, expression in invalid.items():
            source = work / (name + ".ax")
            source.write_text("(import expand)\n(:: main Int)\n(fn (main) { " + expression + " 0 })\n")
            checked = subprocess.run([compiler, "--diagnostic-format=ai", "check", str(source)],
                                     cwd=root, env=environment, capture_output=True,
                                     text=True, timeout=30)
            assert checked.returncode == 1 and "E AX3004 " in checked.stderr, (name, checked.stderr)
        invalid_values = {
            "alignment": "(mkFmtSpec -1 0 0 None None)",
            "zero-flag": "(mkFmtSpec 62 2 0 None None)",
            "radix": "(mkFmtSpec 62 0 0 None (Some (as 121 UInt8)))",
            "negative-precision": "(mkFmtSpec 62 0 0 (Some -1) None)",
        }
        for name, expression in invalid_values.items():
            source = work / (name + ".ax")
            source.write_text("(import expand)\n(:: main Int)\n(fn (main) { " + expression + " 0 })\n")
            executable = work / name
            build = subprocess.run([compiler, "--diagnostic-format=ai", "build", str(source),
                                    "-o", str(executable)], cwd=root, env=environment,
                                   capture_output=True, text=True, timeout=180)
            assert build.returncode == 0, (name, build.stdout, build.stderr)
            result = subprocess.run([str(executable)], cwd=root, capture_output=True,
                                    text=True, timeout=30)
            assert result.returncode == 80 and "precondition failed" in result.stderr, (
                name, result.returncode, result.stderr)
    print("ok   format Options: 12 grammar cases at O0/O2; 4 invalid types and 4 invalid values refused")


if __name__ == "__main__":
    main()
