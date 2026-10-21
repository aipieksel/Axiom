#!/usr/bin/env python3
"""Add satisfied restriction tags without moving source line positions."""

from bisect import bisect_right
from pathlib import Path
import re
import sys

from fuzz import TOKEN, scan, skip_block


SIGNATURE = re.compile(r'^\((?:pub )?:: ').match
FUNCTION = re.compile(r'^\((?:pub )?fn ').match
RESTRICTION = re.compile(r'^[ \t]*;@axiom:restrict\(').match
TAG = ';@axiom:restrict(no-foreign)'


def declaration_name(source, declaration, kind):
    children = declaration.children
    if children and source[children[0].start:children[0].end] == 'pub':
        children = children[1:]
    if len(children) < 2 or source[children[0].start:children[0].end] != kind:
        return None
    name = children[1]
    if kind == 'fn':
        if name.kind != 'form' or not name.children:
            return None
        name = name.children[0]
    return source[name.start:name.end]


def has_restriction(trivia):
    """Read actual line comments, ignoring tags quoted in block comments."""
    position = 0
    while position < len(trivia):
        if trivia.startswith('#|', position):
            position = skip_block(trivia, position + 2)
            continue
        token = TOKEN.match(trivia, position)
        if token is None:
            position += 1
            continue
        if token.lastgroup == 'lc' and RESTRICTION(token.group()):
            return True
        position = token.end()
    return False


def tag_every_fn(source):
    lines = source.split('\n')
    out = list(lines)
    starts = [0]
    starts.extend(i + 1 for i, char in enumerate(source) if char == '\n')
    declarations, _ = scan(source)
    restricted = set()
    for index, declaration in enumerate(declarations):
        previous_end = declarations[index - 1].end if index else 0
        already_restricted = has_restriction(source[previous_end:declaration.start])
        target = index
        signature_name = declaration_name(source, declaration, '::')
        if signature_name is not None and index + 1 < len(declarations):
            following = declarations[index + 1]
            if signature_name == declaration_name(source, following, 'fn'):
                # A matching signature/function pair is one target, even
                # when a blank gives the injector two possible locations.
                target = index + 1
                already_restricted |= has_restriction(
                    source[declaration.end:following.start])
        if already_restricted:
            restricted.add(target)
        line = bisect_right(starts, declaration.start) - 1
        if (declaration.start != starts[line] or line == 0
                or lines[line - 1] != '' or target in restricted):
            continue
        if not (SIGNATURE(lines[line]) or FUNCTION(lines[line])):
            continue
        out[line - 1] = TAG
        restricted.add(target)
    return '\n'.join(out)


if __name__ == '__main__':
    source, destination = map(Path, sys.argv[1:])
    destination.write_text(tag_every_fn(source.read_text(encoding='utf-8')),
                           encoding='utf-8')
