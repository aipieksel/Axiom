#!/usr/bin/env python3
"""Check optional completion selection and its refusal paths."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def verify(compiler, root, work):
    source = (root / 'core/replcomp.ax').read_text()
    fixture = root / 'tests/replcomp/selection-options.axp'
    expected = ''.join(f'{n} true\n' for n in range(1, 15))
    env = dict(os.environ, AXIOM_PATH=str(root / 'core'), AXIOM_STDLIB=str(root / 'stdlib'))
    report = {
        'compiler_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
        'replcomp_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'positive': [], 'controls': [], 'refusals': [],
        'limits': ['Selection, rendering, and candidate bounds only; no global memory-soundness claim.'],
    }

    def exercise(label, level, local_env, positive):
        binary = work / label
        built = subprocess.run(
            [str(compiler), '--diagnostic-format=ai', 'build', str(fixture), '--opt', str(level),
             '--alloc-poison', '-o', str(binary)],
            env=local_env, capture_output=True, text=True, timeout=240)
        assert built.returncode == 0 and not built.stderr, (label, built.returncode, built.stderr)
        run = subprocess.run([str(binary)], env=local_env, capture_output=True, text=True, timeout=30)
        if positive:
            assert run.returncode == 0 and run.stdout == expected and not run.stderr, (
                label, run.returncode, run.stdout, run.stderr)
        else:
            assert run.returncode != 0 or run.stdout != expected or run.stderr, label
        return run

    for level in (0, 2):
        exercise(f'positive-O{level}', level, env, True)
        report['positive'].append({'optimization': level, 'poison': True, 'cases': 14})
        print(f'ok   selection: 14 outcomes O{level} poison', flush=True)

    def change_function(text, before, after, label):
        start = text.index('(pub fn (replComplSelOf ')
        end = text.index('\n(pub ', start + 1)
        body = text[start:end]
        assert body.count(before) == 1, (label, body.count(before))
        return text[:start] + body.replace(before, after, 1) + text[end:]

    variants = [
        ('out-of-range-trusted', change_function(source,
            '(if (&& (>= index 0) (< index (vecLen cands)))',
            '(if true', 'out-of-range-trusted')),
        ('first-index-erased', change_function(source,
            '      (Some index)\n      None)',
            '      None\n      None)', 'first-index-erased')),
        ('idle-forged-first', change_function(source,
            '    None))', '    (Some 0)))', 'idle-forged-first')),
    ]
    for label, variant in variants:
        module_dir = work / (label + '-core')
        module_dir.mkdir()
        for path in (root / 'core').iterdir():
            target = module_dir / path.name
            if path.name == 'replcomp.ax':
                target.write_text(variant)
            else:
                target.symlink_to(path, target_is_directory=path.is_dir())
        run = exercise(label, 0, dict(env, AXIOM_PATH=str(module_dir)), False)
        report['controls'].append({
            'name': label, 'exit': run.returncode, 'changed_output': run.stdout != expected})
        print(f'ok   {label}: effective counterexample', flush=True)

    for label, declaration, code in (
        ('raw-index', '(:: bad Int) (fn (bad) (replComplSelOf vecNew None))', 'AX3004'),
        ('raw-field', '(:: bad ComplSt) (fn (bad) (ComplSt :start 0 :typed "" :cands vecNew :sel -1 :rows 0))', 'AX3004'),
        ('missing-none', '(:: bad Int) (fn (bad) (match (replComplSelOf vecNew None) ((Some index) index)))', 'AX3005'),
        ('missing-some', '(:: bad Int) (fn (bad) (match (replComplSelOf vecNew None) ((None) 0)))', 'AX3005'),
    ):
        path = work / (label + '.ax')
        path.write_text('(import replcomp)\n(import Vec)\n' + declaration +
                        '\n(:: main Int) (fn (main) 0)\n')
        checked = subprocess.run(
            [str(compiler), '--diagnostic-format=ai', 'check', str(path)],
            env=env, capture_output=True, text=True, timeout=90)
        errors = [line for line in checked.stderr.splitlines() if line.startswith('E ')]
        assert checked.returncode == 1 and len(errors) == 1 and f'E {code} ' in errors[0], (
            label, checked.returncode, checked.stderr)
        report['refusals'].append({'name': label, 'code': code})
        print(f'ok   {label}: {code}', flush=True)

    (work / 'report.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', required=True, type=Path)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
        verify(args.axiom.resolve(), root, args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-replcomp-selection-') as directory:
            verify(args.axiom.resolve(), root, Path(directory))


if __name__ == '__main__':
    main()
