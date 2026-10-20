#!/usr/bin/env python3
"""Create a Windows CI preview ZIP, its exact inventory and SHA-256 sidecar."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import struct
import subprocess
import zipfile


def sha(data):
    return hashlib.sha256(data).hexdigest()


def reject_link(path):
    if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
        raise ValueError(f"package source contains a symlink: {path}")


def package(root, compiler, target, output, version=None, commit=None):
    root, compiler, output = Path(root), Path(compiler), Path(output)
    version = version or (root / "VERSION").read_text().strip()
    commit = commit or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("version must be MAJOR.MINOR.PATCH")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("commit must be a complete lowercase Git commit hash")
    machines = {"windows-x86_64": 0x8664, "windows-aarch64": 0xAA64}
    if target not in machines:
        raise ValueError("target must be windows-x86_64 or windows-aarch64")
    binary = compiler.read_bytes()
    if len(binary) < 64 or binary[:2] != b"MZ":
        raise ValueError("compiler is not a Windows PE executable")
    offset = struct.unpack_from("<I", binary, 60)[0]
    if (offset + 6 > len(binary) or binary[offset:offset + 4] != b"PE\0\0"
            or struct.unpack_from("<H", binary, offset + 4)[0] != machines[target]):
        raise ValueError("compiler PE machine does not match target")
    payload = {"bin/axiom.exe": binary}
    for directory, suffix in (("stdlib", ".ax"), ("docs", ".md")):
        source = root / directory
        reject_link(source)
        for path in sorted(source.rglob("*")):
            reject_link(path)
            if path.is_file() and path.suffix == suffix:
                payload[path.relative_to(root).as_posix()] = path.read_bytes()
    for name in ("LICENSE", "README.md", "CHANGELOG.md"):
        reject_link(root / name)
        payload[name] = (root / name).read_bytes()
    if "stdlib/Pre.ax" not in payload or "docs/reference.md" not in payload:
        raise ValueError("package must carry its standard library and reference")
    metadata = {"format": 1, "version": version, "target": target, "commit": commit, "channel": "ci-preview"}
    payload["package.json"] = (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode()
    folded = set()
    for name in payload:
        if name.casefold() in folded or any(part in ("", ".", "..") for part in name.split("/")):
            raise ValueError(f"case-colliding or unsafe package path: {name}")
        folded.add(name.casefold())
    payload["SHA256SUMS"] = "".join(f"{sha(payload[name])}  {name}\n" for name in sorted(payload)).encode()
    name = f"axiom-{version}-{target}"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{name}.zip"
    # Fixed ZIP metadata makes identical inputs produce identical archives.
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as stream:
        for relative in sorted(payload):
            info = zipfile.ZipInfo(f"{name}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            stream.writestr(info, payload[relative])
    checksum = archive.with_name(archive.name + ".sha256")
    checksum.write_text(f"{sha(archive.read_bytes())}  {archive.name}\n", encoding="ascii")
    print(archive)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--target", choices=("windows-x86_64", "windows-aarch64"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument("--commit")
    args = parser.parse_args()
    package(args.root, args.compiler, args.target, args.output, args.version, args.commit)
