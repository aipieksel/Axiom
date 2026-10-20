#!/usr/bin/env python3
"""Execute the Windows path rules on the host, without Windows kernel calls."""
import argparse
import json
import ntpath
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    axiom = str(Path(args.axiom).resolve())
    source = (root / "stdlib/Path.ax").read_text()
    capability = "(:: pathBackslashSeparates Int)\n(fn (pathBackslashSeparates) 1)"
    assert source.count("(import Sys.Platform)") == 1
    source = source.replace("(import Sys.Platform)", capability)
    fixture = (root / "tests/stdlib/980-path-roots.ax").read_text()
    fixture = fixture.replace("(import Path)", "(import WindowsPath)").replace(
        "(import Sys.Platform (pathBackslashSeparates))", capability)
    golden = (root / "tests/stdlib/980-path-roots.out").read_bytes()
    cases = len(golden.splitlines())
    with tempfile.TemporaryDirectory(prefix="axiom-windows-paths-") as tmp:
        work = Path(tmp)
        module = work / "WindowsPath.ax"
        module.write_text(source)
        probe = work / "probe.ax"
        probe.write_text(fixture)
        env = dict(os.environ, AXIOM_STDLIB=str(root / "stdlib"), AXIOM_PATH=str(work))

        def check(level):
            binary = work / f"probe-O{level}"
            subprocess.run([axiom, "--diagnostic-format=ai", "build", str(probe),
                            "--opt", str(level), "-o", str(binary)],
                           check=True, env=env, stdout=subprocess.DEVNULL)
            result = subprocess.run([str(binary)], capture_output=True, timeout=10)
            if result.returncode != 0:
                raise SystemExit(f"FAIL Windows path rules: exit {result.returncode}")
            return result.stdout

        for level in (0, 2):
            if check(level) != golden:
                raise SystemExit(f"FAIL Windows path rules at -O{level}")
            print(f"ok   Windows path roots, joins and cleaning: {cases} cases at -O{level} on the host")

        # ntpath independently specifies how drive-relative names,
        # current-drive roots and UNC shares combine. Compare path
        # spelling after separator normalisation: Path joins with `/`.
        dirs = ("", "base", "C:", "C:base", "C:/base", "D:/base",
                "//server/share", "//server/share/base")
        names = ("", "work", "C:work", "c:work", "D:work", "/work",
                 "\\work", "C:/work", "//other/share/work")
        clean = ("C:foo/../bar", "C:foo/../../bar", "C:", "C:.", "C:..",
                 "C:../a", "C:/a/../../b", "C:\\a\\..\\b", "//server/share/a/../../b",
                 "//server/share", "C:foo//bar", "C:/", "\\a\\..\\b")
        expressions = []
        expected = []
        for directory in dirs:
            for name in names:
                expressions.append(f"(println (WindowsPath::pathJoin {json.dumps(directory)} {json.dumps(name)}))")
                expected.append(ntpath.join(directory, name).replace("\\", "/"))
        for name in clean:
            expressions.append(f"(println (WindowsPath::pathClean {json.dumps(name)}))")
            expected.append(ntpath.normpath(name).replace("\\", "/"))
        model_fixture = "(import IO)\n(import WindowsPath)\n(:: main ())\n;@axiom:effect(io)\n" + \
            "(fn (main) {\n" + "\n".join(expressions) + "\n})\n"
        probe.write_text(model_fixture)
        for level in (0, 2):
            actual = [line.replace("\\", "/") for line in check(level).decode().splitlines()]
            if actual != expected:
                for i, (got, want) in enumerate(zip(actual, expected)):
                    if got != want:
                        raise SystemExit(f"FAIL Windows path ntpath model -O{level}, {expressions[i]}: {got!r}, expected {want!r}")
                raise SystemExit("FAIL Windows path ntpath model: output length changed")
            print(f"ok   Windows path rules match ntpath for {len(expected)} drive/share combinations at -O{level}")
        probe.write_text(fixture)

        # Break drive recognition without changing the fixture or its
        # golden. A build failure is not a successful negative probe.
        anchor = "(== (strByte p 1) 58)"
        assert source.count(anchor) == 1
        module.write_text(source.replace(anchor, "(== (strByte p 1) 59)"))
        output = check(2)
        if len(output.splitlines()) != cases or b"false\n" not in output:
            raise SystemExit("FAIL Windows path negative probe did not expose the broken drive root")
        print("ok   ablating drive recognition changes the Windows path answers")


if __name__ == "__main__":
    main()
