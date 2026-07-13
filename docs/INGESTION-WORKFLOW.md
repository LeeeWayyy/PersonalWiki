# Ingestion Workflow

How one source (file, URL, or media item) flows through ingestion, end to end,
as implemented today. This is the operational companion to
[CHAPTER-INTELLIGENCE-DESIGN.md](CHAPTER-INTELLIGENCE-DESIGN.md), which covers
the analyzer's rationale, artifact contract, and token model — this document
covers the pipeline order, the files each stage owns, what each stage reads and
writes, and what happens when a stage fails. The orchestrator is
`pipeline/ingest.py`; everything runs with `cwd` set to the content repo
(`content/`, overridable via `PW_CONTENT_DIR`), and every successful run ends
in exactly one git commit.

## End-to-End Flow

```text
Web UI (index.astro + ingest-client.js)
    | POST /ingest (file or URL + options)
    v
routers/ingest.py -> validation.normalize_ingest_options (kind, section_heading)
    v
ingest_runner.py -- one asyncio.Lock, git auto-init, preflight, SSE, cancel
    | spawns: python3 pipeline/ingest.py [flags] <target>    <-- CLI entry too
    v
pipeline/ingest.py (orchestrator, cwd = content repo)
    +-- lock + scaffold + preflight (clean index, wiki/, provenance)
    +-- source identity ......... source-identity.py (sha256 dedup, ULID,
    |                             sources/<file> + sidecar; media-identity.py)
    +-- completion check ........ .wiki/log.md (skip if already logged)
    +-- [--chapters / auto] ..... enumerate `## ` sections, one child ingest
    |                             per chapter (resumable)
    +-- extract + caption ....... extract.py -> SOURCE_TEXT (+ .assets/),
    |                             caption.py (soft-fail)
    +-- chapter intelligence .... analyze-chapter.py -> chapter_intelligence.py
    |                             (validated JSON, manifest-verified cache in
    |                             .wiki/chapter-intelligence-cache/)
    +-- candidate retrieval ..... alias-index.py + rg over wiki/, cap CAND_CAP
    +-- prompt + LLM diff ....... build-prompt.py (digest|expand|retry) ->
    |                             llm_client.complete -> unified diff
    +-- scope check + apply ..... diff-paths.py, git apply --index --recount
    +-- quality gate ............ verify-ingest-quality.py (JSON receipt,
    |                             fail-closed)
    +-- post-apply gates ........ format-llm-zone, add-page-id, sync-frontmatter,
    |                             alias check, lint gates, autolink, MOCs
    +-- supersede (media) ....... rewrite-citations.py old_id -> new_id
    +-- log + commit ............ .wiki/log.md, sidecar progress, git commit
    v                             (atomic; rollback on any failure)
