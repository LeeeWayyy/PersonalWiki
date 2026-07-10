# Chapter Intelligence Design

Status: proposed

## Problem

The ingest pipeline currently pays for several independent readings of the same
source:

1. The source-key-term pass extracts a flat list of names for candidate lookup.
2. The main wiki pass reads the chapter again to decide what matters, discover
   entities and topics, and write a diff.
3. `generate-mindmap.py` later reads up to 120,000 source characters again to
   reconstruct the book's claims and argument structure.

The first pass captures too little to be reused. A list of names cannot express
claims, mechanisms, evidence, disagreements, chapter dependencies, or possible
synthesis pages. The later calls therefore repeat expensive reasoning, while
still having inconsistent views of what the chapter is about.

The goal is to extract a reusable, structured understanding of each chapter
once, without making that derived structure an evidence source or weakening the
main ingest's grounding in the original text.

## Decision

Introduce a versioned `chapter-intelligence/1` JSON artifact for each ingested
section. It is a derived cache keyed by source content, section, and prompt
version. It records the chapter's explanatory and argumentative structure, not
just entity names.

The artifact becomes shared input to:

- candidate-page retrieval;
- the main wiki diff prompt;
- argument-map generation;
- future derived views that need source understanding.

The original source remains authoritative. Wiki citations continue to point to
`[src:<source_id>#<section>]`; they never cite the intelligence artifact. The
main wiki pass continues receiving `SOURCE_TEXT` during the initial rollout.

## Data Flow

```text
source section
    |
    +-- deterministic extraction ------------------------------+
    |                                                           |
    +-- chapter analyzer (one structured completion)            |
            |                                                   |
            v                                                   v
     chapter-intelligence/1                               SOURCE_TEXT
            |                                                   |
            +-- candidate retrieval ----------------------------+
            |                                                   |
            +-- main wiki diff prompt (agentic or API) <---------+
            |
            +-- per-source argument-map merge
            |
            +-- future derived products
```

Chapter commits remain serial. Each committed chapter changes the vault and may
improve candidate context for the next chapter. The reusable analysis removes
duplicate comprehension work; it does not make concurrent writes to one Git
index safe.

## Artifact Contract

Suggested location:

```text
.wiki/chapter-intelligence-cache/<prompt-version>/<source-id>/<key>.json
```

`key` is a digest of:

- source SHA-256;
- exact section label (or an explicit whole-source sentinel);
- extracted-text SHA-256;
- analyzer prompt version;
- analyzer model identity where model changes are not output-compatible.

The cache follows the existing language and mind-map cache policy: stale or
invalid entries are regenerated, and rendering or downstream synthesis does not
mutate them.

Proposed `chapter-intelligence/1` shape:

```json
{
  "schema": "chapter-intelligence/1",
  "source_id": "01K...",
  "source_sha256": "...",
  "text_sha256": "...",
  "section_label": "第二章 生命力：质子动力与生命起源",
  "prompt_version": "v1",
  "language": "zh",
  "summary": "A compact account of the chapter's contribution.",
  "central_question": "What question does this chapter answer?",
  "chapter_claim": "The chapter's principal answer or explanatory move.",
  "builds_on": "How this chapter depends on earlier reasoning, or null.",
  "claims": [
    {
      "id": "c1",
      "kind": "claim",
      "text": "A complete, human-readable proposition.",
      "importance": 5,
      "source_spans": [
        {
          "start": 1042,
          "end": 1288,
          "quote": "A short exact excerpt used to verify the span."
        }
      ],
      "entities": ["线粒体", "质子驱动力"]
    },
    {
      "id": "c2",
      "kind": "evidence",
      "text": "The observation or experiment supporting the claim.",
      "importance": 4,
      "source_spans": [],
      "entities": ["线粒体"]
    }
  ],
  "entities": [
    {
      "name": "线粒体",
      "type": "organelle",
      "aliases": [],
      "importance": 5,
      "role": "Why this entity matters to the chapter's explanation.",
      "page_hint": "entity",
      "claim_ids": ["c1"]
    }
  ],
  "topics": [
    {
      "name": "真核细胞起源",
      "question": "The reusable question this topic page should answer.",
      "synthesis_angle": "How several claims or entities fit together.",
      "importance": 5,
      "claim_ids": ["c1"]
    }
  ],
  "relations": [
    {
      "from": "c2",
      "to": "c1",
      "rel": "supports"
    }
  ],
  "open_questions": []
}
```

Allowed claim kinds initially:

```text
question, hypothesis, claim, evidence, mechanism, definition,
contrast, consequence
```

Allowed relation kinds initially:

```text
answers, supports, explains, causes, leads-to, competes-with, refines
```

