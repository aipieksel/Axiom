#!/usr/bin/env python3
"""Check typed emitter/LSP tag ownership and exact consumer semantics."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

SUPPORT = runpy.run_path(str(Path(__file__).parents[1] / 'selfhost/emitter-line-buffer.py'))
run_checked = SUPPORT['run_checked']
replace_once = SUPPORT['replace_once']
check_output = SUPPORT['check_output']


def check(compiler, root, work, dependencies=None):
    core = dependencies or root / 'core'
    source = {name: (root / 'core' / name).read_text() for name in ('codegen.ax', 'lsp.ax')}
    fixture = (root / 'tests/tools/compiler-tag-consumers.axp').read_text()
    expected = [f'{i} true' for i in range(1, 54)]
    env = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    report = {
        'builder': str(compiler),
        'builder_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
        'source_sha256': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in source.items()},
        'dependency_source_sha256': {name: hashlib.sha256((core / name).read_bytes()).hexdigest()
                                     for name in ('core.ax', 'lexer.ax', 'parser.ax', 'typecheck.ax')},
        'fixture_sha256': hashlib.sha256(fixture.encode()).hexdigest(),
        'configurations': [], 'counterexamples': [], 'refusals': [],
        'invariants': [
            {'invariant': 'Typed tag entries preserve source content, offsets and first-witness order.',
             'enforcement': 'Axtags arguments and AxtagEnt.off/content fields; cases 1-14,19-26,31-34,41-53.',
             'counterexamples': ['lost-isr-value', 'lost-empty-isr-stop', 'lost-render-content', 'shifted-lint-offset', 'shifted-contract-offset']},
            {'invariant': 'Absent and present-empty tags have explicit outcomes; file lint suppression retains its owner.',
             'enforcement': 'Exhaustive nodeAxtagsOpt plus collection query; cases 15-18,27-30,35,44,45.',
             'counterexamples': ['lost-import-file-owner']},
            {'invariant': 'Tag vectors, nodes and String payloads own their child shares through retirement and return.',
             'enforcement': 'Shared typed schema and mapped storage; cases 36-40.',
             'counterexamples': ['duplicate-tag-payload-share']},
        ],
        'known_limits': [
            'Selected host native O0/O2 with allocation poisoning; no Windows execution claim.',
            'Only assigned emitter/LSP consumers and their shared typed tag dependency are exercised.',
            'Packed parser slot 7 remains the explicit raw AST storage boundary.',
            'Bare, empty, malformed and unchecked tag text is preserved; no new tag interpretation is introduced.',
            'No held work or lifetime traversal/fixpoint/enforcement algorithm is read, changed or validated.',
            'Focused evidence does not establish global soundness, Rust parity or certification.',
        ],
    }

    def modules_for(label, texts):
        modules = work / (label + '-modules')
        modules.mkdir(exist_ok=True)
        for path in core.iterdir():
            dest = modules / path.name
            if path.name in texts:
                dest.write_text(texts[path.name])
            elif not dest.exists():
                dest.symlink_to(path, target_is_directory=path.is_dir())
        return dict(env, AXIOM_PATH=str(modules))

    def build_run(label, level, texts, fixture_text=fixture, negative=()):
        local = modules_for(label, texts)
        program = work / (label + '.ax')
        program.write_text(fixture_text)
        binary = work / label
        run_checked([str(compiler), '--diagnostic-format=ai', 'build', str(program),
                     '--opt', str(level), '--alloc-poison', '-o', str(binary)],
                    env=local, stdout=work / (label + '-build.stdout'), stderr=work / (label + '-build.stderr'))
        output = work / (label + '.stdout')
        run_checked([str(binary)], env=local, stdout=output, stderr=work / (label + '.stderr'), timeout=30)
        return check_output(output, expected, required_failures=negative)

    for level in (0, 2):
        build_run('positive-O' + str(level), level, source)
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True,
                                         'cases': 53, 'status': 'passed'})
        print(f'ok   typed tag consumers: 53 outcomes at O{level} poison', flush=True)

    controls = [
        ('lost-isr-value', 'codegen.ax',
         '(strTrim (strSlice content 4 (- (strLen content) 5)))', '"fault"', {6, 7, 24, 39}),
        ('lost-empty-isr-stop', 'codegen.ax',
         '(if (&& (strStartsWith content "isr(") (== (strByte content (- (strLen content) 1)) 41))',
         '(if (&& (> (strLen content) 5) (&& (strStartsWith content "isr(") (== (strByte content (- (strLen content) 1)) 41)))',
         {46, 47}),
        ('lost-render-content', 'lsp.ax',
         '(concat (concat ";@axiom:" tag.content) (concat "\\n" (lspPad ind)))',
         '(concat (concat ";@axiom:" "removed") (concat "\\n" (lspPad ind)))', {10, 11, 22, 43, 48, 50}),
        ('shifted-lint-offset', 'lsp.ax',
         '(lspLintTagCodes lx owner tag.off tag.content)',
         '(lspLintTagCodes lx owner (+ tag.off 1) tag.content)', {32}),
        ('lost-import-file-owner', 'lsp.ax',
         '(if (== (nodeTag d) TAG_D_IMPORT)\n              ""\n              (nodeAName d))',
         '(nodeAName d)', {30}),
        ('shifted-contract-offset', 'lsp.ax',
         '(>= off t.off)', '(>= off (+ t.off 100))', {51}),
    ]
    for label, name, before, after, required in controls:
        mutated = dict(source)
        mutated[name] = replace_once(source[name], before, after, label)
        failed = build_run(label, 0, mutated, negative=required)
        report['counterexamples'].append({'name': label, 'failed_cases': sorted(failed)})
        print(f'ok   counterexample {label}: cases {sorted(failed)} fail', flush=True)
    changed_fixture = replace_once(fixture, '(fn (retireTags text)\n  {',
                                   '(fn (retireTags text)\n  {\n    (__retainref text)', 'payload-share control')
    failed = build_run('duplicate-tag-payload-share', 0, source, changed_fixture, {37})
    report['counterexamples'].append({'name': 'duplicate-tag-payload-share', 'failed_cases': sorted(failed)})
    print(f'ok   counterexample duplicate-tag-payload-share: cases {sorted(failed)} fail', flush=True)

    local = dict(env, AXIOM_PATH=str(work / 'positive-O0-modules'))
    refusals = [
        ('raw-tag-payload', '(:: bad Axtags) (fn (bad) (vecPush vecNew 42))', 'AX3004'),
        ('raw-emitter-tags', '(:: bad String) (fn (bad) (isrTagValueIn (vecPush vecNew 42) 0))', 'AX3004'),
        ('raw-lsp-tags', '(:: bad String) (fn (bad) (lspRenderAxtags (vecPush vecNew 42) 0))', 'AX3004'),
        ('missing-absent-tags', '(:: bad (-> ASTNode Int)) (fn (bad node) (match (nodeAxtagsOpt node) ((Some values) (vecLen values))))', 'AX3005'),
    ]
    for label, body, code in refusals:
        program = work / (label + '.ax')
        program.write_text('(import codegen)\n(import lsp)\n(import parser)\n(import core)\n(import Vec)\n'
                           + body + '\n(:: main Int)\n(fn (main) 0)\n')
        result = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(program)],
                                env=local, cwd=root, capture_output=True, text=True, timeout=180)
        (work / (label + '.stdout')).write_text(result.stdout)
        (work / (label + '.stderr')).write_text(result.stderr)
        assert result.returncode == 1 and code in result.stderr, (label, result.returncode, result.stderr)
        report['refusals'].append({'name': label, 'diagnostic': code})
        print(f'ok   refusal {label}: {code}', flush=True)
    (work / 'compiler-tag-consumers-report.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--dependency-core', type=Path, help='frozen shared tag/checker dependency during isolated integration')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    dependencies = args.dependency_core.resolve() if args.dependency_core else None
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
        check(args.axiom.resolve(), root, args.work.resolve(), dependencies)
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-tag-consumers-') as directory:
            check(args.axiom.resolve(), root, Path(directory), dependencies)


if __name__ == '__main__':
    main()
