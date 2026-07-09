#!/usr/bin/env bash
# Test the orphan-asset allowlist (lint #17 check_orphan_assets): an unreferenced
# asset warns; adding it to sources/.orphan-assets-allow silences it; a stale
# allowlist entry (no longer orphaned) is reported. Runs check_orphan_assets()
# directly so it doesn't depend on the rest of the lint passing on a tiny vault.
set -euo pipefail
PIPELINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
C="$TMP/content"
mkdir -p "$C/wiki/entities" "$C/sources/doc.epub.assets"
rc=0
echo "test_orphan_allowlist:"
ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }

# a source with one figure that no wiki page embeds → an orphan
printf 'x' > "$C/sources/doc.epub.assets/fig1.jpg"
cat > "$C/wiki/entities/foo.md" <<'MD'
---
type: Entity
page_id: AAAAAAAAAAAAAAAAAAAAAAAAAA
tags: [concept]
---
# Foo
<!-- llm-zone -->
No embeds here.
<!-- /llm-zone -->
MD

run_check() {  # prints the check_orphan_assets notes
  VAULT_CONTENT_DIR="$C" python3 - "$PIPELINE_ROOT" <<'PY'
import importlib.util, sys
root = sys.argv[1]
spec = importlib.util.spec_from_file_location("lint", root + "/scripts/lint.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
_ok, notes = m.check_orphan_assets()
print("\n".join(notes))
PY
}

out="$(run_check)"
echo "$out" | grep -q '1 orphan asset file' && ok "unreferenced asset warns" || { bad "expected an orphan warning"; echo "$out"; }

# allowlist it → warning goes away
printf '%s\n' '# keep the extracted figure' 'sources/doc.epub.assets/fig1.jpg' > "$C/sources/.orphan-assets-allow"
out="$(run_check)"
echo "$out" | grep -q 'allowlisted' && ok "allowlisted asset is silenced" || { bad "expected allowlisted note"; echo "$out"; }
echo "$out" | grep -q '⚠ 1 orphan' && { bad "still warned after allowlisting"; echo "$out"; } || ok "no orphan warning after allowlist"

# a stale allowlist entry (path that isn't actually an orphan) is reported
printf '%s\n' 'sources/doc.epub.assets/fig1.jpg' 'sources/doc.epub.assets/gone.jpg' > "$C/sources/.orphan-assets-allow"
out="$(run_check)"
echo "$out" | grep -q 'stale allowlist' && ok "stale allowlist entry reported" || { bad "expected stale-entry note"; echo "$out"; }

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
