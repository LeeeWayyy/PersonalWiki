#!/usr/bin/env bash
# End-to-end ingest smoke/regression test for the Python ingest orchestrator and
# the tooling/content split. Ingest runs with cwd=content/ and commits inside
# that repo.
#
# Runs the FULL ingest loop with a deterministic stub LLM (no live model)
# inside an isolated copy of the repo, and asserts the outcome.
#
# Parametrized by entrypoint, defaulting to ingest.py:
#   scripts/tests/test_ingest_e2e.sh
#   scripts/tests/test_ingest_e2e.sh ingest.py
#
# Run from the project root or directly; paths are resolved from this file.

set -euo pipefail
PIPELINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ROOT="$(cd "$PIPELINE_ROOT/.." && pwd)"
ENTRY="${1:-ingest.py}"
if [[ "$ENTRY" == */* ]]; then
  ENTRY_PATH="$ENTRY"
else
  ENTRY_PATH="pipeline/$ENTRY"
fi
STUB="$PIPELINE_ROOT/scripts/tests/stub-llm.py"
BRANCH="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
rc=0
echo "test_ingest_e2e ($ENTRY, branch=$BRANCH):"

CLONE="$TMP/clone"
# Copy a full working tree (tooling + content/) into an isolated CLONE.
# Exclude ALL .git (the top repo isn't used by ingest, and content/.git is
# a submodule gitdir-pointer that would dangle here) — we re-init content/
# as a fresh standalone repo below. Skip heavy regenerable caches + the
# Obsidian dir (gitignored content anyway).
rsync -a --exclude='.git' --exclude='.mypy_cache' --exclude='.ruff_cache' \
      --exclude='.obsidian' --exclude='node_modules' --exclude='backend/.venv' \
      --exclude='dist' --exclude='.astro' --exclude='vault' \
      --exclude='public/pagefind' --exclude='public/vault-assets' \
      --exclude='backend/data' --exclude='pipeline/scripts/tests/.e2e-snapshot.*' \
      "$PROJECT_ROOT/" "$CLONE/"

mkdir -p "$CLONE/content/wiki/entities" "$CLONE/content/wiki/topics" "$CLONE/content/wiki/_index"
if [[ ! -f "$CLONE/content/wiki/_taxonomy.md" ]]; then
  cat > "$CLONE/content/wiki/_taxonomy.md" <<'MD'
# Taxonomy

## Domain
- `biology/cell`

## Form
- `concept`

## Reserved
- `taxonomy-gap`
MD
fi

# ingest stages + commits inside the content repo, so content/ must be a
# git repo with a clean baseline. (No submodule wiring needed — ingest only
# ever operates on content/ directly; the superproject is irrelevant here.)
git -C "$CLONE/content" init -q
git -C "$CLONE/content" config user.email e2e@test
git -C "$CLONE/content" config user.name e2e
git -C "$CLONE/content" add -A
git -C "$CLONE/content" commit -qm "e2e baseline"

SRC="$TMP/e2e-source.txt"
printf 'End-to-end ingest smoke-test source.\nDiscusses mitochondria and ATP.\n' > "$SRC"

HEAD_BEFORE="$(git -C "$CLONE/content" rev-parse HEAD)"
echo "  running $ENTRY (stub LLM)…"
# env -u VAULT_CONTENT_DIR is a SAFETY GUARD: if the caller's environment had
# VAULT_CONTENT_DIR set (e.g. exported by a real ingest), it would point the
# entrypoint at the *real* vault. Force the default ($CLONE/content).
if ( cd "$CLONE" && env -u VAULT_CONTENT_DIR LLM_CMD="$STUB" "./$ENTRY_PATH" "$SRC" ) > "$TMP/out" 2>&1; then
  :
else
  echo "  ✗ $ENTRY exited non-zero:"; sed 's/^/    | /' "$TMP/out" | tail -30; exit 1
fi

ok() { echo "  ✓ $1"; }
bad() { echo "  ✗ $1"; rc=1; }

CROOT="$CLONE/content"

# 1. A new commit was created in the content repo.
HEAD_AFTER="$(git -C "$CROOT" rev-parse HEAD)"
[[ "$HEAD_AFTER" != "$HEAD_BEFORE" ]] && ok "a commit was created" || bad "no new commit"

# 2. Commit subject is `ingest: <ULID>…`.
subj="$(git -C "$CROOT" log -1 --format=%s --grep='^ingest: ')"
sid="$(printf '%s' "$subj" | sed -nE 's/^ingest: ([0-9A-Z]{26}).*/\1/p')"
[[ -n "$sid" ]] && ok "commit subject is an ingest: $subj" || bad "unexpected commit subject: $subj"

# 2b. Whole-source ingest also committed its derived argument map.
map_subj="$(git -C "$CROOT" log -1 --format=%s)"
[[ "$map_subj" == "mindmap: $sid" ]] && ok "argument map committed" || bad "missing argument-map commit: $map_subj"

# 3. The stub's new entity page exists, with an injected page_id.
PAGE="$CROOT/wiki/entities/e2e-entity.md"
[[ -f "$PAGE" ]] && ok "new entity page created" || bad "new entity page missing"
if [[ -f "$PAGE" ]]; then
  grep -qE '^page_id:\s*[0-9A-Z]{26}\s*$' "$PAGE" && ok "page_id injected (add-page-id ran)" || bad "page_id missing"
  grep -qF "[src:${sid}]" "$PAGE" && ok "body cites the run's source_id" || bad "citation missing/mismatched"
  grep -qE "^sources:\s*\[${sid}\]" "$PAGE" && ok "frontmatter sources: synced" || bad "sources: not synced"
fi

# 4. log.md recorded the run under this source_id.
if [[ -n "$sid" ]] && tail -5 "$CROOT/.wiki/log.md" | grep -q "$sid"; then
  ok "log.md appended for the source"
else
  bad "log.md not updated for the source"
fi

# 5. Snapshot the GENERATED files (the real deliverable), with volatile ids and
#    timestamps normalized so rerunning the test does not churn the tracked file.
{
  echo "=== wiki/entities/e2e-entity.md ==="
  cat "$PAGE" 2>/dev/null || echo "(missing)"
  echo "=== .wiki/log.md (last line) ==="
  tail -1 "$CROOT/.wiki/log.md"
} | sed -E \
  -e "s/${sid:-__NOSID__}/<SOURCE_ID>/g" \
  -e 's/^page_id: [0-9A-Z]{26}$/page_id: <PAGE_ID>/' \
  -e 's/^last_ingested: [0-9]{4}-[0-9]{2}-[0-9]{2}$/last_ingested: <DATE>/' \
  -e 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+Z  <SOURCE_ID>/<ADDED>  <SOURCE_ID>/' \
  -e 's/[[:space:]]+$//' \
  > "$TMP/snapshot"
SNAP_OUT="$PIPELINE_ROOT/scripts/tests/.e2e-snapshot.${ENTRY//\//_}.txt"
cp "$TMP/snapshot" "$SNAP_OUT"
ok "snapshot written: ${SNAP_OUT#"$PIPELINE_ROOT"/} ($(wc -l < "$TMP/snapshot" | tr -d ' ') lines)"

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
