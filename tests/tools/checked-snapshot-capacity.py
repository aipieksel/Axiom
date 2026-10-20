#!/usr/bin/env python3
"""Run the production snapshot walker with forced hash collisions."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile


EXPORTS = r'''
(pub :: probeVisit (-> ASTNode (Map Nodes) Bool))
(pub fn (probeVisit node seen) (checkedVisit node seen))

; Key zero occupies slot zero. Read its word without taking a getter
; share, so the count observes only ownership retained by the walker.
(pub :: probeBucketAddress (-> (Map Nodes) Int))
;@axiom:effect(unsafe)
(pub fn (probeBucketAddress seen)
  (__load64 (__load64 (as seen Int) 3) 0))
(pub :: probeBucketCapacity (-> (Map Nodes) Int))
;@axiom:effect(unsafe)
(pub fn (probeBucketCapacity seen) (__load64 (probeBucketAddress seen) 1))
(pub :: probeBucketLength (-> (Map Nodes) Int))
;@axiom:effect(unsafe)
(pub fn (probeBucketLength seen) (__load64 (probeBucketAddress seen) 0))
(pub :: probeBucketCount (-> (Map Nodes) Int))
;@axiom:effect(unsafe)
(pub fn (probeBucketCount seen) (__load64 (probeBucketAddress seen) -2))
(pub :: probeBucketMembers (-> (Map Nodes) ASTNode ASTNode ASTNode Bool))
(pub fn (probeBucketMembers seen first second third)
  {
    (let values (mapGet seen 0 nilNodes))
    (&& (nodeSame (vecGet values 0) first)
      (&& (nodeSame (vecGet values 1) second) (nodeSame (vecGet values 2) third)))
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
'''


def checked_source(root):
    source = (root / 'self_host/checked.ax').read_text()
    begin = source.index('(data CheckedSyntax\n')
    end = source.index('(pub :: checkedSyntaxUnchanged ', begin)
    copy_begin = source.index('(:: checkedCopyDeclarations ')
    copy_end = source.index('(pub :: checkedDeclarationsUnchanged ', copy_begin)
    block = source[copy_begin:copy_end] + source[begin:end]
    key = '(addrHash (addrOf node))'
    assert block.count(key) == 1, 'snapshot hash seam changed'
    return ('(import core)\n(import parser)\n(import Vec)\n(import Map)\n'
            '(import Mem)\n(import Str)\n' + block.replace(key, '0') + EXPORTS)


def run(compiler, root, work, source, name, level):
    directory = work / name
    directory.mkdir(exist_ok=True)
    (directory / 'CheckedSnapshotProbe.ax').write_text(source)
    output = directory / ('snapshot-O' + level)
    env = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'),
               AXIOM_PATH=os.pathsep.join((str(directory), str(root / 'self_host'))))
    build = subprocess.run(
        [compiler, '--diagnostic-format=ai', 'build',
         str(root / 'tests/tools/checked-snapshot-capacity.ax'),
         '--opt', level, '--alloc-poison', '--emit-llvm', '-o', str(output)],
        env=env, cwd=work, capture_output=True, text=True, timeout=180)
    assert build.returncode == 0, (name, level, build.stdout, build.stderr)
    (directory / ('build-O' + level + '.log')).write_text(build.stdout + build.stderr)
    actual = subprocess.run([str(output)], capture_output=True, text=True, timeout=15)
    assert actual.returncode == 0, (name, level, actual.returncode, actual.stderr)
    (directory / ('run-O' + level + '.out')).write_text(actual.stdout)
    return actual.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('compiler')
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    compiler = str(Path(args.compiler).resolve())
    source = checked_source(root)
    expected = ''.join(f'{i} true\n' for i in range(1, 22))
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
        seam = '(let values (vecWithCapacity 1))'
        assert source.count(seam) == 1, 'one-entry constructor seam changed'
        control = source.replace(seam, '(let values vecNew)')
        actual = run(compiler, root, work, control, 'capacity-control', '0')
        assert '4 false\n' in actual, ('capacity control escaped assertion', actual)
        assert actual != expected, 'capacity control unexpectedly passed'
        # Joining a fresh allocation and a borrowed bucket in one binding
        # parks the fresh birth share. The map must be its sole owner.
        seam = re.search(r'\(if \(vecIsNull bucket\)\n.*?true\n\s+\}\)', source, re.S)
        assert seam is not None, 'branch-local ownership seam changed'
        former_phi = '''{
            (let values (if (vecIsNull bucket) (vecWithCapacity 1) bucket))
            (vecPush values node)
            (mapInsert seen key values)
            true
          }'''
        control = source[:seam.start()] + former_phi + source[seam.end():]
        actual = run(compiler, root, work, control, 'ownership-control', '0')
        failed = [line for line in actual.splitlines() if line.endswith(' false')]
        assert failed == ['5 false', '10 false'], ('ownership control', actual)
        print('ok   checked snapshot: 21 cases at O0/O2, capacity and ownership ablations rejected')
    finally:
        if owner is not None:
            owner.cleanup()


if __name__ == '__main__':
    main()
