#!/usr/bin/env bash
# Regression oracle for the §14 build_prompt extraction.
#
# Holds a frozen copy of the ORIGINAL (pre-port) bash build_prompt +
# build_candidate_blob — an independent "golden" reimplementation — and diffs
# its output, byte-for-byte, against scripts/build-prompt.py on identical
# inputs, in both digest and expand modes. No LLM involved. Run from the tooling
# root:
#   scripts/tests/test_build_prompt.sh
#
# When build-prompt.py's prompt assembly legitimately changes, update the copy
# below to match, then re-run.

set -euo pipefail
# Layout: prompts/ + scripts/ live in the tooling root. The test creates a
# throwaway content repo and runs from it, mirroring how ingest invokes
# build-prompt.py with cwd = content/.
TOOLING_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
VAULT_ROOT="$TMP/content"
export VAULT_CONTENT_DIR="$VAULT_ROOT"
mkdir -p "$VAULT_ROOT/wiki/entities" "$VAULT_ROOT/wiki/topics" "$VAULT_ROOT/sources"
cat > "$VAULT_ROOT/wiki/_taxonomy.md" <<'MD'
# Taxonomy

## Domain
- `biology/cell`

## Form
- `concept`

## Reserved
- `taxonomy-gap`
MD
cat > "$VAULT_ROOT/wiki/entities/mitochondria.md" <<'MD'
---
type: Entity
aliases: [Mitochondria]
tags: [concept, biology/cell]
---
# Mitochondria

Existing note about mitochondria.
MD
cat > "$VAULT_ROOT/wiki/topics/eukaryotes.md" <<'MD'
---
type: Topic
aliases: [Eukaryotes]
tags: [concept, biology/cell]
---
# Eukaryotes

Existing topic note about cells.
MD
cd "$VAULT_ROOT"

# ── golden: verbatim copy of ingest.sh build_candidate_blob + build_prompt ──
build_candidate_blob() {
  local expand_list="$1"
  local p
  if [[ ! -s "$CANDIDATES_FILE" ]]; then
    return 0
  fi
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    printf '\n### %s\n```markdown\n' "$p"
    if [[ -s "$expand_list" ]] && grep -qFx "$p" "$expand_list"; then
      cat "$p"
    else
      "$TOOLING_ROOT"/scripts/page-digest.py "$p" 2>/dev/null || cat "$p"
    fi
    printf '\n```\n'
  done < "$CANDIDATES_FILE"
}

build_prompt() {
  local expand_list="$1"
  local out="$2"
  {
    cat "$TOOLING_ROOT/prompts/ingest.md"
    printf '\n\n---\n\n## SOURCE_META\n'
    printf 'source_id: %s\nsha256: %s\nadded: %s\norigin_type: %s\norigin_ref: %s\nbasename: %s\n' \
      "$SOURCE_ID" "$SHA256" "$ADDED" "$ORIGIN_TYPE" "$ORIGIN_REF" "$DEST_BASENAME"
    printf '\n## SECTION_LABEL\n%s\n' "${SECTION_LABEL:-<none — cite as bare [src:$SOURCE_ID]>}"
    printf '\n## SOURCE_TEXT\n'
    cat "$TEXT_FILE"
    printf '\n\n---\n\n## CANDIDATE_PAGES'
    if [[ -s "$expand_list" ]]; then
      printf ' (expanded: %d file(s) shown in full; rest are digests)' "$(wc -l < "$expand_list" | tr -d ' ')"
    else
      printf ' (digests only — emit expand action if you need full content)'
    fi
    printf '\n'
    build_candidate_blob "$expand_list"
    printf '\n---\n\n## ALL_SOURCE_IDS\n%s\n' "$ALL_SOURCE_IDS"
    printf '\n---\n\n## TAXONOMY\n'
    cat wiki/_taxonomy.md
    printf '\n---\n\n## IMAGES\n'
    if [[ -f "${DEST}.assets/_manifest.md" ]]; then
      "$TOOLING_ROOT"/scripts/render-images-block.py "${DEST}.assets/_manifest.md" "$DEST" \
        2>/dev/null || printf '(images-block render failed; LLM proceeds without image table)\n'
    else
      printf '(no images extracted from this source)\n'
    fi
    if [[ -s "$expand_list" ]]; then
      printf '\n---\n\nNow emit the unified diff. Reminder: only modify\n'
      printf 'candidates shown in full or candidates whose digest has\n'
      printf 'no elision marker; leave still-truncated candidates unchanged.\n'
    else
      printf '\n---\n\nNow emit the unified diff, OR a single JSON line\n'
      printf 'requesting expansion if you need full content for any candidate:\n'
      printf '  {"action":"expand","files":["wiki/entities/X.md", ...]}\n'
      printf 'Expansion is allowed at most once per ingest.\n'
    fi
  } > "$out"
}

# ── fixture ──────────────────────────────────────────────────────────────────
export SOURCE_ID="01TESTSOURCEID0000000000AB"
export SHA256="deadbeef$(printf '%056d' 0)"
export ADDED="2026-05-28T00:00:00Z"
export ORIGIN_TYPE="file"
export ORIGIN_REF="sources/test.epub"
export DEST_BASENAME="2026-05-28-test.epub"
export ALL_SOURCE_IDS=$'01KQD4EYT6AR0DE208D70TCWCQ\n01TESTSOURCEID0000000000AB'
TEXT_FILE="$TMP/text.md"; export TEXT_FILE
printf '## 第一章\n这是一段用于测试 build-prompt 的源文本，含 ATP 与线粒体。\n' > "$TEXT_FILE"
CANDIDATES_FILE="$TMP/cands"; export CANDIDATES_FILE
printf 'wiki/entities/mitochondria.md\nwiki/topics/eukaryotes.md\n' > "$CANDIDATES_FILE"
export DEST="sources/test.epub"

run_case() {
  local name="$1" expand_file="$2" section="$3"
  SECTION_LABEL="$section"; export SECTION_LABEL
  build_prompt "$expand_file" "$TMP/golden"
  "$TOOLING_ROOT"/scripts/build-prompt.py \
    --source-id "$SOURCE_ID" --sha256 "$SHA256" --added "$ADDED" \
    --origin-type "$ORIGIN_TYPE" --origin-ref "$ORIGIN_REF" --basename "$DEST_BASENAME" \
    --section-label "$SECTION_LABEL" --all-source-ids "$ALL_SOURCE_IDS" \
    --text-file "$TEXT_FILE" --candidates-file "$CANDIDATES_FILE" \
    --expand-file "$expand_file" --dest "$DEST" > "$TMP/py"
  if diff -u "$TMP/golden" "$TMP/py" > "$TMP/diff"; then
    echo "  ✓ $name: byte-identical ($(wc -c < "$TMP/golden" | tr -d ' ') bytes)"
  else
    echo "  ✗ $name: DIFFERS"; sed -n '1,40p' "$TMP/diff"; return 1
  fi
}

EMPTY="$TMP/empty"; : > "$EMPTY"
EXPAND="$TMP/expand"; printf 'wiki/entities/mitochondria.md\n' > "$EXPAND"

rc=0
echo "test_build_prompt:"
run_case "digest mode, no section-label" "$EMPTY" "" || rc=1
run_case "digest mode, with section-label" "$EMPTY" "第一章" || rc=1
run_case "expand mode (1 file)" "$EXPAND" "第一章" || rc=1
[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
