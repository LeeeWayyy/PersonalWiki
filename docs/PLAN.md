# Personal Learning Website — Implementation Plan

Turning the **DailyNotes** LLM-wiki vault into a private personal website, hosted on
your Mac and reachable only by you — built on **Astro** with a bespoke,
**modern-dashboard** design, an **in-page ingest console** (add local files or
remote links and run the pipeline from the site), and a **Miraa-style
language-learning module** (immersion reader + a personal word & grammar bank).

_Status: draft plan for review — 2026-07-05_

_Update: local-folder ownership is now captured in
[`LOCAL-WIKI-FOLDER-ARCHITECTURE.md`](./LOCAL-WIKI-FOLDER-ARCHITECTURE.md). Treat
older `content/ submodule` wording below as the default local fallback, not as a
hard requirement._

---

## 1. What we're building, and the constraints that shape it

DailyNotes is not an ordinary notes folder. It is the **tooling** for an
LLM-maintained knowledge base (Karpathy's "LLM Wiki" pattern): a Python ingest
pipeline reads sources — books, articles, YouTube transcripts, image notes,
language chapters — and an LLM synthesizes them into wiki pages. The actual
content lives in a separate, deliberately **local-only** `content/` git submodule
that has no remote and is never pushed. The pages are Markdown with a rich,
bespoke format:

- **Provenance citations** `[src:<id>#anchor]` that resolve to source sidecars,
  not to other pages. Anchors come in free-form (`#第一章`, `#§3`) and structured
  (`#H:MM:SS-H:MM:SS` for transcripts, `#card-N`, `#frame-N`) flavours.
- **Two zones per page** — `<!-- human-zone -->` (your notes, LLM never touches)
  and `<!-- llm-zone -->` (LLM synthesis), the latter wrapped in an Obsidian
  `> [!AI]` callout.
- **`[[wikilinks]]` with aliases** across English + 中文 (每页单语，源语言决定语言).
- **MOC index pages** (`wiki/_index/`, one per tag) and **argument-map pages**
  (`wiki/_maps/`, Mermaid `flowchart` + reading-guide), both with regenerated and
  human-preserved zones.
- **Conflict markers** `==CONFLICT: …==` inline on affected pages.
- A **tag taxonomy** (`wiki/_taxonomy.md`) with lint-enforced syntax and
  cardinality.
- An isolated **language-learning subtree** (`lang/`) with study / vocab / grammar
  pages.

Today the vault is small — one entity (`ATP`), a set of MOCs, a Nick Lane EPUB
source, and a Japanese _Little Prince_ study set — but it is designed to grow
continuously through the ingest pipeline. **The website must render the format,
not a one-time snapshot**, and it must rebuild cleanly every time content changes.

### Your decisions (from our kickoff)

| Decision | Your choice | Consequence for the plan |
|---|---|---|
| Privacy / hosting | Host on the Mac, reachable **only by you** | Static site served locally, exposed privately via Tailscale — never on the open web. Honors the vault's "content stays local" rule. |
| Build approach | **Astro, custom-designed** | Bespoke build (not Quartz). Our citation/zone renderers slot into Astro's remark/rehype pipeline; full control of design. See §2. |
| Visual direction | **Modern dashboard** | Card-driven, denser, dark-mode-first, app-like. Dashboards (sources shelf, conflicts, freshness) are first-class, not afterthoughts. See §3. |
| Must-have features | Citations, AI/human zones, wikilinks + graph/backlinks, mindmaps + MOCs + search, "anything else worth building" | Covered in §3 and §7. |
| **Operate ingest from the page** | Add a local file **or** remote link and run the pipeline in-browser | Requires a small **local backend** (an ingest control plane) alongside the static site. See §2 and the new §2a. |
| **Language learning** | Miraa-style reader for the source text + save important words/grammar for future reference | A dedicated immersion-reader view + a persistent, cross-source word/grammar bank on the backend. See the new §3a. |
| Project location | `personal_wiki` folder | The Astro site lives here; it reads content from the DailyNotes vault. Content and tooling stay cleanly separated. |

---

## 2. Recommended architecture

**Build a bespoke [Astro](https://astro.build/) site in `personal_wiki`, reusing
proven vault-handling plumbing but with our own design system; pair it with a
small local backend that runs the ingest pipeline; serve both locally and reach
them privately through Tailscale.**

Because you want to _operate_ the vault from the page (not just read it), the site
is no longer purely static. It becomes a **static reading front-end + a local
control-plane backend**: the front-end is the fast Astro build; the backend is a
small always-on service on the Mac that runs `ingest.py`, streams progress, and
rebuilds the site when content changes (detailed in §2a).

### Why Astro (custom) rather than an off-the-shelf theme

You want a distinctive, design-forward result — so a stock template (Quartz, or a
theme used as-is) is out. Astro is the right foundation because:

- It's **content-first and static** — Markdown/MDX content collections compile to a
  fast static site, which is exactly the shape of a growing vault.
- Its **remark/rehype transformer pipeline** is where our vault-specific syntax
  lives — the `[src:]` citation resolver, the zone handling, the MOC/map passes are
  first-class plugins, not hacks.
- **Full design control** — no fighting a theme's opinions. We build a Tailwind
  design system for the modern-dashboard look you picked.
- **The pieces we'd otherwise get "for free" all have strong Astro-native answers:**
  Pagefind for instant static search (and better CJK handling than Quartz's
  default), a force-directed graph view we render ourselves, and standard remark
  plugins for wikilinks/backlinks.

To avoid rebuilding the boring plumbing, we start from the vault-handling core of
the **Astro "Spaceship" / digital-garden** ecosystem (Obsidian-style links, image
embeds, backlinks) and then replace its presentation entirely with our own design.
We keep their solved problems; we throw away their look.

### The adapter layer (vault-specific rendering)

1. **`[src:]` citation transformer** — parse `[src:<id>#anchor]`, resolve `<id>`
   against a build-time index of `sources/*.md` sidecar frontmatter
   (`source_id → title, path, type`), and render each as a clickable citation chip
   into the source page, with a hover preview and the anchor shown as
   section/timestamp provenance. Anchors are **capability-scoped** (schema §4):
   `#H:MM:SS-H:MM:SS` (transcripts), `#card-N`, `#frame-N` resolve against the
   source's `.cards.json` / `frames.json` audit artifacts (ordinals, not
   `1≤N≤count`); free-form `#§3` / `#第一章` are informational. Degrade gracefully on
   unknown sources.
2. **Zone handling + two-tier structure** — the `<!-- llm-zone -->` /
   `<!-- human-zone -->` HTML comments become distinct components (styled "AI
   synthesis" panel keeping the `[!AI]` intent; human zone as your notes, with a
   show/hide toggle). Critically, once a page cites **≥2 sources** the llm-zone is
   **not one block** but a rolling `### Synthesis` followed by append-only
   `### From src:<id>#<label>` **Evidence** sections (schema §3) — the adapter must
   render these as distinct sections, not flatten them.
2b. **Obsidian embeds (`![[...]]`) — a custom pass, not the off-the-shelf plugin.**
   Schema §14 mandates transclude syntax `![[sources/<asset>.assets/<file>]]` for
   images and **forbids** standard `![alt](path)` in llm-zone, forbids page
   transcludes `![[Page]]`, and forbids embedding images flagged `decorative:true`
   in the `_manifest.md`. Default digital-garden wikilink/embed plugins would render
   `![[...]]` as page transclusion — exactly what the schema forbids — and won't
   resolve `.assets/<sha12>.<ext>` paths or honor the decorative flag. So this is a
   **bespoke remark pass**, not a freebie.
3. **Source pages** — generate readable pages from `sources/*.md` sidecars so
   citations have a destination, and power the **sources shelf** (§7). Media
   sidecars (transcript / cards) link to their canonical artifact and surface cover
   images from the `.assets/` folders.
4. **MOC & argument-map passes** — `_index/` and `_maps/` pages render as dashboard
   views (MOCs) and Mermaid diagrams (argument maps), stripping the "DO NOT
   hand-edit" scaffolding while keeping human-zone commentary.
5. **Content sync step** — a build script copies (or symlinks) the vault's `wiki/`
   — plus selected `sources/` and the `lang/` subtree — into the Astro content
   directory. Nothing is pushed anywhere; the built output stays on the Mac.
6. **Bilingual support** — set `lang` attributes for CJK, ship a CJK-capable font,
   and use Pagefind (which tokenizes Chinese far better than Quartz's default).

### Private hosting on the Mac

- **Serve** the static build with a tiny local server (Astro preview, or Caddy /
  `http-server`) bound to localhost, plus the FastAPI ingest backend (§2a) on a
  neighbouring local port — both fronted by Tailscale.
- **Reach it only from your own devices** via **Tailscale** — MagicDNS +
  Tailscale Serve gives you an HTTPS URL that only your tailnet can hit. No open
  internet exposure, so the local-only vault constraint holds. _(Alternative if
  you later want a real domain + an email-gated login screen: Cloudflare Tunnel +
  Cloudflare Access. Tailscale is the simpler default.)_
- **Keep it running & fresh** with a `launchd` agent for the server, plus a
  **rebuild-on-ingest hook** so the site regenerates whenever the vault changes
  (a `scripts/` target in DailyNotes, or a git hook on the `content/` submodule).

### Data flow

```
             ┌─────────────────────  in-page ingest console  ─────────────────────┐
             │  upload file  /  paste URL  +  options (kind, section, lang)         │
             ▼                                                                      │
   Astro front-end ──HTTP──► ingest control plane (FastAPI, local) ──► ingest.py ──►│
   (static, dashboard)  ◄──SSE live logs──          │                              │
        ▲                                            └──► content/ submodule (local-only)
        │                                                        │
        │                               (sync: copy/symlink wiki/, sources/, lang/)
        │                                                        ▼
        └──────────── astro build (auto, post-ingest) ◄──────────┘
                              │
                    static site + API ──► local server ──► Tailscale ──► only your devices
```

---

## 2a. The ingest control plane (operating the vault from the page)

The DailyNotes pipeline is Python (`ingest.py` + `scripts/`), calls an LLM, and for
media delegates to an external ASR service — so the cleanest backend is a **small
Python service (FastAPI) that wraps the existing pipeline**, rather than
reimplementing anything. The Astro front-end talks to it over local HTTP.

**What the in-page console does**

- **Add by file** — drag-and-drop or pick a local file (EPUB, PDF, image note,
  audio/video). The backend stages it and runs the matching ingest path.
- **Add by link** — paste a URL (article, YouTube, podcast). The backend picks the
  media/URL front-door (`media-identity.py` / `ingest.py --kind`).
- **Options** — expose the flags the pipeline already supports: source `--kind`,
  `--section-label` (e.g. `#第二章`), the isolated `--profile lang` path, `--reocr`,
  frames, supersede. Sensible defaults; advanced options tucked away.
- **Watch it run** — the backend runs the job asynchronously and streams stdout /
  step progress back to the page over **Server-Sent Events** (sidecar → text →
  keyword pre-pass → LLM diff → lint → commit). You see lint failures or conflicts
  live.
- **Auto-refresh** — on a successful commit, the backend runs the sync + `astro
  build` and the page shows the new/updated wiki page.

**Backend shape (FastAPI)**

- `POST /ingest` — multipart file **or** JSON `{url, options}` → returns a `job_id`.
- `GET /jobs/{id}/events` — SSE stream of progress + logs.
- `GET /jobs/{id}` — final status, resulting page(s), any `==CONFLICT:==` raised.
- `POST /rebuild` — force a site rebuild. `GET /sources`, `GET /conflicts`, … back
  the dashboards in §7 with live data instead of build-time snapshots.
- Runs one ingest at a time (a simple job queue); the vault is a git repo, so
  serialized commits avoid races.
- **Also hosts the personal word/grammar bank + SRS** (§3a) in a small **SQLite**
  store: `POST /vocab`, `PATCH /vocab/{id}` (status), `GET /review/queue` (due
  cards), `POST /review/{id}/grade` (FSRS scheduling: again/hard/good/easy),
  `GET /review/stats`, `GET|POST /export` (Anki/CSV), and `POST /translate`
  (on-demand sentence translation via the LLM). This store is separate from the
  vault git history — it's your personal study state, not wiki content.

**Operational realities the backend must handle (grounded in `ingest.py`).**

- **Preflight refuses a dirty tree.** `ingest.py`'s `preflight()` hard-exits
  (`sys.exit(1)`) if the git index is non-empty or `wiki/`/`sources/`/`.wiki/log.md`
  have local changes, or stale `*.assets/` untracked files exist. So a single
  failed/aborted ingest can leave `.rejected` / `.failed.N` / `.apply-err.N`
  artifacts and a dirty tree that **blocks every subsequent job**. The backend must
  (a) run a "tree clean?" check before accepting a job and report a **blocked**
  state naming the offending paths, and (b) detect/surface leftover artifacts with a
  recovery action — not just replay logs.
- **One lock covers ingest + sync + rebuild.** The site sync/rebuild reads (and may
  `git submodule update`) the working tree, which would trip preflight if it races
  an ingest. Serialize all three under a single lock, not just "one ingest at a
  time." (We already hit a `.git/index.lock` permission error in this environment —
  under `launchd` with a different uid, git lock/permission failures are a live risk
  to budget for.)
- **Daemon LLM auth (an explicit Phase-0 spike, not a settled assumption).** Ingest
  calls the LLM through the shared provider (default `PW_LLM_PROVIDER=codex`),
  which needs credentials — and
  `codex` normally relies on interactive login state, which won't exist under a
  headless `launchd` daemon. Phase 0 must **verify** that `codex` runs
  non-interactively with only env-supplied credentials; if it can't, **swap to a
  custom `LLM_CMD` or API fallback**. Also expose a **per-job timeout + cancel**
  (the lang path alone uses a 900 s per-chapter LLM timeout).
- **Run the daemon as the login user, not root.** The observed `.git/index.lock`
  permission error is a uid/sandbox symptom; the `launchd` agent must run as your
  user so git lock/ownership matches the vault's.
- **Cost estimates are honest about their limits.** A wiki ingest spends LLM budget
  on a keyword pre-pass + a main diff call (+ up to one retry) **before** lint can
  reject the result — so a job can spend money and still fail. The dry-run/confirm
  can estimate **ASR** cost up front, but not LLM spend; say so in the UI.

**This is a genuine trust boundary.** Unlike a read-only static site, the console
**executes code, writes to your vault, makes git commits, and spends LLM/ASR
budget.** Safeguards:

- **Never exposed publicly** — bound to localhost, reachable only over Tailscale
  (your devices), same as the reading site.
- **A per-request auth token on every mutating route** (ingest, translate, reocr,
  supersede, review-grade), rate-limited and **never written to `.wiki/log.md`**.
  Tailscale ACLs are the real perimeter; the token stops a stray tailnet device from
  triggering paid actions.
- **Dry-run / confirm** for destructive or costly actions (media transcription,
  supersession); show the resolved identity + estimable (ASR) cost before running.
- **Every ingest is already a git commit** — so anything the pipeline does is
  reversible via the content submodule's history.

---

## 3. Design direction & how each feature renders

**Aesthetic: modern dashboard.** Dark-mode-first, card-driven, denser and more
app-like than a classic wiki. A persistent left rail for navigation (MOCs, tags,
sources, language study), a main reading column, and a right meta rail
(backlinks, source card, mini graph — as in the mockup). Landing page is a
dashboard: recently ingested, open conflicts, sources shelf, tag map.

| Vault feature | Rendering approach |
|---|---|
| `> [!AI]` LLM callout | Styled "AI synthesis" panel component |
| `<!-- llm-zone / human-zone -->` | Custom transformer → distinct panels + show/hide toggle |
| Two-tier `### Synthesis` / `### From src:…` (≥2 sources) | Rendered as distinct rolling + evidence sections (schema §3) |
| `![[…assets/…]]` image embeds | **Bespoke** remark pass (resolve `.assets/`, honor `decorative`, block page-transcludes) |
| `[src:<id>#anchor]` citations | Custom transformer → citation chip → source page, hover preview, capability-scoped anchor |
| Source sidecars → pages | Generated source pages + a **sources shelf** dashboard |
| `[[wikilinks]]` + aliases | remark wiki-link plugin + alias map |
| Backlinks | Computed from the link graph, shown in the right rail |
| Graph view | Custom force-directed graph (d3 / cytoscape / react-force-graph) |
| Full-text search | **Pagefind** (static, fast, CJK-friendly) with ⌘K palette |
| MOC `_index/` pages | Rendered as dashboard index views |
| Argument maps `_maps/` (Mermaid) | Mermaid render + reading-guide layout |
| `==CONFLICT:==` | Highlighted inline **and** aggregated into a conflicts dashboard (§7) |
| Tags + taxonomy | Tag explorer / MOC navigation |
| `lang/` study subtree | Dedicated self-study section → the reader + word bank in §3a |
| Bilingual EN/中文 | `lang` attrs + CJK font + Pagefind tokenization |

---

## 3a. Language-learning: immersion reader & personal word bank

**Reality check (verified against the repo, 2026-07-05).** The `--profile lang`
pipeline is **mid-refactor**, and the plan must build on its _current_ output, not
the older format. Specifically:

- The language pipeline now emits **one self-contained interactive
  `_reading/<slug>.html` plus a structured `_reading/<slug>.reading.json` per
  Japanese source** — native `<ruby>` furigana, per-sentence translation,
  click-a-word data, click-a-sentence grammar, and first-occurrence ("new word")
  highlighting.
- So **a Miraa-style reader already largely exists as a generated artifact.** My
  earlier framing ("we'll build the reader / add a tokenizer") was wrong — the
  pipeline already tokenizes (fugashi + unidic-lite) and renders.
- The old `_vocab`/`_study`/`_grammar` markdown pages are now compatibility
  fallback inputs, not the canonical `--profile lang` output.
- **Current scope is Japanese only** (fugashi/unidic). Other languages need a
  per-language tokenizer in the pipeline; treat non-Japanese as out of scope for v1.

### How the site consumes the reader

- **Option A — embed `_reading/*.html` as-is.** Zero rendering work, but it's a
  standalone page with its own CSS/JS: it won't match the modern-dashboard design
  and, crucially, **can't share state with the cross-source word bank / SRS** (its
  word status is per-page).
