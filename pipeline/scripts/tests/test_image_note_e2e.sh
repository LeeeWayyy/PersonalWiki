#!/usr/bin/env bash
# End-to-end test for the Phase 3 image_note front door (§8.2):
# `media-identity.py <bundle> --kind image_note --post-id <p> --platform rednote`
# → (stub) extract-remote → render .cards.md + .cards.json + commit card images
# under <slug>.cards.md.assets/ → atomic stage + image_note sidecar. Exercises:
# happy path, evidence_artifacts drift verification, dedup reuse, known-post
# bundle-drift die, manual-export (image_bundle) basis. Run from the tooling root.

set -euo pipefail
VAULT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STUB="$VAULT_ROOT/scripts/tests/stub-extract-remote"
MI="$VAULT_ROOT/scripts/media-identity.py"
rc=0
echo "test_image_note_e2e:"

C="$(mktemp -d)/content"; mkdir -p "$C/sources"
trap 'rm -rf "$(dirname "$C")"' EXIT
git -C "$C" init -q; git -C "$C" config user.email t@t; git -C "$C" config user.name t

ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }
run() { env VAULT_CONTENT_DIR="$C" EXTRACT_REMOTE_CMD="$STUB" "$@"; }

# ── 0. destination ownership guards: sidecar-less user data is never replaced. ──
DAY="$(date -u +%F)"
GUARD_MD="$C/sources/${DAY}-rednote-guard-md.cards.md"
printf 'user markdown\n' > "$GUARD_MD"
if run "$MI" /tmp/export.zip --kind image_note --post-id guard-md --platform rednote >/dev/null 2>/tmp/in-guard-md.err; then
  bad "sidecar-less untracked markdown should be refused"
elif [[ "$(cat "$GUARD_MD")" == "user markdown" ]] && grep -q 'no coherent ownership sidecar' /tmp/in-guard-md.err; then
  ok "sidecar-less untracked markdown is refused intact"
else bad "untracked markdown guard changed data or gave wrong error"; fi
rm -f "$GUARD_MD"

GUARD_JSON="$C/sources/${DAY}-rednote-guard-json.cards.json"
printf '{"user":true}\n' > "$GUARD_JSON"
if run "$MI" /tmp/export.zip --kind image_note --post-id guard-json --platform rednote >/dev/null 2>/tmp/in-guard-json.err; then
  bad "sidecar-less untracked audit JSON should be refused"
elif [[ "$(cat "$GUARD_JSON")" == '{"user":true}' ]] && grep -q 'no coherent ownership sidecar' /tmp/in-guard-json.err; then
  ok "sidecar-less untracked audit JSON is refused intact"
else bad "untracked audit JSON guard changed data or gave wrong error"; fi
rm -f "$GUARD_JSON"

GUARD_ASSETS="$C/sources/${DAY}-rednote-guard-assets.cards.md.assets"
mkdir -p "$GUARD_ASSETS"; printf 'user image\n' > "$GUARD_ASSETS/user.jpg"
if run "$MI" /tmp/export.zip --kind image_note --post-id guard-assets --platform rednote >/dev/null 2>/tmp/in-guard-assets.err; then
  bad "sidecar-less untracked assets should be refused"
elif [[ "$(cat "$GUARD_ASSETS/user.jpg")" == "user image" ]] && grep -q 'no coherent ownership sidecar' /tmp/in-guard-assets.err; then
  ok "sidecar-less untracked assets dir is refused intact"
else bad "untracked assets guard changed data or gave wrong error"; fi
rm -rf "$GUARD_ASSETS"

# ── 1. happy path → committed image_note source ──
OUT="$(run "$MI" "/tmp/export.zip" --kind image_note --post-id "64fABC" --platform rednote 2>/tmp/in.err)" \
  || { echo "  ✗ image_note ingest exited non-zero:"; sed 's/^/    | /' /tmp/in.err; exit 1; }
SC="$(ls "$C"/sources/*.cards.md.md 2>/dev/null | head -1)"
[[ -n "$SC" ]] && ok "image_note sidecar written" || { bad "no sidecar"; echo "$OUT"; }
grep -q '^  platform: rednote' "$SC" && ok "platform: rednote" || bad "platform wrong"
grep -q '^  identity_basis: image_post_id' "$SC" && ok "identity_basis: image_post_id" || bad "basis wrong"
grep -qF "  post_id: '64fABC'" "$SC" && ok "post_id recorded" || { bad "no post_id"; grep post_id "$SC"; }
grep -q '^  card_count: 2' "$SC" && ok "card_count: 2" || bad "card_count wrong"
grep -q '^  image_bundle_sha256: ' "$SC" && ok "image_bundle_sha256 present" || bad "no bundle sha"
grep -q "role: cards_json" "$SC" && grep -q "role: card_image" "$SC" && grep -q "role: image_bundle" "$SC" \
  && ok "evidence_artifacts: cards_json + card_image + image_bundle" || bad "evidence_artifacts incomplete"
