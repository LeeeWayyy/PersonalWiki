#!/usr/bin/env bash
# e2e for YouTube `--retranscribe` SUPERSEDE (deferred §8 item) through ingest.py:
#   run 1: ingest <url>                → source A, page note1 cites [src:A]
#   run 2: ingest <url> --retranscribe → source B (supersedes A), page note2 cites B,
#          AND note1's live citation is migrated A→B. A stays immutable; resolver picks B.
# The no-retranscribe path stays byte-identical (covered by test_media_e2e). Stub
# transcript-remote + stub LLM; isolated content/ repo.

set -euo pipefail
PIPELINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ROOT="$(cd "$PIPELINE_ROOT/.." && pwd)"
STUB_LLM="$PIPELINE_ROOT/scripts/tests/stub-llm.py"
STUB_TR="$PIPELINE_ROOT/scripts/tests/stub-transcript-remote"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
rc=0
URL="https://www.youtube.com/watch?v=RETRANSvid1"
echo "test_retranscribe_supersede (youtube --retranscribe through ingest.py):"

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
      LLM_CMD="$STUB_LLM" TRANSCRIPT_REMOTE_CMD="$STUB_TR" STUB_ENTITY="$entity" \
      PW_INGEST_SKIP_ARGUMENT_MAP=1 \
      ./pipeline/ingest.py "$URL" --kind video "$@" )
}

# ── run 1: first transcript ingest ──
echo "  run 1: ingest $URL …"
run_ingest note1 > "$TMP/out1" 2>&1 || { echo "  ✗ run1 failed:"; sed 's/^/    | /' "$TMP/out1" | tail -30; exit 1; }
sidA="$(git -C "$CROOT" log -1 --format=%s | sed -nE 's/^ingest: ([0-9A-Z]{26}).*/\1/p')"
[[ -n "$sidA" ]] && ok "run1 committed source A=$sidA" || { bad "run1 no ingest commit"; exit 1; }
grep -qF "[src:${sidA}]" "$CROOT/wiki/entities/note1.md" && ok "note1 cites A" || bad "note1 missing A citation"

# ── run 2: --retranscribe → supersede A with B ──
echo "  run 2: ingest $URL --retranscribe …"
STUB_TEXT_VARIANT=retranscribed run_ingest note2 --retranscribe > "$TMP/out2" 2>&1 || { echo "  ✗ run2 (--retranscribe) failed:"; sed 's/^/    | /' "$TMP/out2" | tail -30; exit 1; }
sidB="$(git -C "$CROOT" log -1 --format=%s | sed -nE 's/^ingest: ([0-9A-Z]{26}).*/\1/p')"
[[ -n "$sidB" && "$sidB" != "$sidA" ]] && ok "run2 minted a NEW source B=$sidB" || bad "run2 did not mint a new source"

scB="$(git -C "$CROOT" diff-tree --no-commit-id --name-only -r HEAD | grep -E '\.transcript\.md\.md$' | head -1)"
[[ -n "$scB" ]] && grep -qF "supersedes: '[[${sidA}]]'" "$CROOT/$scB" && ok "B sidecar supersedes A" || { bad "B does not supersede A"; grep -n supersedes "$CROOT/$scB" 2>/dev/null; }

grep -qF "[src:${sidB}]" "$CROOT/wiki/entities/note1.md" && ok "note1 citation migrated A→B" || bad "note1 not migrated"
! grep -qF "$sidA" "$CROOT/wiki/entities/note1.md" && ok "no stale A citation remains in note1" || bad "stale A citation remains"
grep -qF "[src:${sidB}]" "$CROOT/wiki/entities/note2.md" && ok "note2 cites B" || bad "note2 missing B citation"

[[ "$(git -C "$CROOT" ls-files -- 'sources/*.transcript.md.md' | wc -l | tr -d ' ')" == "2" ]] && ok "both A and B transcript sidecars present (A immutable)" || bad "expected 2 transcript sidecars"
git -C "$CROOT" grep -qF "source_id: $sidA" -- 'sources/*.transcript.md.md' && ok "A's source artifact still committed" || bad "A's sidecar vanished"
tail -3 "$CROOT/.wiki/log.md" | grep -qF "supersedes ${sidA}" && ok "log records the supersede" || bad "log missing supersede note"
( cd "$CROOT" && VAULT_CONTENT_DIR="$CROOT" "$PIPELINE_ROOT/scripts/lint.py" >/dev/null 2>&1 ) && ok "full lint clean after supersede" || { bad "lint not clean"; ( cd "$CROOT" && VAULT_CONTENT_DIR="$CROOT" "$PIPELINE_ROOT/scripts/lint.py" 2>&1 | grep '✗' | head ); }

# ── --retranscribe on a NEW video with no prior head → must DIE ──
if ( cd "$CLONE" && env -u VAULT_CONTENT_DIR LLM_CMD="$STUB_LLM" TRANSCRIPT_REMOTE_CMD="$STUB_TR" \
        STUB_ENTITY="noteY" PW_INGEST_SKIP_ARGUMENT_MAP=1 \
        ./pipeline/ingest.py "https://www.youtube.com/watch?v=NOPRIORvid9" --kind video --retranscribe \
   ) > "$TMP/out4" 2>&1; then
  bad "--retranscribe with no prior source should die, not mint fresh"
else
  grep -q "nothing to supersede" "$TMP/out4" && ok "--retranscribe with no prior head → dies loud" || { bad "no-head --retranscribe wrong message"; tail -3 "$TMP/out4"; }
fi

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
