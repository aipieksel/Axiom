#!/usr/bin/env python3
"""Exercise MIR operand text ownership and the safe former raw-ledger leak."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def run(command, env, stdout, stderr, timeout=180):
    with stdout.open('w') as output, stderr.open('w') as error:
        result = subprocess.run(command, env=env, stdout=output, stderr=error,
                                timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'exit {result.returncode}: {command}\n{stderr.read_text()[-3000:]}')


def function(ir, name):
    found = re.search(r'^define [^\n]*@' + re.escape(name)
                      + r'\([^\n]*\)[^\n]*\{\n(.*?)^\}', ir, re.M | re.S)
    if not found:
        raise RuntimeError(f'missing emitted function: {name}')
    return found.group(1)


def raw_metadata(source):
    """Restore just the old raw operand storage, preserving target instructions."""
    edits = [
        ('(:: mirOperandTexts (-> Int Strings))',
         '(:: mirOperandTexts (-> Int Ints))'),
        ('(pub :: mirParamOps (-> Strings Ints Strings Int Int))',
         '(pub :: mirParamOps (-> Ints Ints Strings Int Int))'),
        ('(pub :: mirEmitInsts (-> Int (Vec MInst) Strings Int Int))',
         '(pub :: mirEmitInsts (-> Int (Vec MInst) Ints Int Int))\n;@axiom:effect(unsafe)'),
        ('(pub :: mirEmitFn (-> Int MFunc Int Int))',
         '(pub :: mirEmitFn (-> Int MFunc Int Int))\n;@axiom:effect(unsafe)'),
        ('(vecPush values "")', '(vecPush values 0)'),
        ('(vecSet ops (vecGet regs i) (concat "%" (llvmSym (strDup (vecGetStr names i)))))',
         '(vecSet ops (vecGet regs i) (as (concat "%" (llvmSym (strDup (vecGetStr names i)))) Int))'),
        ('(vecSet ops n.dst (constStr n.a))',
         '(vecSet ops n.dst (as (constStr n.a) Int))'),
        ('(let a (vecGet ops n.a))', '(let a (as (vecGet ops n.a) String))'),
        ('(let b (vecGet ops n.b))', '(let b (as (vecGet ops n.b) String))'),
        ('(vecSet ops n.dst r)', '(vecSet ops n.dst (as r Int))'),
        ('(cgSetResult cgB (strDup (vecGet ops tm.a)))',
         '(cgSetResult cgB (as (vecGet ops tm.a) String))'),
    ]
    for before, after in edits:
        expected = 2 if before == '(vecSet ops n.dst r)' else 1
        if source.count(before) != expected:
            raise RuntimeError(f'raw metadata seam changed: {before}')
        source = source.replace(before, after)
    return source


def metadata_ir(path, counted):
    ir = path.read_text()
    witness = '1' if counted else '0'
    births = []
    for name, constructors in [('mirParamOps', ['Str$concat']),
                               ('mirEmitInsts', ['codegen$constStr', 'codegen$regStr'])]:
        body = function(ir, 'codegen$' + name)
        stores = re.findall(r'@Vec\$vecSet\(i64 [^,]+, i64 [^,]+, i64 (%[^, ]+), i64 ([01])\)', body)
        if len(stores) != (1 if name == 'mirParamOps' else 3):
            raise RuntimeError(f'{name}: operand storage count changed')
        for value, actual in stores:
            if actual != witness:
                raise RuntimeError(f'{name}: operand text has wrong reference evidence')
            if not any(re.search(re.escape(value) + r' = call i64 @'
                                 + re.escape(constructor) + r'\(', body)
                       for constructor in constructors):
                raise RuntimeError(f'{name}: expected an ordinary operand text birth')
            if body.count(f'call void @axiom_release(i64 {value})') != int(counted):
                raise RuntimeError(f'{name}: operand text birth cleanup did not follow storage')
            births.append({'function': name, 'value': value, 'witness': int(actual),
                           'ordinary_birth_released': counted})
    emitter = function(ir, 'codegen$mirEmitFn')
    ledger = re.findall(r'(%\S+) = call i64 @codegen\$mirOperandTexts\(', emitter)
    if len(ledger) != 1 or emitter.count(f'call void @axiom_release(i64 {ledger[0]})') != 1:
        raise RuntimeError('local MIR operand ledger does not retire once')
    if counted:
        for name in ['mirEmitInsts', 'mirEmitFn']:
            body = function(ir, 'codegen$' + name)
            # Operand getters use String evidence. Other Vec reads in these
            # functions also carry reference evidence for MInst/MBlock values.
            if re.search(r'@Vec\$vecGet\([^\n]*, i64 0\)', body):
                raise RuntimeError(f'{name}: a raw operand read remains')
    return births


def check(axiom, repo, work):
    source = (repo / 'self_host/codegen.ax').read_text()
    fixture = Path(__file__).with_suffix('.ax').read_text()
    expected = Path(__file__).with_suffix('.out').read_text().splitlines()
    if expected != [f'{i} true' for i in range(1, 23)]:
        raise RuntimeError('update the explicit 22-case count')
    environment = dict(os.environ, AXIOM_STDLIB=str(repo / 'stdlib'))

    def build(label, level, codegen, text):
        modules = work / (label + '-modules')
        modules.mkdir()
        for path in (repo / 'self_host').iterdir():
            destination = modules / path.name
            if path.name == 'codegen.ax':
                destination.write_text(codegen)
            else:
                destination.symlink_to(path, target_is_directory=path.is_dir())
        local = dict(environment, AXIOM_PATH=str(modules))
        input_ = work / (label + '.ax')
        input_.write_text(text)
        binary = work / label
        run([str(axiom), '--diagnostic-format=ai', 'build', str(input_),
             '--opt', str(level), '--alloc-poison', '--emit-llvm', '-o', str(binary)],
            local, work / (label + '.build.stdout'), work / (label + '.build.stderr'))
        return binary, local

    report = []
    for level in [0, 2]:
        label = f'owned-O{level}'
        binary, local = build(label, level, source, fixture)
        run([str(binary)], local, work / (label + '.stdout'), work / (label + '.stderr'), 30)
        if (work / (label + '.stdout')).read_text().splitlines() != expected:
            raise RuntimeError(f'{label}: MIR metadata cases changed')
        if (work / (label + '.stderr')).read_text() != 'mir-operand-soak-growth 0\n':
            raise RuntimeError(f'{label}: live compiler text grows across 1000 scopes')
        evidence = metadata_ir(Path(str(binary) + '.ll'), True)
        print(f'ok   MIR operands: 22 checks and zero live-byte growth at -O{level}', flush=True)

        # The old storage is a safe leak: each raw String birth remains parked.
        # Skip ownership-count assertions, but retain identical byte/CG-result
        # comparisons in every measured emission iteration.
        old_fixture = fixture.replace('    (ledgerChecks function cg)\n', '', 1)
        if old_fixture == fixture:
            raise RuntimeError('raw fixture entry seam changed')
        old_fixture = old_fixture.replace('(type OperandTexts Strings)', '(type OperandTexts Ints)', 1)
        old_fixture = old_fixture.replace('(vecPush values "")', '(vecPush values 0)', 1)
        old_fixture = old_fixture.replace('(vecGet values index)', '(as (vecGet values index) String)', 1)
        old_fixture = old_fixture.replace('(:: textAt (-> OperandTexts Int String))\n',
                                          '(:: textAt (-> OperandTexts Int String))\n;@axiom:effect(unsafe)\n', 1)
        label = f'raw-O{level}'
        binary, local = build(label, level, raw_metadata(source), old_fixture)
        run([str(binary)], local, work / (label + '.stdout'), work / (label + '.stderr'), 30)
        if (work / (label + '.stdout')).read_text().splitlines() != ['19 true', '20 false', '21 true', '22 true']:
            raise RuntimeError(f'{label}: the former raw ledger did not fail only the growth check')
        growth = re.fullmatch(r'mir-operand-soak-growth (\d+)\n',
                              (work / (label + '.stderr')).read_text())
        if not growth or int(growth[1]) <= 0:
            raise RuntimeError(f'{label}: the raw ledger did not leak ordinary text')
        raw_evidence = metadata_ir(Path(str(binary) + '.ll'), False)
        report.append({'optimization': level, 'owned': evidence, 'raw': raw_evidence,
                       'raw_live_growth_bytes_per_1000_scopes': int(growth[1])})
        print(f'ok   raw MIR ledger control: identical body, {growth[1]} live bytes retained at -O{level}', flush=True)
    (work / 'mir-operands-report.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True)
    parser.add_argument('--repo', type=Path, required=True,
                        help='isolated source tree; the probe builds no compiler executable')
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    if args.work:
        work = args.work.resolve()
        work.mkdir(parents=True, exist_ok=True)
        check(args.axiom.resolve(), args.repo.resolve(), work)
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-mir-operands-') as directory:
            check(args.axiom.resolve(), args.repo.resolve(), Path(directory))


if __name__ == '__main__':
    main()