- **Option B (implemented) — re-render from the pipeline's structured data** in
  the site's own design and wire it to the word bank. The pipeline emits ordered
  tokens (lemma/reading/POS), per-sentence translation, per-word gloss, grammar
  points, and first-occurrence flags in the committed `.reading.json` sidecar.

Reader features once on Option B: readable text with furigana, tap-to-look-up
(reading/POS/meaning), inline grammar with example sentences, on-demand
sentence translation / AI explanation via the backend LLM, and LingQ-style
new/known word coloring driven by your word bank. For future **media** sources
(transcripts), timecode anchors (`#H:MM:SS-H:MM:SS`) enable shadowing — a later
enhancement once media ingest is live.

### Word ↔ entry identity (a real caveat)

The pipeline's dedup key is `(lemma, lForm, pos1)`, and its own docstring notes
unidic-lite "does not context-disambiguate homographs," so today it behaves like
`(lemma, pos1)` — one gloss per homograph class (書く vs 描く-type collisions). For
the **cross-source** word bank to match "the same word in a new book," we store a
**normalized lemma key** and accept homograph glosses as a known, documented
limitation (inherited from the tokenizer, not introduced by us).

### The personal word & grammar bank (save for future reference)

A cumulative, **cross-source** store of the words and grammar you've chosen to
keep — the durable reference you asked for. It lives in the backend's SQLite store
(§2a), not the vault git history, because it's your evolving study state.

