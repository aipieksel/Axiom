#!/usr/bin/env python3
"""Run the production typed snapshot seen set with forced hash collisions."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile


EXPORTS = r'''
(pub :: probeNew CheckedSeen)
(pub fn (probeNew) checkedSeenNew)
(pub :: probeVisit (-> ASTNode CheckedSeen Bool))
(pub fn (probeVisit node seen) (checkedVisit node seen))
(pub :: probeCount (-> CheckedSeen Int))
(pub fn (probeCount seen) seen.count)
(pub :: probeCapacity (-> CheckedSeen Int))
(pub fn (probeCapacity seen) (vecLen seen.slots))
(pub :: probeStorageCapacity (-> CheckedSeen Int))
(pub fn (probeStorageCapacity seen) (vecCap seen.slots))
(pub :: probeSlots (-> CheckedSeen (Vec Addr)))
(pub fn (probeSlots seen) seen.slots)
; Read the typed field's word without creating a getter share.
(pub :: probeSlotsCount (-> CheckedSeen Int))
;@axiom:effect(unsafe)
(pub fn (probeSlotsCount seen) (__load64 (__load64 (as seen Int) 0) -2))
(pub :: probeAt (-> CheckedSeen Int Addr))
(pub fn (probeAt seen index) (vecGet seen.slots index))
(pub :: probeHas (-> ASTNode CheckedSeen Bool))
(pub fn (probeHas node seen)
  (addrEq (addrOf node)
    (vecGet seen.slots (checkedSeenFind (addrOf node) seen.slots))))
(pub :: probeOccupied (-> CheckedSeen Int))
(pub fn (probeOccupied seen)
  {
    (let mut count 0)
    (for node in seen.slots
      (if (! (addrEq node (addrOf nilNode))) (set count (+ count 1))))
    count
  })
(pub :: probeNilCount (-> CheckedSeen Int))
(pub fn (probeNilCount seen) (- (vecLen seen.slots) (probeOccupied seen)))
(pub :: probeOwnerDrop (-> ASTNode Int))
(pub fn (probeOwnerDrop node)
  {
    (let seen checkedSeenNew)
    (checkedVisit node seen)
    0
  })
(pub :: probeOwnerDropDelta (-> ASTNode Int))
(pub fn (probeOwnerDropDelta node)
  {
    (let before (- (__axiom_mem_stat 0) (__axiom_mem_stat 1)))
    (for i in 0..100 (probeOwnerDrop node))
    (- (- (__axiom_mem_stat 0) (__axiom_mem_stat 1)) before)
  })
(pub :: probeSnapshot (-> Nodes (Vec CheckedSyntax)))
(pub fn (probeSnapshot nodes) (checkedSnapshot nodes))
(pub :: probeNodeCount (-> (Vec CheckedSyntax) Int))
(pub fn (probeNodeCount snapshots)
  {
    (let mut count 0)
    (for snapshot in snapshots
      (match snapshot
        ((CheckedNode actual accepted) (set count (+ count 1)))
        (_ 0)))
    count
  })
(pub :: probeUnchanged (-> (Vec CheckedSyntax) Bool))
(pub fn (probeUnchanged snapshots)
  {
    (let mut same true)
    (for snapshot in snapshots
      (if (! (checkedSyntaxSame snapshot)) (set same false)))
    same
  })
(pub :: probeListCount (-> (Vec CheckedSyntax) Int))
(pub fn (probeListCount snapshots)
  {
    (let mut count 0)
    (for snapshot in snapshots
      (match snapshot
        ((CheckedNodes actual accepted) (set count (+ count 1)))
        (_ 0)))
    count
  })
(pub :: probeNameCount (-> (Vec CheckedSyntax) Int))
(pub fn (probeNameCount snapshots)
  {
    (let mut count 0)
    (for snapshot in snapshots
      (match snapshot
        ((CheckedNames actual accepted) (set count (+ count 1)))
        (_ 0)))
    count
  })
(pub :: probeNodeSame (-> ASTNode ASTNode Bool))
(pub fn (probeNodeSame actual accepted)
  (checkedSyntaxSame (CheckedNode actual accepted)))
(pub :: probeNodesSame (-> Nodes Nodes Bool))
(pub fn (probeNodesSame actual accepted)
  (checkedSyntaxSame (CheckedNodes actual accepted)))
(pub :: probeNamesSame (-> Names Names Bool))
(pub fn (probeNamesSame actual accepted)
  (checkedSyntaxSame (CheckedNames actual accepted)))
; Every word of a snapshotted node is guarded: bump each in turn.
(pub :: probeAllWordGuards Bool)
(pub fn (probeAllWordGuards)
  {
    (let mut good true)
    (for slot in 0..13
      {
        (let node (mkEInt 42))
        (let snapshots (checkedSnapshot (vecPush vecNew node)))
        (writeNodeWord node slot (+ (nodeWireWord node slot) 1))
        (if (probeUnchanged snapshots) (set good false))
      })
    good
  })
'''


def checked_source(root):
    source = (root / 'self_host/checked.ax').read_text()
    begin = source.index('(data CheckedSyntax\n')
    end = source.index('(pub :: checkedSyntaxUnchanged ', begin)
    copy_begin = source.index('(:: checkedCopyDeclarations ')
    copy_end = source.index('(pub :: checkedDeclarationsUnchanged ', copy_begin)
    block = source[copy_begin:copy_end] + source[begin:end]
    key = '(addrHash node)'
    assert block.count(key) == 1, 'snapshot hash seam changed'
    # Hash 15 wraps the initial 16-slot cluster across zero, then requires
    # reinsertion rather than preserving old indices when capacity grows.
    return ('(import core)\n(import parser)\n(import Vec)\n'
            '(import Mem)\n(import Str)\n' + block.replace(key, '15') + EXPORTS)


def run(compiler, root, work, source, name, level):
    directory = work / name
    directory.mkdir(exist_ok=True)
    (directory / 'CheckedSnapshotProbe.ax').write_text(source)
    output = directory / ('snapshot-O' + level)
    template = root / 'tests/tools/checked-snapshot-capacity.axp'
    fixture = directory / template.with_suffix('.ax').name
    fixture.write_bytes(template.read_bytes())
    env = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'),
               AXIOM_PATH=os.pathsep.join((str(directory), str(root / 'self_host'))))
    formatted = subprocess.run([compiler, '--diagnostic-format=ai', 'fmt', '--check', str(fixture)],
                               env=env, cwd=work, capture_output=True, text=True, timeout=30)
    assert formatted.returncode == 0, (name, formatted.stdout, formatted.stderr)
    build = subprocess.run(
        [compiler, '--diagnostic-format=ai', 'build',
         str(fixture),
         '--opt', level, '--alloc-poison', '--emit-llvm', '-o', str(output)],
        env=env, cwd=work, capture_output=True, text=True, timeout=180)
    (directory / ('build-O' + level + '.log')).write_text(build.stdout + build.stderr)
    assert build.returncode == 0, (name, level, build.stdout, build.stderr)
    actual = subprocess.run([str(output)], capture_output=True, text=True, timeout=15)
    (directory / ('run-O' + level + '.out')).write_text(actual.stdout)
    assert actual.returncode == 0, (name, level, actual.returncode, actual.stderr)
    return actual.stdout


def function_ir(module, name):
    match = re.search(r'^define [^\n]*@"?' + re.escape(name)
                      + r'"?\([^\n]*\) [^\n]*\{\n.*?^\}', module, re.M | re.S)
    assert match is not None, ('missing emitted function', name)
    return match.group()


def assert_owner_release(work):
    module = (work / 'positive/snapshot-O0.ll').read_text()
    for function in ('CheckedSnapshotProbe$checkedSnapshot',
                     'CheckedSnapshotProbe$probeOwnerDrop'):
        body = function_ir(module, function)
        constructor = re.search(r'(?P<value>%[^ ]+) = call i64 '
                                r'@"?CheckedSnapshotProbe\$checkedSeenNew"?\(', body)
        assert constructor is not None, ('seen constructor missing', function)
        release = 'call void @axiom_release(i64 ' + constructor['value'] + ')'
        assert release in body, ('seen owner is parked', function, body)


def rejected(actual, required, name):
    lines = actual.splitlines()
    assert len(lines) == 62, (name, 'incomplete assertions', actual)
    failed = {int(line.split()[0]) for line in lines if line.endswith(' false')}
    assert required <= failed, (name, 'negative control escaped assertions', failed)
    return sorted(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('compiler')
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    compiler = str(Path(args.compiler).resolve())
    source = checked_source(root)
    expected = ''.join(f'{i} true\n' for i in range(1, 63))
    owner = None
    if args.work is None:
        owner = tempfile.TemporaryDirectory(prefix='axiom-checked-snapshot-')
        work = Path(owner.name)
    else:
        work = args.work.resolve()
        work.mkdir(parents=True, exist_ok=True)
    try:
        for level in ('0', '2'):
            actual = run(compiler, root, work, source, 'positive', level)
            assert actual == expected, (level, 'snapshot assertions failed', actual)
        assert_owner_release(work)
        seam = '(addrEq node other)'
        assert source.count(seam) == 1, 'identity comparison seam changed'
        control = source.replace(seam,
            '(== (checkedSeenIndex node capacity) (checkedSeenIndex other capacity))')
        equality = rejected(run(compiler, root, work, control, 'equality-control', '0'),
                            {14, 16, 17}, 'equality')
        seam = re.compile(r'(\(vecSet slots )'
                          r'\(checkedSeenFind node slots\)( node\))')
        assert len(seam.findall(source)) == 1, 'growth reinsertion seam changed'
        control = seam.sub(r'\1i\2', source)
        rehash = rejected(run(compiler, root, work, control, 'rehash-control', '0'),
                          {24, 25, 35}, 'rehash')
        seam = '(< slot 13)'
        assert source.count(seam) == 1, 'node word guard seam changed'
        words = rejected(run(compiler, root, work, source.replace(seam, '(< slot 12)'),
                             'node-words-control', '0'), {62}, 'node words')
        seam = '(== (vecLen actual) (vecLen accepted))'
        assert source.count(seam) == 2, 'list length guard seams changed'
        control = source.replace(seam, '(>= (vecLen actual) (vecLen accepted))')
        lengths = rejected(run(compiler, root, work, control, 'length-control', '0'),
                           {53, 58}, 'list lengths')
        seam = '(nodeSame (vecGet actual i) (vecGet accepted i))'
        assert source.count(seam) == 1, 'child identity guard seam changed'
        control = source.replace(seam,
            '(== (nodeTag (vecGet actual i)) (nodeTag (vecGet accepted i)))')
        children = rejected(run(compiler, root, work, control, 'child-identity-control', '0'),
                            {55}, 'child identity')
        seam = '(addrEq (addrOf (vecGet actual i)) (addrOf (vecGet accepted i)))'
        assert source.count(seam) == 1, 'name identity guard seam changed'
        control = source.replace(seam, '(strEq (vecGet actual i) (vecGet accepted i))')
        names = rejected(run(compiler, root, work, control, 'name-identity-control', '0'),
                         {47, 60}, 'name identity')
        print('ok   checked snapshot: 62 cases at O0/O2; exact owner releases; '
              f'equality {equality}; rehash {rehash}; node words {words}; '
              f'list lengths {lengths}; child identity {children}; name identity {names}')
    finally:
        if owner is not None:
            owner.cleanup()


if __name__ == '__main__':
    main()