The lists are not quotas. The analyzer must capture all materially important
entities, topics, claims, and relations in an information-rich chapter. It must
not stop at five names or invent one wiki page per extracted noun. Importance,
role, and claim linkage let the main ingest make that editorial decision with
better context.

## Validation

The analyzer output is accepted only when:

- `schema`, source id, source hash, text hash, section label, and prompt version
  match the current run;
- every claim id is unique;
- every relation endpoint and `claim_ids` reference exists;
- kinds and relations belong to the declared enums;
- importance is an integer from 1 through 5;
- each source span is within the extracted text and its normalized `quote`
  matches that slice;
- names and prose fields are non-empty scalar strings with bounded lengths;
- entity and topic names are reusable concepts, not chapter titles or section
  labels copied verbatim.

Validation may drop a malformed optional record, but must reject an artifact
with no usable claims or no usable entity/topic coverage. A rejected artifact is
never cached.

## Pipeline Integration

### 1. Analyze Once

Add `pipeline/scripts/analyze-chapter.py`. It receives extracted text and source
metadata, invokes a structured non-agentic completion, validates the JSON, and
atomically writes the cache entry.

For chaptered sources, the analyzer also receives the ordered section list and
the compact spine (`section_label`, `central_question`, `chapter_claim`) of
already analyzed chapters. This is enough to ground `builds_on` without
resending earlier chapter text. The current chapter remains the only chapter
from which it may extract claims or source spans.

This replaces the current free-form `SOURCE_KEY_TERMS` call. The analyzer's
entity and topic names provide the same retrieval seeds, while its claims and
relations preserve the more valuable reasoning already paid for.

The analyzer should use a dedicated setting such as `PW_ANALYZE_MODEL`, with a
small model by default. Its output contract is tighter than the main diff task
and does not require workspace access or an agent loop.

There is no fixed entity or claim count. Output is bounded by importance and
source density: retain every concept needed to explain the chapter's reasoning,
but omit incidental mentions that have no explanatory role. This avoids both
the old five-name ceiling and an unhelpful noun dump.

### 2. Retrieve Candidates

Candidate selection consumes:

- entity names and aliases;
- topic names and questions;
- high-importance claim terms;
- named concepts referenced by several claims.

Retrieval remains deterministic after analysis. Existing alias-index lookup and
body search stay in place. When the vault has no more than `CAND_CAP` pages, all
pages can still be included, but analysis is not wasted because the same
artifact feeds synthesis and maps.

### 3. Build the Wiki Diff Prompt

Replace `SOURCE_KEY_TERMS` with a compact `SOURCE_INTELLIGENCE` block containing:

- chapter question, claim, and dependency;
- important entities with their roles;
- candidate topic questions and synthesis angles;
- claims and typed relations;
- open questions or uncertainty.

The block is guidance and a recall aid, not an instruction to create every
suggested page. `SOURCE_TEXT` remains in the prompt as the only evidentiary text
and the source for citations. The LLM must resolve any conflict in favor of the
source text.

This richer context should reduce two current failure modes: missing important
topics and requesting candidate expansion only after the first expensive diff
call. It may also reduce malformed retries by making the requested edit scope
clearer before diff generation.

### 4. Build Argument Maps

`generate-mindmap.py` should merge all cached chapter artifacts for one source,
then perform a smaller source-level synthesis call. That call connects chapter
claims into a coherent book-level argument and emits the existing map contract.

This preserves an independent map-generation step, which is useful because a
map is a different editorial product from wiki pages, but avoids re-reading up
to 120,000 raw source characters merely to rediscover claims already extracted.

If one or more chapter artifacts are missing, the command may either generate
them first or use the current raw-source path as a compatibility fallback. It
must report which path it used.

## LLM Execution Modes

The reusable artifact contract separates reasoning inputs from provider
execution. The main wiki pass should explicitly support two modes:

### Agentic mode (default today)

- Uses the Codex subscription path and existing workspace-seeded execution.
- May inspect candidate files and produce a unified diff through an agent loop.
- Keeps the current expand and one-retry behavior.
- Receives the same `SOURCE_INTELLIGENCE` and `SOURCE_TEXT` contract.

### API mode (supported path, opt-in initially)

- Uses a single non-agentic completion for diff generation.
- Receives fully materialized candidate context because it cannot inspect the
  workspace.
- Uses structured output where the provider supports it, or the existing strict
  unified-diff protocol otherwise.
- Has separately measured model, timeout, token, and retry settings.

A future `PW_INGEST_EXECUTION_MODE=agentic|api` setting should choose the main
diff executor. It must not overload `PW_LLM_PROVIDER`: provider and execution
style are distinct decisions. For example, an API provider can run a single
completion, while Codex CLI currently runs the agentic workflow.

