# Source Reader + Annotations — design

Read original source material in the web UI and attach notes to exact places
inside it (paragraph / section / page / quote / selection), with bidirectional
links between the source, your notes, the LLM's synthesis claims, and wiki
entities/topics.

## What I'd change vs the first draft (the optimizations)

Your draft is the right shape. Six upgrades make it sturdier and more modern:

1. **Content-hash block anchors, not positional numbering.** `chapter-1-p-003`
   rots the moment a re-ingest inserts a paragraph. Instead give each block a
   short **hash of its normalized text** (scoped to its section): `p-9f3a2c`.
   The anchor survives edits elsewhere in the chapter; keep an ordinal only as a
   human-readable fallback (`第二章 ¶3`).

2. **Adopt the W3C Web Annotation model (the Hypothesis approach).** Your
   "robust selection" instinct is exactly right — this is its proven form. Store a
   `TextQuoteSelector` (exact quote + prefix/suffix context) **and** a
   `TextPositionSelector` (char offsets). The client re-anchors a note by fuzzy
   quote-match, so notes stick even if the source text is regenerated slightly.

3. **One addressing scheme for everything.** Unify the reader anchors, the wiki's
   existing `[src:id#…]` citations, and annotations around a single **resolver key**
   (see "Fragment / anchor grammar" below). Today's chapter anchors (`#第二章`) stay
   valid as a *coarse* target (scroll to the section's first block); a *fine* target
   adds the block hash + disambiguation tuple so it lands on one exact occurrence.
   No migration; old citations keep working, new ones can be sharper.

4. **Storage split that matches the app.** Annotations are personal + mutable →
   **backend SQLite** (like the word bank), never the vault git. Full readable
   document text is a **local generated build artifact** under
   `vault/.blocks/<source_id>.blocks.json`, not committed to the content repo.
   Optional "**promote to note**" writes a chosen annotation into a wiki page's
   `human-zone` when you want it versioned in the vault.

5. **One unified "marginalia" surface, not separate lists.** A single right rail
   merges *your annotations* + *AI citations that land on this passage* + *links*,
   with filter chips (All · Mine · AI · Links). That collapses your items #3 and
   #4 into one elegant panel.

6. **Name the enabling dependency honestly.** Documents (epub/mobi/pdf/url) have no
   committed readable-text artifact by design. The critical path is the local
   sync/build step: `scripts/build-blocks.py` extracts epub/mobi/pdf sources into
   `vault/.blocks/` before Astro bakes those blocks into the static reader. Once
   local blocks exist, the reader + annotations are straightforward.

## Architecture

```
ingest ──► PW_CONTENT_DIR/sources/*   (committed source assets + sidecars)
        │
        ▼
sync ──► vault/ ──► scripts/build-blocks.py ──► vault/.blocks/<source_id>.blocks.json
        │              normalized blocks: {id: content-hash, type, text, page?, section, order}
        ▼
Astro builds a STATIC reader page (blocks baked in)
        │  annotations fetched from backend + overlaid client-side
        ▼
FastAPI backend ── annotations table (W3C-shaped) + re-anchor resolver
```

- Static reader (blocks baked) + client-side annotation overlay = same static/
  backend split as the word bank. No SSR.
- Media transcripts and image-note OCR cards map into the **same block shape**, so
  one reader handles text, audio/video (timecoded blocks), and image notes.

## Data model (annotations, W3C-aligned)

```json
{
  "id": "an_01H…",
  "source_id": "01KQD4EYT6…",
  "target": {
    "block_id": "p-9f3a2c",
    "selector": { "quote": "质子梯度提供了另一条起源路线",
                  "prefix": "…独立演化的判断。", "suffix": "。碱性热液与…",
                  "start": 1423, "end": 1437 }
  },
  "body": "关键：膜先于细胞", "color": "amber",
  "tags": ["mechanism"],
  "links": [{ "type": "entity", "target": "质子驱动力" }],
  "created": "…", "updated": "…"
}
```

## Interface (modern + friendly)

- **Three panes**: a slim left **outline/TOC** with reading progress; a centered
  **reading column** (~62–70ch, your serif) that's calm and book-like; a
  collapsible right **marginalia rail**.
- **Selection popover** (Medium / Hypothesis style): select text → a small
  floating toolbar appears — highlight color, 💬 comment, 🔗 link to entity/topic,
  copy quote, ✦ ask AI. Inline, no modal.
- **Inline highlights** in soft colors; hovering shows the note in a hovercard;
  clicking focuses its card in the rail (and vice-versa).
- **Deep-linking**: `/sources/<id>/read#<anchor>` scrolls to the resolved block and
  pulses it (see the fragment grammar for how a shared block hash is disambiguated);
  a citation chip on a wiki page opens the reader focused there with the citing
  claim pinned at the top of the rail.
- **Per source type**: text → paragraphs; audio/video → timecoded transcript with
  a play head; image notes → card image beside its OCR text. Same annotation UX.
- **Keyboard-first**: `h` highlight, `c` comment, `j/k` next/prev annotation, `⌘K`
  palette. Graceful empty state when a source has no blocks yet ("Re-ingest with
  text extraction to read this here").

## Phasing

- **P1 (MVP)** — text/lang sources: chapter+paragraph blocks, highlight + comment,
  deep-link from citation chips, marginalia rail (your annotations).
- **P2** — unify the rail: AI-citation backlinks per block + links to entities/topics.
- **P3** — W3C fuzzy re-anchoring + "promote to human-zone".
- **P4** — media/image readers, command palette, AI-assist (explain/summarize a
  selection).

Everything stays local-only; reading full source text on the private site is fine
under the same "content stays on your machine" constraint.

---

## Decisions (locked)

- **Start UI-first** against a blocks contract. The `lang` source (Little Prince,
  which already has committed text) is the live test bed; the DailyNotes
  blocks-emitter for epub/pdf comes right after.
- **Annotations in backend SQLite**, with an opt-in **"promote to note"** that
  writes a chosen annotation into a wiki page's `human-zone` (versioned in the
  vault only when you ask).
- **Anchor grain = quote + block; store enough to fuzzy-re-anchor later, but P1
  resolves by EXACT match.** Persist a content-hash block id, a stable `section_id`,
  the neighbouring block ids, and a W3C quote selector (quote + prefix/suffix +
  offsets). P1 uses exact quote/context matching only; fuzzy re-anchoring is P3.

---

## Contracts (frozen before building)

### Blocks — generated `blocks.json`
```json
{ "source_id": "01…", "title": "…", "lang": "ja",
  "blocks": [
    { "id": "p-9f3a2c", "type": "paragraph",
      "section_id": "s-2", "section": "第二章",
      "order": 3, "text": "…", "page": null }
  ] }
```
- **Block `id` hashes only the block's own content** — `p-` + first 8 hex of
  `sha256(type \x1f normalized_text)`. It does **not** include section or position,
  so inserting a heading or paragraph earlier never changes any block id below it.
- `section_id` is **non-positional** — `s-` + first 8 hex of
  `sha256(normalized heading-TEXT ancestry)` (the heading-label path, e.g.
  `第二章` → its own hash; nested → parent-hash/child-hash). It is used only for
  grouping + disambiguation, never in the block hash; `section` is display text
  only. (If two headings share identical text, their `section_id` collides —
  acceptable; quote/context still disambiguates.)
- Identical paragraph text therefore **shares a block id** (even across sections) —
  expected, and NOT disambiguated by a positional `-2`/`-3` suffix. A `block_id` is
  **potentially ambiguous**; `quote` + `prefix`/`suffix` + `context` (neighbour
  block ids) + `section_id` resolve *which* occurrence. `order` is display-only.
- **Lang (P1):** derived at build from the source's `reading.json`
  (`chapters[].paragraphs[]`); no new committed artifact yet.
- **Canonical `block.text` (frozen):** one block per `reading.json` paragraph, its
  text = the paragraph's sentence `jp` strings joined with **no separator** —
  `sentences.map((s) => s.jp).join('')`. This exact string is what gets rendered,
  hashed, quoted, and offset-indexed. **No re-normalization at render time** — the
  rendered DOM text, the selected quote, `normalized_text` for the hash, and any
  future Python rebuild must all produce this identical string, or offsets/quotes
  drift. (`normalized_text` for the *hash* may trim/collapse whitespace, but the
  rendered/quoted/offset string is the raw canonical text.)
- **Documents (P2/current):** `scripts/build-blocks.py` emits
  `vault/.blocks/<source_id>.blocks.json` during local sync/build. It is
  regenerated from committed source assets and sidecars, but the extracted full
  text itself is not committed.
- **Media / image:** transcript segments (with `time`) and OCR cards map into the
  same shape.

### Annotation — backend row / API body
```json
{ "id": "an_01…", "source_id": "01…",
  "target": {
    "block_id": "p-9f3a2c", "section_id": "s-2",
    "context": { "prev_block_id": "p-1c88de", "next_block_id": "p-7a0f2b" },
    "selector": { "quote": "…", "prefix": "…", "suffix": "…", "start": 1423, "end": 1437 } },
  "body": "…", "color": "amber", "tags": ["mechanism"],
  "links": [{ "type": "entity", "target": "质子驱动力" }],
  "created": "…", "updated": "…" }
```
**Offset coordinate system (must be explicit — selection is JS, storage is
Python):** `start`/`end` are **block-local, UTF-16 code-unit offsets** (native JS
`String` indices from the browser Range). The backend stores them as opaque
integers and never re-computes them; the client is the single source of truth for
offsets. `quote`/`prefix`/`suffix` are the portable, language-agnostic anchor;
offsets are only a tie-breaker/scroll hint. `section_id` + `context` (neighbouring
block ids) disambiguate duplicate-hash blocks — the resolver has the data it needs.

### Endpoints (backend)
`POST /annotations` · `GET /annotations?source_id=` · `PATCH /annotations/{id}` ·
`DELETE /annotations/{id}`. **All annotation routes require `PW_AUTH_TOKEN`,
reads included** — these are private notes, more sensitive than vocab rows.
Critically they must **fail closed**: the shared `require_auth()` is a no-op when
the token is unset (fine for open reads elsewhere), so annotations get a stricter
`require_configured_auth()` that returns **503** when `PW_AUTH_TOKEN` is unset —
never serving notes on an unauthenticated backend. (The reader sends the stored
`backendToken` on GET; if the backend is misconfigured, the reader shows a
"set a backend token to use annotations" state rather than failing silently.)
Later: `GET /annotations/for-page` (wiki backlinks) and
`POST /annotations/{id}/promote` (write to `human-zone` via the ingest control
plane's git access).

### Citation deep-links (P1 — includes `remark-inline.mjs`)
Today `[src:id#anchor]` resolves to `/sources/<id>` and uses the anchor **only as
the chip label**. To deep-link, the citation transformer changes the URL to the
**reader route with a fragment**: `/sources/<id>/read#<anchor>`.
- `<anchor>` follows the **Fragment / anchor grammar** below. This is why
  `remark-inline.mjs` is in the P1 file list even though the current content only
  exercises it fully in P2 (documents become readable then; lang sources aren't
  wiki-cited).

### Fragment / anchor grammar (resolves the shared-hash ambiguity)
A `block_id` can be shared by duplicate paragraphs, so a bare `#p-…` is **not**
guaranteed unique. Three fragment forms, one resolver:
- **Coarse** — `#s=<section_id>` (or a legacy chapter key): scroll to the **first
  block of that section**. Used by today's chapter citations.
- **Bare block** — `#p-<hash>`: scroll to the **first matching block** only
  (best-effort; explicitly not guaranteed unique). Fine for casual links.
- **Precise** — the permalink form that carries the same disambiguation tuple an
  annotation stores:
  `#b=<block_id>&s=<section_id>&prev=<prev_block_id>&next=<next_block_id>&q=<quote_sha8>`
  The reader runs the **same duplicate-resolver used for annotations** (match
  `section_id` + neighbour `prev`/`next` + quote hash) to pick the exact occurrence,
  scrolls, and (if `q` present) highlights the quote. "Copy link to highlight"
  emits this form.
- **Citation-index anchors** store either the coarse `s=<section_id>` (default,
  chapter-level today) or the full precise tuple when a fine block citation exists —
  never a lone `block_id`, so the rail/index never points at an ambiguous block.

### Client re-anchoring
- **P1 (exact + disambiguation, no fuzzy):** find candidate blocks by `block_id`
  hash; if more than one occurrence, pick the one whose surrounding text matches
  the stored `quote` + `prefix`/`suffix` + `context` + `section_id`. Inside the chosen block,
  locate `quote` by exact substring; wrap in `<mark>`. Anything that doesn't
  resolve exactly goes to an **orphaned-notes tray** to re-attach in one click.
- **P3 (fuzzy):** approximate matching (prefix/suffix similarity, offset-drift
  tolerance) for notes whose quote changed slightly across a re-ingest. Kept out of
  P1 on purpose so the MVP doesn't ship a whole fuzzy-match engine.

---

## Files touched (P1)

- `src/lib/vault.mjs` — `blocksForSource(id)` (derive blocks from the reading doc).
- `src/pages/sources/[id]/read.astro` — the reader + annotation client (new).
- `src/pages/sources/[id].astro` — add a "Read →" action when blocks exist.
- `src/plugins/remark-inline.mjs` — citation chips → `/sources/<id>/read#<anchor>`.
- `backend/app/db.py` + `backend/app/main.py` — annotations table + endpoints
  (auth on all annotation routes).

## Phases & milestones

- **P1 — Lang reader + annotations** (UI-first). Read the Little Prince source with
  stable block anchors; select → popover (highlight color / comment / link / copy);
  inline highlights + marginalia rail; save/fetch/delete to the backend (auth on
  all routes); citation chips point at `/sources/<id>/read#<anchor>`; deep-link
  scroll-and-pulse; **exact** quote match with duplicate disambiguation + an
  orphan tray (no fuzzy engine). _Milestone: annotate the source; notes persist and
  re-attach exactly after a rebuild._
- **P2 — Documents readable + AI-citation index.** Local sync/build emits
  `vault/.blocks/<source_id>.blocks.json` for epub/mobi/pdf sources (the Nick Lane book
  becomes readable); citation chips deep-link to the exact block; and a
  **build-time citation index** is added
  so the rail can show AI claims per block. Contract:
  `{ source_id, anchor (block_id or coarse chapter), wiki_href, claim_excerpt }`,
  extracted at build by **walking the wiki mdast** (not regex on raw text) to find
  each `[src:id#anchor]` and the text of its **enclosing block node** — which may be
  a paragraph, list item, blockquote (the `[!AI]` callouts), heading, or table
  cell. Citations appear in all of these, so the excerpt logic keys off the mdast
  ancestor block, not "paragraph". (Today's metadata only stores source IDs, not
  positions or claim text — this index is new work, not free.)
- **P3 — Promote + entity links.** "Promote to `human-zone`"; annotation → entity/
  topic links surface as backlinks on wiki pages.
- **P4 — Media/image readers, command palette, AI-assist** (explain/summarize a
  selection).

## Privacy / deploy guard (finding)

`blocks.json` bakes **full source text** (entire books) into the static `dist/`.
That's fine while `dist/` is only ever served locally / behind Tailscale — the
same "content stays on your machine" rule as the rest of the vault. Before any
non-local deployment it is **not** fine (copyright + privacy). Guard it without adding friction to the normal local workflow:
- **Local builds are unaffected.** The default `npm run build` (site is localhost
  and `PW_PUBLIC_BUILD` is unset) emits full text as today — zero extra flags.
- **Public builds must opt in explicitly.** A build marked `PW_PUBLIC_BUILD=1`
  emits full-text blocks **only** if `PW_ALLOW_FULL_TEXT=1` is also set; otherwise
  it ships block *structure* + anchors without body text (or fails with a clear
  message). So the gate fires exactly on public targets, never on your machine.
- A prominent README/deploy warning stating that `dist/` contains full source text
  and must not be published without the flag.

## Resolved by review

- **Auth**: all annotation routes (incl. reads) require `PW_AUTH_TOKEN` and
  **fail closed** (`require_configured_auth()` → 503 if the token is unset).
- **Offsets**: block-local UTF-16 (JS) code units; backend stores opaque ints.
- **Duplicate blocks**: `block_id` may be ambiguous; resolve by quote + `context`
  (neighbour block ids) + `section_id`, never by positional suffix.
- **Section identity**: stable `section_id` from heading ancestry; `section` label
  is display-only.
- **Fuzzy anchoring**: P3 only; the locked decision now says "store enough to
  fuzzy-re-anchor later, P1 is exact-match + orphan tray".
- **Full-text guard**: local builds unaffected; only `PW_PUBLIC_BUILD=1` targets
  need `PW_ALLOW_FULL_TEXT=1`.
- **Block identity**: hashes `type + normalized_text` only — nothing positional or
  section-scoped touches a block id.
- **Canonical `block.text`**: `sentences.map(s=>s.jp).join('')`, frozen; no
  render-time re-normalization.
- **Fragment grammar**: coarse (`#s=…`), bare (`#p-…`, first match), or precise
  (`#b=…&s=…&prev=…&next=…&q=…`) resolver key; citation-index anchors never store a
  lone `block_id`.
- **Doc hygiene**: the top "optimizations" + UX sections now match the frozen
  contract (content-only hashes, `/sources/<id>/read#<anchor>`).
- **Citations**: `remark-inline.mjs` is in P1; chips → `/sources/<id>/read#<anchor>`.
- **Reader route**: keep `/sources/[id]` as provenance; reader is
  `/sources/[id]/read` with a "Read →" button.
- **Coarse anchors**: chapter anchor → first block of that section until P2.

## Still open before P1

1. **Highlight color semantics** — fixed meanings (e.g. amber = note, sage =
   question, terra = important, so the rail can filter by intent) or freeform
   colors? _This is the last decision I need to start P1._
