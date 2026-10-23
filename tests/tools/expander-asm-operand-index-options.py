#!/usr/bin/env python3
"""Check typed assembler indexes, their real callers and preserved diagnostics."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def check(compiler, root, work):
    source = (root / 'core/expand.ax').read_text()
    fixture = root / 'tests/tools/expander-asm-operand-index-options.axp'
    expected = ''.join(f'{i} true\n' for i in range(1, 28))
    env = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))
    report = {'builder_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
              'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
              'configurations': [], 'diagnostics': [], 'controls': [], 'refusals': [],
              'limits': ['Native O0/O2 allocation-poison checks and existing AXDL messages; no memory soundness claim.',
                         'Assembler lookup indexes change; generated slot numbers and AsmOp fields remain separate.']}

    def modules(label, text):
        directory = work / (label + '-core')
        directory.mkdir()
        for path in (root / 'core').iterdir():
            target = directory / path.name
            if path.name == 'expand.ax':
                target.write_text(text)
            else:
                target.symlink_to(path, target_is_directory=path.is_dir())
        return dict(env, AXIOM_PATH=str(directory))

    def build_run(label, text, level, passing=True):
        local = modules(label, text)
        binary = work / label
        built = subprocess.run([str(compiler), '--diagnostic-format=ai', 'build', str(fixture),
                                '--opt', str(level), '--alloc-poison', '-o', str(binary)],
                               env=local, capture_output=True, text=True, timeout=240)
        assert built.returncode == 0 and not built.stderr, (label, built.returncode, built.stderr)
        ran = subprocess.run([str(binary)], env=local, capture_output=True, text=True, timeout=30)
        if passing:
            assert ran.returncode == 0 and ran.stdout == expected and not ran.stderr, (label, ran)
        else:
            assert ran.returncode != 0 or ran.stdout != expected or ran.stderr, label
        return ran

    for level in (0, 2):
        build_run('positive-O' + str(level), source, level)
        report['configurations'].append({'optimization': level, 'poison': True, 'cases': 27})
        print(f'ok   operand indexes: 27 outcomes O{level} poison', flush=True)

    diagnostic_files = ('1044-inline-asm', '1280-inline-asm-forms', '1281-asm-operand-unused')
    def diagnostics(native, expected_equal):
        local = dict(env, AXIOM_PATH=str(root / 'core'))
        for name in diagnostic_files:
            folder = root / 'tests/diagnostics'
            result = subprocess.run([str(native), '--diagnostic-format=ai', 'check', name + '.ax'],
                                    cwd=folder, env=local, capture_output=True, text=True, timeout=90)
            lines = ''.join(line + '\n' for line in result.stderr.splitlines() if line.startswith(('E ', 'W ')))
            golden = (folder / (name + '.axdl')).read_text()
            if expected_equal:
                assert result.returncode == 1 and lines == golden, (name, result.returncode, lines)
                report['diagnostics'].append({'name': name, 'bytes': len(lines.encode())})
            elif name == '1280-inline-asm-forms':
                assert result.returncode == 1 and lines != golden, name
    diagnostics(compiler, True)

    def replace_once(text, before, after, label):
        assert text.count(before) == 1, (label, text.count(before))
        return text.replace(before, after, 1)

    def replace_in_function(text, function, before, after, label):
        start = text.index('(pub fn (' + function + ' ')
        end = text.find('\n(pub ', start + 1)
        assert end > start, label
        part = text[start:end]
        assert part.count(before) == 1, (label, part.count(before))
        return text[:start] + part.replace(before, after, 1) + text[end:]

    missing = replace_in_function(
        source, 'expAsmOpAt',
        '(if (|| (< i 0) (>= i (vecLen ops)))\n    None',
        '(if (|| (< i 0) (>= i (vecLen ops)))\n    (Some 0)', 'missing-forged')
    no_index = replace_once(source, '(if (strEq p.name nm)\n        (Some i)',
                            '(if (strEq p.name nm)\n        None', 'present-erased')
    bad_start = replace_in_function(
        source, 'expAsmOpAt', '(if (|| (< i 0) (>= i (vecLen ops)))',
        '(if (>= i (vecLen ops))', 'negative-start-unchecked')
    no_name = replace_in_function(
        source, 'expAsmNameAt', '(if (strEq (vecGetStr names i) nm)\n      (Some i)',
        '(if (strEq (vecGetStr names i) nm)\n      None', 'name-present-erased')
    no_answer = replace_in_function(
        source, 'expAsmAnswered', '(if (== k 0)\n          (Some i)',
        '(if (== k 0)\n          None', 'answer-present-erased')
    no_unused = replace_in_function(
        source, 'expAsmFirstUnused',
        '(if (&& (== (vecGet used i) 0) (== (strLen p.reg) 0))\n        (Some i)',
        '(if (&& (== (vecGet used i) 0) (== (strLen p.reg) 0))\n        None',
        'unused-present-erased')
    for label, text in [('missing-forged', missing), ('present-erased', no_index),
                        ('negative-start-unchecked', bad_start),
                        ('name-present-erased', no_name), ('answer-present-erased', no_answer),
                        ('unused-present-erased', no_unused)]:
        result = build_run(label, text, 0, False)
        report['controls'].append({'name': label, 'returncode': result.returncode,
                                   'changed_output': result.stdout != expected})
        print(f'ok   {label}: effective counterexample', flush=True)

    lost_duplicate = replace_once(source, '(let prev (expAsmOpAt ops nm 0))',
                                  '(let prev (:: None (Option Int)))', 'duplicate-operand-unchecked')
    variant = modules('duplicate-operand-unchecked', lost_duplicate)
    main = work / 'duplicate-operand-unchecked-main.ax'
    main.write_bytes((root / 'core/main.ax').read_bytes())
    mutant_compiler = work / 'duplicate-operand-unchecked-compiler'
    built = subprocess.run([str(compiler), '--diagnostic-format=ai', 'build', str(main),
                            '--opt', '1', '-o', str(mutant_compiler)],
                           env=variant, capture_output=True, text=True, timeout=240)
    assert built.returncode == 0 and not built.stderr, ('mutant compiler', built.returncode, built.stderr)
    duplicate = root / 'tests/tools/expander-asm-operand-duplicate.axp'
    good = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(duplicate)],
                          env=dict(env, AXIOM_PATH=str(root / 'core')), capture_output=True, text=True, timeout=90)
    bad = subprocess.run([str(mutant_compiler), '--diagnostic-format=ai', 'check', str(duplicate)],
                         env=dict(env, AXIOM_PATH=str(root / 'core')), capture_output=True, text=True, timeout=90)
    good_errors = [line for line in good.stderr.splitlines() if line.startswith('E ')]
    bad_errors = [line for line in bad.stderr.splitlines() if line.startswith('E ')]
    assert good.returncode == 1 and len(good_errors) == 1 and 'second operand named' in good_errors[0]
    assert bad_errors != good_errors, ('duplicate operand mutant not effective', bad.returncode, bad.stderr)
    report['controls'].append({'name': 'duplicate-operand-unchecked', 'changed_diagnostic': True})
    print('ok   duplicate operand: effective diagnostic counterexample', flush=True)

    local = modules('refusals', source)
    for label, declaration, code in (
        ('raw-index', '(:: bad Int) (fn (bad) (expAsmOpAt vecNew "x" 0))', 'AX3004'),
        ('raw-name-index', '(:: bad Int) (fn (bad) (expAsmNameAt vecNew "x" 0))', 'AX3004'),
        ('raw-answered-index', '(:: bad Int) (fn (bad) (expAsmAnswered vecNew 0 0))', 'AX3004'),
        ('raw-unused-index', '(:: bad Int) (fn (bad) (expAsmFirstUnused vecNew vecNew 0))', 'AX3004'),
        ('missing-none', '(:: bad Int) (fn (bad) (match (expAsmOpAt vecNew "x" 0) ((Some index) index)))', 'AX3005'),
        ('missing-some', '(:: bad Int) (fn (bad) (match (expAsmOpAt vecNew "x" 0) ((None) 0)))', 'AX3005'),
    ):
        path = work / (label + '.ax')
        path.write_text('(import core)\n(import expand)\n(import Vec)\n' + declaration +
                        '\n(:: main Int) (fn (main) 0)\n')
        result = subprocess.run([str(compiler), '--diagnostic-format=ai', 'check', str(path)],
                                env=local, capture_output=True, text=True, timeout=90)
        errors = [line for line in result.stderr.splitlines() if line.startswith('E ')]
        assert result.returncode == 1 and len(errors) == 1 and f'E {code} ' in errors[0], (label, result)
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
        check(args.axiom.resolve(), root, args.work.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-expander-asm-index-options-') as directory:
            check(args.axiom.resolve(), root, Path(directory))


if __name__ == '__main__':
    main()
