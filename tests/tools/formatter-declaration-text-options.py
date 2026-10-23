#!/usr/bin/env python3
"""Check optional formatter declaration text, grouping and owning results."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(compiler, root, work):
    module = root / 'core/format.ax'; source = module.read_text()
    fixture = root / 'tests/tools/formatter-declaration-text-options.axp'
    golden_path = root / 'tests/tools/formatter-declaration-text-options.golden'
    golden = json.loads(golden_path.read_text())
    extra = fixture.read_text()
    for import_line in ('(import IO)', '(import Fmt)', '(import Sys)'):
        extra = extra.replace(import_line, '')
    imports = '(import IO)\n(import Fmt)\n(import Sys)\n'
    environment = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'), AXIOM_PATH=str(root / 'core'))
    expected = ''.join(f'{i} true\n' for i in range(1, 41))
    report = {'compiler': str(compiler), 'compiler_sha256': digest(compiler),
              'source_sha256': digest(module), 'fixture_sha256': digest(fixture),
              'golden_sha256': digest(golden_path), 'configurations': [], 'controls': [],
              'refusals': [], 'formats': [],
              'invariants': ['Missing declaration text differs from a present empty token.',
                             'A qualified macro head has no atom text and still prints as an invocation.',
                             'Only matching present nonempty names join a signature to a function.',
                             'Returned name text survives its source; queries and formatting borrow the source.'],
              'remaining': ['fnText/fnHeadText text absence',
                            'flat-layout failure String APIs and FRes tagged status/text'],
              'limits': ['Host O0/O2 allocation-poison probes; CI runs complete target and formatter suites.',
                         'Synthetic empty FTok ranges test the owning FNode API; scanning does not produce empty identifiers.']}

    def build(label, text, level=0):
        path = work / (label + '.ax'); path.write_text(imports + text + '\n' + extra)
        executable = work / label
        built = subprocess.run([str(compiler), '--diagnostic-format=ai', 'build', str(path),
                                '--opt', str(level), '--alloc-poison', '-o', str(executable)],
                               cwd=root, env=environment, capture_output=True, text=True, timeout=240)
        (work / (label + '.build.stdout')).write_text(built.stdout)
        (work / (label + '.build.stderr')).write_text(built.stderr)
        assert built.returncode == 0, (label, built.returncode, built.stdout, built.stderr)
        return executable

    binaries = []
    for level in (0, 2):
        executable = build('declarations-O' + str(level), source, level)
        result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True, timeout=30)
        (work / ('declarations-O' + str(level) + '.stdout')).write_text(result.stdout)
        assert result.returncode == 0 and not result.stderr and result.stdout == expected, result
        binaries.append(executable)
        report['configurations'].append({'optimization': level, 'allocation_poisoning': True,
                                         'outcomes': 40, 'status': 'passed'})
        print(f'ok   declaration text: 40 outcomes O{level}', flush=True)

    def alter(name, old, new, count=1):
        start = source.index('(fn (' + name + ' ')
        next_signature = re.search(r'^\((?:pub )?:: ', source[start:], re.M)
        end = start + next_signature.start() if next_signature else len(source)
        body = source[start:end]
        assert body.count(old) == count, (name, old, body.count(old))
        return source[:start] + body.replace(old, new) + source[end:]

    controls = [
        ('discard-empty-head', alter('fpDeclHead', '(Some (ftText src (fnTok head)))',
                                    '(if (== (strLen (ftText src (fnTok head))) 0) None (Some (ftText src (fnTok head))))'), {18}),
        ('discard-empty-name', alter('fpDeclName', '(Some (ftText src (fnTok x)))',
                                    '(if (== (strLen (ftText src (fnTok x))) 0) None (Some (ftText src (fnTok x))))', 2), {19}),
        ('invent-missing-head', alter('fpDeclHead', 'None', '(Some "")', 2), {1, 2, 3, 7, 38}),
        ('join-empty-names', alter('fpDeclNamesStick', '(&& (!= (strLen name) 0) (strEq name next))',
                                  '(strEq name next)'), {21}),
        ('reject-qualified-invocation', alter('fpDecl', '((None) (fpDeclInvocation p n))',
                                            '((None) (fpBad p))'), {35, 36}),
        ('discard-names', alter('fpDeclName', '(match (fpDeclHead src n)',
                               '(match (:: None (Option String))'), {9, 10, 11, 19, 28, 29}),
        ('retain-borrowed-source', alter('fpDeclName', '{\n    (let b',
                                        '{\n    (__retainref src)\n    (let b'), {30, 40}),
    ]
    for label, changed, failures in controls:
        executable = build(label, changed)
        result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True, timeout=30)
        failed = {int(line.split()[0]) for line in result.stdout.splitlines() if line.endswith(' false')}
        assert result.returncode == 0 and not result.stderr and failed == failures, (
            label, failed, failures, result.returncode, result.stdout, result.stderr)
        report['controls'].append({'name': label, 'failed_cases': sorted(failed)})
        print(f'ok   control {label}: {sorted(failed)}', flush=True)

    invalid = [
        ('head-string-result', '(:: bad (-> FNode String)) (fn (bad n) (fpDeclHead "" n))', 'AX3004'),
        ('name-string-result', '(:: bad (-> FNode String)) (fn (bad n) (fpDeclName "" n))', 'AX3004'),
        ('unchecked-head-match', '(:: bad (-> FNode String)) (fn (bad n) (match (fpDeclHead "" n) ((Some text) text)))', 'AX3005'),
        ('raw-head-state', '(:: bad Bool) (fn (bad) (fpDeclHeadIs "" "fn"))', 'AX3004'),
        ('raw-name-state', '(:: bad Bool) (fn (bad) (fpDeclNamesStick "" None))', 'AX3004'),
    ]
    for label, declaration, code in invalid:
        path = work / (label + '.ax'); path.write_text(source + '\n' + declaration + '\n(:: main Int) (fn (main) 0)\n')
        result = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(path)],
                                cwd=root, env=environment, capture_output=True, text=True, timeout=120)
        (work / (label + '.stderr')).write_text(result.stderr)
        errors = [line for line in result.stderr.splitlines() if line.startswith('E ')]
        assert result.returncode == 1 and len(errors) == 1 and f'E {code} ' in errors[0], (label, result)
        report['refusals'].append({'name': label, 'code': code})

    assert len(golden['cases']) == 16
    for row in golden['cases']:
        outputs = []
        for executable in binaries:
            result = subprocess.run([str(executable), 'format', row['source']], env=environment,
                                    capture_output=True, text=True, timeout=30)
            assert result.returncode == 0 and not result.stderr, (row['name'], result)
            assert result.stdout == str(row['ok']) + '\n' + row['text'], (row, result.stdout)
            outputs.append(result.stdout)
            if row['ok']:
                fixed = subprocess.run([str(executable), 'format', row['text']], env=environment,
                                       capture_output=True, text=True, timeout=30)
                assert fixed.returncode == 0 and fixed.stdout == result.stdout, (row['name'], 'not fixed', fixed)
        assert outputs[0] == outputs[1], row['name']
        report['formats'].append({'name': row['name'], 'ok': row['ok'], 'bytes': len(row['text'].encode()),
                                  'sha256': hashlib.sha256(row['text'].encode()).hexdigest()})
    (work / 'formatter-declaration-options-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ok   16 byte-exact format cases and fixedpoints; 7 controls; 5 refusals', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True); parser.add_argument('--work', type=Path)
    args = parser.parse_args(); root = Path(__file__).resolve().parents[2]
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True); run(args.axiom.resolve(), root, args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-formatter-declarations-') as directory:
            run(args.axiom.resolve(), root, Path(directory))


if __name__ == '__main__':
    main()
