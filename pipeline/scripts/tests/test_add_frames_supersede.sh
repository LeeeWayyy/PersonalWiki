#!/usr/bin/env bash
# e2e for "add frames to an existing transcript" SUPERSEDE (deferred §8 item) via ingest.py:
#   run 1: ingest <url>          (transcript-remote) → source A (transcript-only), note1 cites A
#   run 2: ingest <url> --frames (extract-remote)    → source B (supersedes A): B carries A's
#          transcript BYTE-EXACT + grafts the frame bundle; note1's live citation migrates A→B.
# Byte-exact carry-forward is the whole point — B's .transcript.md must equal A's (so every
# existing transcript citation stays valid), NOT the frame-run's transcript. A stays immutable.
# Stub extract-remote + transcript-remote + stub LLM; isolated content/ repo.

set -euo pipefail
PIPELINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ROOT="$(cd "$PIPELINE_ROOT/.." && pwd)"
STUB_LLM="$PIPELINE_ROOT/scripts/tests/stub-llm.py"
STUB_TR="$PIPELINE_ROOT/scripts/tests/stub-transcript-remote"
STUB_EX="$PIPELINE_ROOT/scripts/tests/stub-extract-remote"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
rc=0
URL="https://www.youtube.com/watch?v=ADDFRAMESv1"
echo "test_add_frames_supersede (youtube --frames on a transcript head, through ingest.py):"

CLONE="$TMP/clone"
rsync -a --exclude='.git' --exclude='.mypy_cache' --exclude='.ruff_cache' \
      --exclude='.obsidian' --exclude='node_modules' --exclude='backend/.venv' \
      --exclude='dist' --exclude='.astro' --exclude='vault' \
      --exclude='content' \
      --exclude='public/pagefind' --exclude='public/vault-assets' \
      --exclude='backend/data' --exclude='pipeline/scripts/tests/.e2e-snapshot.*' \
      "$PROJECT_ROOT/" "$CLONE/"
mkdir -p "$CLONE/content/wiki/entities" "$CLONE/content/wiki/topics" \
         "$CLONE/content/wiki/_index" "$CLONE/content/sources"
cp "$PROJECT_ROOT/ci-fixtures/content/wiki/_taxonomy.md" "$CLONE/content/wiki/_taxonomy.md"
CROOT="$CLONE/content"
git -C "$CROOT" init -q
git -C "$CROOT" config user.email e2e@test
git -C "$CROOT" config user.name e2e
git -C "$CROOT" add -A
git -C "$CROOT" commit -qm "e2e baseline"

ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }
run_ingest() {  # $1=STUB_ENTITY  $2.. = extra ingest args
  local entity="$1"; shift
  ( cd "$CLONE" && env -u VAULT_CONTENT_DIR \
      LLM_CMD="$STUB_LLM" TRANSCRIPT_REMOTE_CMD="$STUB_TR" EXTRACT_REMOTE_CMD="$STUB_EX" \
      STUB_ENTITY="$entity" PW_INGEST_SKIP_ARGUMENT_MAP=1 \
      ./pipeline/ingest.py "$URL" --kind video "$@" )
}

# ── run 1: plain transcript ingest → source A (transcript-only) ──
echo "  run 1: ingest $URL …"
run_ingest note1 > "$TMP/out1" 2>&1 || { echo "  ✗ run1 failed:"; sed 's/^/    | /' "$TMP/out1" | tail -30; exit 1; }
sidA="$(git -C "$CROOT" log -1 --format=%s | sed -nE 's/^ingest: ([0-9A-Z]{26}).*/\1/p')"
[[ -n "$sidA" ]] && ok "run1 committed transcript source A=$sidA" || { bad "run1 no ingest commit"; exit 1; }
scA="$(git -C "$CROOT" ls-files -- 'sources/*.transcript.md.md' | head -1)"
grep -q '^  frame_count:' "$CROOT/$scA" && bad "A should be transcript-only (no frame_count)" || ok "A is transcript-only"
shaA="$(grep -m1 '^sha256:' "$CROOT/$scA" | awk '{print $2}')"
grep -qF "[src:${sidA}]" "$CROOT/wiki/entities/note1.md" && ok "note1 cites A" || bad "note1 missing A citation"

