#!/usr/bin/env python3
"""Execute an internal AST fault probe before counting its primary diagnostic.

The fixture is compiled against a frozen copy of the current compiler modules.
It calls the production extractor with a bad precondition and with a corrupted
scan memo. No source program is claimed to reach either internal state.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tempfile

SUPPORT = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'selfhost/emitter-line-buffer.py'))
run_checked = SUPPORT['run_checked']
replace_once = SUPPORT['replace_once']
CODE = 'AX3146'
FORMATS = {'ai': 'axdl', 'human': 'human', 'json': 'json'}


def validate(result, mode, fmt, expected):
    if result.returncode != 1 or result.stdout:
        raise AssertionError((mode, fmt, 'expected exit 1 and no successful output', result.returncode, result.stdout))
    if result.stderr != expected:
        raise AssertionError((mode, fmt, 'diagnostic differs from golden', result.stderr))
    if fmt == 'ai':
        assert b'\x1b' not in result.stderr
        assert len(result.stderr.splitlines()) == 1
        assert result.stderr.startswith(b'E AX3146 internal-propagate.ax:1:1-2 propagate-invariant ')
    elif fmt == 'json':
        row = json.loads(result.stderr)
        assert row['severity'] == 'error' and row['code'] == CODE
        assert row['slug'] == 'propagate-invariant'
        assert row['span']['start'] == {'line': 1, 'col': 1}
        assert row['span']['end'] == {'line': 1, 'col': 2}
    else:
        assert b'AX3146' in result.stderr and b'compiler' in result.stderr


def check(compiler, root, work, bless=False):
    fixtures = root / 'tests/diagnostics/internal'
    fixture = (fixtures / 'propagate-invariant.axp').read_text()
    frozen = work / 'frozen'
    shutil.copytree(root / 'core', frozen / 'core')
    shutil.copytree(root / 'stdlib', frozen / 'stdlib')
    source = (frozen / 'core/expand.ax').read_text()
    env = dict(os.environ, AXIOM_STDLIB=str(frozen / 'stdlib'))
    report = {'builder': str(compiler), 'builder_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
              'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
              'fixture_sha256': hashlib.sha256(fixture.encode()).hexdigest(),
              'inputs': {str(p.relative_to(frozen)): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted(frozen.rglob('*.ax'))},
              'configurations': [], 'controls': [], 'observed_primary_codes': [],
              'limits': ['Injected internal AST faults, not a user-source refusal.',
                         'Host native O0/O2 allocation poisoning; no target-wide soundness claim.']}

    def build(label, contents, level):
        modules = work / (label + '-modules')
        modules.mkdir()
        for path in (frozen / 'core').iterdir():
            dest = modules / path.name
            if path.name == 'expand.ax':
                dest.write_text(contents)
            else:
                dest.symlink_to(path, target_is_directory=path.is_dir())
        local = dict(env, AXIOM_PATH=str(modules))
        path = work / (label + '.ax')
        path.write_text(fixture)
        binary = work / label
        run_checked([str(compiler), '--diagnostic-format=ai', 'build', str(path), '--opt', str(level),
                     '--alloc-poison', '-o', str(binary)], env=local, timeout=240,
                    stdout=work / (label + '.build.stdout'), stderr=work / (label + '.build.stderr'))
        return binary, local

    def execute(binary, local, mode, fmt):
        result = subprocess.run([str(binary), mode, fmt], env=local, capture_output=True, timeout=30)
        stem = f'{binary.name}-{mode}-{fmt}'
        (work / (stem + '.stdout')).write_bytes(result.stdout)
        (work / (stem + '.stderr')).write_bytes(result.stderr)
        (work / (stem + '.exit')).write_text(str(result.returncode) + '\n')
        return result

    expected = {}
    for level in (0, 2):
        binary, local = build(f'positive-O{level}', source, level)
        for mode in ('leaf', 'memo'):
            for fmt, extension in FORMATS.items():
                result = execute(binary, local, mode, fmt)
                golden = fixtures / ('propagate-invariant.' + extension)
                if bless and level == 0 and mode == 'leaf':
                    validate(result, mode, fmt, result.stderr)
                    golden.write_bytes(result.stderr)
                expected[fmt] = golden.read_bytes()
                validate(result, mode, fmt, expected[fmt])
        report['configurations'].append({'optimization': level, 'poison': True,
                                         'modes': ['leaf', 'memo'], 'renderers': list(FORMATS), 'exit': 1})
        print(f'ok   internal propagation diagnostic: leaf/memo, three renderers, O{level} poison, exit 1', flush=True)

    guardless = replace_once(source, '(qmInvariant env st e))', 'e)', 'remove extractor guard')
    stateless = replace_once(source, '(set st.failed true)', '(set st.failed false)', 'remove failure state')
    begin = source.index('    (expEmit env\n', source.index('(pub fn (qmInvariant'))
    end = source.index('\n    e\n', begin)
    quiet = source[:begin] + source[end:]
    warning = replace_once(source, '(mkDiagNote SEV_ERROR "AX3146"',
                           '(mkDiagNote SEV_WARNING "AX3146"', 'demote internal failure')
    for name, contents, mode in [('remove-guard', guardless, 'memo'), ('remove-state', stateless, 'leaf'),
                                 ('remove-diagnostic', quiet, 'leaf'), ('demote-error', warning, 'leaf')]:
        binary, local = build(name, contents, 0)
        result = execute(binary, local, mode, 'ai')
        if result.returncode not in (0, 98):
            raise AssertionError((name, 'mutant must run normally, not crash', result.returncode, result.stderr))
        try:
            validate(result, mode, 'ai', expected['ai'])
        except AssertionError:
            pass
        else:
            raise AssertionError((name, 'control did not fail'))
        report['controls'].append({'name': name, 'mode': mode, 'exit': result.returncode,
                                   'stderr_sha256': hashlib.sha256(result.stderr).hexdigest()})
        print(f'ok   effective internal fault control: {name}, exit {result.returncode}', flush=True)

    # Coverage is earned by the successful executed proof above. A golden alone
    # never contributes a code to check-diagnostic-coverage.sh.
    observed = sorted(set(re.findall(rb'^E (AX\d{4}) ', (work / 'positive-O2-leaf-ai.stderr').read_bytes(), re.M)))
    if observed != [CODE.encode()]:
        raise AssertionError(('unexpected observed primary codes', observed))
    report['observed_primary_codes'] = [c.decode() for c in observed]
    (work / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report['observed_primary_codes']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axiom', required=True, type=Path)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--codes-out', type=Path)
    parser.add_argument('--bless', action='store_true')
    args = parser.parse_args()
    if args.codes_out and args.codes_out.exists():
        raise AssertionError('coverage output must be fresh; refusing stale proof')
    root = Path(__file__).resolve().parents[3]
    if args.work:
        args.work.mkdir(parents=True, exist_ok=False)
        codes = check(args.axiom.resolve(), root, args.work.resolve(), args.bless)
    else:
        with tempfile.TemporaryDirectory(prefix='axiom-propagate-invariant-') as directory:
            codes = check(args.axiom.resolve(), root, Path(directory), args.bless)
    if args.codes_out:
        args.codes_out.write_text('\n'.join(codes) + '\n')


if __name__ == '__main__':
    main()