- **Save from anywhere** — a "Save" action in any reader word popover or grammar
  card adds the item, recording its normalized lemma, reading, gloss, the source it
  came from, and the chapter anchor (so you can jump back to context).
- **Status per item** — new / learning / known; this also drives the reader's word
  coloring.
- **Review — full SRS.** Anki-grade spaced repetition (FSRS scheduler, with SM-2 as
  a fallback): per-card due dates, ease/interval/lapse tracking, review-grade
  buttons (again / hard / good / easy), daily new-card and review limits, decks
  (e.g. per source or per POS), and review-history stats. The flashcard UI fits the
  modern-dashboard aesthetic (front: word/sentence; back: reading + meaning + a real
  example from your reading). Two-way **Anki/CSV export** (and import) so it
  interoperates with an existing Anki setup.
- **One reference, many sources** — because it's keyed by lemma across sources, the
  same word seen in a new book links to what you already saved.

---

## 4. Respecting the "content stays local" constraint

This matters more than usual because the vault was explicitly designed never to
leave the machine. The plan preserves that:

- The website **reads** the vault; it never adds a remote to `content/` or pushes
  it.
- The Astro project in `personal_wiki` is its own thing. If you ever version it,
  version the **site code**, not the synced content (a `.gitignore` keeps synced
  vault content and the build output out of any repo).