# ── run 2: --frames → supersede A with B (carries A's transcript byte-exact + frames) ──
echo "  run 2: ingest $URL --frames (unchanged transcript → NO_CHANGES) …"
STUB_NO_CHANGES=1 run_ingest note1 --frames > "$TMP/out2" 2>&1 || { echo "  ✗ run2 (--frames) failed:"; sed 's/^/    | /' "$TMP/out2" | tail -30; exit 1; }
sidB="$(git -C "$CROOT" log -1 --format=%s | sed -nE 's/^ingest \(no-changes\): ([0-9A-Z]{26}).*/\1/p')"
[[ -n "$sidB" && "$sidB" != "$sidA" ]] && ok "run2 minted a NEW source B=$sidB" || bad "run2 did not mint a new source"

scB="$(git -C "$CROOT" diff-tree --no-commit-id --name-only -r HEAD | grep -E '\.transcript\.md\.md$' | head -1)"
[[ -n "$scB" ]] && grep -qF "supersedes: '[[${sidA}]]'" "$CROOT/$scB" && ok "B sidecar supersedes A" || { bad "B does not supersede A"; grep -n supersedes "$CROOT/$scB" 2>/dev/null; }
grep -q '^  frame_count:' "$CROOT/$scB" && ok "B carries a frame bundle (frame_count set)" || bad "B has no frame_count"
grep -q 'role: frame_bundle' "$CROOT/$scB" && grep -q 'role: frame_image' "$CROOT/$scB" && ok "B evidence has frames_json/frame_image/frame_bundle" || bad "B frame evidence incomplete"

# BYTE-EXACT carry-forward: B's .transcript.md == A's (same sha), holds A's transcript text,
# and does NOT contain the frame-run's transcript text.
shaB="$(grep -m1 '^sha256:' "$CROOT/$scB" | awk '{print $2}')"
[[ -n "$shaA" && "$shaB" == "$shaA" ]] && ok "B's transcript.md sha == A's (byte-exact carry-forward)" || bad "B transcript sha != A ($shaB vs $shaA)"
# the .transcript.json is ALSO carried byte-exact (transcript_json_sha256 must match A's)
jshaA="$(grep -m1 '  transcript_json_sha256:' "$CROOT/$scA" | awk '{print $2}')"
jshaB="$(grep -m1 '  transcript_json_sha256:' "$CROOT/$scB" | awk '{print $2}')"
[[ -n "$jshaA" && "$jshaB" == "$jshaA" ]] && ok "B's transcript.json sha == A's (byte-exact)" || bad "B transcript.json sha != A ($jshaB vs $jshaA)"
# PROVENANCE: B must report A's real transcript tool (transcript-remote), NOT the frames
# extractor — B carries A's transcript, so the field must trace to A, not to extract-remote.
grep -q '^  transcript_tool: transcript-remote' "$CROOT/$scB" && ok "B reports A's transcript_tool (not extract-remote)" || { bad "B misattributes transcript provenance"; grep -n transcript_tool "$CROOT/$scB"; }
mdB="${scB%.md}"
grep -qF "mitochondria and ATP in cells" "$CROOT/$mdB" && ok "B holds A's transcript text" || bad "B missing A's transcript text"
grep -qF "Frame e2e" "$CROOT/$mdB" && bad "B leaked the frame-run transcript (not carried from A)" || ok "B did NOT use the frame-run transcript"

# citation migration A→B + A immutable
grep -qF "[src:${sidB}]" "$CROOT/wiki/entities/note1.md" && ok "note1 citation migrated A→B" || bad "note1 not migrated"
! grep -qF "$sidA" "$CROOT/wiki/entities/note1.md" && ok "no stale A citation remains in note1" || bad "stale A citation remains"
[[ "$(git -C "$CROOT" ls-files -- 'sources/*.transcript.md.md' | wc -l | tr -d ' ')" == "2" ]] && ok "both A and B sidecars present (A immutable)" || bad "expected 2 transcript sidecars"
git -C "$CROOT" grep -qF "source_id: $sidA" -- 'sources/*.transcript.md.md' && ok "A's source artifact still committed" || bad "A's sidecar vanished"
tail -3 "$CROOT/.wiki/log.md" | grep -qF "supersedes ${sidA}" && ok "log records the supersede" || bad "log missing supersede note"
( cd "$CROOT" && VAULT_CONTENT_DIR="$CROOT" "$PIPELINE_ROOT/scripts/lint.py" >/dev/null 2>&1 ) && ok "full lint clean after supersede" || { bad "lint not clean"; ( cd "$CROOT" && VAULT_CONTENT_DIR="$CROOT" "$PIPELINE_ROOT/scripts/lint.py" 2>&1 | grep '✗' | head ); }
# (the carry-forward no-drift re-hash guard is exercised at the media-identity level in
#  test_video_frames_e2e §5, where the transcript-only head can be controlled precisely.)

