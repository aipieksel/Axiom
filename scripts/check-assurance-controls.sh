#!/usr/bin/env bash
# The assurance page's rule tables are checked claims.
#
# `docs/assurance.md` pairs each area a checker is responsible for with
# the codes that refuse it, a fixture it refuses and a program it
# accepts, in tables headed
#
#     | Area | Refused as | A refusal | An acceptance |
#
# Nothing else reads those tables. A row could name a fixture that was
# renamed, a code the fixture no longer draws, or an acceptance the
# compiler now refuses, with every other gate green. This gate reads
# every table with that header, whatever section it sits under, so a
# table added later under the same header is covered the day it is
# written. For every row:
#
#   - the refusal path is checked, and one of the row's codes must be
#     an `E ` or `W ` line;
#   - the acceptance path is checked, and no `E ` line may appear
#     (warnings are allowed, and the exit status must be 0);
#   - a path that does not exist, a row with no code or no path, and a
#     table with no rows are each a FAIL. Zero tables is a FAIL.
#
# Paths are repository-relative. A `tests/diagnostics/` fixture is
# copied flat beside `tests/diagnostics/mods/*.ax`, as
# `check-diagnostics.sh` does, because a helper module's name is its
# file stem; a compiler module is checked with `AXIOM_PATH` naming
# `self_host/`; everything else is checked as itself.
#
# Then the negative controls. A refusal fixture can draw its code and
# still not test the rule the row names, if the code reaches it another
# way. For three codes the gate removes the enforcement in a copy of
# `self_host/`, builds a compiler from the copy, and requires the
# refusal fixtures to be accepted by it: no line with the code, no
# error the compiler under test did not also report, and no crash.
#
#   AX3042  `emitUndeclaredEffect` answers without emitting. It is the
#           one site that emits the code.
#   AX3010  `emitAxtag` answers without emitting. The one site again.
#   AX3005  the two sites: `checkExhaustiveMatrix` accepts every
#           matrix, and the fall-through arm never fires.
#
# A control runs against every row whose `Refused as` names its code,
# and against one fixture of its own, so a control with no row today
# still runs on something the compiler under test refuses with that
# code. Each seam is an exact string that must occur exactly once
# before it is edited; zero or several matches is a FAIL, never a
# skip. A reformat of `self_host/typecheck.ax` (`axiom fmt`) that
# respells a seam fails here loudly, and both strings are then updated
# to the new spelling.
#
# Cost: one compiler build per control, cached by `gate_build_tree`
# on the sources it reads. Cold, the three builds take about half a
# minute each on a laptop. The row checks are a `check` per path.
#
# Usage:
#   scripts/check-assurance-controls.sh
#   AXIOM_AXC=path/to/compiler scripts/check-assurance-controls.sh
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/gate.sh"
gate_init
command -v python3 >/dev/null || { echo "FAIL: python3 is not on PATH"; exit 1; }
gate_build_axc axc

page="docs/assurance.md"
header='| Area | Refused as | A refusal | An acceptance |'

failed=0
checks=0
ok()  { echo "ok   $*"; checks=$((checks + 1)); }
bad() { echo "FAIL $*"; failed=$((failed + 1)); }

# --------------------------------------------------------------------
# The rows. One tab-separated record each:
#   table  row  area  codes  refusal-paths  acceptance-paths
# with codes and paths space-separated. A code is `AX` and four digits
# inside backticks in the second column. A path is a backticked token
# with a `/` in the third or fourth column. A row missing any of the
# three is printed with the field empty and refused below; nothing is
# dropped on the way.
# --------------------------------------------------------------------
echo "== the tables under '$header' in $page =="
python3 - "$repo_root/$page" "$header" "$work/tables.n" > "$work/rows.tsv" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read().split("\n")
header = re.sub(r"\s+", " ", sys.argv[2].strip())
tables = 0
i = 0
while i < len(text):
    if re.sub(r"\s+", " ", text[i].strip()) == header:
        tables += 1
        i += 1
        if i < len(text) and re.match(r"^\|[\s:-]+(\|[\s:-]+)*\|?\s*$", text[i]):
            i += 1
        row = 0
        while i < len(text) and text[i].lstrip().startswith("|"):
            cells = [c.strip() for c in text[i].strip().strip("|").split("|")]
            while len(cells) < 4:
                cells.append("")
            row += 1
            codes = re.findall(r"`(AX[0-9]{4})`", cells[1])
            def paths(cell):
                return [p for p in re.findall(r"`([^`]+)`", cell) if "/" in p]
            print("\t".join([str(tables), str(row), cells[0], " ".join(codes),
                             " ".join(paths(cells[2])), " ".join(paths(cells[3]))]))
            i += 1
        continue
    i += 1
