#!/usr/bin/env bash
# Smoke test for scripts/add-page-id.py (backfills a rename-safe page_id ULID into
# frontmatter). Also guards the audit consolidation: new_ulid now comes from _util.
# Asserts: a missing page_id is added as a 26-char ULID, re-runs are idempotent, and
# --check exits non-zero iff a page lacks page_id. Run from the tooling root.

set -euo pipefail
VAULT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
API="$VAULT_ROOT/scripts/add-page-id.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
C="$TMP/content"; mkdir -p "$C/wiki/entities"
rc=0
echo "test_add_page_id:"
ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }

P="$C/wiki/entities/p.md"
cat > "$P" <<'MD'
---
type: Entity
aliases: [Example]
---
# Example
body
MD

# ── 1. backfill a missing page_id ──
VAULT_CONTENT_DIR="$C" "$API" "$P" >/dev/null 2>&1 || { bad "add-page-id exited non-zero"; }
pid="$(grep -oE '^page_id: [0-9A-Z]{26}$' "$P" | awk '{print $2}')"
[[ -n "$pid" ]] && ok "page_id added as a 26-char ULID ($pid)" || bad "no valid page_id added"

# ── 2. idempotent re-run (same id, exactly one page_id line) ──
VAULT_CONTENT_DIR="$C" "$API" "$P" >/dev/null 2>&1
pid2="$(grep -oE '^page_id: [0-9A-Z]{26}$' "$P" | awk '{print $2}')"
[[ "$(grep -c '^page_id:' "$P")" == "1" && "$pid2" == "$pid" ]] && ok "re-run is idempotent (id unchanged, no dup)" || bad "re-run not idempotent"

# ── 3. --check: a page WITHOUT page_id → exit 1; a page WITH → exit 0 ──
Q="$C/wiki/entities/q.md"; printf -- '---\ntype: Entity\n---\n# Q\n' > "$Q"
VAULT_CONTENT_DIR="$C" "$API" --check "$Q" >/dev/null 2>&1 && bad "--check should fail on a page missing page_id" || ok "--check fails on missing page_id"
VAULT_CONTENT_DIR="$C" "$API" --check "$P" >/dev/null 2>&1 && ok "--check passes on a page with page_id" || bad "--check should pass when page_id present"

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
