#!/usr/bin/env python3
"""Check owned generated origins and optional attribution."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compiler")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    compiler = str(Path(arguments.compiler).resolve())
    environment = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"),
                       AXIOM_PATH=str(root / "core"))
    expected = "".join(f"{index} true\n" for index in range(1, 25))
    golden = (root / "tests/tools/expansion-origin-options.golden").read_text()
    with tempfile.TemporaryDirectory(prefix="axiom-expansion-origin-options-") as directory:
        work = Path(directory)
        for level in ("0", "2"):
            for fixture, output in (("expansion-origin-options", expected),
                                    ("expansion-origin-symbols", golden)):
                executable = work / (fixture + "-O" + level)
                build = subprocess.run(
                    [compiler, "--diagnostic-format=ai", "build",
                     str(root / "tests/tools" / (fixture + ".axp")),
                     "--opt", level, "--alloc-poison", "-o", str(executable)],
                    cwd=root, env=environment, capture_output=True, text=True, timeout=180)
                assert build.returncode == 0, (level, fixture, build.stdout, build.stderr)
                result = subprocess.run([str(executable)], cwd=root, capture_output=True,
                                        text=True, timeout=30)
                assert result.returncode == 0, (level, fixture, result.returncode, result.stderr)
                assert not result.stderr, (level, fixture, result.stderr)
                assert result.stdout == output, (level, fixture, result.stdout)
        invalid = {
            "nullable-lookup": '(:: erased (-> GeneratedOrigins String)) (fn (erased origins) (expGenOf origins "generated"))',
            "raw-origin-rows": '(:: erased (-> Ints (Option String))) (fn (erased origins) (expGenOf origins "generated"))',
            "numeric-name": '(:: erased GeneratedOrigin) (fn (erased) (GeneratedOrigin :name 3 :macro "maker"))',
            "numeric-macro": '(:: erased GeneratedOrigin) (fn (erased) (GeneratedOrigin :name "generated" :macro 3))',
        }
        for name, declaration in invalid.items():
            source = work / (name + ".ax")
            source.write_text("(import core)\n(import expand)\n" + declaration + "\n(:: main Int) (fn (main) 0)\n")
            checked = subprocess.run([compiler, "--diagnostic-format=ai", "check", str(source)],
                                     cwd=root, env=environment, capture_output=True,
                                     text=True, timeout=30)
            assert checked.returncode == 1 and "E AX3004 " in checked.stderr, (name, checked.stderr)
        original = (root / "core/expand.ax").read_text()
        controls = (
            ("discard-present", "None", 2),
            ("substitute-name", "(Some origin.name)", 2),
            ("lose-empty", '(if (strEq origin.macro "") None (Some origin.macro))', 3),
        )
        for name, replacement, witness in controls:
            needle = "(Some origin.macro)"
            assert original.count(needle) == 1
            overlay = work / (name + "-core")
            overlay.mkdir()
            for path in (root / "core").iterdir():
                if path.name == "expand.ax":
                    (overlay / path.name).write_text(original.replace(needle, replacement))
                else:
                    (overlay / path.name).symlink_to(path, target_is_directory=path.is_dir())
            executable = work / name
            build = subprocess.run([compiler, "--diagnostic-format=ai", "build",
                                    str(root / "tests/tools/expansion-origin-options.axp"),
                                    "--opt", "0", "--alloc-poison", "-o", str(executable)],
                                   cwd=root, env=dict(environment, AXIOM_PATH=str(overlay)),
                                   capture_output=True, text=True, timeout=240)
            assert build.returncode == 0, (name, build.returncode, build.stderr)
            result = subprocess.run([str(executable)], cwd=root, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0 and not result.stderr, (name, result.returncode, result.stderr)
            rows = result.stdout.splitlines()
            assert len(rows) == 24, (name, result.stdout)
            failed = [i for i, row in enumerate(rows, 1) if row != f"{i} true"]
            assert witness in failed, (name, failed)
            print(f"ok   {name}: cases {failed} detect the mutation", flush=True)
        source = work / "missing-none.ax"
        source.write_text('(import core)\n(import expand)\n(:: bad (-> GeneratedOrigins String))\n'
                          '(fn (bad origins) (match (expGenOf origins "x") ((Some value) value)))\n')
        checked = subprocess.run([compiler, "--diagnostic-format=ai", "check", str(source)],
                                 cwd=root, env=environment, capture_output=True, text=True, timeout=30)
        errors = [line for line in checked.stderr.splitlines() if line.startswith("E ")]
        assert checked.returncode == 1 and len(errors) == 1 and "E AX3005 " in errors[0], checked.stderr
    print("ok   generated origins: 24 owner/missing/empty/qualification/LSP cases O0/O2; exact AXSYM; 4 erased API refusals; 3 effective controls; missing None refused")


if __name__ == "__main__":
    main()