- The only network exposure is Tailscale, scoped to your own devices. Nothing is
  ever served to the public internet.
- The sync step reads from the checked-out submodule working tree (the site build
  will `git submodule update --init` if needed).

---

## 5. Phased build

**Phase 0 — Reconcile, scaffold & first render.** First, **reconcile the moving
pieces in DailyNotes** (§3a): commit the rewritten `generate-language-pages.py`,
update `schema.md`, and decide the fate of the old `_vocab`/`_study`/`_grammar`
pages — we can't design the lang module against an uncommitted, contradictory
state. Because the reader's structured JSON sidecar (§3a, Option B) is a new
committed artifact, fold its `schema.md` / `lint.py` blessing into this same
reconciliation (those files are already being touched). Then initialize the Astro
project in `personal_wiki`, wire the content-sync
step and vault plumbing (wikilinks, backlinks), and get the `ATP` entity + MOC
pages rendering in a first dashboard shell. **Verify Pagefind CJK search here, not
later** — ~90% of current content is Chinese, so if tokenization is inadequate we
must know before building on it. _Milestone: the vault is browsable and searchable
locally; the DailyNotes lang refactor is settled._

**Phase 1 — Adapter layer.** Build the `[src:]` citation transformer + source
pages, the AI/human zone components **including the two-tier Synthesis/Evidence
structure**, and the **bespoke `![[…assets/…]]` embed pass** (resolve `.assets/`,
honor the `decorative` flag, block page-transcludes). _Milestone: every citation is
a working chip with a source preview; multi-source pages render their evidence
sections; images embed correctly; LLM vs human content is visually distinct._

