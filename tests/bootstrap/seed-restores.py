#!/usr/bin/env python3
"""Check restore-event structure using private Git histories, without a compiler."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/lib/seed-restores.sh"


def git(repo, *args):
    return subprocess.check_output([
        "git", "-c", "user.name=Seed restore probe", "-c", "user.email=probe@example.invalid",
        "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args
    ], text=True, stderr=subprocess.PIPE).strip()


def commit(repo, name):
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", name)
    return git(repo, "rev-parse", "HEAD")


def populate(repo, seed, metadata):
    directory = repo / "bootstrap"
    if directory.is_dir():
        shutil.rmtree(directory)
    elif directory.exists():
        directory.unlink()
    directory.mkdir()
    (directory / "axiom-host.ll").write_text(seed)
    for name in ("CHAIN", "STAMP", "SHA256SUMS", "README.md"):
        (directory / name).write_text(f"{name}: {metadata}\n")


def probe(helper, repo, writer, prior, canonical):
    result = subprocess.run([
        "bash", "-c", '''set -u -o pipefail
source "$1"
if seed_restore_check "$2" "$3" "$4" "$5"; then rc=0; else rc=$?; fi
printf '%s\\n' "$seed_restore_reason" "$seed_restore_writer" "$seed_restore_prior" "$seed_restore_tree"
exit "$rc"
''', "restore-probe", str(helper), str(repo), writer, prior, str(canonical)
    ], capture_output=True, text=True, timeout=10)
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0, result.stdout.splitlines(), result.stderr


def main():
    with tempfile.TemporaryDirectory(prefix="axiom-seed-restores-") as tmp:
        work = Path(tmp)
        repo = work / "history"
        repo.mkdir()
        git(repo, "init", "--quiet")
        populate(repo, "old seed\n", "original")
        original = commit(repo, "canonical original")
        populate(repo, "new seed\n", "later")
        later = commit(repo, "canonical later")
        populate(repo, "old seed\n", "original")
        restored = commit(repo, "restore all bootstrap bytes")
        (repo / "unrelated.txt").write_text("outside bootstrap\n")
        unrelated = commit(repo, "does not write a seed")
        populate(repo, "temporary seed\n", "temporary")
        temporary = commit(repo, "temporary seed")
        # All IR bytes equal the original, but one metadata byte differs.
        populate(repo, "old seed\n", "original")
        (repo / "bootstrap/STAMP").write_text("STAMP: originaX\n")
        changed_metadata = commit(repo, "restored IR with changed STAMP")
        populate(repo, "temporary seed 2\n", "temporary")
        commit(repo, "temporary seed again")
        populate(repo, "old seed\n", "original")
        (repo / "bootstrap/extra.txt").write_text("a copied tree must not gain a file\n")
        extra_blob = commit(repo, "restored IR plus another bootstrap blob")
        populate(repo, "temporary seed 3\n", "temporary")
        commit(repo, "temporary seed before mode change")
        populate(repo, "old seed\n", "original")
        os.chmod(repo / "bootstrap/axiom-host.ll", 0o755)
        changed_mode = commit(repo, "restored bytes with changed Git mode")
        shutil.rmtree(repo / "bootstrap")
        (repo / "bootstrap").write_text("not a tree\n")
        not_tree = commit(repo, "bootstrap became a blob")

        canonical = work / "canonical"
        canonical.write_text(f"{original}\n{later}\n{temporary}\n")
        wrong_canonical = work / "without-original"
        wrong_canonical.write_text(f"{later}\n")
        reverse_canonical = work / "reverse-canonical"
        reverse_canonical.write_text(f"{restored}\n")
        cases = [
            ("entire-tree restore", restored, original, canonical, True, "matches canonical"),
            ("abbreviated commits", restored[:8], original[:8], canonical, True, "matches canonical"),
            ("wrong copy target", restored, later, canonical, False, "entire bootstrap tree"),
            ("changed metadata blob", changed_metadata, original, canonical, False, "entire bootstrap tree"),
            ("additional bootstrap blob", extra_blob, original, canonical, False, "entire bootstrap tree"),
            ("changed Git mode", changed_mode, original, canonical, False, "entire bootstrap tree"),
            ("noncanonical prior", restored, original, wrong_canonical, False, "not a canonical"),
            ("missing canonical file", restored, original, work / "absent", False, "not a canonical"),
            ("canonical path is a directory", restored, original, work, False, "not a canonical"),
            ("reversed chronology", original, restored, reverse_canonical, False, "ancestry"),
            ("same endpoints", restored, restored, canonical, False, "same commit"),
            ("unknown writer", "0" * 40, original, canonical, False, "writer"),
            ("unknown prior", restored, "0" * 40, canonical, False, "prior"),
            ("moving ref", "HEAD", original, canonical, False, "hexadecimal"),
            ("not a seed writer", unrelated, original, canonical, False, "did not write a seed"),
            ("bootstrap blob", not_tree, original, canonical, False, "directories"),
        ]
        for label, writer, prior, listing, expected, reason in cases:
            ok, lines, stderr = probe(HELPER, repo, writer, prior, listing)
            assert ok == expected and lines and reason in lines[0] and not stderr, (label, ok, lines, stderr)
            if expected:
                assert lines[1:3] == [restored, original]
                assert lines[3] == git(repo, "rev-parse", f"{original}:bootstrap")
            print(f"ok   restore event: {label}")

        source = HELPER.read_text()
        anchor = 'if [[ "$writer_tree" != "$prior_tree" ]]; then'
        assert source.count(anchor) == 1, "tree equality ablation lost its target"
        ablated = work / "restore-no-tree-check.sh"
        ablated.write_text(source.replace(anchor, "if false; then"))
        for label, writer in (("wrong copy target", restored), ("changed metadata blob", changed_metadata),
                              ("additional bootstrap blob", extra_blob), ("changed Git mode", changed_mode)):
            prior = later if label == "wrong copy target" else original
            ok, _, stderr = probe(ablated, repo, writer, prior, canonical)
            assert ok and not stderr, (label, stderr)
        print("ok   negative probe: removing whole-tree equality admits four invalid restores")

        anchor = 'if ! git -C "$repo" merge-base --is-ancestor "$seed_restore_prior" "$seed_restore_writer" 2>/dev/null; then'
        assert source.count(anchor) == 1, "chronology ablation lost its target"
        ablated.write_text(source.replace(anchor, "if false; then"))
        ok, _, stderr = probe(ablated, repo, original, restored, reverse_canonical)
        assert ok and not stderr, stderr
        print("ok   negative probe: removing chronology admits a future seed as the copy source")


if __name__ == "__main__":
    main()
