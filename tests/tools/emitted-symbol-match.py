#!/usr/bin/env python3
"""Compare emitted-line symbol matching with the former allocated needles."""

import argparse
from pathlib import Path
import os
import random
import re
import runpy
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
scan = runpy.run_path(str(ROOT / 'scripts/lib/fuzz.py'))['scan']
OLD_SCANNERS = '''
(pub :: oldLinesCallSym (-> Ints String Int Int))
;@axiom:effect(unsafe)
(pub fn (oldLinesCallSym lines sym i)
  (if (>= i (vecLen lines))
    0
    (if (|| (== (strHasSub (as (vecGet lines i) String) (concat "@" (concat sym "("))) 1)
          (oldAsmLineNamesSym (as (vecGet lines i) String) sym))
      1
      (oldLinesCallSym lines sym (+ i 1)))))

(pub :: oldAsmLineNamesSym (-> String String Bool))
(pub fn (oldAsmLineNamesSym line sym)
  (if (== (strHasSub line " asm ") 0)
    false
    (|| (== (strHasSub line (concat (concat "ptr @" sym) ",")) 1)
      (== (strHasSub line (concat (concat "ptr @" sym) ")")) 1))))
'''
SUPPORT = '''
(import Str)
(import Vec)
(type Ints (Vec Int))

(:: sameBool (-> Bool Bool Bool))
(fn (sameBool a b) (if a b (! b)))

(:: hexDigit (-> Int Int))
(fn (hexDigit byte)
  (if (<= byte 57) (- byte 48) (+ (- byte 97) 10)))

(:: byteString (-> String String))
;@axiom:effect(unsafe)
(fn (byteString hex)
  {
    (let n (/ (strLen hex) 2))
    (let text (strAlloc n))
    (for i in 0..n
      (__store8 (strData text) i
        (+ (* (hexDigit (strByte hex (* i 2))) 16)
          (hexDigit (strByte hex (+ (* i 2) 1))))))
    text
  })

(:: checkSegments (-> String String String String Bool Bool))
(fn (checkSegments hh ph mh sh expected)
  {
    (let hay (byteString hh))
    (let prefix (byteString ph))
    (let middle (byteString mh))
    (let suffix (byteString sh))
    (let actual (strHasSegments hay prefix middle suffix))
    (let old (== (strHasSub hay (concat prefix (concat middle suffix))) 1))
    (&& (sameBool actual expected) (sameBool actual old))
  })

(:: checkLine (-> String String Bool Bool Bool))
(fn (checkLine hh sh expected asmExpected)
  {
    (let line (byteString hh))
    (let sym (byteString sh))
    (let lines vecNew)
    (for i in 0..2
      (vecPush lines (as line Int)))
    (&& (sameBool (asmLineNamesSym line sym) asmExpected)
      (&& (sameBool (asmLineNamesSym line sym) (oldAsmLineNamesSym line sym))
        (&& (sameBool (== (linesCallSym lines sym 0) 1) expected)
          (&& (== (linesCallSym lines sym 0) (oldLinesCallSym lines sym 0))
            (&& (== (linesCallSym lines sym 1) (oldLinesCallSym lines sym 1))
              (&& (== (linesCallSym lines sym 2) 0)
                (== (linesCallSym lines sym 3) 0)))))))
  })
'''


def definition(source, name):
    """Copy only a named signature/function pair, never its imports."""
    signature = re.search(r'^\((?:pub )?:: ' + re.escape(name) + r'\s', source, re.M)
    assert signature, ('signature missing', name)
    function = re.search(r'^\((?:pub )?fn \(' + re.escape(name) + r'(?:\s|\))',
                         source[signature.end():], re.M)
    assert function, ('function missing', name)
    start = signature.end() + function.start()
    # All tested functions use the formatter's indented body form. The
    # next column-zero declaration cannot be a child of this function.
    following = re.search(r'^\(', source[start + 1:], re.M)
    end = start + 1 + following.start() if following else len(source)
    forms, _ = scan(source[start:end])
    assert forms and forms[0].kind == 'form', ('function form missing', name)
    return source[signature.start():start + forms[0].end].rstrip() + '\n'


def cases():
    rng = random.Random(74031)
    alphabet = bytes((0, 1, 9, 10, 13, 32, 34, 40, 41, 44, 64, 92, 97, 98, 128, 255))

    def word(length):
        return bytes(rng.choice(alphabet) for _ in range(length))

    segments = [(b'', b'', b'', b''), (b'abc', b'', b'', b''),
                (b'abc', b'a', b'b', b'c'), (b'ab', b'a', b'b', b'c'),
                (b'\0\xffx', b'\0', b'\xff', b'x'),
                (b'aaaaab', b'aa', b'aa', b'ab')]
    for _ in range(240):
        prefix, middle, suffix = word(rng.randrange(5)), word(rng.randrange(13)), word(rng.randrange(5))
        needle = prefix + middle + suffix
        segments.append((word(rng.randrange(8)) + needle + word(rng.randrange(8)), prefix, middle, suffix))
        segments.append((word(rng.randrange(28)), prefix, middle, suffix))
    lines = []
    names = [b'', b'f', b'ff', b'"quoted name"', b'"a\\22b"',
             b'\0', b'x\0y', b'\xff\x80', b'a,b)', b' asm ']
    for sym in names:
        lines.extend((hay, sym) for hay in (
            b'', b'@' + sym + b'(', b'prefix @' + sym + b'(suffix',
            b'@' + sym, b'@' + sym + b'x(', b'ptr @' + sym + b',',
            b' asm ptr @' + sym + b',', b' asm ptr @' + sym + b')',
            b' asm ptr @' + sym + b' ', b'asm ptr @' + sym + b',',
            b' ASM ptr @' + sym + b')', b' asm "ptr @' + sym + b'),"'))
    for _ in range(80):
        lines.append((word(rng.randrange(48)), word(rng.randrange(12))))
    return segments, lines


