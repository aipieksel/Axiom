#!/usr/bin/env python3
"""Check the full missing-doc report and its public-reference subset."""

import argparse
from collections import Counter
from pathlib import Path
import re
import runpy
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
public_reference_module = runpy.run_path(
    str(ROOT / 'scripts/lib/stdlib-reference.py'))['public_reference_module']
REPORT = re.compile(r'^(.+):([1-9][0-9]*): ([^\s]+)$')
DECLARATION = re.compile(
    r'^\((?:pub\s+(?:(?:::|macro|data|struct|type|effect)\s+([^\s()]+)'
    r'|fn\s+\(([^\s()]+))|effect\s+([^\s()]+))')


def verify(repo, report, status, reference):
    repo = Path(repo).resolve()
    library = (repo / 'stdlib').resolve()
    lines = report.splitlines()
    errors = []
    public = []
    reported = set()
    expected_status = 1 if lines else 0
    if status != expected_status:
        errors.append(f'global --missing report lists {len(lines)} declarations '
                      f'but exits {status}, expected {expected_status}')
    for line in lines:
        match = REPORT.fullmatch(line)
        if not match:
            errors.append(f'malformed --missing line: {line!r}')
            continue
        spelling, number, name = match.groups()
        path = Path(spelling)
        if not path.is_absolute():
            path = repo / path
        path = path.resolve()
        try:
            path.relative_to(library)
            relative = path.relative_to(repo).as_posix()
        except ValueError:
            errors.append(f'{spelling}: report path is outside this standard library')
            continue
        try:
            source = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError) as error:
            errors.append(f'{spelling}: cannot read reported declaration: {error}')
            continue
        index = int(number) - 1
        if index >= len(source):
            errors.append(f'{spelling}:{number}: declaration line is outside the source')
            continue
        declaration = DECLARATION.match(source[index])
        actual_name = next((n for n in declaration.groups() if n), None) if declaration else None
        if actual_name != name:
            errors.append(f'{spelling}:{number}: {name!r} is not the public declaration '
                          f'at that line: {source[index][:70]!r}')
            continue
        identity = (path, index, name)
        if identity in reported:
            errors.append(f'{spelling}:{number}: duplicate missing declaration {name!r}')
            continue
        reported.add(identity)
        if public_reference_module(relative):
            public.append(name)
    blank = re.findall(r'^\| `([^`]*)`.*\| *\|$', reference, re.M)
    if Counter(public) != Counter(blank):
        absent = list((Counter(blank) - Counter(public)).elements())
        extra = list((Counter(public) - Counter(blank)).elements())
        errors.append(f'public --missing names differ from reference blanks: '
                      f'missing={sorted(absent)!r}, extra={sorted(extra)!r}')
    return errors, len(lines), len(public)


class MissingReference(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='axiom-doc-missing-')
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)

    def module(self, path, source):
        result = self.repo / path
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_text(source, encoding='utf-8')
        return result

    def check(self, report, status, reference='', expected=True):
        errors, _, _ = verify(self.repo, report, status, reference)
        self.assertEqual(not errors, expected, errors)

    def test_public_reference_scope(self):
        included = ['stdlib/Pre.ax', 'stdlib/Task/Worker.ax',
                    'stdlib/Sys/Platform.darwin.ax', 'stdlib/Internalised.ax',
                    'stdlib/Extra/Platform.windows.ax',
                    'stdlib/Sys/Platform.new-target.ax']
        excluded = ['stdlib/Internal/WorkerProcess.ax',
                    'stdlib/Sys/Platform.windows.ax', 'stdlib/Sys/Platform.freebsd.ax',
                    'stdlib/Sys/Platform.linux-aarch64.ax',
                    'stdlib/Sys/Platform.linux-x86_64.ax',
                    'stdlib/Sys/Platform.baremetal-aarch64.ax',
                    'app/Example.ax', 'stdlib/../External.ax']
        for path in included:
            with self.subTest(path=path):
                self.assertTrue(public_reference_module(path))
        for path in excluded:
            with self.subTest(path=path):
                self.assertFalse(public_reference_module(path))

    def test_internal_and_other_platforms_keep_global_failure_status(self):
        internal = self.module('stdlib/Internal/Private.ax', '(pub :: private Int)\n')
        windows = self.module('stdlib/Sys/Platform.windows.ax', '(pub :: system Int)\n')
        report = f'{internal}:1: private\n{windows}:1: system\n'
        self.check(report, 1)
        self.check(report, 0, expected=False)
        self.check(report, 2, expected=False)
        self.check(report * 2, 1, expected=False)
        # Filtering the report before checking the exit status would make
        # this empty public subset incorrectly demand exit 0.
        self.assertEqual(verify(self.repo, report, 1, '')[1:], (2, 0))

    def test_public_names_and_duplicates_match_the_reference(self):
        path = self.module('stdlib/Public.ax', '(pub :: visible Int)\n')
        report = f'{path}:1: visible\n'
        reference = '| `visible` | `Int` | |\n'
        self.check(report, 1, reference)
        self.check(report, 1, '', expected=False)
        self.check('', 0, reference, expected=False)
        self.check(report * 2, 1, reference, expected=False)

    def test_empty_global_report_requires_success(self):
        self.check('', 0)
        self.check('', 1, expected=False)

    def test_all_global_entries_must_name_actual_public_declarations(self):
        path = self.module('stdlib/Internal/Private.ax',
                           '(pub :: public Int)\n(:: private Int)\n')
        for suffix in ['0: public', '3: public', '2: private', '1: impostor']:
            with self.subTest(suffix=suffix):
                self.check(f'{path}:{suffix}\n', 1, expected=False)
        self.check(f'{path}:1: public\n', 1)
        self.check('not a report line\n', 1, expected=False)
        self.check('\n', 1, expected=False)
        self.check(f'{self.repo}/missing.ax:1: public\n', 1, expected=False)

    def test_public_declaration_shapes(self):
        shapes = ['(pub :: item Int)', '(pub fn (item value) value)',
                  '(pub struct item sealed)', '(pub data item (Member))',
                  '(pub type item Int)', '(pub macro item ((item x) x))',
                  '(effect item (operation (-> Int Int)))']
        for shape in shapes:
            with self.subTest(shape=shape):
                path = self.module('stdlib/Internal/Shapes.ax', shape + '\n')
                self.check(f'{path}:1: item\n', 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repo', type=Path)
    parser.add_argument('report', type=Path)
    parser.add_argument('status', type=int)
    parser.add_argument('reference', type=Path)
    args = parser.parse_args()
    errors, total, public = verify(args.repo, args.report.read_text(encoding='utf-8'),
                                   args.status, args.reference.read_text(encoding='utf-8'))
    for error in errors:
        print('FAIL doc: ' + error)
    if errors:
        return 1
    print(f'ok   --missing lists {total} global declarations with the right exit status; '
          f'{public} match the public reference; every declaration location is verified')
    return 0


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        unittest.main(argv=[sys.argv[0]])
    else:
        sys.exit(main())
