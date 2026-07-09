#!/usr/bin/env bash
# End-to-end test for the Phase 3 video-frames front door (§8.3):
# `media-identity.py <url> --kind video --frames` → (stub) extract-remote →
# transcript + frames.json + frame images under <slug>.transcript.md.assets/ →
# atomic stage + frames sidecar. Exercises: fresh ingest, evidence_artifacts drift
# verify, the #frame-N validator (valid/out-of-range/capability), idempotent reuse,
# and add-frames-to-a-transcript supersede (byte-exact carry-forward + no-drift guard).
# Run from the tooling root.

set -euo pipefail
VAULT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STUB="$VAULT_ROOT/scripts/tests/stub-extract-remote"
MI="$VAULT_ROOT/scripts/media-identity.py"
LINT="$VAULT_ROOT/scripts/lint.py"
rc=0
VID="FRAMESvid01"; URL="https://www.youtube.com/watch?v=${VID}"
echo "test_video_frames_e2e:"

C="$(mktemp -d)/content"; mkdir -p "$C/sources" "$C/wiki/entities"
trap 'rm -rf "$(dirname "$C")"' EXIT
git -C "$C" init -q; git -C "$C" config user.email t@t; git -C "$C" config user.name t
printf '# tax\n' > "$C/wiki/_taxonomy.md"

ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }
run() { env VAULT_CONTENT_DIR="$C" EXTRACT_REMOTE_CMD="$STUB" "$@"; }

# ── 1. fresh video + --frames → new frames source ──
OUT="$(run "$MI" "$URL" --kind video --frames 2>/tmp/fr.err)" \
  || { echo "  ✗ frames ingest exited non-zero:"; sed 's/^/    | /' /tmp/fr.err; exit 1; }
SC="$(ls "$C"/sources/*.transcript.md.md 2>/dev/null | head -1)"
[[ -n "$SC" ]] && ok "frames sidecar written" || { bad "no sidecar"; echo "$OUT"; }
grep -q '^  platform: youtube' "$SC" && grep -qF "  video_id: ${VID}" "$SC" && ok "platform youtube + video_id" || bad "identity wrong"
grep -q '^  frame_count: 2' "$SC" && ok "frame_count: 2" || bad "frame_count wrong"
grep -q '^  frame_policy:' "$SC" && ok "frame_policy recorded" || bad "no frame_policy"
grep -q "role: transcript_json" "$SC" && grep -q "role: frames_json" "$SC" && grep -q "role: frame_image" "$SC" && grep -q "role: frame_bundle" "$SC" \
  && ok "evidence_artifacts: transcript_json + frames_json + frame_image + frame_bundle" || bad "evidence_artifacts incomplete"
MD="${SC%.md}"
ADIR="$MD.assets"
[[ -f "$ADIR/frames.json" ]] && grep -q '"index": 1' "$ADIR/frames.json" && ok "frames.json written (1-based index)" || bad "frames.json missing/0-based"
[[ -f "$ADIR/frame-000000.jpg" && -f "$ADIR/frame-000001.jpg" ]] && ok "frame images committed under .assets/" || bad "frame images missing"
grep -qE '^\[00:00-00:05\]' "$MD" && ok "transcript md rendered" || bad "md not rendered"
echo "$OUT" | grep -q "AUDIT_JSON=.*transcript.json" && ok "emit contract (AUDIT_JSON)" || { bad "emit"; echo "$OUT"; }
FID="$(echo "$OUT" | sed -nE "s/^SOURCE_ID=(.*)/\1/p" | tr -d "'\"")"

# ── 2. drift: evidence_artifacts (transcript_json + frames_json + frame images + bundle) verify ──
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
  && ok "check_source_drift verifies the frames source" || { bad "drift check failed"; VAULT_CONTENT_DIR="$C" uv run --quiet "$DRIVER"; }
echo "tampered" > "$ADIR/frame-000000.jpg"
[[ "$(VAULT_CONTENT_DIR="$C" uv run --quiet "$DRIVER" 2>&1 | head -1)" == "FAIL" ]] && ok "tampered frame image → drift caught" || bad "frame tamper not caught"
git -C "$C" checkout -- sources/ >/dev/null 2>&1; rm -f "$DRIVER"

# ── 3. #frame-N validator ──
mkpage() { cat > "$C/wiki/entities/p.md" <<MD
---
type: Entity
page_id: $(printf 'P%.0s' {1..26})
sources: [${FID}]
---
# P
<!-- llm-zone -->
$1
<!-- /llm-zone -->
MD
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm p >/dev/null; }
fagate() { VAULT_CONTENT_DIR="$C" "$LINT" --gate=frame-anchors >/dev/null 2>&1; }
mkpage "A frame [src:${FID}#frame-1] and [src:${FID}#frame-2]."
fagate && ok "valid #frame-1/#frame-2 → pass" || bad "valid frames failed"
mkpage "Out of range [src:${FID}#frame-9]."
fagate && bad "#frame-9 should fail" || ok "#frame-N out of range → fail"

