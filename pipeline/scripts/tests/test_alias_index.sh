#!/usr/bin/env bash
# Smoke test for scripts/alias-index.py (builds wiki/.alias-index.json: normalized
# alias → page multimap, used by intelligence-based candidate retrieval + lint).

set -euo pipefail
PIPELINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AI="$PIPELINE_ROOT/scripts/alias-index.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
C="$TMP/content"; mkdir -p "$C/wiki/entities"
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
echo "test_alias_index:"
ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }

cat > "$C/wiki/entities/mitochondria.md" <<MD
---
type: Entity
page_id: $(printf 'M%.0s' {1..26})
aliases: [Powerhouse of the cell]
tags: [biology/cell, concept]
---
# Mitochondria
body
MD

# ── build the index ──
VAULT_CONTENT_DIR="$C" "$AI" build >/dev/null 2>&1 || bad "alias-index build exited non-zero"
IDX="$C/wiki/.alias-index.json"
[[ -f "$IDX" ]] && ok ".alias-index.json written" || { bad "no index written"; exit "$rc"; }
python3 -c "import json,sys; d=json.load(open('$IDX')); sys.exit(0 if isinstance(d,dict) and d else 1)" \
  && ok "index is a non-empty JSON object" || bad "index not a valid non-empty object"
# the page's own filename stem is an implicit alias → must be present (normalized)
grep -qi 'mitochondria' "$IDX" && ok "page alias indexed (mitochondria)" || bad "expected alias not indexed"
grep -qi 'powerhouse' "$IDX" && ok "declared alias indexed (powerhouse of the cell)" || bad "declared alias missing"

# ── lookup resolves the alias back to the page ──
out="$(printf '%s\n' 'Mitochondria' | VAULT_CONTENT_DIR="$C" "$AI" lookup)" || {
  bad "lookup exited non-zero"
  out=""
}
expected=$'Mitochondria\twiki/entities/mitochondria.md'
[[ "$out" == "$expected" ]] && ok "lookup resolves the alias on stdin" \
  || { bad "lookup stdout mismatch"; printf '    expected: %q\n    actual:   %q\n' "$expected" "$out"; }

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