def fixture(root, raw_source, guard_mutation=False):
    source = (root / 'core/codegen.ax').read_text()
    candidate = ''.join(definition(source, name) for name in
                        ('linesCallSym', 'asmLineNamesSym', 'strHasSegments'))
    if guard_mutation:
        seam = '(strHasSub line " asm ")'
        assert candidate.count(seam) == 1
        candidate = candidate.replace(seam, '1')
    reference = ''.join(definition(source, name) for name in ('strHasSub', 'strHasSubFrom'))
    raw = Path(raw_source).read_text()
    reference += ''.join(definition(raw, name) for name in ('memCmp', 'memCmpFrom'))
    segments, lines = cases()
    assertions = []
    for hay, prefix, middle, suffix in segments:
        expected = str(prefix + middle + suffix in hay).lower()
        arguments = ' '.join('"' + value.hex() + '"' for value in (hay, prefix, middle, suffix))
        assertions.append(f'    (if (! (checkSegments {arguments} {expected})) (set failed true))')
    for hay, sym in lines:
        asm_expected = b' asm ' in hay and (b'ptr @' + sym + b',' in hay or b'ptr @' + sym + b')' in hay)
        expected = b'@' + sym + b'(' in hay or asm_expected
        assertions.append(f'    (if (! (checkLine "{hay.hex()}" "{sym.hex()}" '
                          f'{str(expected).lower()} {str(asm_expected).lower()})) (set failed true))')
    main = '\n(:: main Int)\n(fn (main)\n  {\n    (let mut failed false)\n'
    main += '\n'.join(assertions) + '\n    (if failed 1 42)\n  })\n'
    return SUPPORT + candidate + reference + OLD_SCANNERS + main, len(segments), len(lines)


def function_ir(module, name):
    found = re.search(r'^define [^\n]*@' + re.escape(name) + r'\([^\n]*\n.*?^}', module, re.M | re.S)
    assert found, ('IR function missing', name)
    return found.group()


def no_pattern_allocation(module, name):
    body = function_ir(module, name)
    calls = re.findall(r'call [^\n]*@([^ (]+)\(', body)
    assert not any(call in {'axiom_alloc', 'Str$concat', 'Str$strAlloc', 'Str$strSlice',
                           'Str$strDup', 'Vec$vecNew'} for call in calls), (name, calls)
    allowed = {
        'linesCallSym': {'Vec$vecGet', 'Vec$vecLen', 'asmLineNamesSym',
                         'strHasSegments', 'axiom_retain', 'axiom_release'},
        'asmLineNamesSym': {'strHasSub', 'strHasSegments'},
        'strHasSegments': {'Str$strByte', 'Str$strLen'},
    }
    if name in allowed:
        assert set(calls) <= allowed[name], ('unexpected scanner callee', name, calls)
    return body


def run(compiler, work, source, level, env, name='positive', expected=42):
    directory = work / name
    directory.mkdir(exist_ok=True)
    entry = directory / 'scanner.ax'
    entry.write_text(source)
    output = directory / ('scanner-O' + level)
    result = subprocess.run(
        [compiler, '--diagnostic-format=ai', 'build', str(entry), '--opt', level,
         '--alloc-poison', '--emit-llvm', '-o', str(output)],
        env=env, cwd=work, text=True, capture_output=True, timeout=180)
    (directory / ('build-O' + level + '.log')).write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (name, level, result.stderr)
    assert not any(line.startswith(('E ', 'W ')) for line in result.stderr.splitlines()), result.stderr
    actual = subprocess.run([str(output)], cwd=work, capture_output=True, timeout=30)
    assert actual.returncode == expected and not actual.stdout and not actual.stderr, (
        name, level, actual.returncode, actual.stdout, actual.stderr)
    module = output.with_suffix('.ll').read_text()
    assert 'store i64 -6510615555426900571' in module, 'allocator poisoning was not emitted'
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('compiler')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--stdlib', type=Path)
    parser.add_argument('--raw-source', type=Path)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix='axiom-emitted-symbols-')
    work = (args.work or Path(temporary.name)).resolve()
    work.mkdir(parents=True, exist_ok=True)
    stdlib = (args.stdlib or args.root / 'stdlib').resolve()
    raw = args.raw_source or args.root / 'core/raw.ax'
    env = dict(os.environ, AXIOM_STDLIB=str(stdlib))
    env.pop('AXIOM_PATH', None)
    source, segments, lines = fixture(args.root, raw)
    for level in ('0', '2'):
        module = run(args.compiler, work, source, level, env)
        for name in ('linesCallSym', 'asmLineNamesSym', 'strHasSegments'):
            no_pattern_allocation(module, name)
        # The same compiler's emitted old scanners must violate this IR
        # property, even though they agree on every observable result.
        for name in ('oldLinesCallSym', 'oldAsmLineNamesSym'):
            old_body = function_ir(module, name)
            assert '@Str$concat(' in old_body, ('old allocation witness missing', name, level)
            try:
                no_pattern_allocation(module, name)
            except AssertionError:
                pass
            else:
                raise AssertionError(('allocated-pattern negative control passed', name, level))
    mutated, _, _ = fixture(args.root, raw, guard_mutation=True)
    run(args.compiler, work, mutated, '0', env, name='without-asm-guard', expected=1)
    print(f'emitted symbol match: {segments} segment and {lines} line cases at O0/O2 '
          'with allocator poisoning; allocation and asm-guard controls refused')


if __name__ == '__main__':
    main()
