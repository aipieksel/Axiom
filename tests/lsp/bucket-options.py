#!/usr/bin/env python3
"""Check lazy LSP bucket ownership, optional misses and typed insertion."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def run(arguments, env):
    return subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=180)


def check(compiler, root, work):
    original = (root / 'core/lsp.ax').read_text()
    source = root / 'tests/selfhost/lsp-bucket-options.ax'
    environment = dict(os.environ, AXIOM_STDLIB=str(root / 'stdlib'))

    def build_run(label, contents, level, expected):
        modules = work / (label + '-modules')
        modules.mkdir()
        for path in (root / 'core').iterdir():
            target = modules / path.name
            if path.name == 'lsp.ax':
                target.write_text(contents)
            else:
                target.symlink_to(path, target_is_directory=path.is_dir())
        local = dict(environment, AXIOM_PATH=str(modules))
        binary = work / label
        result = run([str(compiler), '--diagnostic-format=ai', 'build', str(source),
                      '--opt', str(level), '--alloc-poison', '-o', str(binary)], local)
        assert result.returncode == 0, (label, result.stdout, result.stderr)
        result = run([str(binary)], local)
        assert result.returncode == expected, (label, result.returncode, result.stdout, result.stderr)
        assert result.stdout == '' and result.stderr == '', (label, result.stdout, result.stderr)

    for level in (0, 2):
        build_run('positive-O' + str(level), original, level, 42)
        print('ok   lazy typed LSP buckets at O' + str(level) + ' with allocation poisoning', flush=True)
    marker = '(vecSet buckets slot (Some fresh))'
    assert original.count(marker) == 1
    build_run('missing-owner', original.replace(marker, '()'), 0, 2)
    print('ok   negative probe: unrecorded bucket owner fails the stored-value checks', flush=True)
    marker = '(set entry.key key)'
    assert original.count(marker) == 1
    build_run('stale-key', original.replace(marker, '()'), 0, 2)
    print('ok   negative probe: ignored replacement fails the updated-key check', flush=True)
    invalid = work / 'untyped-bucket.ax'
    invalid.write_text('(import lsp)\n(import Vec)\n(:: main Int)\n(fn (main)\n'
                       '  {\n    (let buckets (:: vecNew (Vec (Vec String))))\n'
                       '    (lspHlBucketPut buckets 0 "value")\n    0\n  })\n')
    local = dict(environment, AXIOM_PATH=str(root / 'core'))
    result = run([str(compiler), '--diagnostic-format=ai', 'check', str(invalid)], local)
    assert result.returncode == 1 and 'AX3004' in result.stderr, (result.returncode, result.stdout, result.stderr)
    assert 'compilation failed due to' in result.stderr
    print('ok   untyped bucket table is refused with AX3004', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', type=Path, required=True)
    parser.add_argument('--work', type=Path)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    compiler = arguments.axiom.resolve()
    if arguments.work:
        work = arguments.work.resolve()
        work.mkdir(parents=True, exist_ok=True)
        check(compiler, root, work)
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-lsp-buckets-') as directory:
            check(compiler, root, Path(directory))


if __name__ == '__main__':
    main()