# ── 4. idempotent reuse on a frames head ──
mkpage "x"  # reset page to avoid stale frame anchors
OUT2="$(run "$MI" "$URL" --kind video --frames 2>/dev/null)"
echo "$OUT2" | grep -q "EXISTING_SIDECAR=.*transcript.md.md" && ok "re-ingest a frames source → idempotent reuse" || { bad "did not reuse"; echo "$OUT2"; }

# ── 5. transcript-only head + --frames → ADD FRAMES via supersede (byte-exact carry-forward).
#       Build a proper transcript-only head A (committed .transcript.md + .transcript.json with
#       matching hashes), then exercise the no-drift guard and the supersede. ──
VID2="ADDFRAMEvid"; SID2="$(printf 'S%.0s' {1..26})"
A_MD="$C/sources/t.transcript.md"; A_JSON="$C/sources/t.transcript.json"
cat > "$A_MD" <<TXT
# t

<https://www.youtube.com/watch?v=${VID2}>
[0:00-0:05] carried transcript line
TXT
printf '%s' '{"segments":[{"start":0.0,"end":5.0,"speaker":"SPEAKER_00","text":"carried transcript line"}],"language":"en","meta":{"model":"large-v3","video_id":"ADDFRAMEvid","duration_s":5.0}}' > "$A_JSON"
TSHA="$(shasum -a 256 "$A_MD" | awk '{print $1}')"
JSHA="$(shasum -a 256 "$A_JSON" | awk '{print $1}')"
cat > "$C/sources/t.transcript.md.md" <<YAML
---
source_id: ${SID2}
type: source
sha256: ${TSHA}
origin_type: video
media: {platform: youtube, video_id: ${VID2}, transcript_json_sha256: ${JSHA}}
---
# t
YAML
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm transcript-only >/dev/null

# 5a. no-drift guard: corrupt A's committed transcript.md (drift from its sidecar sha) →
#     --frames must refuse to carry it forward.
printf '\ntampered\n' >> "$A_MD"
if run "$MI" "https://www.youtube.com/watch?v=${VID2}" --kind video --frames >/tmp/cf.err 2>&1; then
  bad "drifted carried transcript should die, not supersede"
else grep -q "drifted from sidecar" /tmp/cf.err && ok "drifted carry-forward transcript → dies loud" || { bad "wrong drift message"; tail -2 /tmp/cf.err; }; fi
git -C "$C" checkout -- "$A_MD" >/dev/null 2>&1

# 5a'. --frames --retranscribe on a head is still unsupported (re-transcribe + reframe) → die.
if run "$MI" "https://www.youtube.com/watch?v=${VID2}" --kind video --frames --retranscribe >/tmp/cfrt.err 2>&1; then
  bad "--frames --retranscribe should die (re-transcribe + reframe deferred)"
else grep -q "retranscribe" /tmp/cfrt.err && ok "--frames --retranscribe → dies (still deferred)" || { bad "wrong --retranscribe message"; tail -2 /tmp/cfrt.err; }; fi

# 5a''. physical-asset net: a head presenting transcript-only (no frame_count/evidence) but
#       with committed `.assets/` frame files (a hand-edited frames source) → --frames refuses
#       to carry it forward, even though _head_has_frames() returns False on the YAML signals.
VIDH="HANDEDITv12"; SIDH="$(printf 'H%.0s' {1..26})"
mkdir -p "$C/sources/he.transcript.md.assets"
cat > "$C/sources/he.transcript.md" <<TXT
# he

<https://www.youtube.com/watch?v=${VIDH}>
[0:00-0:05] hi
TXT
printf '[]' > "$C/sources/he.transcript.md.assets/frames.json"
# sha256 must be present + correct so the assets-net (which runs FIRST) is what fires —
# without it the run would die earlier on the missing-hash guard with a different message.
HSHA="$(shasum -a 256 "$C/sources/he.transcript.md" | awk '{print $1}')"
cat > "$C/sources/he.transcript.md.md" <<YAML
---
source_id: ${SIDH}
type: source
sha256: ${HSHA}
origin_type: video
media: {platform: youtube, video_id: ${VIDH}, transcript_tool: transcript-remote}
---
# he
YAML
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm handedited-frames >/dev/null
if run "$MI" "https://www.youtube.com/watch?v=${VIDH}" --kind video --frames >/tmp/he.err 2>&1; then
  bad "transcript-only-presenting head WITH committed frame assets should die"
else grep -q "committed frame assets" /tmp/he.err && ok "committed frame assets on a transcript-only head → dies loud" || { bad "wrong assets-net message"; tail -2 /tmp/he.err; }; fi
git -C "$C" rm -rq sources/he.transcript.md sources/he.transcript.md.md sources/he.transcript.md.assets >/dev/null 2>&1
git -C "$C" commit -qm drop-handedited >/dev/null 2>&1

