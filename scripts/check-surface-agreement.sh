#!/usr/bin/env bash
# The compiler's four doors agree about every diagnostics fixture.
#
# `check` on the command line, the language server's published
# diagnostics, the REPL's reported codes and `build` each reach the
# checker by a path of their own and render its answer their own way,
# so each can lose, add or misplace a diagnostic without the others
# noticing. The other gates pin each door to a golden of its own; none
# asks whether two doors give one answer. This one does
# (`tests/tools/surface-agreement.py`):
#
#   - CLI and LSP, every fixture: the set of (severity, code, line,
#     column) `check` prints equals the set the server publishes for the
#     same document, with the column in the server's UTF-16 code units
#     computed from the fixture's own bytes. The server's lints
#     (`nolint.ax`) are its own layer and are set aside.
#   - CLI and build, every tenth refused fixture: `build` exits 1 with
#     the same codes and writes no executable.
#   - CLI and REPL, every refused fixture small enough to be a session
#     (at most two declarations): typed in form by form, the REPL
#     reports each code `check` printed. A signature with no definition
#     yet is the one thing the REPL defers.
#
# Usage:
#   scripts/check-surface-agreement.sh
#   AXIOM_AXC=path/to/compiler scripts/check-surface-agreement.sh
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/gate.sh"
gate_init
command -v python3 >/dev/null || { echo "FAIL: python3 is not on PATH"; exit 1; }
gate_build_axc axc

echo "== the CLI, the language server, the REPL and build agree =="
if python3 "$repo_root/tests/tools/surface-agreement.py" "$axc" "$repo_root/tests/diagnostics"; then
  exit 0
fi
echo "check-surface-agreement: the doors disagree"
exit 1
