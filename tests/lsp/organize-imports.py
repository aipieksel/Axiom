#!/usr/bin/env python3
"""Apply source actions over stdio and check source bytes and behaviour."""
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

Client = runpy.run_path(str(Path(__file__).with_name("workspace-overlays.py")))["Client"]
KIND = "source.organizeImports"
MAIN = "\n;; The entry's docs stay above its signature.\n(:: main Int)\n(fn (main) (+ z a))\n"


def offset(text, position):
    lines = text.splitlines(keepends=True)
    prefix = sum(len(line) for line in lines[:position["line"]])
    line = lines[position["line"]] if position["line"] < len(lines) else ""
    units = position["character"]
    for i in range(len(line) + 1):
        if len(line[:i].encode("utf-16-le")) // 2 == units:
            return prefix + i
    raise AssertionError((position, line))


def run(compiler):
    checks = 0
    with tempfile.TemporaryDirectory(prefix="axiom-organize-imports-") as root:
        directory = Path(root)
        (directory / "Zed.ax").write_text("(pub :: z Int)\n(pub fn (z) 19)\n")
        (directory / "Alpha.ax").write_text("(pub :: a Int)\n(pub fn (a) 23)\n")
        (directory / "Nested").mkdir()
        (directory / "Nested" / "Beta.ax").write_text("(pub :: b Int)\n(pub fn (b) 7)\n")
        client = Client(compiler, cwd=root)
        try:
            caps = client.request("initialize", {"processId": None,
                "rootUri": directory.as_uri(), "capabilities": {}})["capabilities"]
            assert KIND in caps["codeActionProvider"]["codeActionKinds"], caps
            client.send("initialized", {})
            checks += 1
            serial = 0

            def opened(source):
                nonlocal serial
                serial += 1
                path = directory / ("case%d.ax" % serial)
                # Deliberately leave the source unsaved: the edit must use the overlay.
                path.write_text("(:: main Int)\n(fn (main) 0)\n")
                client.opening(path.as_uri(), source)
                return path.as_uri()

            def actions(uri, only=(KIND,)):
                context = {"diagnostics": []}
                if only is not None:
                    context["only"] = list(only)
                return client.request("textDocument/codeAction", {
                    "textDocument": {"uri": uri},
                    # A source action applies to the header even from the last line.
                    "range": {"start": {"line": 1000, "character": 0},
                              "end": {"line": 1000, "character": 0}},
                    "context": context})

            def returns(text, expected):
                path = directory / "applied.ax"
                path.write_text(text)
                built = subprocess.run([compiler, "--diagnostic-format=ai", "build",
                    "--input", str(path), "--output", str(directory / "program")],
                    capture_output=True, text=True, timeout=60)
                assert built.returncode == 0, built.stderr
                ran = subprocess.run([str(directory / "program")], timeout=10)
                assert ran.returncode == expected, (text, ran.returncode, expected)

            def edited(source, expected, only=(KIND,), execute=False):
                nonlocal checks
                uri = opened(source)
                result = actions(uri, only)
                assert len(result) == 1, (source, result)
                action = result[0]
                assert action["kind"] == KIND and action["title"] == "Organise imports", action
                assert "command" not in action and "diagnostics" not in action, action
                changes = action["edit"]["changes"]
                assert list(changes) == [uri] and len(changes[uri]) == 1, action
                edit = changes[uri][0]
                start = offset(source, edit["range"]["start"])
                end = offset(source, edit["range"]["end"])
                actual = source[:start] + edit["newText"] + source[end:]
                assert actual == expected, (source, actual, expected, action)
                # Idempotence is checked through the same live unsaved document.
                client.change(uri, actual, 2)
                assert actions(uri) == [], actual
                if execute:
                    for text in (source, actual):
                        returns(text, 42)
                client.send("textDocument/didClose", {"textDocument": {"uri": uri}})
                checks += 1

            def refused(source, valid=False):
                nonlocal checks
                uri = opened(source)
                if valid:
                    assert not [d for d in client.latest(uri)["diagnostics"]
                                if d["severity"] == 1], client.latest(uri)
                assert actions(uri) == [], (source, actions(uri))
                client.send("textDocument/didClose", {"textDocument": {"uri": uri}})
                checks += 1

            prefix = ";;! Module docs stay at the top. 😀\n\n"
            zed = ";; Zed's attached docs é\n; A maintainer note.\n(import Zed (z)) ; trailing ) 😀"
            alpha = ";; Alpha's attached docs\n(import Alpha\n  (a))"
            edited(prefix + zed + "\n\n" + alpha + "\n" + MAIN,
                   prefix + alpha + "\n\n" + zed + "\n" + MAIN, execute=True)
            edited("(import Zed)\n(import Alpha)" + MAIN,
                   "(import Alpha)\n(import Zed)" + MAIN, only=("source",))
            edited("  (import Zed)\n\t(import Alpha)\n",
                   "\t(import Alpha)\n  (import Zed)\n")
            edited("(import Zed)\n(import Alpha)", "(import Alpha)\n(import Zed)")
            edited("(import Zed)\r\n(import Alpha)\r\n", "(import Alpha)\r\n(import Zed)\r\n")
            edited("(import Zed)\n(import Alpha) ; é😀", "(import Alpha) ; é😀\n(import Zed)")
            edited(";;! Module\r\n\r\n;; Zed docs\r\n(import Zed)\r\n;; Alpha docs\r\n(import Alpha)\r\n",
                   ";;! Module\r\n\r\n;; Alpha docs\r\n(import Alpha)\r\n;; Zed docs\r\n(import Zed)\r\n")
            edited("(import Zed)\n; attached to Alpha\n(import Alpha)\n(import Nested.Beta)\n",
                   "; attached to Alpha\n(import Alpha)\n(import Nested.Beta)\n(import Zed)\n")
            edited("(import ; a misleading ( in a comment\n Zed (z))\n(import Alpha (a))\n",
                   "(import Alpha (a))\n(import ; a misleading ( in a comment\n Zed (z))\n")
            edited("(import Zed (z #| ) ( |#))\n(import Alpha (a))\n",
                   "(import Alpha (a))\n(import Zed (z #| ) ( |#))\n")
            edited("(import Zed (z #| ;@axiom:effect(io) |#))\n(import Alpha (a))\n",
                   "(import Alpha (a))\n(import Zed (z #| ;@axiom:effect(io) |#))\n")
            tagged_main = ";@axiom:effect(pure)\n(:: main Int)\n(fn (main) (+ z a))\n"
            edited("(import Zed)\n(import Alpha)\n" + tagged_main,
                   "(import Alpha)\n(import Zed)\n" + tagged_main)
            for source in [
                "", "(:: main Int)\n(fn (main) 0)\n", "(import Zed)\n",
                "(import Alpha)\n(import Zed)\n",  # already sorted
                "(import Zed)\n(import Alpha",  # incomplete
                "(import Zed)\n(:: main Int)\n(import Alpha)\n(fn (main) 0)\n",  # late
                "(:: main Int)\n(fn (main) { (import Zed) (import Alpha) 0 })\n",  # nested
                "(import Zed) (import Alpha)\n",  # shared line
                "(import Zed)\n(import Alpha)\n(import Zed (z))\n",  # repeated module
                "(import Zed)\n; A separate section\n\n(import Alpha)\n",
                "(import Zed)\n; --------\n(import Alpha)\n",
                "(import Zed)\n;;! Module heading\n(import Alpha)\n",
                "(import Zed)\n#| A block between imports |#\n(import Alpha)\n",
                "(import Zed) #| trailing block |#\n(import Alpha)\n",
                ";@axiom:effect(pure)\n(import Zed)\n(import Alpha)\n",
                ";@axiom:nolint(lint-unused-let)\n\n(import Zed)\n(import Alpha)\n",
                "(import Zed) ;@axiom:effect(io)\n(import Alpha)\n" + MAIN,
                "(import Zed ;@axiom:effect(io)\n)\n(import Alpha)\n" + MAIN,
                "(import Zed)\n(import AModuleThatDoesNotExist)\n",
            ]:
                refused(source)

            refused("(import Zed) ;@axiom:nolint(lint-unused-let)\n(import Alpha)\n"
                    "(:: helper Int)\n(fn (helper) { (let unused 1) 0 })\n"
                    "(:: main Int)\n(fn (main) { (let unused 1) helper })\n", valid=True)

            source = "(import Zed)\n(import Alpha)\n" + MAIN
            uri = opened(source)
            for only in [(), ("quickfix",), ("refactor",),
                         ("source.fixAll",), (KIND + ".child",), None]:
                assert all(a["kind"] != KIND for a in actions(uri, only)), only
                checks += 1

            typo_uri = opened("(:: helper (-> Int Int))\n(fn (helper x) (+ x 1))\n"
                              "(:: main Int)\n(fn (main) (helpr 3))\n")
            filtered = client.request("textDocument/codeAction", {
                "textDocument": {"uri": typo_uri},
                "context": {"diagnostics": [], "only": ["quickfix"]}})
            assert filtered and all(a["kind"] == "quickfix" for a in filtered), filtered
            assert client.request("textDocument/codeAction", {
                "textDocument": {"uri": typo_uri},
                "context": {"diagnostics": [], "only": ["refactor"]}}) == []
            checks += 2

            # Last imported macro wins, even when both orders typecheck.
            for module, value in [("ZMacro", 1), ("AMacro", 2)]:
                (directory / (module + ".ax")).write_text(
                    "(pub macro (answer) %d)\n" % value)
            macro_main = "(:: main Int)\n(fn (main) (answer))\n"
            macro_source = "(import ZMacro)\n(import AMacro)\n" + macro_main
            returns(macro_source, 2)
            returns("(import AMacro)\n(import ZMacro)\n" + macro_main, 1)
            refused(macro_source, valid=True)
            # Conflicts can arrive through different transitive imports.
            (directory / "ZFacade.ax").write_text("(import ZMacro)\n")
            (directory / "AFacade.ax").write_text("(import AMacro)\n")
            refused("(import ZFacade)\n(import AFacade)\n", valid=True)
            # The dependency overlay, rather than its harmless saved contents, wins.
            macro_uri = (directory / "Alpha.ax").as_uri()
            client.opening(macro_uri, "(pub macro (answer) 3)\n")
            refused("(import ZMacro)\n(import Alpha)\n", valid=True)
            client.send("textDocument/didClose", {"textDocument": {"uri": macro_uri}})

            # A unique macro's structural queries also depend on declaration order.
            (directory / "ZData.ax").write_text("(pub data Z (C Int))\n")
            (directory / "AData.ax").write_text("(pub data A (C Int Int))\n")
            arity_main = "(macro (size) (syntax/arity C))\n(:: main Int)\n(fn (main) (size))\n"
            arity_source = "(import ZData)\n(import AData)\n" + arity_main
            returns(arity_source, 1)
            returns("(import AData)\n(import ZData)\n" + arity_main, 2)
            refused(arity_source, valid=True)

            # Generated constructors must be included in the same safety check.
            for module, data, fields in [("ZGenerated", "Z", "Int"),
                                         ("AGenerated", "A", "Int Int")]:
                (directory / (module + ".ax")).write_text(
                    "(macro make%s ((make%s) (pub data %s (C %s))))\n(make%s)\n"
                    % (data, data, data, fields, data))
            generated_source = "(import ZGenerated)\n(import AGenerated)\n" + arity_main
            returns(generated_source, 1)
            returns("(import AGenerated)\n(import ZGenerated)\n" + arity_main, 2)
            refused(generated_source, valid=True)

            # The same full guard permits real library modules without macro collisions.
            edited("(import Vec)\n(import Str)\n", "(import Str)\n(import Vec)\n")
        finally:
            client.close()
    print("ok   organise imports: %d source, protocol, refusal and execution checks" % checks)


if __name__ == "__main__":
    run(os.path.abspath(sys.argv[1]))