**Phase 2 — Design system & rich views.** Implement the modern-dashboard design
(dark-first, cards, left/right rails), the custom graph view, Pagefind search with
a ⌘K palette, Mermaid argument maps, and verify bilingual CJK rendering + search.
_Milestone: it looks and reads like the intended product._

**Phase 3 — Ingest control plane.** Build the FastAPI backend wrapping `ingest.py`,
the in-page **ingest console** (file upload + URL + options), live SSE progress,
and auto-rebuild on success. This phase carries the operational load from §2a —
**preflight/blocked-tree detection, the single ingest+sync+rebuild lock, leftover
`.rejected`/`.failed` recovery, non-interactive daemon LLM auth, and per-job
timeout/cancel** — plus the per-request auth token and confirm/dry-run guards.
_Milestone: you drop a file or paste a link on the page, watch it ingest, and the
new wiki page appears — and a failed run leaves a recoverable, clearly-reported
state._

**Phase 4 — Language-learning module.** Have `generate-language-pages.py` emit its
already-computed reader data as a **committed structured JSON sidecar** (Option B,
§3a), render the immersion reader in the site's design from it (tap-to-look-up,
inline grammar, on-demand translation), and build the word/grammar bank + Anki-grade
SRS review dashboard backed by SQLite (with the normalized cross-source lemma key).
_Milestone: you read the Little Prince chapters interactively, save words, and
review them with spaced repetition._