open(sys.argv[3], "w").write("%d\n" % tables)
PY
rc=$?
n_tables="$(cat "$work/tables.n" 2>/dev/null || echo 0)"
n_rows="$(wc -l < "$work/rows.tsv" | tr -d ' ')"
if (( rc != 0 )); then
  bad "the table reader failed (exit $rc)"
elif (( n_tables == 0 )); then
  bad "$page has no table headed '$header'; the claims this gate checks are gone"
elif (( n_rows == 0 )); then
  bad "$page has $n_tables such table(s) and no rows under them"
else
  ok "$n_tables table(s), $n_rows rows"
  # Every table has at least one row: a header left over an emptied
  # table would otherwise count as covered by the other's rows.
  for t in $(seq 1 "$n_tables"); do
    if ! awk -F'\t' -v t="$t" '$1 == t { found = 1 } END { exit !found }' "$work/rows.tsv"; then
      bad "table $t has the header and no rows"
    fi
  done
fi

# --------------------------------------------------------------------
# Running `check` on a repository path.
#
# run_check <compiler> <path>: the compiler's whole output lands in
# `$work/last.out`, the status is returned. `codes_in <file>` lists the
# distinct codes on its `E ` and `W ` lines, `errors_in <file>` counts
# the `E ` lines.
# --------------------------------------------------------------------
mkdir -p "$work/diag"
cp "$repo_root"/tests/diagnostics/mods/*.ax "$work/diag/" 2>/dev/null || true

run_check() {
  local cc="$1" p="$2" rc=0 base
  case "$p" in
    tests/diagnostics/*)
      base="$(basename "$p")"; base="${base%.axbad}"; base="${base%.ax}.ax"
      cp "$repo_root/$p" "$work/diag/$base"
      ( cd "$work/diag" && AXIOM_PATH="$repo_root/self_host" \
          "$cc" --diagnostic-format=ai check "$base" ) > "$work/last.out" 2>&1 </dev/null || rc=$?
      ;;
    self_host/*)
      AXIOM_PATH="$repo_root/self_host" \
        "$cc" --diagnostic-format=ai check "$p" > "$work/last.out" 2>&1 </dev/null || rc=$?
      ;;
    *)
      "$cc" --diagnostic-format=ai check "$p" > "$work/last.out" 2>&1 </dev/null || rc=$?
      ;;
  esac
  return "$rc"
}
codes_in()  { grep -oE '^[EW] AX[0-9]{4}' "$1" | cut -d' ' -f2 | LC_ALL=C sort -u | tr '\n' ' ' | sed 's/ $//'; }
errors_in() { grep -c '^E ' "$1" | tr -d ' '; }
has_code()  { grep -qE "^[EW] $2 " "$1"; }   # <file> <code>

# --------------------------------------------------------------------
# 1. Every row's refusal is refused with one of its codes, and every
#    row's acceptance checks clean.
# --------------------------------------------------------------------
echo
echo "== each row: the refusal draws a named code, the acceptance draws no error =="
while IFS=$'\t' read -r t r area codes refusals acceptances; do
  label="table $t row $r ($area)"
  if [[ -z "$codes" ]]; then bad "$label names no \`AXnnnn\` code"; continue; fi
  if [[ -z "$refusals" ]]; then bad "$label names no refusal path"; continue; fi
  if [[ -z "$acceptances" ]]; then bad "$label names no acceptance path"; continue; fi
  for p in $refusals; do
    if [[ ! -f "$repo_root/$p" ]]; then bad "$label: refusal $p does not exist"; continue; fi
    run_check "$axc" "$p"; rc=$?
    hit=""
    for c in $codes; do has_code "$work/last.out" "$c" && { hit="$c"; break; }; done
    if [[ -n "$hit" ]]; then
      ok "$label: $p is refused as $hit"
    else
      bad "$label: $p draws none of [$codes]; it exits $rc with [$(codes_in "$work/last.out")]"
      sed 's/^/     /' "$work/last.out" | head -3
    fi
  done
  for p in $acceptances; do
    if [[ ! -f "$repo_root/$p" ]]; then bad "$label: acceptance $p does not exist"; continue; fi
    run_check "$axc" "$p"; rc=$?
    n_err="$(errors_in "$work/last.out")"
    if (( rc == 0 && n_err == 0 )); then
      ok "$label: $p checks clean"
    else
      bad "$label: $p is not accepted; it exits $rc with $n_err error line(s) [$(codes_in "$work/last.out")]"
      sed 's/^/     /' "$work/last.out" | head -3
    fi
  done
done < "$work/rows.tsv"

# --------------------------------------------------------------------
# 2. Negative controls. Each is a code, a fixture of the gate's own,
#    and one or two seams in `self_host/typecheck.ax`: the text as the
#    formatter lays it out, then the same text with the enforcement
#    gone. The fixtures under a control are its own and those of every
#    row that names the code.
# --------------------------------------------------------------------

# fixtures_for <code>: the gate's fixture, then every refusal path of
# a row that names the code, each once.
fixtures_for() {
  { echo "$2"
    awk -F'\t' -v c="$1" '{ n = split($4, cs, " "); for (k = 1; k <= n; k++) if (cs[k] == c) { print $5; break } }' "$work/rows.tsv" | tr ' ' '\n'
  } | awk 'NF && !seen[$0]++'
}

# ablate <tree> <file> <old> <new> [<old> <new> ...]: apply each seam
# to `<tree>/self_host/<file>` after counting it. Prints the counts;
# returns 1 when any seam does not occur exactly once, with the file
# untouched.
ablate() {
  local tree="$1" file="$2"; shift 2
  python3 - "$tree/self_host/$file" "$@" <<'PY'
import sys
p = sys.argv[1]
pairs = sys.argv[2:]
s = open(p, encoding="utf-8").read()
bad = 0
for k in range(0, len(pairs), 2):
    n = s.count(pairs[k])
    print("     seam %d: %d match(es) for %r" % (k // 2 + 1, n, pairs[k][:56]))
    if n != 1:
        bad += 1
if bad:
    sys.exit(1)
for k in range(0, len(pairs), 2):
    s = s.replace(pairs[k], pairs[k + 1])
open(p, "w", encoding="utf-8").write(s)
PY
}

# control <code> <what the mutant does> <own fixture> <seam pairs...>
control() {
  local code="$1" what="$2" own="$3"; shift 3
  local tree="$work/abl-$code" mutant="$work/axc-$code" p rc real_codes mut_codes n_err c new=""
  echo
  echo "== negative control: $code, $what =="
  mkdir -p "$tree"
  cp -R "$repo_root/self_host" "$tree/"
  if ! ablate "$tree" typecheck.ax "$@"; then
    bad "$code: a seam does not occur exactly once in self_host/typecheck.ax, so there is nothing to ablate"
    return
  fi
  ok "$code: every seam occurs once, and the copy is ablated"
  if ! gate_build_tree "$axiom" "$tree" "$repo_root/stdlib" "$mutant" > "$work/abl-$code.build.log" 2>&1; then
    bad "$code: the ablated compiler would not build"
    sed 's/^/     /' "$work/abl-$code.build.log" | head -10
    return
  fi
  grep -E '^== ' "$work/abl-$code.build.log" | sed 's/^/     /'
  for p in $(fixtures_for "$code" "$own"); do
    if [[ ! -f "$repo_root/$p" ]]; then bad "$code: $p does not exist"; continue; fi
    # The compiler under test must refuse it with the code, or the
    # control's own fixture has gone stale.
    run_check "$axc" "$p"; rc=$?
    if ! has_code "$work/last.out" "$code"; then
      bad "$code: the compiler under test does not refuse $p as $code [$(codes_in "$work/last.out")], so this control has nothing to show"
      continue
    fi
    real_codes=" $(grep -oE '^E AX[0-9]{4}' "$work/last.out" | cut -d' ' -f2 | LC_ALL=C sort -u | tr '\n' ' ')"
    run_check "$mutant" "$p"; rc=$?
    n_err="$(errors_in "$work/last.out")"
    mut_codes="$(codes_in "$work/last.out")"
    new=""
    for c in $(grep -oE '^E AX[0-9]{4}' "$work/last.out" | cut -d' ' -f2 | LC_ALL=C sort -u); do
      [[ "$real_codes" == *" $c "* ]] || new="$new $c"
    done
    if has_code "$work/last.out" "$code"; then
      bad "$code: the ablated compiler still refuses $p as $code, so the fixture does not test this enforcement"
      grep -E "^[EW] $code " "$work/last.out" | sed 's/^/     /' | head -3
    elif (( rc > 1 )); then
      bad "$code: the ablated compiler exits $rc on $p; a crash is not an acceptance"
      sed 's/^/     /' "$work/last.out" | head -3
    elif [[ -n "$new" ]]; then
      bad "$code: the ablated compiler reports$new on $p, which the compiler under test does not; the seam is not the rule this control names"
    elif (( n_err == 0 )); then
      ok "$code ablated: $p is accepted (0 errors; the compiler under test refuses it)"
    else
      ok "$code ablated: $p no longer draws $code; its $n_err remaining error line(s) [$mut_codes] are the compiler under test's own"
    fi
  done
}

# AX3042, a required effect undeclared. `emitUndeclaredEffect` is the
# one emission site; its guard `(== (nodeSpan d) 0)` answers 0 for a
# spanless node, and the mutant answers 0 for every node.
control AX3042 "an undeclared effect is never reported" \
  tests/diagnostics/363-effect-fix.ax \
  '(pub fn (emitUndeclaredEffect tc d name)
  (if (== (nodeSpan d) 0)' \
  '(pub fn (emitUndeclaredEffect tc d name)
  (if (== (nodeSpan d) (nodeSpan d))'

# AX3010, a claim the body contradicts. `emitAxtag` is the one
# emission site, with the same guard.
control AX3010 "a contradicted claim is never reported" \
  tests/diagnostics/340-axtag-pure-io.ax \
  '(pub fn (emitAxtag tc d msg)
  (if (== (nodeSpan d) 0)' \
  '(pub fn (emitAxtag tc d msg)
  (if (== (nodeSpan d) (nodeSpan d))'

# AX3005, a match that misses a case. Two sites: `matExhaustive`
# answers 0 or 1 and `checkExhaustiveMatrix` reports on 0, so `>= 0`
# accepts every matrix; the fall-through arm (no constructor arm, no
# catch-all) is keyed on `ctorChecked`, and `(!= x x)` never holds.
control AX3005 "every match is exhaustive" \
  tests/diagnostics/1107-match-literal-column.ax \
  '(if (== (matExhaustive tc matrix types missOut) 1)' \
  '(if (>= (matExhaustive tc matrix types missOut) 0)' \
  '(if (&& (== ctorChecked 0)' \
  '(if (&& (!= ctorChecked ctorChecked)'

echo
if (( failed > 0 )); then
  echo "check-assurance-controls: $failed of $((checks + failed)) checks failed"
  exit 1
fi
echo "check-assurance-controls: $checks checks - every row's refusal draws its code, every"
echo "                          acceptance checks clean, and three ablations accept"
echo "                          the fixtures they should"
