#!/usr/bin/env python3
"""Check local macro colours without a compiler or a source-corpus sweep."""

import argparse
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile


class HighlightRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.active = False
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "td" and attrs.get("class") == "line":
            self.active = True
            self.row = []
        if tag == "span" and self.active:
            self.stack.append(attrs.get("class", "").replace(" ", "."))

    def handle_endtag(self, tag):
        if tag == "span" and self.active:
            self.stack.pop()
        if tag == "td" and self.active:
            self.rows.append(self.row)
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.row.extend([self.stack[-1] if self.stack else "plain"] * len(data))


def main():
    repo = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree-sitter", type=Path,
                        default=repo / "tree-sitter-axiom/node_modules/.bin/tree-sitter")
    parser.add_argument("--grammar", type=Path, default=repo / "tree-sitter-axiom")
    args = parser.parse_args()
    cli = args.tree_sitter.resolve()
    grammar = args.grammar.resolve()
    fixture = repo / "tree-sitter-axiom/test/highlight/local-macros.ax"
    checks = []
    source_row = None
    for row, line in enumerate(fixture.read_text().splitlines()):
        marker = re.fullmatch(r";\s*(\^+)\s+([a-z.]+)", line)
        if marker:
            assert source_row is not None, "highlight assertion has no source line"
            checks.append((source_row, line.index("^"), len(marker[1]), marker[2]))
        elif line.strip() and not line.startswith(";"):
            source_row = row
    assert len(checks) >= 20, "the local macro controls were lost"

    with tempfile.TemporaryDirectory(prefix="axiom-local-macros-") as scratch:
        scratch = Path(scratch)
        env = dict(os.environ, XDG_CACHE_HOME=str(scratch / "cache"))
        captures = set(re.findall(r"@([a-z.]+)", (grammar / "queries/highlights.scm").read_text()))
        config = scratch / "config.json"
        config.write_text(json.dumps({"parser-directories": [],
                                      "theme": {name: "#000000" for name in captures}}))

        def run(root, command):
            result = subprocess.run([str(cli), *command], cwd=root, env=env,
                                    text=True, capture_output=True)
            if result.returncode:
                raise AssertionError(result.stderr or result.stdout)
            return result.stdout

        def colours(root):
            html = run(root, ["highlight", "--grammar-path", str(root),
                              "--config-path", str(config), "--html", "--css-classes", str(fixture)])
            rows = HighlightRows()
            rows.feed(html)
            return [(row, col, expected, rows.rows[row][col:col + length])
                    for row, col, length, expected in checks
                    if rows.rows[row][col:col + length] != [expected] * length]

        # Copies keep parser builds and the ablation away from the checkout.
        candidate = scratch / "candidate"
        for name in ("grammar.js", "tree-sitter.json", "package.json", "src", "queries"):
            src, dst = grammar / name, candidate / name
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        mismatches = colours(candidate)
        assert not mismatches, f"incorrect local macro colours: {mismatches}"
        print(f"ok   {len(checks)} local macro, shadowing, nested-scope and template controls")

        query = run(candidate, ["query", "queries/highlights.scm", str(fixture)])
        seen = defaultdict(set)
        for capture, start, end in re.findall(
                r"capture: \d+ - ([\w.]+), start: (\(\d+, \d+\)), end: (\(\d+, \d+\))", query):
            seen[(start, end)].add(capture)
        assert seen, "the highlight query produced no captures"
        assert all(len(roles) == 1 for roles in seen.values()), "a node has multiple highlight captures"
        print("ok   each captured fixture node has one highlight role")

        locals_file = candidate / "queries/locals.scm"
        locals_query = locals_file.read_text()
        binding = "(source_file (macro_declaration name: (identifier) @local.definition))"
        assert locals_query.count(binding) == 1, "the macro binding rule moved; update its ablation"
        locals_file.write_text(locals_query.replace(binding, "", 1))
        mismatches = colours(candidate)
        assert len(mismatches) >= 4, "removing the actual macro binding did not break the controls"
        assert all(expected == "function.macro" and set(actual) == {"function.call"}
                   for _, _, expected, actual in mismatches), mismatches
        print(f"ok   removing the macro binding breaks {len(mismatches)} call controls; other controls stay valid")

        isolation = """((macro_declaration body: (_) @local.scope)
  (#set! local.scope-inherits false))
((macro_rule) @local.scope
  (#set! local.scope-inherits false))"""
        assert locals_query.count(isolation) == 1, "the template scope rules moved; update their ablation"
        locals_file.write_text(locals_query.replace(isolation, "", 1))
        mismatches = colours(candidate)
        assert len(mismatches) >= 3, "removing the actual template scopes did not break the controls"
        assert all((expected, tuple(set(actual))) in {
            ("variable", ("function",)), ("function.call", ("function.macro",))
        } for _, _, expected, actual in mismatches), mismatches
        print(f"ok   removing template isolation breaks {len(mismatches)} raw-template controls")


if __name__ == "__main__":
    main()
