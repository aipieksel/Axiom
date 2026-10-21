#!/usr/bin/env python3
"""The importable standard-library modules covered by the public reference."""

from pathlib import PurePosixPath
import sys


def public_reference_module(path):
    parts = PurePosixPath(path).parts
    if (not parts or parts[0] != 'stdlib' or '..' in parts
            or not str(path).endswith('.ax')):
        return False
    # Internal is a library-only boundary (AX5002). The other platform
    # implementations repeat the public names of the documented one.
    if len(parts) > 2 and parts[1] == 'Internal':
        return False
    if len(parts) == 3 and parts[1] == 'Sys':
        name = parts[2]
        if (name.startswith('Platform.linux-') or name in {
                'Platform.freebsd.ax', 'Platform.windows.ax',
                'Platform.baremetal-aarch64.ax'}):
            return False
    return True


if __name__ == '__main__':
    for line in sys.stdin:
        path = line.rstrip('\n')
        if public_reference_module(path):
            print(path)