# 5b. clean head → supersede: mint B (frames) carrying A byte-exact.
OUT5="$(run "$MI" "https://www.youtube.com/watch?v=${VID2}" --kind video --frames 2>/tmp/cf2.err)" \
  || { bad "transcript-only head + --frames (supersede) exited non-zero"; sed 's/^/    | /' /tmp/cf2.err | tail -5; }
SIDB="$(echo "$OUT5" | sed -nE 's/^SOURCE_ID=(.*)/\1/p' | tr -d "'\"")"
[[ -n "$SIDB" && "$SIDB" != "$SID2" ]] && ok "transcript-only head + --frames → mints new frames source" || bad "no new source minted"
echo "$OUT5" | grep -qF "SUPERSEDES=${SID2}" && ok "emit SUPERSEDES=A (ingest migrates citations)" || { bad "no SUPERSEDES emit"; echo "$OUT5"; }
NEWSC="$(echo "$OUT5" | sed -nE 's/^SIDECAR=(.*)/\1/p' | tr -d "'\"")"
grep -qF "supersedes: '[[${SID2}]]'" "$C/$NEWSC" && ok "B supersedes A" || bad "B does not supersede A"
grep -q '^  frame_count:' "$C/$NEWSC" && ok "B carries a frame bundle" || bad "B has no frames"
BSHA="$(grep -m1 '^sha256:' "$C/$NEWSC" | awk '{print $2}')"
[[ "$BSHA" == "$TSHA" ]] && ok "B's transcript.md sha == A's (byte-exact carry-forward)" || bad "B transcript drifted from A ($BSHA vs $TSHA)"
git -C "$C" checkout -- sources/ >/dev/null 2>&1; git -C "$C" clean -fdq sources/ >/dev/null 2>&1  # drop staged supersede artifacts

# 5c. a #frame-N citing an INDEPENDENT transcript-only source → capability error
VID3="CAPonlyvid1"; SID3="$(printf 'Q%.0s' {1..26})"
cat > "$C/sources/cap.transcript.md" <<TXT
# cap

<https://www.youtube.com/watch?v=${VID3}>
[0:00-0:05] hello
TXT
CSHA="$(shasum -a 256 "$C/sources/cap.transcript.md" | awk '{print $1}')"
cat > "$C/sources/cap.transcript.md.md" <<YAML
---
source_id: ${SID3}
type: source
sha256: ${CSHA}
origin_type: video
media: {platform: youtube, video_id: ${VID3}}
---
# cap
YAML
cat > "$C/wiki/entities/q.md" <<MD
---
type: Entity
page_id: $(printf 'P%.0s' {1..26})
sources: [${SID3}]
---
# Q
<!-- llm-zone -->
Bad [src:${SID3}#frame-1].
<!-- /llm-zone -->
MD
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm q >/dev/null
fagate && bad "#frame on a non-frames source should fail" || ok "#frame on a non-frames source → capability error"

# 5d. provenance: a transcript-only head whose transcript_tool is a NON-token (hand-edited,
#     e.g. contains a `:`) must DIE on carry-forward, not be silently sanitized/rewritten into
#     B's sidecar. Build a fully-valid head (md+json+hashes) differing only in transcript_tool.
VID4="TOKtestvid1"; SID4="$(printf 'K%.0s' {1..26})"
TT_MD="$C/sources/tt.transcript.md"; TT_JSON="$C/sources/tt.transcript.json"
cat > "$TT_MD" <<TXT
# tt

<https://www.youtube.com/watch?v=${VID4}>
[0:00-0:05] token test line
TXT
printf '%s' '{"segments":[{"start":0.0,"end":5.0,"speaker":"SPEAKER_00","text":"token test line"}],"language":"en","meta":{"model":"large-v3","video_id":"TOKtestvid1","duration_s":5.0}}' > "$TT_JSON"
TT_SHA="$(shasum -a 256 "$TT_MD" | awk '{print $1}')"
TTJ_SHA="$(shasum -a 256 "$TT_JSON" | awk '{print $1}')"
cat > "$C/sources/tt.transcript.md.md" <<YAML
---
source_id: ${SID4}
type: source
sha256: ${TT_SHA}
origin_type: video
media: {platform: youtube, video_id: ${VID4}, transcript_tool: 'b:ad tool', transcript_json_sha256: ${TTJ_SHA}}
---
# tt
YAML
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm bad-token >/dev/null
if run "$MI" "https://www.youtube.com/watch?v=${VID4}" --kind video --frames >/tmp/tt.err 2>&1; then
  bad "non-token transcript_tool should die, not be silently rewritten"
else grep -q "not a safe scalar token" /tmp/tt.err && ok "non-token transcript_tool → dies loud (no silent rewrite)" || { bad "wrong token message"; tail -2 /tmp/tt.err; }; fi
git -C "$C" rm -rq sources/tt.transcript.md sources/tt.transcript.md.md sources/tt.transcript.json >/dev/null 2>&1
git -C "$C" commit -qm drop-bad-token >/dev/null 2>&1

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
