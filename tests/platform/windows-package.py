#!/usr/bin/env python3
"""Check preview package inventory, PE architecture, provenance and reproducibility."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("package_windows", ROOT / "scripts/package-windows.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="axiom-windows-package-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("VERSION", "LICENSE", "README.md", "CHANGELOG.md", "stdlib/Pre.ax", "docs/reference.md"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("0.7.8" if name == "VERSION" else name)
        self.compiler = self.root / "axiom.exe"

    def emit(self, target="windows-x86_64", machine=0x8664, commit="a" * 40, version="0.7.8"):
        data = bytearray(70)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 60, 64)
        data[64:68] = b"PE\0\0"
        struct.pack_into("<H", data, 68, machine)
        self.compiler.write_bytes(data)
        return module.package(self.root, self.compiler, target, self.root / "out", version, commit)

    def test_inventory_and_reproducibility(self):
        first = self.emit().read_bytes()
        archive = self.emit()
        self.assertEqual(first, archive.read_bytes())
        digest, filename = archive.with_name(archive.name + ".sha256").read_text().strip().split("  ")
        self.assertEqual(digest, hashlib.sha256(first).hexdigest())
        self.assertEqual(filename, archive.name)
        with zipfile.ZipFile(archive) as stream:
            prefix = "axiom-0.7.8-windows-x86_64/"
            self.assertTrue(all(name.startswith(prefix) and "\\" not in name
                                for name in stream.namelist()))
            payload = {name[len(prefix):]: stream.read(name) for name in stream.namelist()}
        inventory = dict((path, sha) for sha, path in (line.split("  ") for line in payload["SHA256SUMS"].decode().splitlines()))
        self.assertEqual(set(inventory), set(payload) - {"SHA256SUMS"})
        for path, sha in inventory.items():
            self.assertEqual(sha, hashlib.sha256(payload[path]).hexdigest())
        metadata = json.loads(payload["package.json"])
        self.assertEqual(metadata["target"], "windows-x86_64")
        self.assertEqual(metadata["commit"], "a" * 40)
        self.assertEqual(metadata["channel"], "ci-preview")

    def test_arm64(self):
        self.assertIn("windows-aarch64", self.emit("windows-aarch64", 0xAA64).name)

    def test_wrong_machine(self):
        with self.assertRaisesRegex(ValueError, "machine"):
            self.emit(machine=0xAA64)

    def test_invalid_provenance(self):
        with self.assertRaisesRegex(ValueError, "commit"):
            self.emit(commit="a" * 7)
        with self.assertRaisesRegex(ValueError, "version"):
            self.emit(version="../escape")

    def test_missing_library(self):
        (self.root / "stdlib/Pre.ax").unlink()
        with self.assertRaisesRegex(ValueError, "library"):
            self.emit()

    def test_source_links(self):
        try:
            (self.root / "stdlib/Alias.ax").symlink_to(self.root / "stdlib/Pre.ax")
        except OSError as error:
            self.skipTest(f"host cannot create symbolic links: {error}")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.emit()
        (self.root / "stdlib/Alias.ax").unlink()
        (self.root / "README.md").unlink()
        (self.root / "README.md").symlink_to(self.root / "LICENSE")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.emit()
        (self.root / "README.md").unlink()
        (self.root / "README.md").write_text("README.md")
        (self.root / "stdlib").rename(self.root / "library")
        (self.root / "stdlib").symlink_to(self.root / "library", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.emit()

    def test_case_collision(self):
        # Distinct spellings can reach a Windows package from Linux.
        (self.root / "stdlib/pre.ax").write_text("collision")
        if (self.root / "stdlib/Pre.ax").read_text() == "collision":
            self.skipTest("host filesystem folds case")
        with self.assertRaisesRegex(ValueError, "case-colliding"):
            self.emit()


if __name__ == "__main__":
    unittest.main()