The chapter analyzer itself remains non-agentic in both modes. Its job is one
bounded structured extraction, so an agent loop would add cost without useful
capability.

## Token Model

Today, an information-rich chapter can be read independently by the key-term
pass, the main diff pass, an expand/retry pass, and the map generator. The new
flow changes the cost profile to:

1. one small structured analysis per chapter, cached by content;
2. one main diff call that reuses the analysis but remains source-grounded;
3. optional expand/retry calls, with better initial retrieval context;
4. one compact source-level map merge over chapter artifacts.

The first implementation should measure:

- analyzer input/output tokens and latency;
- number of entities/topics/claims extracted;
- candidate recall against pages ultimately modified;
- expand and patch-retry rates;
- main-pass input/output tokens;
- argument-map input tokens;
- manual quality scores for coverage, readability, and factual grounding.

For vaults at or below `CAND_CAP`, the current pipeline skips the key-term call.
Adding chapter analysis therefore has a small new cost if the user only wants a
wiki diff and never generates derived views. It pays for itself only when it
reduces main-pass retries, improves output enough to avoid re-ingest, or is
reused by argument maps and other products. Phase 1 must measure this case
separately rather than assuming a universal token saving.

Do not remove `SOURCE_TEXT` from the main pass based only on token estimates.
That optimization requires evidence that citations, nuance, and page quality do
not regress.

After that evidence exists, source spans provide a safer payload reduction:
the prompt can carry the structured analysis plus merged evidence windows around
validated spans. In agentic mode, the complete extracted text can remain
available as a workspace file for on-demand inspection. In API mode, the caller
can fall back to full text when coverage checks fail. This is a later
optimization, not part of Phase 2.

## Failure and Recovery

- Cache writes are atomic (`tmp` plus `os.replace`).
- Cache generation occurs before any vault page mutation.
- Analyzer failure leaves no accepted cache and does not dirty the vault.
- A stale hash or prompt version is a cache miss, never a partial reuse.
- Downstream consumers validate the artifact before use.
- During rollout, analyzer failure falls back to the existing source-text path
  and is visible in the ingest log.
- Cache entries are derived state and may be deleted and regenerated safely.

## Rollout

### Phase 1: Shadow analysis

- Add the analyzer, schema validation, atomic cache, and fixtures.
- Run it during ingest but do not alter candidate selection or prompts.
- Compare its coverage with pages and maps produced by the current pipeline.

### Phase 2: Wiki integration

- Replace `SOURCE_KEY_TERMS` with `SOURCE_INTELLIGENCE`.
- Feed structured names into candidate retrieval.
- Add the compact intelligence block to the main prompt.
- Keep `SOURCE_TEXT`, agentic execution, expansion, and retry unchanged.

### Phase 3: Argument-map reuse

- Merge chapter artifacts in `generate-mindmap.py`.
- Keep raw-source generation as an explicit fallback.
- Compare node coverage and relation accuracy on existing books before making
  the structured path the default.

### Phase 4: Execution and payload experiments

- Add explicit agentic/API execution modes.
- Pilot a single-completion API diff path against the agentic baseline.
- Experiment with reduced source payloads only after quality and citation tests
  exist.

## Tests

At minimum, add:

- schema validation tests for valid, stale, malformed, and dangling-reference
  artifacts;
- cache-key and invalidation tests;
- a two-chapter fixture proving independent cache entries and deterministic
  source-level merge order;
- candidate-retrieval tests using entity aliases, topics, and claim terms;
- prompt tests proving both `SOURCE_INTELLIGENCE` and `SOURCE_TEXT` are present;
- an argument-map test that uses chapter artifacts without extracting raw source;
- a fallback test for a missing or invalid chapter artifact;
- golden quality fixtures for an information-rich chapter, including more than
  five important entities and at least one cross-entity synthesis topic.

## Non-Goals

- The artifact is not a replacement source, citation target, or wiki page.
- It does not authorize parallel chapter commits against one vault Git index.
- It does not force one page per extracted entity.
- It does not remove the main pass's access to original source text in the first
  rollout.
- It does not remove agentic Codex execution while that is the preferred
  subscription-backed path.

## Open Decisions

These should be resolved with Phase 1 measurements:

- Whether the analyzer receives the full extracted chapter or a deterministic
  chunked representation for chapters near the 100,000-character limit.
- Whether cross-chapter entity identity is normalized during each analysis or
  during the source-level merge.
- Whether map generation should auto-create missing analysis entries or require
  an explicit `--analyze` step.
- Which small model provides sufficient Chinese and English extraction quality
  for the analyzer.
- Whether API diff generation should emit a unified diff directly or a
  structured edit plan rendered deterministically into a diff.
