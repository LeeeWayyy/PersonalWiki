# MOBI Support Plan

Add `.mobi` / `.azw3` support to both the **ingest pipeline** (source → wiki
markdown for the LLM pipeline) and the **in-app Source Reader** (source →
`blocks.json` rendered in the app).

Status: implemented. This file is retained as the design record and rollout
checklist for the MOBI/AZW support path.

## TL;DR

- The only two format-aware components are `pipeline/scripts/extract.py`
  (ingest) and `scripts/build-blocks.py` (reader). Everything else — source
  identity, ingest runner, reader UI, annotations, study — is format-blind.
- Both extractors dispatch on file suffix. A `.mobi` today falls through:
  ingest → `UnicodeDecodeError` → exit 3; reader → `raw = []` → no
  `blocks.json` → source shows "未提取正文", no Read button.
- Strategy for both: **convert MOBI → EPUB/HTML via the `mobi` package
  (KindleUnpack), then delegate to the existing EPUB/HTML extractor.** Do not
  reimplement MOBI parsing.
- Net change: one dispatch branch + one `extract_mobi` shim in each of the
  two extractors, one optional param on the ingest EPUB extractor, a dep
  declaration, docs, a fixture-based test, and a cosmetic title-regex tweak.

## Background: how ingestion routes formats today

`source-identity.py` copies the source into `sources/` preserving its original
extension, assigns a ULID, writes the sidecar. `origin_type` is only
`file`/`url` — it never inspects the format. A `.mobi` flows through untouched.

`ingest.py:637` shells `extract.py <dest> --write-assets [...]` and consumes
stdout as the source text — format-blind.

`extract.py` `dispatch()` (`extract.py:1139`) is the **only** ingest format
gate. It routes on `path.suffix.lower()`:

- `.epub` → `extract_epub`
- `.pdf` → `extract_pdf`
- `.html/.htm/.xhtml` → `extract_html_file`
- text extensions → `extract_text_file`
- anything else → `extract_text_file` → binary → `UnicodeDecodeError` → exit 3

## Background: how in-app reading works today

The Source Reader UI is **100% format-agnostic**. It renders
`.blocks/<sid>.blocks.json`:

- `blocksForSource()` (`src/lib/vault.mjs:243`) prefers
  `.blocks/<sid>.blocks.json`; its only fallback is `loadLang()` reading.json
  (**language sources only**). For a book/document source with no blocks.json,
  it returns `null` → source is unreadable, no Read button
  (`src/pages/sources/index.astro:82`).
- `.blocks/<sid>.blocks.json` is produced by `scripts/build-blocks.py`, a
  **separate, self-contained extractor** (its own zipfile/OPF/HTML parsing, no
  shared code with `extract.py`). It dispatches on `asset.suffix.lower()`
  (`build-blocks.py:293`): `.epub` / `.pdf` / `.md` only; anything else →
  `raw = []` → no output.
- `build-blocks.py` is run best-effort by `sync_content.py:206` via
  `sys.executable`. Its pdf path already degrades gracefully when pypdf is
  absent (`try: import pypdf except: return []`).

**Consequence:** for a MOBI to be readable in-app, `build-blocks.py` MUST emit
a `blocks.json` for it. There is no generic-markdown fallback for documents.

## Conversion library

`mobi` (PyPI, KindleUnpack-based):

```python
tempdir, filepath = mobi.extract("book.mobi")   # caller deletes tempdir
```

- `filepath` is `.epub` (modern KF8 / `.azw3`), `.html` (older MOBI6), or
  `.pdf` (Print Replica).
- Supports mobi + azw derivatives, unencrypted only.
- **License: GPL-3.0-only.** See decision D1.

## Part 1 — Ingest (`pipeline/scripts/extract.py`)

1. **PEP-723 deps** (`extract.py:4`): add `"mobi==<pinned>"`. Pin exactly and
   add it to the existing breaking-change comment block (`extract.py:11`):
   image bytes must stay byte-stable across runs or every asset re-hashes.
2. **`extract_mobi(path, *, write_assets, source_id)`**: convert via
   `mobi.extract`, dispatch the converted file by suffix to `extract_epub` /
   `extract_html_file`, `finally: shutil.rmtree(tempdir)`. Reject `.pdf` output
   in v1 with a clear message (see D3).
3. **`dispatch()`** (`extract.py:1139`): add
   `if ext in (".mobi", ".azw", ".azw3"): return extract_mobi(...)`.
4. **`assets_dir` override**: `extract_epub` / `extract_html_file` derive
   `assets_dir = path.parent/(path.name + ".assets")` internally
   (`extract.py:223`, `:1053`). When delegating on the *converted temp file*,
   assets would land in the temp dir and be deleted. Add an optional
   `assets_dir: Path | None = None` param (one-line default:
   `assets_dir = assets_dir or path.parent/(path.name + ".assets")`) and pass
   the **original** `.mobi`'s `.assets` path from `extract_mobi`.
5. **Provenance note:** resulting `origin_refs` carry `kind="epub"` pointing at
   the converted epub's internal chapter paths. Acceptable — there is no
   meaningful "mobi internal path", and source provenance is preserved via the
   sidecar sha256.
6. **Docs:** module docstring + `--write-assets` help (`extract.py:23`,
   `:1190`); `README.md:37,133`; `pipeline/schema.md:758`.

## Part 2 — Reader (`scripts/build-blocks.py`)