**Phase 5 — Private serving & automation.** Stand up the local server + backend
behind Tailscale, add `launchd` agents so both stay running, and finalize the
rebuild hook. _Milestone: you open a Tailscale URL from your phone, read the wiki,
ingest new content, and study — all from anywhere, visible only to you._

**Phase 6 — Dashboards ("anything else").** See §7.

---

## 6. Integration with the ingest pipeline

The wiki and the website should stay in lockstep the same way the tooling and
content repos already do. Add a `make site` / `scripts/build-site.*` target in the
**tooling** repo that runs the sync + `astro build`, and have the ingest backend
call it after a successful commit (or use a filesystem watcher on the working tree).
**Avoid a git hook inside the `content/` submodule** — the README is emphatic about
the tooling/content split, and hooks living in the content repo blur it. That way a
fresh ingest is immediately visible on the site with no manual step, and the
rebuild is serialized under the same lock as ingest (§2a).

---

## 7. Dashboards worth building from this vault ("anything else")

The modern-dashboard direction makes these natural home-screen widgets, and the
vault's metadata makes them nearly free:

- **Sources library / shelf** — a browsable index of every source (books, videos,
  podcasts, image notes) from the sidecars, with cover art from `.assets/`, grouped
  by `origin_type`. The "what have I studied" landing view.
