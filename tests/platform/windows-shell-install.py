#!/usr/bin/env python3
"""Exercise the Windows bash dispatcher with process-boundary mocks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="axiom-windows-dispatch-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.write("uname", '#!/bin/sh\ncase "$1" in -s) echo MINGW64_NT-10.0 ;; -m) echo x86_64 ;; esac\n')
        self.write("cygpath", '#!/bin/sh\nprintf "%s\\n" "$2"\n')
        self.capture = self.work / "args.json"
        self.env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", AXIOM_TEST_ARGS=str(self.capture))
        for key in ("AXIOM_PREFIX", "AXIOM_VERSION"):
            self.env.pop(key, None)

    def write(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def powershell(self, name="pwsh"):
        self.write(name, f'#!{sys.executable}\nimport json,os,sys\nopen(os.environ["AXIOM_TEST_ARGS"],"w").write(json.dumps(sys.argv[1:]))\n')

    def invoke(self, *args):
        return subprocess.run(["/bin/bash", str(ROOT / "scripts/install.sh"), *args], env=self.env, text=True, capture_output=True)

    def test_no_published_windows_release(self):
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Windows release binaries are not published", result.stderr)
        self.assertIn("--archive", result.stderr)

    def test_arguments_keep_spaces(self):
        self.powershell()
        prefix = str(self.work / "prefix with spaces")
        result = self.invoke("--archive", "archive with spaces.zip", "--checksum", "digest with spaces", "--prefix", prefix)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())
        self.assertEqual(args, ["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/install.ps1"), "-Archive", "archive with spaces.zip", "-Prefix", prefix, "-ChecksumPath", "digest with spaces"])

    def test_builtin_powershell_fallback(self):
        self.powershell("powershell.exe")
        result = self.invoke("--archive", "archive.zip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.capture.exists())

    def test_missing_powershell(self):
        result = self.invoke("--archive", "archive.zip")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PowerShell 5.1", result.stderr)

    def test_version_cannot_select_preview(self):
        self.powershell()
        result = self.invoke("--archive", "archive.zip", "--version", "9.9.9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--version does not select", result.stderr)

    def test_gate_default_prefers_exe_on_windows(self):
        tree = self.work / "tree"
        (tree / "scripts/lib").mkdir(parents=True)
        (tree / ".axiom-bin").mkdir()
        (tree / "scripts/lib/gate.sh").write_text((ROOT / "scripts/lib/gate.sh").read_text())
        for name in ("axiom", "axiom.exe"):
            compiler = tree / ".axiom-bin" / name
            compiler.write_text('#!/bin/sh\necho "test compiler"\n')
            compiler.chmod(0o755)
        caller = tree / "scripts/probe.sh"
        caller.write_text('#!/bin/bash\nsource "$(dirname "$0")/lib/gate.sh"\ngate_init --no-stdlib\nprintf "%s\\n" "$axiom"\n')
        env = dict(self.env)
        for name in ("AXIOM", "AXIOM_AXC"):
            env.pop(name, None)
        result = subprocess.run(["/bin/bash", str(caller)], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(tree / ".axiom-bin/axiom.exe"))


if __name__ == "__main__":
    unittest.main()
