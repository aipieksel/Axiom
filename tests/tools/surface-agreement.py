#!/usr/bin/env python3
"""The compiler's surfaces agree about every diagnostics fixture.

One compiler, four doors: `check` on the command line, the language
server's published diagnostics, the REPL's reported codes, and `build`.
Each has its own path to the checker and its own rendering, so each can
lose, add or misplace a diagnostic on its own. This script asks the
same question at every door and requires one answer:

  CLI vs LSP     every fixture: the set of (severity, code, line, column)
                 the CLI prints equals the set the server publishes, with
                 the column in the server's UTF-16 code units, computed
                 here from the fixture's own bytes.
  CLI vs build   every tenth fixture: `build` refuses what `check`
                 refuses, with the same codes, and writes no executable.
  CLI vs REPL    the fixtures small enough to be a REPL session (at most
                 two declarations): the REPL reports the same codes.

Usage: surface-agreement.py COMPILER FIXTURE_DIR [--all-build]
Exit 0 when every door agrees; a disagreement is printed and exits 1.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

AXDL = re.compile(r'^([EW]) (AX\d{4}) ([^ ]+?):(\d+):(\d+)(?:-\d+)?(?::\d+)? ')


def u16(s):
    return len(s.encode('utf-16-le')) // 2


def cli_set(compiler, fixture, env):
    """(severity, code, line, utf16 column) of every diagnostic `check`
    prints for `fixture`, and the exit status."""
    proc = subprocess.run(
        [compiler, '--diagnostic-format=ai', 'check', fixture.name],
        cwd=fixture.parent, env=env, capture_output=True, text=True, timeout=120)
    src_lines = fixture.read_text(encoding='utf-8').split('\n')
    found = set()
    for raw in proc.stderr.split('\n'):
        m = AXDL.match(raw)
        if not m:
            continue
        sev, code, path, line, col = m.groups()
        if Path(path).name != fixture.name:
            continue  # a diagnostic in an imported module
        line, col = int(line), int(col)
        text = src_lines[line - 1] if line - 1 < len(src_lines) else ''
        found.add((sev, code, line, u16(text[:col - 1])))
    return found, proc.returncode


def lsp_client():
    """The protocol client the LSP gate drives its sessions with: one
    server per session, a diagnostics barrier after every open."""
    import runpy
    overlays = Path(__file__).resolve().parents[1] / 'lsp' / 'workspace-overlays.py'
    return runpy.run_path(str(overlays))['Client']


def lsp_set(compiler, fixture, env, client_class):
    """(severity, code, line, utf16 column) of every diagnostic the
    server publishes for `fixture` once its open has been checked."""
    uri = fixture.resolve().as_uri()
    text = fixture.read_text(encoding='utf-8')
    saved = dict(os.environ)
    os.environ.update(env)
    try:
        client = client_class(compiler, cwd=str(fixture.parent))
        try:
            client.opening(uri, text)
            client.request('shutdown', None)
            client.send('exit', None)
        finally:
            try:
                client.process.wait(timeout=30)
            except Exception:
                client.process.kill()
    finally:
        os.environ.clear(); os.environ.update(saved)
    published = [p for p in client.publications if p.get('uri') == uri]
    found = set()
    if published:
        for d in published[-1]['diagnostics']:
            code = d.get('code')
            if not (isinstance(code, str) and code.startswith('AX')):
                continue  # a lint (`nolint.ax`) is the server's own layer, not the checker's
            sev = 'E' if d.get('severity') == 1 else 'W'
            start = d['range']['start']
            found.add((sev, code, start['line'] + 1, start['character']))
    return found


def build_refuses(compiler, fixture, env, work):
    """`build` of a refused fixture: exit 1, the same codes `check`
    printed, and no executable written."""
    out = work / (fixture.stem + '.bin')
    proc = subprocess.run(
        [compiler, '--diagnostic-format=ai', 'build', fixture.name, '-o', str(out)],
        cwd=fixture.parent, env=env, capture_output=True, text=True, timeout=300)
    codes = {m.group(2) for m in (AXDL.match(l) for l in proc.stderr.split('\n')) if m}
    return proc.returncode, codes, out.exists()


def declarations(text):
    return len(re.findall(r'^\((?:pub )?(?:fn|::|data|struct|effect|macro|import|extern)\b', text, re.M))


def repl_codes(compiler, fixture, env):
    """The codes the REPL reports when the fixture's forms are typed in,
    one top-level form at a time."""
    text = fixture.read_text(encoding='utf-8')
    forms, depth, cur, in_str = [], 0, '', False
    i = 0
    while i < len(text):
        c = text[i]
        if in_str:
            cur += c
            if c == '\\' and i + 1 < len(text):
                cur += text[i + 1]; i += 1
            elif c == '"':
                in_str = False
        elif c == ';' and depth == 0:
            j = text.find('\n', i)
            comment = text[i:len(text) if j < 0 else j]
            if comment.startswith(';@axiom:'):
                forms.append(comment.strip())  # a tag line stays, as its own prompt line
            i = len(text) if j < 0 else j
            continue
        elif c == ';':
            j = text.find('\n', i)
            i = len(text) if j < 0 else j
            cur += '\n'
            continue
        else:
            if c == '"':
                in_str = True
            elif c in '({[':
                depth += 1
            elif c in ')}]':
                depth -= 1
            cur += c
            if depth == 0 and cur.strip():
                forms.append(' '.join(cur.split()))
                cur = ''
        i += 1
    script = '\n'.join(forms) + '\n'
    proc = subprocess.run([compiler, 'repl', '--no-banner'], cwd=fixture.parent, env=env,
                          input=script, capture_output=True, text=True, timeout=120)
    return set(re.findall(r'\[(AX\d{4})\]', proc.stdout + proc.stderr))


def main():
    compiler = str(Path(sys.argv[1]).resolve())
    fixdir = Path(sys.argv[2]).resolve()
    all_build = '--all-build' in sys.argv[3:]
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    fixtures = sorted(p for p in fixdir.glob('[0-9]*.ax') if p.with_suffix('.axdl').exists())
    assert len(fixtures) >= 200, f'only {len(fixtures)} fixtures with a golden; the corpus moved'
    client_class = lsp_client()
    bad = []
    compared = built = repl = 0
    with tempfile.TemporaryDirectory(prefix='axiom-surfaces-') as work:
        work = Path(work)
        for k, fx in enumerate(fixtures):
            cli, status = cli_set(compiler, fx, env)
            lsp = lsp_set(compiler, fx, env, client_class)
            compared += 1
            if cli != lsp:
                bad.append(f'{fx.name}: CLI {sorted(cli)} vs LSP {sorted(lsp)}')
            errors = {c for (s, c, _, _) in cli if s == 'E'}
            if errors and (all_build or k % 10 == 0):
                rc, codes, exists = build_refuses(compiler, fx, env, work)
                built += 1
                if rc != 1 or codes != {c for (_, c, _, _) in cli} or exists:
                    bad.append(f'{fx.name}: build exit {rc}, codes {sorted(codes)} vs check {sorted({c for (_, c, _, _) in cli})}, executable written: {exists}')
            # Three codes a session cannot draw, by the REPL's own rules:
            # a redefinition at the prompt REPLACES the earlier declaration
            # (`replDeclsSrcDropping`), so AX3006 never arises; a signature
            # waits for its definition on a later line, so AX3015 is deferred
            # to the expression that reaches it; and a line that is not a
            # declaration is an expression there, so an unknown declaration
            # head (AX3027 in a file) is an undefined variable at the prompt.
            expected = errors - {'AX3006', 'AX3015', 'AX3027'}
            if expected and declarations(fx.read_text(encoding='utf-8')) <= 2 and not fx.name.endswith('-import.ax'):
                got = repl_codes(compiler, fx, env)
                repl += 1
                # A declaration macro invoked at the prompt is an expression
                # there, and the expander names the unmatched invocation
                # AX3027 in that position where a file draws AX3018.
                def drawn(code):
                    return code in got or (code == 'AX3018' and 'AX3027' in got)
                if not all(drawn(c) for c in expected):
                    bad.append(f'{fx.name}: REPL reported {sorted(got)}, check {sorted(expected)}')
    for line in bad:
        print('FAIL ' + line)
    if bad:
        print(f'FAIL {len(bad)} disagreement(s) over {compared} fixtures ({built} built, {repl} typed into the REPL)')
        return 1
    print(f'ok   the four surfaces agree over {compared} fixtures: CLI and LSP everywhere, build on {built}, the REPL on {repl}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
