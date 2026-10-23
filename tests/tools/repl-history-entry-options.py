#!/usr/bin/env python3
"""Distinguish absent history entries from empty entries and saved drafts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(compiler, root, work):
    module = root / 'core/replhist.ax'; source = module.read_text()
    repl = root / 'core/repl.ax'; repl_source = repl.read_text()
    fixture = root / 'tests/tools/repl-history-entry-options.axp'
    consumer = root / 'tests/tools/repl-history-entry-consumers.axp'
    extra = fixture.read_text().replace('(import IO)', '').replace('(import Fmt)', '')
    environment = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'), AXIOM_PATH=str(root / 'core'))
    report = {'compiler': str(compiler), 'compiler_sha256': digest(compiler),
              'source_sha256': digest(module), 'repl_sha256': digest(repl),
              'fixture_sha256': digest(fixture), 'consumer_sha256': digest(consumer),
              'configurations': [], 'controls': [], 'refusals': [],
              'invariants': ['A valid empty entry and empty draft are Some; an invalid index is None.',
                             'A negative public browse cursor is None; a cursor at or beyond the end still selects the saved draft.',
                             'A missing browse entry leaves editor text/output unchanged and reports no repaint.',
                             'Returned text outlives its history; matching/discarding results preserves source entry borrow counts.'],
              'remaining': ['History persistence path, completion selection/detail queries, Ctrl-C event carrier'],
              'limits': ['Host O0/O2 allocation-poison probes; CI runs full target and REPL suites.',
                         'Borrow counts do not establish global destructor completeness.',
                         'No evaluation/process-call, lifetime or ownership-inference algorithm changes.']}

    def build(label, text, level=0, env=None):
        path = work / (label + '.ax'); path.write_text(text)
        executable = work / label
        result = subprocess.run([str(compiler), '--diagnostic-format=ai', 'build', str(path),
                                 '--opt', str(level), '--alloc-poison', '-o', str(executable)],
                                cwd=root, env=env or environment, capture_output=True, text=True, timeout=300)
        (work / (label + '.build.stdout')).write_text(result.stdout)
        (work / (label + '.build.stderr')).write_text(result.stderr)
        assert result.returncode == 0, (label, result)
        return executable

    def outcomes(executable):
        result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0 and not result.stderr, result
        return result.stdout

    for level in (0, 2):
        executable = build('entries-O' + str(level), '(import IO)\n' + source + '\n' + extra, level)
        assert outcomes(executable) == ''.join(f'{i} true\n' for i in range(1, 25))
        executable = build('browse-O' + str(level), consumer.read_text(), level)
        assert outcomes(executable) == ''.join(f'{i} true\n' for i in range(1, 17))
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True,
                                         'entry_outcomes': 24, 'browse_outcomes': 16})
        print(f'ok   entries/browse: 24+16 outcomes O{level}', flush=True)

    def alter(text, name, old, new):
        definition = re.search(r'\((?:pub )?fn \(' + re.escape(name) + r'\s', text)
        assert definition, name
        start = definition.start(); following = re.search(r'^\((?:pub )?:: ', text[start:], re.M)
        end = start + following.start() if following else len(text)
        body = text[start:end]
        assert body.count(old) == 1, (name, old, body.count(old))
        return text[:start] + body.replace(old, new) + text[end:]

    controls = [
        ('invent-missing-entry', alter(source, 'histAt', '\n    None', '\n    (Some "")'), {3, 4, 5, 6, 8, 11}),
        ('discard-empty-entry', alter(source, 'histAt', '(Some (vecGet h.entries i))',
                                      '(if (== (strLen (vecGet h.entries i)) 0) None (Some (vecGet h.entries i)))'),
         {2, 19, 22}),
        ('invent-invalid-cursor', alter(source, 'histCurrent', '(histAt h pos)',
                                        '(if (< pos 0) (Some h.draft) (histAt h pos))'), {8}),
        ('discard-empty-draft', alter(source, 'histCurrent', '(Some h.draft)',
                                      '(if (== (strLen h.draft) 0) None (Some h.draft))'), {12}),
        ('retain-entry', alter(source, 'histAt', '(Some (vecGet h.entries i))',
                               '{ (__retainref (vecGet h.entries i)) (Some (vecGet h.entries i)) }'), {16, 17, 18}),
    ]
    for label, changed, expected in controls:
        executable = build(label, '(import IO)\n' + changed + '\n' + extra)
        result = outcomes(executable)
        failed = {int(line.split()[0]) for line in result.splitlines() if line.endswith(' false')}
        assert failed == expected, (label, failed, expected, result)
        report['controls'].append({'name': label, 'failed_cases': sorted(failed)})
        print(f'ok   control {label}: {sorted(failed)}', flush=True)

    # Build a private module snapshot; no source under test is mutated.
    control_core = work / 'paint-control-core'
    shutil.copytree(root / 'core', control_core)
    (control_core / 'repl.ax').write_text(alter(repl_source, 'replHistText', '((None) None)', '((None) (Some ""))'))
    control_env = dict(environment, AXIOM_PATH=str(control_core))
    executable = build('paint-missing-entry', consumer.read_text(), env=control_env)
    result = outcomes(executable)
    failed = {int(line.split()[0]) for line in result.splitlines() if line.endswith(' false')}
    assert failed == {3, 9, 10}, (failed, result)
    report['controls'].append({'name': 'paint-missing-entry', 'failed_cases': sorted(failed)})
    print('ok   control paint-missing-entry: [3, 9, 10]', flush=True)

    invalid = [
        ('entry-string', '(:: bad (-> Hist String)) (fn (bad h) (histAt h 0))', 'AX3004'),
        ('current-string', '(:: bad (-> Hist String)) (fn (bad h) (histCurrent h))', 'AX3004'),
        ('index-option', '(:: bad (-> Hist (Option String))) (fn (bad h) (histAt h None))', 'AX3004'),
        ('unchecked-entry', '(:: bad (-> Hist String)) (fn (bad h) (match (histAt h 0) ((Some s) s)))', 'AX3005'),
    ]
    for label, declaration, code in invalid:
        path = work / (label + '.ax'); path.write_text(source + '\n' + declaration + '\n(:: main Int) (fn (main) 0)\n')
        result = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(path)],
                                cwd=root, env=environment, capture_output=True, text=True, timeout=120)
        (work / (label + '.stderr')).write_text(result.stderr)
        errors = [line for line in result.stderr.splitlines() if line.startswith('E ')]
        assert result.returncode == 1 and len(errors) == 1 and f'E {code} ' in errors[0], (label, result)
        report['refusals'].append({'name': label, 'code': code})
    path = work / 'paint-string.ax'
    path.write_text('(import repl)\n(import replhist)\n(:: bad (-> Hist String))\n(fn (bad h) (replHistText h))\n(:: main Int) (fn (main) 0)\n')
    result = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(path)],
                            cwd=root, env=environment, capture_output=True, text=True, timeout=120)
    (work / 'paint-string.stderr').write_text(result.stderr)
    errors = [line for line in result.stderr.splitlines() if line.startswith('E ')]
    assert result.returncode == 1 and len(errors) == 1 and 'E AX3004 ' in errors[0], result
    report['refusals'].append({'name': 'paint-string', 'code': 'AX3004'})
    (work / 'repl-history-entry-options-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ok   6 effective controls; 5 type/exhaustiveness refusals', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True); parser.add_argument('--work', type=Path)
    args = parser.parse_args(); root = Path(__file__).resolve().parents[2]
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True); run(args.axiom.resolve(), root, args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-repl-entry-') as directory:
            run(args.axiom.resolve(), root, Path(directory))


if __name__ == '__main__':
    main()
