#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""
Deterministic stub for $LLM_CMD, used by scripts/tests/test_ingest_e2e.sh.

Reads the prompt on stdin and emits a canned response so the full ingest
pipeline can be exercised end-to-end with NO live LLM:

  - Keyword pre-pass prompt  → a few keyword lines.
  - Main ingest prompt       → a unified diff CREATING a new entity page
                               that cites the run's real source_id (parsed
                               out of the prompt's SOURCE_META block), so
                               the citation resolves and all gates pass.

The new page's name is overridable via $STUB_ENTITY (default e2e-entity)
so a test can ingest twice without colliding.
"""

import json
import os
import re
import sys

prompt = sys.stdin.read()
entity = os.environ.get("STUB_ENTITY", "e2e-entity")
# Optional media anchor appended to the citation (e.g. STUB_CARD_ANCHOR=card-1), so a
# media-spine test exercises the card/frame/timestamp gate NON-vacuously. Unset (default)
# = a bare [src:<id>] citation, which is what the document-path e2e expects.
anchor = os.environ.get("STUB_CARD_ANCHOR", "")

# Language-profile prompt (generate-language-pages.py): "## SENTENCES" and
# "## WORDS" blocks. Emit the JSON the generator expects: every sentence
# translated, every listed word glossed, plus one deterministic grammar point.
if "## SENTENCES" in prompt and "## WORDS" in prompt:
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

# The main ingest prompt contains the SOURCE_TEXT + CANDIDATE_PAGES blocks;
# the keyword pre-pass prompt does not.
if "## SOURCE_TEXT" not in prompt:
    # Keyword pre-pass: short retrieval seeds, one per line.
    print("\n".join(["mitochondria", "ATP", "energy", entity, "biology"]))
    sys.exit(0)

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

body = [
    "---",
    "type: Entity",
    f"aliases: [{entity}]",
    "tags: [concept, biology/cell]",
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
    f"> An end-to-end stub claim about {entity} [src:{sid}{('#' + anchor) if anchor else ''}].",
    "<!-- /llm-zone -->",
]
path = f"wiki/entities/{entity}.md"
# Unified diff: new file. git apply --recount fixes the hunk counts, so the
# +N count need only be ≥ the real line count.
diff = [
    f"diff --git a/{path} b/{path}",
    "new file mode 100644",
    "--- /dev/null",
    f"+++ b/{path}",
    f"@@ -0,0 +1,{len(body)} @@",
] + [f"+{line}" for line in body]
print("\n".join(diff))
