#!/usr/bin/env python3
"""Add satisfied restriction tags without moving source line positions."""

from bisect import bisect_right
from pathlib import Path
import re
import sys

from fuzz import scan


SIGNATURE = re.compile(r'^\((?:pub )?:: ').match
FUNCTION = re.compile(r'^\((?:pub )?fn ').match
ANNOTATION = re.compile(r'^[ \t]*;@axiom:').match
RESTRICTION = re.compile(r'^[ \t]*;@axiom:restrict\(').match
TAG = ';@axiom:restrict(no-foreign)'


def tag_every_fn(source):
    lines = source.split('\n')
    out = list(lines)
    starts = [0]
    starts.extend(i + 1 for i, char in enumerate(source) if char == '\n')
    declarations, _ = scan(source)
    for declaration in declarations:
        line = bisect_right(starts, declaration.start) - 1
        if (declaration.start != starts[line] or line == 0
                or lines[line - 1] != ''):
            continue
        if SIGNATURE(lines[line]):
            # Start after the whole signature, including a multiline type.
            # Only its contiguous annotation block belongs to this test:
            # a later declaration's restriction must not suppress this tag.
            next_line = bisect_right(starts, declaration.end)  # zero-based
            already_restricted = False
            while next_line < len(lines) and ANNOTATION(lines[next_line]):
                already_restricted |= bool(RESTRICTION(lines[next_line]))
                next_line += 1
            if already_restricted:
                continue
        elif not FUNCTION(lines[line]):
            continue
        out[line - 1] = TAG
    return '\n'.join(out)


if __name__ == '__main__':
    source, destination = map(Path, sys.argv[1:])
    destination.write_text(tag_every_fn(source.read_text(encoding='utf-8')),
                           encoding='utf-8')
