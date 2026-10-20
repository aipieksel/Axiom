#!/usr/bin/env python3
"""Exercise parser-backed query hints, hover, completion and token ranges."""
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workspace = load("workspace", "workspace-overlays.py")
boundaries = load("boundaries", "memory-boundaries.py")


def run(compiler):
    compiler = str(Path(compiler).resolve())
    root = Path(__file__).resolve().parents[2]
    os.environ.setdefault("AXIOM_STDLIB", str(root / "stdlib"))
    parser = (root / "stdlib/Axqlite/AxqlParse.ax").read_text()
    keyword_table = parser[parser.index("(fn (keywordKind "):parser.index("; The lexer's state:")]
    reserved = set(re.findall(r'\(spells src at \d+ "([A-Z]+)"\)', keyword_table))
    offered = re.search(r'\(pub fn \(axqlKeywords\)\s+"([^"]+)"', parser)[1].split()
    assert reserved and set(offered) == reserved, (reserved, offered)
    with tempfile.TemporaryDirectory(prefix="axiom-lsp-axql-") as directory:
        path = Path(directory) / "Queries.ax"
        source = '''(import Axqlite)
(:: queries (-> Connection Int))
;@axiom:effect(io)
(fn (queries connection)
  {
    (let schema (axqSql "CREATE TABLE users (name TEXT, age INTEGER)"))
    (let query (axqSql "SELECT typeOf(name) FROM users"))
    (let malformed (axqSql "SELEC name FROM users"))
    (let script (axqExecBatch connection "SELECT 1; SELECT 2"))
    (let prepared (axqPrepare connection "SELECT na"))
    (let unicode (Axqlite::axqSql "SELECT 'é😀', name FROM users"))
    (let escaped (axqSql "SELECT\\nname FROM users"))
    (let carriage (axqSql "SELECT\\rname FROM users"))
    (let apostrophe (axqSql "SELECT 'it\\'\\'s'"))
    (let ordinary "SELEC ordinary text")
    0
  })
'''
        path.write_text(source)
        checked = boundaries.terminal(compiler, path)
        assert checked == [], checked
        client = workspace.Client(compiler, cwd=path.parent)
        try:
            capabilities = client.request("initialize", {})["capabilities"]
            client.opening(path.as_uri(), source)

            def request(method, offset):
                return client.request("textDocument/" + method, {
                    "textDocument": {"uri": path.as_uri()},
                    "position": boundaries.position(source, offset)})

            hints = [d for d in client.latest(path.as_uri())["diagnostics"]
                     if d["code"] == "lint-axql"]
            assert len(hints) == 1, hints
            assert hints[0]["severity"] == 4, hints
            assert hints[0]["range"] == boundaries.source_range(
                source, "SELEC", source.index("SELEC name")), hints

            for word, note in (("SELECT typeOf", "AXQL keyword"),
                               ("typeOf(name)", "AXQL function"),
                               ("users\"", "table this document creates"),
                               ("name) FROM", "column of `users`")):
                offset = source.index(word) + 2
                hover = request("hover", offset)
                assert hover and note in hover["contents"]["value"], (word, hover)
                text = word.split("(")[0].split(")")[0].split('"')[0].split()[0]
                assert hover["range"] == boundaries.source_range(source, text,
                    source.index(word)), (word, hover)

            completion = request("completion", source.index("SELECT na") + len("SELECT na"))
            items = {item["label"]: item for item in completion["items"]}
            assert items["name"]["detail"] == "column of users", items
            assert "main" not in items and "fn" not in items, items
            # A caret just after the quote belongs to the Axiom form.
            after = request("completion", source.index('SELECT na"') + len('SELECT na"'))
            assert any(item["label"] == "fn" for item in after["items"]), after

            legend = capabilities["semanticTokensProvider"]["legend"]["tokenTypes"]
            data = client.request("textDocument/semanticTokens/full", {
                "textDocument": {"uri": path.as_uri()}})["data"]
            tokens = {}
            line, column = 0, 0
            for i in range(0, len(data), 5):
                delta, char, length, kind, modifiers = data[i:i + 5]
                line += delta
                column = column + char if delta == 0 else char
                tokens[(line, column, length)] = legend[kind]

            for needle, kind in (("SELECT typeOf", "keyword"),
                                 ("typeOf(name)", "function"),
                                 ("'é😀'", "string"),
                                 ('"SELEC ordinary text"', "string"),
                                 ('"SELECT\\nname FROM users"', "string")):
                word = needle if needle.startswith(('"', "'")) else needle.split("(")[0].split()[0]
                start = boundaries.position(source, source.index(needle))
                length = len(word.encode("utf-16-le")) // 2
                assert tokens.get((start["line"], start["character"], length)) == kind, (
                    needle, kind, tokens)

            qualified = source.replace('(axqSql "SELEC', '(Axqlite::axqSql "SELEC')
            client.change(path.as_uri(), qualified, 2)
            hints = [d for d in client.latest(path.as_uri())["diagnostics"]
                     if d["code"] == "lint-axql"]
            assert len(hints) == 1 and "`SELEC`" in hints[0]["message"], hints

            escaped = source.replace("SELEC name", "SELEC\\nname")
            client.change(path.as_uri(), escaped, 3)
            hints = [d for d in client.latest(path.as_uri())["diagnostics"]
                     if d["code"] == "lint-axql"]
            assert len(hints) == 1 and hints[0]["range"] == boundaries.source_range(
                escaped, "SELEC\\nname FROM users"), hints

            dotted = source.replace('(axqSql "SELEC', '(Store.Queries::axqSql "SELEC')
            client.change(path.as_uri(), dotted, 4)
            hover = client.request("textDocument/hover", {
                "textDocument": {"uri": path.as_uri()},
                "position": boundaries.position(dotted, dotted.index("SELEC name") + 2)})
            assert hover is None, hover  # An unknown query word has no invented help.
            hints = [d for d in client.latest(path.as_uri())["diagnostics"]
                     if d["code"] == "lint-axql"]
            assert len(hints) == 1, hints
            # Completion still reads the final identifier of the dotted head.
            incomplete = dotted.replace('SELEC name FROM users', 'SEL')
            client.change(path.as_uri(), incomplete, 5)
            completions = client.request("textDocument/completion", {
                "textDocument": {"uri": path.as_uri()},
                "position": boundaries.position(incomplete, incomplete.index('"SEL"') + 4)})
            assert any(item["label"] == "SELECT" for item in completions["items"]), completions

            # A real Axiom type error still agrees with the CLI beside
            # the query hint; the editor never treats it as checked data.
            refused = source.replace('(axqSql "SELEC name FROM users")', '(axqSql 7)')
            path.write_text(refused)
            checked = boundaries.terminal(compiler, path)
            assert [d["code"] for d in checked] == ["AX3004"], checked
            client.change(path.as_uri(), refused, 6)
            published = [d for d in client.latest(path.as_uri())["diagnostics"]
                         if d["severity"] != 4]
            assert [d["code"] for d in published] == ["AX3004"], published

            # A declaration tag and a file tag both recognise the new hint.
            for tag in ("lint-axql", "hints"):
                changed = source.replace("(:: queries", f";@axiom:nolint({tag})\n(:: queries")
                client.change(path.as_uri(), changed, 7 if tag == "lint-axql" else 8)
                diagnostics = client.latest(path.as_uri())["diagnostics"]
                assert not [d for d in diagnostics if d["code"] in (
                    "lint-axql", "lint-unknown-nolint")], diagnostics
        finally:
            client.close()
    print("ok   axql (parser hints, keyword/function/schema hover, completion, quote boundary, UTF-16 tokens, escaped strings, type refusal and suppression)")


if __name__ == "__main__":
    try:
        run(sys.argv[1])
    except (AssertionError, subprocess.SubprocessError) as error:
        sys.exit("FAIL axql: " + str(error))
