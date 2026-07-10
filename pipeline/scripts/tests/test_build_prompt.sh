#!/usr/bin/env bash
# Regression oracle for prompt assembly.
#
# Holds an independent "golden" reimplementation of build_prompt +
# build_candidate_blob and diffs its output, byte-for-byte, against
# scripts/build-prompt.py on identical inputs. No LLM involved. Run from the
# tooling root:
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

# ── golden: independent copy of build_candidate_blob + build_prompt ─────────
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

rstrip_blank_lines() {
  awk '{ lines[++n] = $0 } END { while (n > 0 && lines[n] == "") n--; for (i = 1; i <= n; i++) print lines[i] }'
}

schema_preamble() {
  awk '/^## / { exit } { print }' "$TOOLING_ROOT/prompts/schema-ingest.md" | rstrip_blank_lines
}

schema_section() {
  local heading="$1"
  awk -v want="## $heading" '
    /^## / { emit = ($0 == want) }
    emit { print }
  ' "$TOOLING_ROOT/prompts/schema-ingest.md" | rstrip_blank_lines
}

image_block_has_rows() {
  local image_block="$1"
  local first=""
  [[ -s "$image_block" ]] || return 1
  IFS= read -r first < "$image_block" || return 1
  [[ "$first" != \(* ]]
}

build_schema() {
  local operation="$1" expand_list="$2" image_block="$3"
  local sections=(
    "Page Selection And Coverage"
    "Page Types"
    "Frontmatter"
    "Tags"
    "Zones"
    "Citations"
    "Voice And Attribution"
    "Language And Naming"
    "Prose Shape"
  )
  if [[ -s "$CANDIDATES_FILE" ]]; then
    sections+=("Candidate Pages")
    if [[ -s "$expand_list" ]]; then
      sections+=("Expanded Candidate Editing")
    else
      sections+=("Candidate Digests And Expansion")
    fi
    sections+=("Multi-Source Synthesis" "Candidate Updates And Conflicts")
  fi
  if image_block_has_rows "$image_block"; then
    sections+=("Images")
  fi
  if [[ "$operation" == "retry" ]]; then
    sections+=("Patch Retry")
  fi

  schema_preamble
  printf '\n'
  local first=1 section
  for section in "${sections[@]}"; do
    if [[ "$first" -eq 0 ]]; then
      printf '\n'
    fi
    schema_section "$section"
    first=0
  done
}

build_prompt() {
  local expand_list="$1"
  local out="$2"
  local operation="$3"
  local image_block="$TMP/image-block"
  if [[ -f "${DEST}.assets/_manifest.md" ]]; then
    "$TOOLING_ROOT"/scripts/render-images-block.py "${DEST}.assets/_manifest.md" "$DEST" \
      > "$image_block" 2>/dev/null \
      || printf '(images-block render failed; LLM proceeds without image table)\n' > "$image_block"
  else
    printf '(no images extracted from this source)\n' > "$image_block"
  fi
  {
    cat "$TOOLING_ROOT/prompts/ingest.md"
    printf '\n\n---\n\n## SCHEMA\n'
    build_schema "$operation" "$expand_list" "$image_block"
    printf '\n\n---\n\n## ALL_SOURCE_IDS\n%s\n' "$ALL_SOURCE_IDS"
    printf '\n---\n\n## TAXONOMY\n'
    cat wiki/_taxonomy.md
    printf '\n\n---\n\n## SOURCE_META\n'
    printf 'source_id: %s\nsha256: %s\nadded: %s\norigin_type: %s\norigin_ref: %s\nbasename: %s\n' \
      "$SOURCE_ID" "$SHA256" "$ADDED" "$ORIGIN_TYPE" "$ORIGIN_REF" "$DEST_BASENAME"
    printf '\n## SOURCE_KEY_TERMS\n'
    if [[ -s "$SOURCE_TERMS_FILE" ]]; then
      terms="$(cat "$SOURCE_TERMS_FILE")"
      printf '%s' "$terms"
    else
      printf '(not available)'
    fi
    printf '\n'
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
    printf '\n---\n\n## IMAGES\n'
    cat "$image_block"
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
SOURCE_TERMS_FILE="$TMP/source-terms"; export SOURCE_TERMS_FILE
printf 'ATP\n线粒体\n化学渗透偶联\n' > "$SOURCE_TERMS_FILE"
CANDIDATES_FILE="$TMP/cands"; export CANDIDATES_FILE
printf 'wiki/entities/mitochondria.md\nwiki/topics/eukaryotes.md\n' > "$CANDIDATES_FILE"
export DEST="sources/test.epub"

run_case() {
  local name="$1" expand_file="$2" section="$3" operation="$4"
  SECTION_LABEL="$section"; export SECTION_LABEL
  build_prompt "$expand_file" "$TMP/golden" "$operation"
  "$TOOLING_ROOT"/scripts/build-prompt.py \
    --source-id "$SOURCE_ID" --sha256 "$SHA256" --added "$ADDED" \
    --origin-type "$ORIGIN_TYPE" --origin-ref "$ORIGIN_REF" --basename "$DEST_BASENAME" \
    --section-label "$SECTION_LABEL" --all-source-ids "$ALL_SOURCE_IDS" \
    --source-terms-file "$SOURCE_TERMS_FILE" \
    --text-file "$TEXT_FILE" --candidates-file "$CANDIDATES_FILE" \
    --expand-file "$expand_file" --dest "$DEST" \
    --operation "$operation" > "$TMP/py"
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
run_case "digest mode, no section-label" "$EMPTY" "" "digest" || rc=1
run_case "digest mode, with section-label" "$EMPTY" "第一章" "digest" || rc=1
run_case "expand mode (1 file)" "$EXPAND" "第一章" "expand" || rc=1

mkdir -p "${DEST}.assets"
cat > "${DEST}.assets/_manifest.md" <<'MD'
---
schema_version: 1
source_id: SRC1
images:
  - file: fig1.png
    sha256: 0123456789abcdef
    bytes: 12345
    dimensions: [640, 480]
    origin_refs: []
    decorative: false
    caption: Test figure caption
    caption_source: vision
    caption_model: test
    caption_at: 2026-05-28T00:00:00Z
    caption_error: null
    caption_error_kind: null
---
MD
run_case "retry mode with image table" "$EXPAND" "第一章" "retry" || rc=1
[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
