#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""
Deterministic stub for $LLM_CMD, used by scripts/tests/test_ingest_e2e.sh.

Reads the prompt on stdin and emits a canned response so the full ingest
pipeline can be exercised end-to-end with NO live LLM:

  - Chapter analyzer prompt  → validated chapter-intelligence JSON.
  - Main ingest prompt       → a unified diff CREATING a new entity page
                               that cites the run's real source_id (parsed
                               out of the prompt's SOURCE_META block), so
                               the citation resolves and all gates pass.

The new page's name is overridable via $STUB_ENTITY (default e2e-entity)
so a test can ingest twice without colliding.
"""

import json
import difflib
import os
import re
import sys

prompt = sys.stdin.read()
entity = os.environ.get("STUB_ENTITY", "e2e-entity")
# Optional media anchor appended to the citation (e.g. STUB_CARD_ANCHOR=card-1), so a
# media-spine test exercises the card/frame/timestamp gate NON-vacuously. Unset (default)
# = a bare [src:<id>] citation, which is what the document-path e2e expects.
anchor = os.environ.get("STUB_CARD_ANCHOR", "")
requested_taxonomy_tag = os.environ.get("STUB_TAXONOMY_TAG", "")


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "section"


def _json_string_field(name: str) -> str:
    match = re.search(rf'"{re.escape(name)}"\s*:\s*("(?:\\.|[^"\\])*")', prompt)
    return json.loads(match.group(1)) if match else ""


section_label = _json_string_field("section_label")
if os.environ.get("STUB_ENTITY_FROM_SECTION") and section_label:
    entity = f"{os.environ.get('STUB_ENTITY_PREFIX', '')}{_slug(section_label)}"


if "You are a chapter-intelligence analyzer" in prompt:
    text_match = re.search(
        r"<EXTRACTED_SOURCE_TEXT>\n(.*?)\n</EXTRACTED_SOURCE_TEXT>",
        prompt,
        re.DOTALL,
    )
    source_text = text_match.group(1) if text_match else ""
    quote = source_text[: min(80, len(source_text))]
    artifact = {
        "schema": "chapter-intelligence/1",
        "source_id": _json_string_field("source_id"),
        "source_sha256": _json_string_field("source_sha256"),
        "text_sha256": _json_string_field("text_sha256"),
        "section_label": _json_string_field("section_label"),
        "prompt_version": _json_string_field("prompt_version"),
        "language": "en",
        "summary": f"Structured test analysis for {entity}.",
        "central_question": f"Why does {entity} matter?",
        "chapter_claim": f"{entity} is a reusable concept in this source.",
        "builds_on": None,
        "claims": [{
            "id": "c1",
            "kind": "claim",
            "text": f"{entity} is a reusable concept in this source.",
            "importance": 5,
            "source_spans": ([{"start": 0, "end": len(quote), "quote": quote}]
                             if quote else []),
            "entities": [entity],
        }],
        "entities": [{
            "name": entity,
            "type": "concept",
            "aliases": [],
            "importance": 5,
            "role": "Primary concept used by the deterministic ingest fixture.",
            "page_hint": "entity",
            "claim_ids": ["c1"],
        }],
        "topics": [],
        "relations": [],
        "page_candidates": [{
            "page_type": "entity",
            "name": entity,
            "importance": 5,
            "required": True,
            "claim_ids": ["c1"],
            "reason": "Reusable fixture concept.",
        }],
        "claim_coverage": [{
            "claim_id": "c1",
            "page_candidates": [{"page_type": "entity", "name": entity}],
            "skip_reason": None,
        }],
        "open_questions": [],
    }
    print(json.dumps(artifact, ensure_ascii=False))
    sys.exit(0)

# Language-profile prompt (generate-language-pages.py): "## SENTENCES" and
# "## WORDS" blocks. Emit the JSON the generator expects: every sentence
# translated, every listed word glossed, plus one deterministic grammar point.
if "## SENTENCES" in prompt and "## WORDS" in prompt:
    fail_lang_chapter = os.environ.get("STUB_FAIL_LANG_CHAPTER", "")
    if fail_lang_chapter and fail_lang_chapter in prompt:
        print(f"stub language failure for {fail_lang_chapter}", file=sys.stderr)
        sys.exit(9)
    sent_block = prompt.split("## SENTENCES", 1)[1].split("## WORDS", 1)[0]
    word_block = prompt.split("## WORDS", 1)[1]
    sentences = re.findall(r"^(\d+)\. (.+)$", sent_block, re.MULTILINE)
    # Numbered word list: `<i>. <lemma>（<reading>）[<pos>]`. Echo each index
    # with a deterministic gloss keyed to the lemma (so tests can assert text).
    words = re.findall(r"^(\d+)\. (.+?)（", word_block, re.MULTILINE)
    obj = {
        "sentences": [{"s": int(i), "en": f"translation of {jp}"}
                      for i, jp in sentences],
        "words": [{"i": int(i), "meaning_en": f"gloss of {lemma}", "notes": ""}
                  for i, lemma in words],
        "grammar_points": [
            {"pattern": "〜です", "explanation": "Polite copula.",
             "example_jp": "本です。", "s": 1 if sentences else 0},
        ],
    }
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)

if os.environ.get("STUB_ENTITY_FROM_SECTION") and not section_label:
    m = re.search(r"^## SECTION_LABEL\n([^\n]*)$", prompt, re.MULTILINE)
    if m:
        section_label = m.group(1).strip()
        entity = f"{os.environ.get('STUB_ENTITY_PREFIX', '')}{_slug(section_label)}"

# $STUB_NO_CHANGES=1 forces the main call to return NO_CHANGES (the LLM had nothing to add) —
# the expected outcome of an --add-frames supersede, whose transcript is byte-identical to the
# predecessor. Lets a test exercise the no-changes + citation-migration path.
if os.environ.get("STUB_NO_CHANGES"):
    print("NO_CHANGES: stub forced no-op (content already absorbed)", flush=True)
    sys.exit(0)

# Main call: extract the source_id this run assigned (SOURCE_META block).
m = re.search(r"^source_id:\s*([0-9A-Z]{26})\s*$", prompt, re.MULTILINE)
if not m:
    print("NO_CHANGES: stub could not find source_id in prompt", flush=True)
    sys.exit(0)
sid = m.group(1)
taxonomy_match = re.search(
    r"^## TAXONOMY\n(.*?)(?=\n+---\n+## SOURCE_META$)",
    prompt,
    re.MULTILINE | re.DOTALL,
)
if not taxonomy_match:
    raise SystemExit("stub-llm: prompt is missing the TAXONOMY block")
taxonomy = taxonomy_match.group(1)
domain_match = re.search(
    r"^## Domain\n- `([^`]+)`",
    taxonomy,
    re.MULTILINE,
)
domain_tag = requested_taxonomy_tag or (domain_match.group(1) if domain_match else "general/knowledge")
section_citation_match = re.search(
    r"^## SECTION_CITATION\n(\[src:[^\n]+\])$", prompt, re.MULTILINE
)
if anchor:
    citation = f"[src:{sid}#{anchor}]"
elif section_citation_match:
    citation = section_citation_match.group(1)
else:
    citation = f"[src:{sid}]"

body = [
    "---",
    "type: Entity",
    f"aliases: [{entity}]",
    f"tags: [{domain_tag}, concept]",
    f"sources: [{sid}]",
    "last_ingested: 2026-01-01",
    "---",
    f"# {entity}",
    "",
    "<!-- human-zone -->",
    "<!-- /human-zone -->",
    "",
    "<!-- llm-zone -->",
    "> [!AI] LLM Synthesis",
    ">",
    f"> {entity} is a reusable concept in the end-to-end fixture. It carries enough context to exercise the human-readable prose gate {citation}.",
    ">",
    f"> The second paragraph explains why {entity} belongs in the wiki graph. It also verifies paragraph-level provenance {citation}.",
    "<!-- /llm-zone -->",
]
path = f"wiki/entities/{entity}.md"
# Unified diff: new file. git apply --recount fixes the hunk counts, so the
# +N count need only be ≥ the real line count.
page_diff = [
    f"diff --git a/{path} b/{path}",
    "new file mode 100644",
    "--- /dev/null",
    f"+++ b/{path}",
    f"@@ -0,0 +1,{len(body)} @@",
] + [f"+{line}" for line in body]

taxonomy_diff = []
taxonomy_bullet = f"- `{requested_taxonomy_tag}`"
if requested_taxonomy_tag and taxonomy_bullet not in taxonomy.splitlines():
    old_lines = taxonomy.splitlines()
    if "## Form" not in old_lines:
        raise SystemExit("stub-llm: TAXONOMY block is missing ## Form")
    if os.environ.get("STUB_INVALID_TAXONOMY_ONCE") and "## Patch Retry" not in prompt:
        insert_at = len(old_lines)
    else:
        insert_at = old_lines.index("## Form")
        while insert_at and not old_lines[insert_at - 1]:
            insert_at -= 1
    new_lines = [*old_lines[:insert_at], taxonomy_bullet, *old_lines[insert_at:]]
    taxonomy_diff = ["diff --git a/wiki/_taxonomy.md b/wiki/_taxonomy.md", *difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="a/wiki/_taxonomy.md",
        tofile="b/wiki/_taxonomy.md",
        lineterm="",
    )]

print("\n".join([*taxonomy_diff, *page_diff]))
