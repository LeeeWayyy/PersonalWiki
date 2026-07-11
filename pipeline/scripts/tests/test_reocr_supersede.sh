#!/usr/bin/env bash
# e2e for image_note `--reocr` SUPERSEDE (deferred §8 item) through ingest.py:
#   run 1: ingest post P1            → source A, page note1 cites [src:A#card-1]
#   run 2: ingest post P1 --reocr    → source B (supersedes A), page note2 cites B,
#          AND note1's live citation is migrated A→B (rewrite-citations) so nothing is
#          orphaned. A stays in sources/ (immutable); the resolver now picks B as head.
# Stub extract-remote + stub LLM; isolated content/ repo.

set -euo pipefail
PIPELINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ROOT="$(cd "$PIPELINE_ROOT/.." && pwd)"
STUB_LLM="$PIPELINE_ROOT/scripts/tests/stub-llm.py"
STUB_EXTRACT="$PIPELINE_ROOT/scripts/tests/stub-extract-remote"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
rc=0
echo "test_reocr_supersede (image_note --reocr through ingest.py):"

CLONE="$TMP/clone"
rsync -a --exclude='.git' --exclude='.mypy_cache' --exclude='.ruff_cache' \
      --exclude='.obsidian' --exclude='node_modules' --exclude='backend/.venv' \
      --exclude='dist' --exclude='.astro' --exclude='vault' \
      --exclude='public/pagefind' --exclude='public/vault-assets' \
      --exclude='backend/data' --exclude='pipeline/scripts/tests/.e2e-snapshot.*' \
      "$PROJECT_ROOT/" "$CLONE/"
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
      LLM_CMD="$STUB_LLM" EXTRACT_REMOTE_CMD="$STUB_EXTRACT" STUB_IMAGE_POSTID="" \
      STUB_ENTITY="$entity" STUB_CARD_ANCHOR="card-1" \
      ./pipeline/ingest.py "/tmp/e2e-export.zip" --kind image_note --post-id P1 --platform rednote "$@" )
}

# ── run 1: first ingest of post P1 ──
echo "  run 1: ingest post P1 (stub)…"
run_ingest note1 > "$TMP/out1" 2>&1 || { echo "  ✗ run1 failed:"; sed 's/^/    | /' "$TMP/out1" | tail -30; exit 1; }
sidA="$(git -C "$CROOT" log -1 --format=%s | sed -nE 's/^ingest: ([0-9A-Z]{26}).*/\1/p')"
[[ -n "$sidA" ]] && ok "run1 committed source A=$sidA" || { bad "run1 no ingest commit"; exit 1; }
grep -qF "[src:${sidA}#card-1]" "$CROOT/wiki/entities/note1.md" && ok "note1 cites A#card-1" || bad "note1 missing A citation"

# ── run 2: --reocr → supersede A with B ──
echo "  run 2: ingest post P1 --reocr (stub)…"
STUB_TEXT_VARIANT=reocr run_ingest note2 --reocr > "$TMP/out2" 2>&1 || { echo "  ✗ run2 (--reocr) failed:"; sed 's/^/    | /' "$TMP/out2" | tail -30; exit 1; }
sidB="$(git -C "$CROOT" log -1 --format=%s | sed -nE 's/^ingest: ([0-9A-Z]{26}).*/\1/p')"
[[ -n "$sidB" && "$sidB" != "$sidA" ]] && ok "run2 committed a NEW source B=$sidB" || bad "run2 did not mint a new source"

# B's sidecar supersedes A
scB="$(git -C "$CROOT" diff-tree --no-commit-id --name-only -r HEAD | grep -E '\.cards\.md\.md$' | head -1)"
[[ -n "$scB" ]] && grep -qF "supersedes: '[[${sidA}]]'" "$CROOT/$scB" && ok "B sidecar supersedes A" || { bad "B does not supersede A"; grep -n supersedes "$CROOT/$scB" 2>/dev/null; }

# note1's citation was MIGRATED A→B (nothing orphaned), and note2 cites B
grep -qF "[src:${sidB}#card-1]" "$CROOT/wiki/entities/note1.md" && ok "note1 citation migrated A→B" || bad "note1 still cites A (not migrated)"
! grep -qF "$sidA" "$CROOT/wiki/entities/note1.md" && ok "no stale A citation remains in note1" || bad "stale A citation remains"
grep -qF "[src:${sidB}#card-1]" "$CROOT/wiki/entities/note2.md" && ok "note2 cites B#card-1" || bad "note2 missing B citation"

# A stays committed (immutable) AND B was added → exactly 2 card sidecars; A's id still present
[[ "$(git -C "$CROOT" ls-files -- 'sources/*.cards.md.md' | wc -l | tr -d ' ')" == "2" ]] && ok "both A and B sidecars present (A immutable, B supersedes)" || bad "expected 2 card sidecars (A + B)"
git -C "$CROOT" grep -qF "source_id: $sidA" -- 'sources/*.cards.md.md' && ok "A's source artifact still committed" || bad "A's sidecar vanished"

# log records the supersede
tail -3 "$CROOT/.wiki/log.md" | grep -qF "supersedes ${sidA}" && ok "log records the supersede" || bad "log missing supersede note"

# the whole tree is lint-clean (resolver picks B; no orphaned/ambiguous citations)
( cd "$CROOT" && VAULT_CONTENT_DIR="$CROOT" "$PIPELINE_ROOT/scripts/lint.py" >/dev/null 2>&1 ) && ok "full lint clean after supersede" || { bad "lint not clean after supersede"; ( cd "$CROOT" && VAULT_CONTENT_DIR="$CROOT" "$PIPELINE_ROOT/scripts/lint.py" 2>&1 | grep '✗' | head ); }

# ── --reocr on a NEW post (DIFFERENT images → different bundle, so the cross-basis scan
#    doesn't fire) with no prior head → must DIE loud (no silent fresh mint) ──
if ( cd "$CLONE" && env -u VAULT_CONTENT_DIR \
        LLM_CMD="$STUB_LLM" EXTRACT_REMOTE_CMD="$STUB_EXTRACT" STUB_IMAGE_POSTID="" \
        STUB_IMAGE_CARDS=3 STUB_ENTITY="noteX" STUB_CARD_ANCHOR="card-1" \
        ./pipeline/ingest.py "/tmp/e2e-export.zip" --kind image_note --post-id PNEW --platform rednote --reocr \
   ) > "$TMP/out3" 2>&1; then
  bad "--reocr with no prior source should die, not mint fresh"
else
  grep -q "nothing to supersede" "$TMP/out3" && ok "--reocr with no prior head → dies loud" || { bad "--reocr no-head died with the wrong message"; tail -3 "$TMP/out3"; }
fi

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