MD="${SC%.md}"
grep -q '^## card 1' "$MD" && grep -q '^## card 2' "$MD" && ok ".cards.md rendered with card headings" || bad ".cards.md headings missing"
CJ="${MD%.cards.md}.cards.json"
[[ -f "$CJ" ]] && grep -q '"heading_anchor": "card-1"' "$CJ" && ok ".cards.json audit artifact written" || bad "no .cards.json"
ADIR="$MD.assets"
[[ -f "$ADIR/card-00000.jpg" && -f "$ADIR/card-00001.jpg" ]] && ok "card images committed under .cards.md.assets/" || bad "card images missing"
echo "$OUT" | grep -q "AUDIT_JSON=.*cards.json" && echo "$OUT" | grep -q "DEST=.*cards.md" && ok "emit contract (DEST + AUDIT_JSON)" || { bad "emit contract"; echo "$OUT"; }

# A coherent untracked sidecar only owns the exact evidenced payload. An extra
# file in its asset directory could be user data and must survive a re-run.
printf 'user extra\n' > "$ADIR/user-extra.jpg"
if run "$MI" /tmp/export.zip --kind image_note --post-id 64fABC --platform rednote >/dev/null 2>/tmp/in-extra.err; then
  bad "coherent orphan with an unlisted asset should be refused"
elif [[ "$(cat "$ADIR/user-extra.jpg")" == "user extra" ]] \
     && grep -q 'unlisted or does not match' /tmp/in-extra.err; then
  ok "coherent sidecar does not authorize deletion of an unlisted asset"
else bad "unlisted orphan asset changed or wrong error"; fi
rm -f "$ADIR/user-extra.jpg"

# ── 2. lint drift: evidence_artifacts (cards.json + images + bundle) all verify ──
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm c1
DRIVER="$VAULT_ROOT/scripts/tests/_drift_driver.py"
cat > "$DRIVER" <<'PY'
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lint
ok, notes = lint.check_source_drift()
print("OK" if ok else "FAIL")
[print(n) for n in notes]
PY
[[ "$(VAULT_CONTENT_DIR="$C" uv run --quiet "$DRIVER" 2>&1 | head -1)" == "OK" ]] \
  && ok "check_source_drift verifies the committed image_note (evidence_artifacts)" || { bad "drift check failed"; VAULT_CONTENT_DIR="$C" uv run --quiet "$DRIVER"; }
# tamper a committed card image → drift caught
echo "tampered" > "$ADIR/card-00000.jpg"
[[ "$(VAULT_CONTENT_DIR="$C" uv run --quiet "$DRIVER" 2>&1 | head -1)" == "FAIL" ]] && ok "tampered card image → drift caught" || bad "image tamper not caught"
git -C "$C" checkout -- "sources/" >/dev/null 2>&1; rm -f "$DRIVER"

# ── 3. dedup: re-run same post_id → reuse ──
OUT2="$(run "$MI" "/tmp/export.zip" --kind image_note --post-id "64fABC" --platform rednote 2>/dev/null)"
echo "$OUT2" | grep -q "EXISTING_SIDECAR=.*cards.md.md" && ok "re-ingest same post_id → reuse" || { bad "did not dedup"; echo "$OUT2"; }
[[ "$(ls "$C"/sources/*.cards.md.md | wc -l | tr -d ' ')" == "1" ]] && ok "no duplicate source minted" || bad "duplicate minted"

# ── 4. known-post bundle drift: same post_id, different bundle (3 cards) → die ──
if run env STUB_IMAGE_CARDS=3 "$MI" "/tmp/export.zip" --kind image_note --post-id "64fABC" --platform rednote >/dev/null 2>&1; then
  bad "changed bundle for a known post should die (without --reocr)"
else ok "known-post bundle drift → die (needs --reocr)"; fi

# ── 5. manual export (no --post-id) → image_bundle basis ──
OUT5="$(run env STUB_IMAGE_POSTID="" "$MI" "/tmp/export2.zip" --kind image_note --platform unknown 2>/tmp/in5.err)" \
  || { echo "  ✗ manual-export ingest failed:"; sed 's/^/    | /' /tmp/in5.err; exit 1; }
SC5="$(ls -t "$C"/sources/*.cards.md.md | head -1)"
grep -q '^  identity_basis: image_bundle' "$SC5" && grep -q '^  platform: unknown' "$SC5" \
  && ok "manual export → platform: unknown + identity_basis: image_bundle" || { bad "manual-export identity wrong"; grep -E 'platform|identity_basis' "$SC5"; }

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
