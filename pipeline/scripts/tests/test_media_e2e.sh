#!/usr/bin/env bash
# End-to-end test for the Phase 2 media front door (expansion-plan §7):
# `ingest.py --kind video <url>` → media-identity.py → (stub) transcript-remote
# → validate/render → atomic move → keyword/diff/lint/commit spine.
#
# Uses a stub transcript-remote (no network/GPU) + the stub LLM, in an isolated
# copy. Asserts the committed media artifacts + the atomicity invariant.
#
# Run from the project root or directly; paths are resolved from this file.

set -euo pipefail
PIPELINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ROOT="$(cd "$PIPELINE_ROOT/.." && pwd)"
STUB_LLM="$PIPELINE_ROOT/scripts/tests/stub-llm.py"
STUB_TR="$PIPELINE_ROOT/scripts/tests/stub-transcript-remote"   # via $TRANSCRIPT_REMOTE_CMD
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
rc=0
VID="STUBvideo01"; URL="https://www.youtube.com/watch?v=${VID}&list=PLx"
echo "test_media_e2e:"

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
git -C "$CLONE/content" init -q
git -C "$CLONE/content" config user.email e2e@test
git -C "$CLONE/content" config user.name e2e
git -C "$CLONE/content" add -A
git -C "$CLONE/content" commit -qm "media-e2e baseline"

ok() { echo "  ✓ $1"; }
bad() { echo "  ✗ $1"; rc=1; }

run_ingest() { ( cd "$CLONE" && env -u VAULT_CONTENT_DIR TRANSCRIPT_REMOTE_CMD="$STUB_TR" \
                  LLM_CMD="$STUB_LLM" STUB_ENTITY="media-e2e-entity" \
                  PW_INGEST_SKIP_ARGUMENT_MAP=1 "$@" ); }

# ── 1. atomicity: a remote failure must leave sources/ byte-identical ──
SRC_BEFORE="$(git -C "$CLONE/content" status --porcelain sources/ | sort)"
if run_ingest env STUB_TRANSCRIPT_FAIL=1 ./pipeline/ingest.py --kind video "$URL" >/dev/null 2>&1; then
  bad "ingest should have failed on stubbed remote failure"
else
  ok "remote failure → ingest exits non-zero"
fi
SRC_AFTER="$(git -C "$CLONE/content" status --porcelain sources/ | sort)"
[[ "$SRC_BEFORE" == "$SRC_AFTER" ]] && ok "remote failure left sources/ byte-identical" \
  || bad "remote failure dirtied sources/: $SRC_AFTER"

# ── 2. happy path: full media ingest ──
HEAD_BEFORE="$(git -C "$CLONE/content" rev-parse HEAD)"
if run_ingest ./pipeline/ingest.py --kind video "$URL" > "$TMP/out" 2>&1; then :; else
  echo "  ✗ media ingest exited non-zero:"; sed 's/^/    | /' "$TMP/out" | tail -30; exit 1
fi
[[ "$(git -C "$CLONE/content" rev-parse HEAD)" != "$HEAD_BEFORE" ]] && ok "a commit was created" || bad "no commit"

SLUG_MD="$(git -C "$CLONE/content" show --name-only --format= HEAD | grep -E 'sources/.*\.transcript\.md$' | head -1)"
SLUG_JSON="$(git -C "$CLONE/content" show --name-only --format= HEAD | grep -E 'sources/.*\.transcript\.json$' | head -1)"
SLUG_SC="$(git -C "$CLONE/content" show --name-only --format= HEAD | grep -E 'sources/.*\.transcript\.md\.md$' | head -1)"
[[ -n "$SLUG_MD" ]]   && ok "committed transcript.md ($SLUG_MD)"   || bad "no transcript.md committed"
[[ -n "$SLUG_JSON" ]] && ok "committed transcript.json (audit artifact)" || bad "transcript.json NOT committed (provenance hole)"
[[ -n "$SLUG_SC" ]]   && ok "committed sidecar (.md.md)" || bad "no sidecar committed"

# slug shape + video_id + canonical fields in the sidecar
if [[ -n "$SLUG_SC" ]]; then
  grep -qE "video_id: ${VID}\b" "$CLONE/content/$SLUG_SC" && ok "sidecar records normalized video_id" || bad "video_id missing/wrong"
  grep -qE "origin_type: video" "$CLONE/content/$SLUG_SC" && ok "origin_type: video" || bad "origin_type wrong"
  grep -qF "watch?v=${VID}" "$CLONE/content/$SLUG_SC" && ok "canonical_url normalized (dropped &list=)" || bad "canonical_url not normalized"
  grep -qE "transcript_json_sha256: [0-9a-f]{64}" "$CLONE/content/$SLUG_SC" && ok "json hash recorded" || bad "transcript_json_sha256 missing"
  # the committed json matches its recorded hash (drift guard precondition)
  want="$(grep -oE 'transcript_json_sha256: [0-9a-f]{64}' "$CLONE/content/$SLUG_SC" | awk '{print $2}')"
  got="$(shasum -a 256 "$CLONE/content/$SLUG_JSON" | awk '{print $1}')"
  [[ "$want" == "$got" ]] && ok "committed .json matches transcript_json_sha256" || bad "json hash mismatch"
  # enhanced-meta consumption (recorded from the service payload, not hardcoded):
  grep -qE 'align_succeeded: true' "$CLONE/content/$SLUG_SC" && ok "records align_succeeded from meta" || bad "align_succeeded not consumed"
  grep -qE 'duration_s: 14' "$CLONE/content/$SLUG_SC" && ok "records media duration_s from meta" || bad "duration_s not consumed"
  grep -qE 'asr_engine: whisperx@3\.1\.1' "$CLONE/content/$SLUG_SC" && ok "records asr_engine@version from meta" || bad "asr_engine version not consumed"
  grep -qE 'asr_model: .?large-v3' "$CLONE/content/$SLUG_SC" && ok "records asr_model from meta" || bad "asr_model not consumed"
  grep -qE 'channel:.*Stub Channel' "$CLONE/content/$SLUG_SC" && ok "records producer (channel) from info.json meta" || bad "channel not consumed"
  grep -qE 'selected_format:.*251' "$CLONE/content/$SLUG_SC" && ok "records selected_format" || bad "selected_format not consumed"
  grep -qE 'transcript_job_id:.*stubjob123' "$CLONE/content/$SLUG_SC" && ok "records job_id" || bad "job_id not consumed"
  grep -qE 'transcript_server:.*transcript@0\.1\.0' "$CLONE/content/$SLUG_SC" && ok "records transcript_server identity@version" || bad "transcript_server not consumed"
fi

# the LLM page cites the media source_id
sid="$(git -C "$CLONE/content" log -1 --format=%s | sed -nE 's/^ingest: ([0-9A-Z]{26}).*/\1/p')"
PAGE="$CLONE/content/wiki/entities/media-e2e-entity.md"
[[ -n "$sid" && -f "$PAGE" ]] && grep -qF "[src:${sid}]" "$PAGE" && ok "wiki page cites the media source_id" || bad "page missing/citation mismatch"

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