- **Provenance explorer** — from any source, list every wiki page that cites it
  (inverse of the citation index built in Phase 1).
- **Open-conflicts dashboard** — surface all `==CONFLICT:==` markers in one place
  so contradictions don't hide on individual pages.
- **Freshness / timeline** — order pages by `last_ingested` to see what's recent
  and what's gone stale.
- **Language-learning section** — present the `lang/` study/vocab/grammar pages as
  a dedicated self-study area, separate from the wiki.
- **Argument-map reading guide** — promote the `_maps/` flowcharts to a
  first-class "how to read this book" view.
- **Tag / taxonomy explorer** — browse the MOCs as a structured table of contents.

These are prioritized suggestions, not commitments — we'd pick from them in Phase 4.

---

## 8. Risks & open questions

- **Graph view build effort.** A custom force-directed graph is more work than a
  theme's built-in one; we budget for it in Phase 2 (a mature library like
  `react-force-graph` keeps it contained).
- **CJK search quality.** Pagefind handles Chinese far better than Quartz's default,
  but we verify tokenization early against real 中文 pages.
- **Citation-anchor coverage.** The `[src:]` transformer must handle all anchor
  grammars (free-form + `H:MM:SS` ranges + `card-N` + `frame-N`) and degrade
  gracefully on unknown sources.
- **Media & large assets.** EPUB image assets and (future) transcripts can be
  large; decide what the site copies vs links to.
- **Submodule checkout.** The build must ensure `content/` is checked out on the
  Mac (it is a gitlink; the sync step initializes it if absent).
- **Local-only guarantee.** Tailscale ACLs must be confirmed to expose the site to
  your devices only — this is the single most important safety check.
- **Ingest is a write/paid action (§2a).** The console runs code, commits to the
  vault, and spends LLM/ASR budget. Mitigations: Tailscale-only, an auth token,
  and confirm/dry-run for costly steps. Long-running jobs run async with live logs;
  ingests are serialized to keep git commits clean.
- **Backend lifecycle.** Two processes now stay resident (site server + ingest
  API); a `launchd` agent supervises both, and the API must locate the DailyNotes
  tooling + `content/` submodule reliably (absolute paths, as the pipeline already
  expects).
- **Lang pipeline is mid-refactor (§3a) — must reconcile first.** The rewritten
  `generate-language-pages.py` is uncommitted and contradicts `schema.md` + committed
  `lang/` content. The site consumes the pipeline's _current_ output (Option B: a
  structured JSON sidecar it already has the data for), but DailyNotes must be
  reconciled (commit script, update schema, retire old pages) in Phase 0 before we
  build the reader. Japanese-only for v1.
- **Homograph glosses / cross-source key.** The tokenizer's `(lemma, lForm, pos1)`
  key is homograph-blind (documented). The word bank stores a normalized lemma key
  and accepts one gloss per homograph class — a known, inherited limitation.
- **SRS engine.** Full Anki-grade review means a real scheduler (FSRS) and review
  bookkeeping in SQLite; more surface area than a simple queue, but well-trodden
  (reference FSRS implementations exist). Budgeted into Phase 4.
- **On-demand translation cost.** Sentence translation / AI explanation calls the
  LLM per request; cache results and gate behind the same auth as ingest.
- **Word-bank durability.** The SQLite study store is personal state, not vault
  content — back it up (it's a single file) and keep an export path so you're never
  locked in.

### Two things I'd want to confirm before Phase 0

1. **Node/Tailscale on the Mac** — Astro needs Node 20+; private access assumes
   Tailscale is (or can be) installed. Any preference otherwise (e.g. Cloudflare
   Access instead)?
2. **Human-zone visibility** — should your private `human-zone` notes render on the
   site (it's only you), be hidden by default with a toggle, or be omitted?
_(Language-module scope is settled: full Anki-grade SRS, and a reader manifest
emitted by `generate-language-pages.py`.)_

---

## 9. Suggested first step

If this direction looks right, Phase 0 is a self-contained, low-risk chunk:
scaffold the Astro project in `personal_wiki`, wire the sync step + vault plumbing,
and get the current vault rendering in a first dashboard shell. From there each
phase is independently shippable.
