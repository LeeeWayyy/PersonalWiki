#!/usr/bin/env bash
# Unit test for the §8.4 evidence_artifacts[] drift guard in lint.check_source_drift:
# file entries (path+sha256) are re-hashed; bundle_recipe entries (image_note's
# image_bundle_sha256) are recomputed from the committed .cards.json; the top-level
# scalar must agree; the audit rows must carry index/image_sha256/image_path and each
# committed image must have a card_image evidence entry. Tampering any guarded artifact
# (or hand-editing the scalar) must be caught. Run from the tooling root.

set -euo pipefail
VAULT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
rc=0
echo "test_evidence_artifacts:"

C="$(mktemp -d)/content"; mkdir -p "$C/sources"
trap 'rm -rf "$(dirname "$C")"' EXIT
git -C "$C" init -q; git -C "$C" config user.email t@t; git -C "$C" config user.name t

SLUG="2026-06-08-rednote-abc"
ADIR="$C/sources/$SLUG.cards.md.assets"
mkdir -p "$ADIR"
sha() { shasum -a 256 "$1" | awk '{print $1}'; }

# real committed card images (the per-image evidence is re-hashed against these)
printf 'card-image-0-bytes' > "$ADIR/card-00000.jpg"
printf 'card-image-1-bytes' > "$ADIR/card-00001.jpg"
IA="$(sha "$ADIR/card-00000.jpg")"
IB="$(sha "$ADIR/card-00001.jpg")"
IP0="sources/$SLUG.cards.md.assets/card-00000.jpg"
IP1="sources/$SLUG.cards.md.assets/card-00001.jpg"

# canonical cards.md + the .cards.json audit artifact (full row schema)
printf '## card 1\nhello\n\n## card 2\nworld\n' > "$C/sources/$SLUG.cards.md"
MDSHA="$(sha "$C/sources/$SLUG.cards.md")"
cat > "$C/sources/$SLUG.cards.json" <<JSON
[{"index": 0, "heading_anchor": "card-1", "text": "hello", "image_sha256": "${IA}", "image_path": "${IP0}"},
 {"index": 1, "heading_anchor": "card-2", "text": "world", "image_sha256": "${IB}", "image_path": "${IP1}"}]
JSON
CJSHA="$(sha "$C/sources/$SLUG.cards.json")"
# image_bundle_sha256 = sha256 of the \n-joined index-ordered image_sha256 values
BUNDLE="$(printf '%s\n%s' "$IA" "$IB" | shasum -a 256 | awk '{print $1}')"

write_sidecar() { # $1 = bundle sha (scalar + evidence)
cat > "$C/sources/$SLUG.cards.md.md" <<YAML
---
source_id: 01CARDS00000000000000000A
type: source
sha256: ${MDSHA}
origin_type: image_note
media:
  platform: rednote
  post_id: abc
  identity_basis: image_post_id
  card_count: 2
  image_bundle_sha256: ${1}
  evidence_artifacts:
    - {role: cards_json, path: sources/${SLUG}.cards.json, sha256: ${CJSHA}}
    - {role: card_image, path: ${IP0}, sha256: ${IA}}
    - {role: card_image, path: ${IP1}, sha256: ${IB}}
    - {role: image_bundle, sha256: ${1}, bundle_recipe: image_sha256_index_join, from: sources/${SLUG}.cards.json}
---
# x
YAML
}
write_sidecar "$BUNDLE"
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm baseline

DRIVER="$VAULT_ROOT/scripts/tests/_drift_driver.py"
cat > "$DRIVER" <<'PY'
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/
import lint
ok, notes = lint.check_source_drift()
print("OK" if ok else "FAIL")
for n in notes:
    print(n)
PY
drift() { VAULT_CONTENT_DIR="$C" uv run --quiet "$DRIVER" 2>&1; }
ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }

[[ "$(drift | head -1)" == "OK" ]] && ok "clean evidence_artifacts (file + images + bundle + scalar) → pass" || { bad "clean state failed"; drift; }

# tamper a committed card image → card_image file-entry re-hash must catch it
printf 'TAMPERED' > "$ADIR/card-00000.jpg"
[[ "$(drift | head -1)" == "FAIL" ]] && ok "tampered card image → drift caught (card_image entry)" || bad "image drift not caught"
printf 'card-image-0-bytes' > "$ADIR/card-00000.jpg"

# tamper the .cards.json file → cards_json file-entry re-hash + bundle recompute catch it
printf '[{"index": 0, "heading_anchor": "card-1", "text": "x", "image_sha256": "%s", "image_path": "%s"}]' "$IA" "$IP0" \
  > "$C/sources/$SLUG.cards.json"
OUT="$(drift)"
[[ "$(echo "$OUT" | head -1)" == "FAIL" ]] && echo "$OUT" | grep -q "drifted" && ok "tampered .cards.json → drift caught (file entry)" || { bad "file drift not caught"; echo "$OUT"; }
git -C "$C" checkout -- "sources/$SLUG.cards.json" >/dev/null 2>&1

# corrupt the recorded BUNDLE sha (scalar + bundle entry) → recompute + scalar mismatch
write_sidecar "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
OUT2="$(drift)"
[[ "$(echo "$OUT2" | head -1)" == "FAIL" ]] && echo "$OUT2" | grep -q "bundle" && ok "wrong recorded image_bundle sha → drift caught (recipe recompute)" || { bad "bundle recipe drift not caught"; echo "$OUT2"; }
write_sidecar "$BUNDLE"

# drop the image_bundle_sha256 scalar → required-scalar check must fail
python3 - "$C/sources/$SLUG.cards.md.md" <<'PY'
import sys, re
p = sys.argv[1]; t = open(p).read()
open(p, "w").write(re.sub(r"\n  image_bundle_sha256: \w+", "", t))
PY
OUT3="$(drift)"
[[ "$(echo "$OUT3" | head -1)" == "FAIL" ]] && echo "$OUT3" | grep -q "missing media.image_bundle_sha256" && ok "missing image_bundle_sha256 scalar → fail (required)" || { bad "missing scalar not caught"; echo "$OUT3"; }
write_sidecar "$BUNDLE"

# drop one card_image evidence entry → completeness (per-image coverage) must fail
python3 - "$C/sources/$SLUG.cards.md.md" "$IP1" <<'PY'
import sys, re
p, ip1 = sys.argv[1], sys.argv[2]; t = open(p).read()
open(p, "w").write(re.sub(r"\n    - \{role: card_image, path: " + re.escape(ip1) + r"[^\n]*\}", "", t))
PY
OUT4="$(drift)"
[[ "$(echo "$OUT4" | head -1)" == "FAIL" ]] && echo "$OUT4" | grep -q "no 'card_image' evidence entry" && ok "removed card_image entry → completeness fail" || { bad "completeness gap not caught"; echo "$OUT4"; }

rm -f "$DRIVER"
[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
