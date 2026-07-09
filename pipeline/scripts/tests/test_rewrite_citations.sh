#!/usr/bin/env bash
# Smoke test for scripts/rewrite-citations.py (repoints every [src:OLD#anchor] +
# `sources:` entry to a new source_id, preserving the anchor — used by supersede flows).
# Also guards the audit consolidation: die now comes from _util. Run from the tooling root.

set -euo pipefail
VAULT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RW="$VAULT_ROOT/scripts/rewrite-citations.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
C="$TMP/content"; mkdir -p "$C/wiki/entities"
rc=0
OLD="$(printf 'A%.0s' {1..26})"; NEW="$(printf 'B%.0s' {1..26})"
echo "test_rewrite_citations:"
ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }

P="$C/wiki/entities/p.md"
cat > "$P" <<MD
---
type: Entity
sources: [${OLD}]
---
# P
<!-- llm-zone -->
A claim citing [src:${OLD}#card-1] and again [src:${OLD}].
<!-- /llm-zone -->
MD
UNREL="$C/wiki/entities/u.md"; printf -- '---\ntype: Entity\n---\n# U\nno citations here.\n' > "$UNREL"

# ── run the rewrite OLD → NEW ──
out="$(cd "$C" && VAULT_CONTENT_DIR="$C" "$RW" "$OLD" "$NEW" 2>&1)" || { bad "rewrite exited non-zero: $out"; }
echo "$out" | grep -q 'rewrote 1 page' && ok "reported rewriting exactly the 1 affected page" || { bad "wrong page count"; echo "    $out"; }
grep -qF "[src:${NEW}#card-1]" "$P" && ok "anchor preserved on migration ([src:NEW#card-1])" || bad "anchor not preserved"
grep -qF "[src:${NEW}]" "$P" && ok "bare citation migrated" || bad "bare citation not migrated"
grep -qF "sources: [${NEW}]" "$P" && ok "frontmatter sources: migrated" || bad "frontmatter sources not migrated"
! grep -qF "$OLD" "$P" && ok "no stale OLD id remains" || bad "stale OLD id remains"
# unrelated page untouched (no spurious rewrite)
[[ "$(cat "$UNREL")" == "$(printf -- '---\ntype: Entity\n---\n# U\nno citations here.\n')" ]] && ok "unrelated page untouched" || bad "unrelated page modified"

# ── guards: bad ULID + identical ids die loud ──
( cd "$C" && VAULT_CONTENT_DIR="$C" "$RW" "not-a-ulid" "$NEW" ) >/dev/null 2>&1 && bad "non-ULID arg should die" || ok "non-ULID arg → dies"
( cd "$C" && VAULT_CONTENT_DIR="$C" "$RW" "$NEW" "$NEW" ) >/dev/null 2>&1 && bad "identical ids should die" || ok "identical old==new → dies"

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