# ── NO_CHANGES supersede: --add-frames carries A's transcript byte-identical, so the LLM
#    almost always has NOTHING to add and returns NO_CHANGES. That path must STILL migrate the
#    predecessor's citations — otherwise D commits as head while old pages keep citing C. ──
URL2="https://www.youtube.com/watch?v=ADDFRAMESv2"
ri2() {  # $1=STUB_ENTITY  $2=extra env assignment ("" or STUB_NO_CHANGES=1)  $3..=ingest args
  local entity="$1" extra="$2"; shift 2
  ( cd "$CLONE" && env -u VAULT_CONTENT_DIR LLM_CMD="$STUB_LLM" TRANSCRIPT_REMOTE_CMD="$STUB_TR" \
      EXTRACT_REMOTE_CMD="$STUB_EX" STUB_ENTITY="$entity" ${extra:+$extra} \
      PW_INGEST_SKIP_ARGUMENT_MAP=1 \
      ./pipeline/ingest.py "$URL2" --kind video "$@" )
}
echo "  run 3: ingest $URL2 (transcript) → C …"
ri2 note3 "" > "$TMP/out3" 2>&1 || { echo "  ✗ run3 failed:"; sed 's/^/    | /' "$TMP/out3" | tail -20; exit 1; }
sidC="$(git -C "$CROOT" log -1 --format=%s | sed -nE 's/^ingest: ([0-9A-Z]{26}).*/\1/p')"
[[ -n "$sidC" ]] && grep -qF "[src:${sidC}]" "$CROOT/wiki/entities/note3.md" && ok "run3 committed C, note3 cites C" || { bad "run3 setup failed"; exit 1; }
echo "  run 4: ingest $URL2 --frames with LLM NO_CHANGES → D supersedes C …"
ri2 note4 "STUB_NO_CHANGES=1" --frames > "$TMP/out4" 2>&1 || { echo "  ✗ run4 failed:"; sed 's/^/    | /' "$TMP/out4" | tail -20; exit 1; }
grep -q "NO_CHANGES" "$TMP/out4" && ok "run4 hit the NO_CHANGES path" || bad "run4 did not return NO_CHANGES"
sidD="$(git -C "$CROOT" log -1 --format=%s | sed -nE 's/^ingest \(no-changes\): ([0-9A-Z]{26}).*/\1/p')"
[[ -n "$sidD" && "$sidD" != "$sidC" ]] && ok "run4 minted new frames source D in a no-changes commit" || bad "run4 wrong commit/source ($sidD)"
# THE KEY ASSERTION: a NO_CHANGES supersede STILL migrates C→D (no orphan).
grep -qF "[src:${sidD}]" "$CROOT/wiki/entities/note3.md" && ok "note3 migrated C→D despite NO_CHANGES" || bad "NO_CHANGES supersede ORPHANED C's citation"
! grep -qF "$sidC" "$CROOT/wiki/entities/note3.md" && ok "no stale C citation remains" || bad "stale C citation remains after NO_CHANGES supersede"
git -C "$CROOT" grep -qF "supersedes: '[[${sidC}]]'" -- 'sources/*.transcript.md.md' && ok "D supersedes C" || bad "D missing supersedes pointer"
( cd "$CROOT" && VAULT_CONTENT_DIR="$CROOT" "$PIPELINE_ROOT/scripts/lint.py" >/dev/null 2>&1 ) && ok "full lint clean after NO_CHANGES supersede" || { bad "lint not clean"; ( cd "$CROOT" && VAULT_CONTENT_DIR="$CROOT" "$PIPELINE_ROOT/scripts/lint.py" 2>&1 | grep '✗' | head ); }

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
