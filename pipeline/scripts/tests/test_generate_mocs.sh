#!/usr/bin/env bash
# Smoke test for scripts/generate-mocs.py (generates Map-of-Content pages under
# wiki/_index/ from page tags, idempotently). Uses the real taxonomy + one valid
# member page (page_id + h1 + Domain tag + one Form tag). Run from the tooling root.

set -euo pipefail
PIPELINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GM="$PIPELINE_ROOT/scripts/generate-mocs.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
C="$TMP/content"; mkdir -p "$C/wiki/entities" "$C/wiki/_index"
cat > "$C/wiki/_taxonomy.md" <<'MD'
# Taxonomy

## Domain
- `biology/cell`

## Form
- `concept`

## Reserved
- `taxonomy-gap`
MD
rc=0
echo "test_generate_mocs:"
ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }

cat > "$C/wiki/entities/p.md" <<MD
---
type: Entity
page_id: $(printf 'P%.0s' {1..26})
tags: [biology/cell, concept]
aliases: [Example]
---
# Example
body
MD

# ── dry-run: reports the MOC it would create, writes nothing ──
out="$(VAULT_CONTENT_DIR="$C" "$GM" --dry-run 2>&1)" || { bad "dry-run exited non-zero"; echo "    $out"; }
echo "$out" | grep -qiE '_index/.*\.md' && ok "dry-run reports a MOC for the tagged page" || { bad "dry-run reported no MOC"; echo "    $out"; }
[[ -z "$(ls -A "$C/wiki/_index" 2>/dev/null)" ]] && ok "dry-run wrote nothing" || bad "dry-run wrote files"

# ── real run: writes a MOC under _index/ that lists the member ──
VAULT_CONTENT_DIR="$C" "$GM" >/dev/null 2>&1 || bad "generate-mocs exited non-zero"
moc="$(grep -rl "Example" "$C/wiki/_index/" 2>/dev/null | head -1)"
[[ -n "$moc" ]] && ok "MOC written under _index/ listing the member ($(basename "$moc"))" || bad "no MOC lists the member"

# ── idempotent re-run: no change on a second pass ──
before="$(cat "$C/wiki/_index/"*.md 2>/dev/null | shasum)"
VAULT_CONTENT_DIR="$C" "$GM" >/dev/null 2>&1
after="$(cat "$C/wiki/_index/"*.md 2>/dev/null | shasum)"
[[ "$before" == "$after" ]] && ok "re-run idempotent (MOC bytes unchanged)" || bad "re-run changed the MOC"

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
