#!/usr/bin/env python3
"""Exercise the prune interner owner using actual emitted compiler modules.

The source template becomes a private .ax input. Negative probes change only
the copied codegen module. Existing parked line getters and raw CG storage
remain outside this owner change; heap measurements compare like-for-like runs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import tempfile

SUPPORT = runpy.run_path(str(Path(__file__).with_name('emitter-line-buffer.py')))
run_checked = SUPPORT['run_checked']
replace_once = SUPPORT['replace_once']
check_output = SUPPORT['check_output']

BRIDGES = r'''
; Private-copy-only views; none transfer the CG's existing owner share.
(pub :: probeLines (-> Int Ints))
(pub fn (probeLines cg) (memGetWordVec cg 0))
(pub :: probeLineAt (-> Ints Int String))
(pub fn (probeLineAt lines index) (vecGetStr lines index))
(pub :: probeStringCount (-> String Int))
;@axiom:effect(unsafe)
(pub fn (probeStringCount value) (__load64 (as value Int) -2))
'''


def function(ir, name):
    match = re.search(r'^define [^\n]*@' + re.escape(name) +
                      r'\([^\n]*\)[^\n]*\{\n(.*?)^\}', ir, re.M | re.S)
    if match is None:
        raise RuntimeError(f'missing actual emitted function {name}')
    return match.group(1)


def result(body, callee, arguments=''):
    match = re.search(r'(%\S+) = (?:musttail )?call i64 @' + re.escape(callee) +
                      r'\(' + arguments, body)
    if match is None:
        raise RuntimeError(f'missing call to {callee}')
    return match.group(1)


def check_owner_ir(path):
    ir = path.read_text()
    body = function(ir, 'codegen$pruneDeadDefs')
    owner = result(body, 'Intern$internNewTyped')
    raw = result(body, 'Intern$internWordOf', 'i64 ' + re.escape(owner) + r'\)')
    ctx = result(body, 'codegen$pruneCtxNew', 'i64 ' + re.escape(raw) + ',')
    release = f'call void @axiom_release(i64 {owner})'
    if body.count(release) != 1:
        raise RuntimeError('prune interner must have exactly one ordinary owner release')
    if f'call void @axiom_release(i64 {raw})' in body:
        raise RuntimeError('borrowed scalar interner address must not be released')
    required = ('@codegen$pruneIndexDefs(', '@codegen$pruneWalk(',
                '@codegen$pruneRebuild(', '@raw$memSetWord(i64 %cg, i64 0,',
                f'call void @axiom_release(i64 {ctx})')
    drop = body.index(release)
    for operation in required:
        if operation not in body or body.rindex(operation) >= drop:
            raise RuntimeError(f'prune owner retires before {operation}')
    uses = [line for line in body.splitlines()
            if re.search(re.escape(ctx) + r'(?![0-9])', line)]
    if any(body.index(line) > drop for line in uses):
        raise RuntimeError('prune context remains in use after the interner owner retires')
    context = function(ir, 'codegen$pruneCtxNew')
    if len(re.findall(r'@Vec\$vecPush\(i64 %\S+, i64 %si, i64 0\)', context)) != 1:
        raise RuntimeError('raw context slot0 must keep its unchanged nonowning scalar layout')
    if '@Intern$internNew(' in body or '@Intern$internFree(' in body:
        raise RuntimeError('prune must use ordinary typed ownership rather than a manual raw lifetime')
    index = function(ir, 'codegen$pruneIndexDefs')
    getter = result(index, 'raw$vecGetStr')
    return {'owner': owner, 'borrowed_word': raw, 'context': ctx,
            'ordinary_final_releases': 1, 'drop_after_index_walk_rebuild_publish_ctx': True,
            'raw_context_slot0_evidence': 0,
            'unchanged_index_getter_release_count': index.count(f'call void @axiom_release(i64 {getter})'),
            'known_index_getter_loss_out_of_scope': True}


def private_modules(directory, source_root, source):
    directory.mkdir(parents=True, exist_ok=True)
    for item in (source_root / 'self_host').iterdir():
        target = directory / item.name
        if item.name == 'codegen.ax':
            target.write_text(source + BRIDGES)
        elif not target.exists():
            target.symlink_to(item, target_is_directory=item.is_dir())
    return directory


def check(axiom, repo, source_root, work):
    template = repo / 'tests/selfhost/emitter-prune-owner.axp'
    expected = template.with_suffix('.out').read_text().splitlines()
    if expected != [f'{i} true' for i in range(1, 29)]:
        raise RuntimeError('update the explicit 28-case verdict inventory')
    fixture = work / 'fixture.ax'
    fixture.write_text(template.read_text())
    source = (source_root / 'self_host/codegen.ax').read_text()
    report = {'builder': str(axiom), 'source_root': str(source_root),
              'builder_sha256': hashlib.sha256(axiom.read_bytes()).hexdigest(),
              'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
              'positive': [], 'negative': []}
    env = dict(os.environ)
    env['PATH'] = '/opt/homebrew/opt/llvm/bin:' + env.get('PATH', '')
    env['AXIOM_STDLIB'] = str(source_root / 'stdlib')
    env['AXIOM_STDLIB_PATH'] = str(source_root / 'stdlib')
    run_checked([str(axiom), '--diagnostic-format=ai', 'fmt', '--check', str(fixture)],
                env=env, stdout=work / 'format.stdout', stderr=work / 'format.stderr')

    def build_run(label, level, emitter, negative=False):
        modules = private_modules(work / (label + '-modules'), source_root, emitter)
        local = dict(env, AXIOM_PATH=str(modules))
        binary = work / label
        run_checked([str(axiom), '--diagnostic-format=ai', 'build', str(fixture),
                     '--opt', str(level), '--alloc-poison', '--emit-llvm', '-o', str(binary)],
                    env=local, stdout=work / (label + '.build.stdout'),
                    stderr=work / (label + '.build.stderr'))
        output = work / (label + '.stdout')
        error = work / (label + '.stderr')
        run_checked([str(binary)], env=local, stdout=output, stderr=error, timeout=60)
        check_output(output, expected)
        growth = re.fullmatch(r'prune-soak-growth (-?\d+) (-?\d+)\n', error.read_text())
        if growth is None:
            raise RuntimeError(f'{label}: missing exact retained-heap measurements')
        row = {'label': label, 'cases': len(expected),
               'index_scope_retained_bytes': int(growth[1]),
               'whole_pruner_retained_bytes': int(growth[2])}
        try:
            row['owner_ir'] = check_owner_ir(binary.with_suffix('.ll'))
        except RuntimeError as problem:
            if not negative:
                raise
            row['owner_ir_expected_failure'] = str(problem)
        else:
            if negative:
                raise RuntimeError(f'{label}: restoring the raw owner unexpectedly passed owner proof')
        static_output = work / (label + '.static.stdout')
        static_error = work / (label + '.static.stderr')
        run_checked([str(binary), '--emit-staticlib'], env=local,
                    stdout=static_output, stderr=static_error, timeout=30)
        check_output(static_output, [f'{i} true' for i in range(1, 4)])
        if static_error.read_text():
            raise RuntimeError(f'{label}: unexpected static-library-path stderr')
        row['static_library_cases'] = 3
        return row

    for level in (0, 2):
        row = build_run(f'positive-O{level}', level, source)
        report['positive'].append(row)
        print(f'ok   prune owner: 28 semantic + 3 static-library cases at O{level}; '
              f'index retention {row["index_scope_retained_bytes"]}, '
              f'whole-pruner retention {row["whole_pruner_retained_bytes"]}', flush=True)
    raw = replace_once(source,
                       '(let siOwner internNewTyped)\n      (let si (internWordOf siOwner))',
                       '(let si internNew)', 'restore only the raw prune interner')
    for level in (0, 2):
        row = build_run(f'raw-owner-O{level}', level, raw, negative=True)
        positive = report['positive'][level // 2]['whole_pruner_retained_bytes']
        row['extra_retained_bytes_vs_candidate'] = row['whole_pruner_retained_bytes'] - positive
        if row['extra_retained_bytes_vs_candidate'] <= 0:
            raise RuntimeError(f'raw-owner-O{level}: raw interner did not increase retained heap')
        report['negative'].append(row)
        print(f'ok   raw-owner ablation at O{level}: owner proof fails and retains '
              f'{row["extra_retained_bytes_vs_candidate"]} additional bytes', flush=True)
    (work / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    if args.work:
        work = args.work.resolve()
        work.mkdir(parents=True, exist_ok=True)
        check(args.axiom.resolve(), repo, args.source_root.resolve(), work)
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-prune-owner-') as path:
            check(args.axiom.resolve(), repo, args.source_root.resolve(), Path(path))


if __name__ == '__main__':
    main()
