#!/usr/bin/env bash
# Shared by the seed replay gates, never by the bootstrap itself:
# it reads a commit's `scripts/reseed.sh` through git, and the
# offline bootstrap path (scripts/check-offline-bootstrap.sh) runs
# no git.

# seed_emit_argv <repo> <commit> <target>
#
# Sets `seed_argv` to the arguments that emit `in.ax` for <target> the
# way <commit>'s own `scripts/reseed.sh` emitted its seed. A replay
# compares bytes with that seed, so it has to ask the same question.
#
# Two spellings exist. A commit whose reseed says `emit-llvm in.ax`
# emitted with `emit-llvm in.ax --target T`. Every earlier commit used
# the positional form, `in.ax T`, which its compiler still accepts. The
# two are different emissions on a compiler that writes a line table:
# the positional form leaves the table empty, which is why the seeds it
# wrote read `@__axiom_linetab_n = internal constant i64 0`. A commit
# with no `scripts/reseed.sh` predates the script and used the
# positional form, the only one its compiler had.
#
# Consumers: `scripts/check-seed-lineage.sh` (each row, orphan and walk
# seed against its own commit) and `scripts/check-seed-provenance.sh`
# (the committed seed against the commit that wrote it).
seed_emit_argv() {
  local repo="$1" commit="$2" target="$3"
  if git -C "$repo" show "${commit}:scripts/reseed.sh" 2>/dev/null | grep -q 'emit-llvm in\.ax'; then
    seed_argv=(emit-llvm in.ax --target "$target")
  else
    seed_argv=(in.ax "$target")
  fi
}

# seed_probe_path <private copy of Path.ax>
#
# The provenance ablation must work with the source of the seed's own
# commit. Older sources compare byte zero directly; current sources
# share a separator predicate. Change one ASCII byte (47 -> 46), once,
# so '/' becomes '.' without depending on which source layout is read.
seed_probe_path() {
  local file="$1" needle replacement tmp="$1.probe.$$"
  if grep -Fq '(== (strByte p 0) 47)' "$file"; then
    needle='(== (strByte p 0) 47)'
    replacement='(== (strByte p 0) 46)'
  elif grep -Fq '(== b 47)' "$file"; then
    needle='(== b 47)'
    replacement='(== b 46)'
  else
    echo "the provenance probe has no recognised separator predicate in $file" >&2
    return 1
  fi
  if ! awk -v needle="$needle" -v replacement="$replacement" '
      !changed && (at=index($0, needle)) {
        $0=substr($0, 1, at-1) replacement substr($0, at+length(needle))
        changed=1
      }
      { print }
      END { if (!changed) exit 1 }
    ' "$file" > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  mv "$tmp" "$file"
}
