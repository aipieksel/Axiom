#!/usr/bin/env python3
"""Source-only controls for the restriction gate's line-preserving injector."""

from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / 'scripts/lib/restriction-tags.py'
sys.path.insert(0, str(HELPER.parent))
inject = runpy.run_path(str(HELPER))['tag_every_fn']
TAG = ';@axiom:restrict(no-foreign)'


class RestrictionTags(unittest.TestCase):
    def check(self, source, expected):
        actual = inject(source)
        self.assertEqual(actual, expected)
        self.assertEqual(actual.count('\n'), source.count('\n'))
        self.assertEqual(inject(actual), actual)
        # Every changed line was blank; syntax and existing tags stay at
        # their original line numbers with their original spelling.
        for before, after in zip(source.split('\n'), actual.split('\n')):
            if before != after:
                self.assertEqual((before, after), ('', TAG))

    def test_effect_before_restriction_and_multiple_tags(self):
        for annotations in (
                ';@axiom:effect(pure)\n;@axiom:restrict(no-io, strict)',
                ';@axiom:nolint(AX3037)\n;@axiom:effect(pure)\n'
                ';@axiom:restrict(no-io)\n;@axiom:no_refactor',
                ';@axiom:restrict(no-io)\n;@axiom:effect(pure)',
                '  ;@axiom:effect(pure)\n  ;@axiom:restrict(no-io)'):
            with self.subTest(annotations=annotations):
                source = f'\n(:: kept (-> Int Int))\n{annotations}\n(fn (kept n) n)\n'
                self.check(source, source)

    def test_other_annotations_do_not_suppress_injection(self):
        source = ('\n(pub :: tagged (-> Int Int))\n;@axiom:effect(pure)\n'
                  ';@axiom:nolint(AX3037)\n(pub fn (tagged n) n)\n')
        self.check(source, TAG + source)

    def test_distinct_declarations_keep_their_own_annotations(self):
        source = ('\n(:: first Int)\n;@axiom:effect(pure)\n(fn (first) 1)\n'
                  '\n(:: second Int)\n;@axiom:effect(pure)\n'
                  ';@axiom:restrict(no-io)\n(fn (second) 2)\n')
        self.check(source, TAG + source)

    def test_annotation_scan_stops_at_a_new_declaration(self):
        source = ('\n(:: first Int)\n;@axiom:effect(pure)\n'
                  '(:: second Int)\n;@axiom:restrict(no-io)\n(fn (second) 2)\n')
        self.check(source, TAG + source)

    def test_multiline_signature(self):
        source = ('\n(pub :: kept\n  (-> Int\n    Int))\n;@axiom:effect(pure)\n'
                  ';@axiom:restrict(no-io)\n(pub fn (kept n) n)\n')
        self.check(source, source)
        unrestricted = source.replace(';@axiom:restrict(no-io)\n', '')
        self.check(unrestricted, TAG + unrestricted)

    def test_unsigned_functions_and_tags_above_signatures(self):
        source = ('(fn (first) 0)\n\n(fn (unsigned) 1)\n'
                  '\n(pub fn (public) 2)\n'
                  '\n;@axiom:restrict(no-io)\n(:: kept Int)\n(fn (kept) 3)\n'
                  '\n;@axiom:restrict(no-io)\n(fn (bare) 4)\n')
        expected = source.replace('\n\n(fn (unsigned)', f'\n{TAG}\n(fn (unsigned)')
        expected = expected.replace('\n\n(pub fn (public)', f'\n{TAG}\n(pub fn (public)')
        self.check(source, expected)

    def test_nested_and_commented_declarations_are_unchanged(self):
        source = ('#|\n\n(:: comment Int)\n(fn (comment) 1)\n|#\n'
                  '(impl Example\n\n  (fn (member) 2))\n')
        self.check(source, source)

    def test_callback_fixture_keeps_existing_restrictions(self):
        source = (ROOT / 'tests/selfhost/1042-callback-alias-effects.ax').read_text()
        actual = inject(source)
        self.assertEqual(actual.count(';@axiom:restrict('), 9)
        for name in ('known', 'chosen', 'stored'):
            signature = next(i for i, line in enumerate(source.splitlines())
                             if line.startswith(f'(:: {name} '))
            self.assertEqual(actual.splitlines()[signature - 1], '')
        self.check(source, actual)
        # The old immediate-next-line rule would add a second restriction
        # before these three effect-first declarations. This is a refusing
        # control on the actual regression, independent of the new helper.
        lines = source.split('\n')
        duplicates = [i for i, line in enumerate(lines)
                      if line.startswith('(:: ') and i > 0 and lines[i - 1] == ''
                      and lines[i + 1] == ';@axiom:effect(pure)'
                      and lines[i + 2].startswith(';@axiom:restrict(')]
        self.assertEqual(len(duplicates), 3)

    def test_cli_uses_the_same_injector(self):
        source = '\n(:: kept Int)\n;@axiom:effect(pure)\n;@axiom:restrict(no-io)\n(fn (kept) 1)\n'
        with tempfile.TemporaryDirectory(prefix='axiom-restriction-tags-') as directory:
            original = Path(directory) / 'source.ax'
            tagged = Path(directory) / 'tagged.ax'
            original.write_text(source, encoding='utf-8')
            subprocess.run([sys.executable, str(HELPER), str(original), str(tagged)],
                           check=True)
            self.assertEqual(tagged.read_text(encoding='utf-8'), source)


if __name__ == '__main__':
    unittest.main()