1. **Dispatch branch** in `main()` (`build-blocks.py:293`):
   `elif ext in {".mobi", ".azw3", ".azw"}: raw = extract_mobi(asset, sid)`.
2. **`extract_mobi(asset, sid)`**: mirror the pdf best-effort pattern —
   `try: import mobi except Exception: return []`. Convert via `mobi.extract`,
   then in a `finally: shutil.rmtree(tempdir)`:
   - converted `.epub` → call the existing `extract_epub(Path(filepath), sid)`.
     This reuses chapters, headings, and image extraction to
     `public/vault-assets/_epub/`. **Verify** the converted epub parses: this
     `extract_epub` uses a hand-rolled OPF reader (`epub_docs`,
     `build-blocks.py:127`) that only matches **double-quoted** `<item
     href="...">` attributes. KindleUnpack emits double-quoted OPF, so it
     should work, but confirm against the fixture before assuming.
   - converted `.html` (older MOBI6) → feed through the existing `Blocks`
     HTMLParser. **Known limitation:** build-blocks' image copy is
     epub-zip-specific (`_write_image(zf, ...)`, `build-blocks.py:145`), so the
     `.html` path yields **text only, no image blocks** in the reader. Text
     reading works; figures are dropped. Acceptable for v1 (most modern books
     convert to `.epub`); note it rather than plumb a second image path.
3. **`backend/requirements.txt`**: add `mobi` (recommended) so the environment
   running `build-blocks.py` actually has it; otherwise mobi reading is
   silently skipped like pdf-without-pypdf. GPL-3 flag → D1.
4. **`lang` default** (`build-blocks.py:309`): currently `"zh" if ext==".epub"`.
   Decide whether `.mobi` defaults to `"zh"` or reads `lang` from the sidecar.
   Cosmetic.

## Part 3 — Frontend (cosmetic only)

- Book/article classifier already matches `mobi|azw`
  (`src/pages/sources/index.astro:17`). No change.
- "Read →" is gated only on `sourceHasBlocks` — appears automatically once
  blocks.json exists. No change.
- Upload `<input type="file">` has **no `accept=` filter**
  (`src/pages/index.astro:102`), so `.mobi` is not blocked functionally. But
  the picker **label** reads "Choose a file (PDF, md, epub, audio)" — update
  the copy to mention mobi/ebooks so the format isn't invisible to users.
  One-string change.
- **Title-cleaner regex** `\.(epub|pdf|md|txt)$` at
  `src/pages/sources/[id]/read.astro:35` and
  `src/pages/sources/[id]/read/[chapter]/index.astro:43` — add `mobi|azw3` so
  the display title isn't `mybook.mobi`. Two-token change, two spots.

## No change

`source-identity.py`, `ingest.py`, `ingest_runner`, `promote`, `study`/FSRS,
`annotations`, source-reader anchors/chapters — all keyed on `source_id` +
block ids, format-blind.

## Testing

- No existing unit test covers `extract.py`'s format paths (only e2e via
  `test_ingest_e2e.sh` with a single source). `build-blocks.py` has none.
- Unit tests stub `mobi.extract` to return converted HTML and assert extracted
  text contains a known `## ` heading (ingest), assets route to the original
  source's `.assets` directory, and `build-blocks.py` emits >0 blocks (reader).
- The reader keeps the best-effort posture for a missing `mobi` import. Ingest
  declares the pinned inline dependency, so `uv run --script extract.py` installs
  it automatically.

## Decisions to confirm before coding

- **D1 — License.** `mobi` is GPL-3.0-only. The project already treats AGPL
  `pymupdf` as opt-in-behind-env-var (`extract.py:494`). Options:
  (a) hard dep in both extractors [recommended — it's a dev-time extraction
  tool, not linked into a distributed binary];
  (b) opt-in behind an env flag like pymupdf;
  (c) shell out to calibre `ebook-convert` instead (also GPL-3, but a separate
  process → no linking concern; downside: heavy, not pip-installable, needs a
  preflight `shutil.which` check).
- **D2 — Idempotency.** Pin `mobi` exactly and note the breaking-change risk:
  a version bump could shift extracted image bytes → asset re-hash → churn
  (same class as the pinned pillow/pdfplumber note).
- **D3 — Print Replica.** MOBI that converts to `.pdf`: reuse `extract_pdf`
  (threads `assets_dir` through two pdf helpers, `extract.py:538/746`), or skip
  in v1 with a warning [recommended for v1].
- **D4 — Shared conversion helper?** Both extractors get a ~5-line
  `mobi.extract` shim. Keep them duplicated (mark with `# ponytail:`): the two
  epub parsers are independently designed (build-blocks deliberately avoids
  third-party deps), and coupling them for five lines is not worth it.

## Change checklist

- [x] `extract.py`: PEP-723 dep (pinned) + comment
- [x] `extract.py`: `extract_mobi` + `dispatch` branch
- [x] `extract.py`: `assets_dir` param on `extract_epub` / `extract_html_file`
- [x] `extract.py` + `README.md` + `schema.md`: docs
- [x] `build-blocks.py`: `extract_mobi` + dispatch branch
- [x] `backend/requirements.txt`: `mobi`
- [x] shared `cleanTitle`: title regex for source reader pages
- [x] `index.astro:102`: upload picker label copy
- [x] stubbed-converter tests (ingest heading, asset routing, reader block count)