Downstream: generate-mindmap.py reuses cached artifacts; the Astro site renders
[src:<id>#sec=...] citations (src/lib/source-citations.mjs, remark-inline.mjs)
```

## Entry Points

**Web UI.** `src/pages/index.astro` mounts `src/scripts/ingest-client.js`,
which POSTs to the backend `/ingest` route with either a file upload
(multipart, staged under the backend's stage dir) or a JSON `{url, options}`
body. Options are validated by `backend/app/validation.py::normalize_ingest_options`:
`kind` must be `auto|wiki|lang|video|audio|image_note` (`media` is normalized to
`video`), and `section_heading` is only allowed for `auto`/`wiki`. The route
(`backend/app/routers/ingest.py`) starts a job and the client streams its log
via SSE (`/jobs/{id}/events`), with `/jobs/{id}/cancel` available.
`POST /ingest/sections` (same file-or-url body, no `options`) runs
`extract.py --list-sections` — no LLM, no vault writes — so the UI's
"List chapters" button can fill the section-heading picker instead of
requiring the exact heading to be typed.

`backend/app/ingest_runner.py` is the control plane: a single `asyncio.Lock`
serializes all jobs, `ensure_content_git` auto-initializes a git repo in the
wiki folder if needed (blocked by `PW_INGEST_NO_AUTO_GIT=1`), and its own
preflight mirrors ingest.py's dirty-tree scopes (tool-owned wiki pages,
tracked provenance, staged files, and stale assets; `lang/_reading/` for lang
jobs) so a doomed run is reported as `blocked`
up front. It builds the argv (`--profile lang`, `--kind <k>`, or
`--section '^<escaped heading>$' --section-label=<heading>`; document jobs use
`--limit 0`), runs `pipeline/ingest.py` in its own process group with an idle
timeout (2100s of silence), then optionally `REBUILD_CMD`.
`PW_INGEST_STUB=1` exercises the job
flow without the real pipeline. `backend/app/serve.py` enforces exactly one
Uvicorn worker — jobs, locks, and staged uploads are process-local.

**CLI.** `python3 pipeline/ingest.py [flags] <path-or-url>` is the same engine
invoked directly; see [Running It](#running-it).

## Stage 1 — Preflight, Lock, Scaffold

Owner: `pipeline/ingest.py` (`acquire_content_ingest_lock`,
`ensure_wiki_scaffold`, `preflight`). An exclusive `fcntl` lock on
`.wiki/ingest.lock` serializes CLI and backend runs. The scaffold creates
`wiki/{entities,topics,_index}`, `sources/`, `.wiki/`, and a placeholder
`wiki/_taxonomy.md` with only neutral fallback tags in an empty repo. Preflight refuses to run (exit 1,
nothing mutated) when: the git index is non-empty; tracked or untracked
changes exist under `wiki/`; tracked edits exist under `.wiki/log.md` or
`sources/`; or untracked files sit under an existing `sources/*.assets/` dir
(the terminal commit would sweep them in).

## Stage 2 — Source Identity

Owner: `pipeline/scripts/source-identity.py` (documents) or
`media-identity.py` (`--kind video|audio|image_note`; delegates ASR/OCR to the
remote extraction service and additionally emits `TEXT_FILE` and `AUDIT_JSON`).
The document path sha256-hashes the input (URLs fetched with curl, size- and
time-capped) and dedups against *tracked* sidecars in `sources/*.md`: a sha
match reuses the existing `source_id` and asset (after re-hashing the stored
asset and refusing on drift); otherwise it mints a ULID `source_id`, copies
the asset to `sources/<date>-<name>`, and writes the `<dest>.md` sidecar
(`source_id`, `sha256`, `added`, `origin_type`, `origin_ref`, `supersedes`,
`title`, and EPUB `author` when present). The same helper's `--fetch-only`
mode gives section listing the identical bounded HTTP-only fetch policy.
ingest.py drives identity via a two-phase `--reserve-handshake`: the
child emits `IDENTITY_READY=new` with all future paths *before* touching the
vault, ingest registers them for cleanup, then replies `PUBLISH` — a
cancellation can never strand a source file the orchestrator doesn't know
about. Output is shell-quoted `KEY=VALUE` lines parsed into the `SRC` dict.

After identity, completion is checked against `.wiki/log.md`: a whole-source
run already logged, or a `--section-label` already logged (or covered by a
whole-source line), exits 0 without doing anything.

## Stage 3 — Chapter Selection and Slicing

Owner: `pipeline/ingest.py` (`run_chaptered` and helpers). Chapter mode is
entered by `--chapters` or automatically for a local `.epub/.mobi/.azw/.azw3`
file in the wiki profile with no section/kind flags. One full extraction
(`extract.py --limit 0`) enumerates `## ` sections with body sizes;
`_grouped_chapter_ranges` groups sections under chapter headings
(`PW_CHAPTER_HEADING_RX`, CJK `第…章` + English defaults) and excludes
front/back matter (`PW_NONCONTENT_HEADING_RX`). With no chapter markers, each
section ≥ `PW_CHAPTER_MIN_CHARS` (default 200) becomes its own unit. Repeated
headings get stable `[occurrence i/N]` labels. Generated labels replace control
characters and truncate to 200 characters without changing extraction headings.

Each chapter is then a **child process** running the normal single-section
ingest (`PW_INGEST_NO_AUTOCHAPTER=1` prevents re-entry), with its own
preflight and commit. Ordered chapter labels pass via
`PW_SOURCE_CHAPTER_OUTLINE` so the analyzer can use prior-chapter spines.
Chapters already in `.wiki/log.md` for this source (matched by sha256 →
source_id) are skipped, so re-running the same command resumes after a
failure; asset extraction/captioning happens only in the first new chapter's
run. A child failure stops the loop with that child's exit code.

Single-section slicing: `--section REGEX` must match exactly one `## ` heading
of the extractor output; internal children use `PW_SECTION_RANGE` (ordered
heading ordinals) or `PW_SECTION_OCCURRENCE` instead. A truncation marker
(`--limit` exceeded, default 100000 chars) is fail-closed: truncated text is
never logged as complete.

## Stage 4 — Extraction and Captioning

Owner: `pipeline/scripts/extract.py` (invoked by ingest.py). Produces the
plain-text `SOURCE_TEXT` temp file and, unless a later chapter run set
`PW_INGEST_SKIP_ASSETS=1`, writes extracted images to `<dest>.assets/` with a
`_manifest.md`. Media runs skip extract.py entirely — `media-identity.py`
already rendered the transcript text. New images are captioned by
`caption.py` (backend/model/lang/limit from `CAPTION_*` env vars); captioning
failure is a warning, not fatal. Empty extracted text is fatal.

## Stage 5 — Chapter Intelligence Analysis

Owner: `pipeline/scripts/analyze-chapter.py` (CLI) →
`pipeline/scripts/chapter_intelligence.py` (library). The
[design doc](CHAPTER-INTELLIGENCE-DESIGN.md) covers the artifact contract;
operationally:

- One structured completion plus at most one validation-repair completion
  (`llm_client.complete`) produces a
  `chapter-intelligence/1` artifact: claims with exact source quotes,
  entities, topics, relations, `page_candidates`, `builds_on`. Model from
  `--analyze-model`/`PW_ANALYZE_MODEL` (independent of the diff model),
  reasoning effort from `PW_ANALYZE_REASONING_EFFORT` (default `low`), timeout
  from `PW_ANALYZE_TIMEOUT_S` (default 1800s).
- Quotes are materialized to canonical `start`/`end` offsets against the
  extracted text; unmatched quotes fail validation (`validate_artifact`).
- The validated artifact is cached at
  `.wiki/chapter-intelligence-cache/<prompt-version>/<source-id>/<key>.json`
  with a sibling `.manifest`. The key digests source + text sha256, section
  label, prompt version, model identity, schema-rules digest, ordered
  sections, prior-chapter spines, and the prompt-template hash — any drift
  misses. `read_cache_entry` verifies manifest shape, key, and artifact digest
  before reuse. Invalid output gets one repair attempt; a second invalid
  response is preserved for debugging and fails the run.
- For chaptered books, `discover_prior_spines` feeds compact spines
  (label/central_question/chapter_claim) of already-cached chapters into the
  next chapter's analysis.

Failure is fail-closed: the run dies with "no wiki diff was attempted" — no
vault mutation has happened yet.

## Stage 6 — Candidate Retrieval

Owner: `pipeline/ingest.py::collect_candidates`. If the vault has ≤ `CAND_CAP`
(default 20) wiki pages, all pages are candidates. Otherwise search terms
derived from the intelligence artifact (entities/topics/aliases, ordered by
required flag then importance) are matched via exact alias lookup
(`alias-index.py lookup`, required matches pinned past the cap) plus `rg` body
search; the top-scored paths become the candidates file.
`_renderer_intelligence_with_existing_types` then projects a renderer-only
copy of the artifact flipping a candidate's `page_type` to match a unique
existing vault owner (prevents duplicate Entity/Topic pairs); the strict
artifact is untouched for validation and caching.

## Stage 7 — Prompt Build and LLM Diff Loop

Owner: `pipeline/scripts/build-prompt.py`, prompts in `pipeline/prompts/`
(`ingest.md` instruction block + `schema-ingest.md` rule blocks selected per
operation). The prompt contains: instructions, selected schema sections,
`ALL_SOURCE_IDS` (from tracked sidecars only), the taxonomy, `SOURCE_META`,
the compacted `SOURCE_INTELLIGENCE` projection, `SECTION_LABEL` plus the exact
pre-encoded `SECTION_CITATION` token, `SOURCE_TEXT`, candidate pages (digests
via `page-digest.py`, full content for expanded paths), and the images table.

The LLM (via `llm_client`, model from `--model`/`PW_LLM_MODEL`, timeout
`PW_LLM_TIMEOUT_S` default 1800s) must emit a unified diff, a one-shot
`{"action":"expand", ...}` request (answered with a second `expand`-operation
prompt), or a `NO_CHANGES:` line. Provider failures or empty output get one
retry; an enabled API is the fallback after local failure. Codex runs in an
isolated temp workdir seeded with the taxonomy and exactly the candidate pages
(`_seed_workset`); the real
tree is only ever mutated by applying the emitted diff. `NO_CHANGES` is not a
free pass: `handle_no_changes_or_continue` runs the same quality gate with an
empty modified set, reports candidate omissions as warnings, then logs and
commits the no-change run. A supersede
whose text artifact is byte-identical to a predecessor already cited by
committed pages (`_supersede_coverage_proven`).

## Stage 8 — Scope Check, Apply, Retry

Owner: `pipeline/ingest.py` + `scripts/diff-paths.py` + `scripts/apply-diff.py`.
The diff is fence-stripped, then scope-checked: it may only touch
`wiki/entities/`, `wiki/topics/`, and `wiki/_taxonomy.md`. Taxonomy changes
may add valid bullet lines only; deleting, rewriting, or reordering existing
lines, or emitting a taxonomy-only diff, fails closed. A
malformed diff (no `diff --git` headers) gets one auto-retry with a `retry`
prompt; an out-of-scope diff is rejected outright, raw saved as
`<tmp>.rejected`. `git apply --index --recount` applies it; on failure the
failing paths are parsed, merged into the candidates/expand set, and one retry
is attempted. Non-retryable git errors (corrupt patch etc.) die immediately,
preserving `<tmp>.failed.N`/`.apply-err.N` artifacts. Right after a successful
apply, `_assert_existing_citation_anchors_preserved` dies if the diff removed
any pre-existing `[src:...]` anchor (bare → anchored is allowed, the reverse
is not).

## Stage 9 — Quality Gate and Post-Apply Gates

Owner: `pipeline/scripts/verify-ingest-quality.py` (subprocess) →
`pipeline/scripts/ingest_quality.py::evaluate_quality`. Runs on the staged
modified pages plus unchanged candidates, prints a JSON
`ingest-quality-receipt/1` (coverage summary, warnings, errors) and exits
non-zero on failure; any internal exception fails closed with a
`gate.internal` receipt. It deterministically reports candidate coverage;
omitted importance-4/5 recommendations are warnings after any substantive page
change or an explicit `NO_CHANGES`. Modified substantive llm-zone paragraphs
must carry the exact
canonical citation `[src:<id>#sec=<encoded label>]`; central pages have
substantive prose; entities avoid forbidden attribution phrasing. Then, in order:
`format-llm-zone.py`, `add-page-id.py`, `sync-frontmatter.py`,
`alias-index.py check` (alias-uniqueness abort), `lint.py --gate=tags`,
`--gate=images` (media runs also `--gate=media-anchors`), `autolink.py --all`,
`generate-mocs.py`, and finally `lint.py --gate=page-id`. Every gate failure
routes through `die()` and rolls back.

## Stage 10 — Supersede, Log, Commit

A media re-transcribe/re-OCR mints a fresh source that `supersedes:` the old
one; `rewrite-citations.py old new` migrates live citations in old pages
(anchor preserved verbatim), then the media-anchor gate re-runs. The old asset
stays in `sources/`, immutable. Completion tracking: one line is appended to
`.wiki/log.md` (`<added>  <source_id>[#section]  pages: <staged paths>`) and
`update-sidecar-progress.py` refreshes the per-chapter checklist in the
sidecar (soft-fail). The commit stages exactly: `.wiki/log.md`, the source
asset + sidecar (new sources), the `.assets/` dir, the audit JSON (media), and
the wiki paths from the diff. `--chapters` yields one such commit per chapter.

## Failure Handling and Rollback

Every fatal path goes through `die()`. Before the first tracked-file mutation
(`git apply --index`, citation rewrite, or log append) rollback is unarmed and
`die` only removes run-created untracked artifacts (new source asset, sidecar,
assets dir — registered via the identity handshake). Once
`_ROLLBACK_ON_FAILURE` is armed, `_rollback_after_apply_failure` unstages and
restores all wiki/provenance paths, deletes rolled-back added pages, and
verifies the tree is clean; only then is the new source removed. If rollback
itself fails, source provenance is deliberately **kept** so staged citations
can never point at a deleted source. SIGTERM/SIGINT (backend cancel, idle
timeout) run this same cleanup; the backend allows 30 seconds before escalating
so child termination and Git rollback can finish.

## Downstream Consumers

- `pipeline/scripts/generate-mindmap.py` builds per-source argument maps under
  `wiki/_maps/` from the validated chapter-intelligence cache when a complete
  set exists (`scan_validated_entries`), falling back to source text; its own
  LLM output is cached in `.wiki/mindmap-cache/`. Citations use the canonical
  `[src:<id>#sec=...]` encoding from `source_citations.py`.
- Astro site: `src/lib/vault.mjs` indexes sidecars into a `source_id → meta`
  map; `src/plugins/remark-inline.mjs` renders `[src:...]` body citations as
  linked chips; `src/lib/source-citations.mjs` is the JS mirror of
  `source_citations.py`'s `#sec=` percent-encoding codec.

## Verification Tooling

- `verify-ingest-quality.py --intelligence J --source-id ID --section-label L
  [--modified P...] [--existing P...]` — the quality gate as a standalone CLI;
  prints the JSON receipt, exit 0 iff `ok`.
- `verify-chapter-intelligence-baseline.py <baseline.json> <artifact.json>...`
  — checks analyzer artifacts against a hand-written
  `chapter-intelligence-baseline/1` expectation file (required entity/topic
  alias groups, claim term groups, relation kinds, forbidden/analysis-only
  concepts) for regression-testing analyzer quality on a known book.

## Key Files

| File | Role |
| --- | --- |
| `pipeline/ingest.py` | Orchestrator: lock, preflight, chapter loop, LLM loop, gates, commit, rollback |
| `pipeline/scripts/source-identity.py` | sha256 dedup, ULID, source asset + sidecar (two-phase publish) |
| `pipeline/scripts/extract.py` | Text + image extraction, `--section` slicing |
| `pipeline/scripts/analyze-chapter.py` / `chapter_intelligence.py` | Chapter-intelligence artifact: prompt, validation, manifest-verified cache |
| `pipeline/scripts/build-prompt.py` + `pipeline/prompts/*.md` | Main diff prompt assembly (digest/expand/retry) |
| `pipeline/scripts/apply-diff.py` / `diff-paths.py` | Fence-strip, expand detection, scope + retry-path parsing |
| `pipeline/scripts/ingest_quality.py` / `verify-ingest-quality.py` | Deterministic coverage/citation/prose quality gate (JSON receipt) |
| `pipeline/scripts/source_citations.py` / `src/lib/source-citations.mjs` | Canonical `[src:<id>#sec=...]` citation codec (Python / JS) |
| `backend/app/serve.py` / `ingest_runner.py` / `routers/ingest.py` / `validation.py` | Web control plane: jobs, preflight, SSE, cancel |
| `src/scripts/ingest-client.js` (mounted by `src/pages/index.astro`) | Browser client |
| `.wiki/log.md`, `sources/*.md`, `.wiki/chapter-intelligence-cache/` | Completion log, sidecars, analysis cache (cache + lock are git-excluded) |

## Running It

```bash
# whole document or URL (auto chapter mode for .epub/.mobi/.azw/.azw3)
python3 pipeline/ingest.py path/to/book.epub
python3 pipeline/ingest.py https://example.com/article

# explicit chapter-by-chapter (resumable; one commit per chapter)
python3 pipeline/ingest.py --chapters book.epub

# one section only (regex must match exactly one `## ` heading)
python3 pipeline/ingest.py --section '^第1章.*$' --section-label '第1章 ...' book.epub

# media front doors
python3 pipeline/ingest.py --kind video 'https://youtube.com/watch?v=...'
python3 pipeline/ingest.py --kind audio --feed-url FEED --episode-title '...' EPISODE_URL
python3 pipeline/ingest.py --kind image_note --post-id ID bundle.zip
python3 pipeline/ingest.py --kind video --retranscribe URL   # supersede + re-ASR
python3 pipeline/ingest.py --kind video --rerender URL       # re-render committed JSON, no ASR

# other flags
#   --model M            main diff model;  --analyze-model M  analyzer model
#   --limit N            extraction char cap (default 100000; 0 = unlimited)
#   --images-only        assets + captions only, no analysis/diff
#   --profile lang       language pages under content/lang/, no wiki synthesis
#   --frames --cadence S media keyframes; --force accepts a low coverage result
```

Full flag list: `python3 pipeline/ingest.py --help` (argparse in `main()`).
Environment knobs referenced above: `PW_CONTENT_DIR`, `PW_LLM_MODEL`,
`PW_ANALYZE_MODEL`, `PW_ANALYZE_REASONING_EFFORT`, `PW_LLM_TIMEOUT_S`,
`PW_ANALYZE_TIMEOUT_S`, `CAND_CAP`, `CAPTION_BACKEND/MODEL/LANG/LIMIT`,
`PW_CHAPTER_HEADING_RX`/`PW_SECTION_HEADING_RX`/`PW_NONCONTENT_HEADING_RX`,
`PW_CHAPTER_MIN_CHARS`; backend: `PW_PORT`, `PW_INGEST_STUB=1`,
`PW_INGEST_NO_AUTO_GIT=1`, `REBUILD_CMD`, `INGEST_CMD`.
