#!/usr/bin/env bash
# Full-spine e2e for a MEDIA ingest THROUGH ingest.py (deferred §8 item): the other
# media tests drive media-identity.py directly, so they never exercise the real
# orchestrator — preflight, the media front-door routing, the truncated prompt copy
# (TEXT_FILE), build-prompt, the stub LLM diff, the per-kind media-anchor lint gate,
# and the atomic commit that git-adds the canonical asset + .cards.json (AUDIT_JSON) +
# the .assets/ dir + sidecar. Uses the image_note path (extract-remote + card-anchors).
# Stub extract-remote + stub LLM; isolated content/ repo.

set -euo pipefail
PIPELINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ROOT="$(cd "$PIPELINE_ROOT/.." && pwd)"
STUB_LLM="$PIPELINE_ROOT/scripts/tests/stub-llm.py"
STUB_EXTRACT="$PIPELINE_ROOT/scripts/tests/stub-extract-remote"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
rc=0
echo "test_media_ingest_spine (image_note via ingest.py):"

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
mkdir -p "$CROOT/wiki/entities" "$CROOT/wiki/topics" "$CROOT/wiki/_index"
if [[ ! -f "$CROOT/wiki/_taxonomy.md" ]]; then
  cat > "$CROOT/wiki/_taxonomy.md" <<'MD'
# Taxonomy

## Domain
- `biology/cell`

## Form
- `concept`

## Reserved
- `taxonomy-gap`
MD
fi

git -C "$CROOT" init -q
git -C "$CROOT" config user.email e2e@test
git -C "$CROOT" config user.name e2e
git -C "$CROOT" add -A
git -C "$CROOT" commit -qm "e2e baseline"
HEAD_BEFORE="$(git -C "$CROOT" rev-parse HEAD)"

ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }

PAGE="$CROOT/wiki/entities/rednote-note.md"
[[ ! -f "$PAGE" ]] && ok "target page absent before the run (no vacuous pass)" || bad "rednote-note.md pre-exists in baseline — test would false-pass"

echo "  running ingest.py --kind image_note (stub extract-remote + stub LLM)…"
# env -u VAULT_CONTENT_DIR: don't let an exported var point at the real vault.
# STUB_IMAGE_POSTID="" → service returns no post_id, so the CLI --post-id wins with no
# reconciliation mismatch. STUB_CARD_ANCHOR=card-1 → the stub page cites [src:<id>#card-1]
# so the media-anchor gate (check_card_anchors) is exercised NON-vacuously. The bundle path
# is a dummy (the stub ignores its source arg).
if ( cd "$CLONE" && env -u VAULT_CONTENT_DIR \
        LLM_CMD="$STUB_LLM" EXTRACT_REMOTE_CMD="$STUB_EXTRACT" STUB_IMAGE_POSTID="" \
        STUB_ENTITY="rednote-note" STUB_CARD_ANCHOR="card-1" \
        PW_INGEST_SKIP_ARGUMENT_MAP=1 \
        ./pipeline/ingest.py "/tmp/e2e-export.zip" --kind image_note --post-id P1 --platform rednote \
   ) > "$TMP/out" 2>&1; then
  :
else
  echo "  ✗ ingest.py exited non-zero:"; sed 's/^/    | /' "$TMP/out" | tail -40; exit 1
fi

# 1. a commit was created with an `ingest: <ULID>` subject
HEAD_AFTER="$(git -C "$CROOT" rev-parse HEAD)"
[[ "$HEAD_AFTER" != "$HEAD_BEFORE" ]] && ok "a commit was created" || bad "no new commit"
subj="$(git -C "$CROOT" log -1 --format=%s)"
sid="$(printf '%s' "$subj" | sed -nE 's/^ingest: ([0-9A-Z]{26}).*/\1/p')"
[[ -n "$sid" ]] && ok "commit subject is an ingest: $subj" || bad "unexpected commit subject: $subj"

# 2. the canonical .cards.md, the .cards.json audit (AUDIT_JSON), the .assets/ images,
#    and the sidecar are all TRACKED in the new commit. Derive the sidecar from THIS
#    commit's changed files (not `ls | head`, which would pick any pre-existing sidecar).
tracked() { git -C "$CROOT" ls-files --error-unmatch -- "$1" >/dev/null 2>&1; }
rel="$(git -C "$CROOT" diff-tree --no-commit-id --name-only -r "$HEAD_AFTER" | grep -E '\.cards\.md\.md$' | head -1)"
[[ -n "$rel" ]] && ok "image_note sidecar committed by this run" || bad "no .cards.md.md in the new commit"
if [[ -n "$rel" ]]; then
  stem="${rel%.cards.md.md}"
  tracked "$stem.cards.md"   && ok "canonical .cards.md tracked"        || bad ".cards.md not tracked"
  tracked "$stem.cards.json" && ok ".cards.json audit (AUDIT_JSON) tracked" || bad ".cards.json not tracked"
  tracked "$stem.cards.md.md" && ok "sidecar tracked"                  || bad "sidecar not tracked"
  [[ -n "$(git -C "$CROOT" ls-files -- "$stem.cards.md.assets/")" ]] && ok ".cards.md.assets/ images tracked" || bad "assets dir not tracked"
fi

# 3. the stub page was created and cites the run's source_id WITH a #card-1 anchor — so the
#    media-anchor gate (check_card_anchors) actually resolved an anchor (non-vacuous).
[[ -f "$PAGE" ]] && ok "wiki page created" || bad "wiki page missing"
if [[ -f "$PAGE" && -n "$sid" ]]; then
  grep -qF "[src:${sid}#card-1]" "$PAGE" && ok "page cites the media source_id with #card-1 (anchor gate exercised)" || bad "#card-1 citation missing"
  grep -qE '^page_id:\s*[0-9A-Z]{26}\s*$' "$PAGE" && ok "page_id injected" || bad "page_id missing"
fi

# 4. log.md recorded the run
[[ -n "$sid" ]] && tail -5 "$CROOT/.wiki/log.md" | grep -q "$sid" && ok "log.md appended" || bad "log.md not updated"

# 5. working tree clean after the atomic commit (no stray staged/untracked under sources/)
[[ -z "$(git -C "$CROOT" status --porcelain -- sources/)" ]] && ok "sources/ clean after commit (atomic)" || { bad "sources/ dirty after commit"; git -C "$CROOT" status --porcelain -- sources/; }

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
