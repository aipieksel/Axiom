# Structural checks for `restore <writer> <prior-seed>` lineage events.
# A restore copies a previously emitted bootstrap tree; it does not
# claim that the writer's source emitted that tree. The caller must
# separately certify the canonical prior row by replay or checkpoint.

# seed_restore_check <repo> <writer> <prior-seed> <canonical-full-hash-file>
#
# Return 0 only for a chronological, byte-identical restoration of the
# entire bootstrap Git tree. Results are exposed to the caller without
# printing: seed_restore_reason, seed_restore_writer, seed_restore_prior
# and seed_restore_tree. The canonical file contains one resolved seed
# row commit per line, excluding orphan and restore events.
seed_restore_check() {
  local repo="$1" writer="$2" prior="$3" canonical="$4"
  local writer_tree prior_tree kind history
  seed_restore_reason=""
  seed_restore_writer=""
  seed_restore_prior=""
  seed_restore_tree=""

  if [[ ! "$writer" =~ ^[0-9a-fA-F]{4,40}$ || ! "$prior" =~ ^[0-9a-fA-F]{4,40}$ ]]; then
    seed_restore_reason="restore endpoints must be hexadecimal commit IDs"
    return 1
  fi
  seed_restore_writer="$(git -C "$repo" rev-parse --verify --quiet "$writer^{commit}" 2>/dev/null)" || {
    seed_restore_reason="restore writer $writer does not resolve to a commit"
    return 1
  }
  seed_restore_prior="$(git -C "$repo" rev-parse --verify --quiet "$prior^{commit}" 2>/dev/null)" || {
    seed_restore_reason="restore prior $prior does not resolve to a commit"
    return 1
  }
  if [[ "$seed_restore_writer" == "$seed_restore_prior" ]]; then
    seed_restore_reason="restore writer and prior seed are the same commit"
    return 1
  fi
  if [[ ! -f "$canonical" || ! -r "$canonical" ]] || ! grep -xF "$seed_restore_prior" "$canonical" >/dev/null 2>&1; then
    seed_restore_reason="restore prior $prior is not a canonical seed row"
    return 1
  fi
  if ! git -C "$repo" merge-base --is-ancestor "$seed_restore_prior" "$seed_restore_writer" 2>/dev/null; then
    seed_restore_reason="restore prior $prior is not earlier in the writer's ancestry"
    return 1
  fi
  # Use the same path-limited history as the lineage gate. Capture its
  # status before matching so a failed Git read cannot pass on a prefix.
  history="$(git -C "$repo" log --format=%H -- 'bootstrap/*.ll' 2>/dev/null)" || {
    seed_restore_reason="cannot read the seed history for restore writer $writer"
    return 1
  }
  if ! grep -xF "$seed_restore_writer" <<< "$history" >/dev/null; then
    seed_restore_reason="restore writer $writer did not write a seed in this history"
    return 1
  fi
  writer_tree="$(git -C "$repo" rev-parse --verify --quiet "$seed_restore_writer:bootstrap" 2>/dev/null)" || {
    seed_restore_reason="restore writer $writer has no bootstrap tree"
    return 1
  }
  prior_tree="$(git -C "$repo" rev-parse --verify --quiet "$seed_restore_prior:bootstrap" 2>/dev/null)" || {
    seed_restore_reason="restore prior $prior has no bootstrap tree"
    return 1
  }
  for kind in "$writer_tree" "$prior_tree"; do
    if [[ "$(git -C "$repo" cat-file -t "$kind" 2>/dev/null)" != tree ]]; then
      seed_restore_reason="restore endpoints must contain bootstrap directories"
      return 1
    fi
  done
  if [[ "$writer_tree" != "$prior_tree" ]]; then
    seed_restore_reason="restore writer $writer does not copy the entire bootstrap tree of $prior"
    return 1
  fi
  seed_restore_tree="$writer_tree"
  seed_restore_reason="entire bootstrap tree $writer_tree matches canonical seed $seed_restore_prior"
  return 0
}
