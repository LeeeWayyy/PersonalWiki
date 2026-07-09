# Architecture Improvement Plan

Scope: current `personal_wiki` working tree.

This plan is intentionally about project architecture and layout, not new product
features. The current architecture is directionally sound: Astro builds a static
reader over a synced `vault/` snapshot, FastAPI owns private write paths and study
state, and the Python pipeline owns content generation. The main work is
to tighten boundaries, remove a few contract drifts, and make large UI/API
surfaces easier to change safely.

## Current Architecture Verdict

What is working well:

- The static-site plus local-backend split is the right model for a private wiki:
  read paths stay fast and cacheable, while ingest, annotations, study state, and
  LLM calls stay behind the local FastAPI service.
- Private and generated data are kept out of this app repo's git: the local wiki folder, `/content/`, `/vault/`,
  `/dist/`, `public/vault-assets/`, runtime SQLite files, venvs, and caches are
  ignored. CI builds against `ci-fixtures/content`, which is the right privacy
  pattern.
- The pipeline has a clear schema and a strong shell-test suite. Backend route
  tests cover the important local safety properties: fail-closed auth, preflight,
  upload limits, job cancellation, annotation validation, promotion, and study
  review paths.
- The source-reader and language-reader features are already cohesive at the
  product level. They now need implementation boundaries that match that product
  complexity.

What should improve:

- The browser app still has external runtime dependencies despite the
  "self-contained/private" project claim.
- Several modules have grown into broad ownership surfaces and are becoming hard
  to test in isolation.
- The source-reader block artifact contract has drifted between design docs and
  implementation.
- Shared browser concerns such as backend URL/token handling and API calls are
  duplicated across pages.
- Styling is split between useful global classes and a large amount of inline
  per-page styling, which makes consistency harder as the app grows.

## Findings

### P0 - External Runtime Calls Conflict With The Self-Contained Privacy Model

Evidence:

- `src/layouts/Base.astro` loads Google Fonts from `fonts.googleapis.com` and
  `fonts.gstatic.com`.
- `src/pages/wiki/[...slug].astro` imports Mermaid from jsDelivr at runtime.

Impact:

- Opening the private site can make third-party network requests and leak that
  the local wiki page was opened.
- Map rendering depends on external CDN availability.
- A stored backend token currently lives in `localStorage`; a same-origin script
  injection or accidental third-party script include would have a direct path to
  token exfiltration.
- This conflicts with the README's "self-contained" framing.

Recommended direction:

- Replace Google-hosted fonts with system font stacks or self-hosted local font
  files in `public/fonts/`.
- Add Mermaid as a local dependency and bundle it through Astro/Vite, or pre-render
  Mermaid diagrams at build time.
- Add a restrictive Content-Security-Policy and `Referrer-Policy` so the browser
  refuses external scripts, styles, fonts, images, and network connections even
  if a CDN link sneaks back into a template later.
- Add a lightweight CI/static scan that fails on unexpected `https://` runtime
  imports in `src/` and `public/`; the scan is a review aid, while CSP is the
  browser-enforced safety net.

### P1 - Backend Route Ownership Is Too Broad

Evidence:

- `backend/app/main.py` is 721 lines and owns app setup, auth, request parsing,
  ingest routes, vocab routes, review routes, export, translation, AI assist,
  annotation validation, annotation CRUD, and promotion.

Impact:

- Changes to unrelated API areas can conflict in one file.
- Route behavior and request validation are harder to reason about.
- FastAPI can provide stronger request models than the current ad hoc validation
  while preserving existing endpoints.

Recommended direction:

- Keep route URLs stable, but split by ownership:
  - `backend/app/settings.py`
  - `backend/app/auth.py`
  - `backend/app/routers/health.py`
  - `backend/app/routers/ingest.py`
  - `backend/app/routers/study.py`
  - `backend/app/routers/llm.py`
  - `backend/app/routers/annotations.py`
  - `backend/app/services/`
  - `backend/app/store/`
- Introduce Pydantic request/response models for the mutating JSON routes.
- Keep SQLite simple for now; do not add Alembic unless migrations become more
  complex than the current additive schema.

### P1 - Source Reader Page Is Doing Too Much

Evidence:

- `src/pages/sources/[id]/read.astro` is 670 lines and includes page rendering,
  page-specific CSS, annotation API calls, annotation re-anchoring, fuzzy matching,
  image-region drawing, rail rendering, promotion menus, AI assist, fragment
  resolution, command palette, and keyboard shortcuts.

Impact:

- The highest-complexity UI has very little isolated test surface.
- Small reader changes require editing a large Astro page with template, style,
  and browser logic mixed together.

Recommended direction:

- Keep the Astro page as a server-rendered shell.
- Extract browser logic into modules under `src/scripts/source-reader/`:
  - `api.ts`
  - `anchors.ts`
  - `annotations.ts`
  - `regions.ts`
  - `rail.ts`
  - `commands.ts`
- Move page CSS to `src/styles/source-reader.css`.
- Keep pure logic such as block resolution, quote matching, and fragment parsing
  free of DOM dependencies where possible.

### P1 - Source Block Contract Drift Should Be Resolved

Status: fixed immediately in this cleanup. The source-reader design doc and
reader empty-state copy now describe generated local `vault/.blocks` artifacts.

Evidence:

- [`SOURCE-READER-DESIGN.md`](./SOURCE-READER-DESIGN.md) previously described committed
  `sources/<slug>.blocks.json` artifacts.
- The implementation now emits generated local artifacts under
  `vault/.blocks/<source_id>.blocks.json` from `scripts/build-blocks.py`.
- `README.md` matches the generated `vault/.blocks` behavior.
- `src/pages/sources/[id]/read.astro` previously said documents become readable
  once the ingest pipeline emits `blocks.json`.

Impact:

- Future changes may target the wrong owner: ingest-time committed artifact vs
  local build-time generated artifact.
- Drift protection and privacy expectations are unclear for full source text.

Recommended direction:

- Prefer the current generated-local `vault/.blocks` design for privacy: full book
  text should not be committed unless there is a deliberate reason.
- Keep [`SOURCE-READER-DESIGN.md`](./SOURCE-READER-DESIGN.md), README, and reader
  empty-state copy aligned with the generated-local artifact choice.
- If drift detection is needed, store only small hashes/metadata in content git,
  not the full block text.

### P1 - Shared Browser Backend Access Is Duplicated

Evidence:

- `public/ingest-client.js`, `src/pages/reader/[slug].astro`,
  `src/pages/reader/review.astro`, and `src/pages/sources/[id]/read.astro` each
  read `backendUrl` / `backendToken` from `localStorage` and build fetch headers.

Impact:

- Auth, error handling, base URL behavior, and future token-storage changes must
  be edited in multiple places.
- Browser code in `public/` is not type-checked or bundled.

Recommended direction:

- Move `public/ingest-client.js` into `src/scripts/ingest-client.ts` and serve it
  through Astro/Vite.
- Add a shared `src/scripts/backend-client.ts` with:
  - backend settings load/save
  - auth header creation
  - JSON request helper
  - SSE/fetch streaming helper
  - consistent auth/offline error mapping

### P2 - The Dedicated Ingest Page Has Stale Design Tokens

Status: fixed immediately in this cleanup. `/ingest` now uses `--terra` for the
primary action and `--sage` for online status.

Evidence:

- `src/pages/ingest.astro` previously referenced `var(--accent)` and
  `var(--teal)`, but the active theme defines `--terra`, `--amber`, and `--sage`.
- The dashboard also embeds an ingest console using the same shared client.

Impact:

- The dedicated ingest page can render with missing colors.
- The same workflow is represented in two different UI structures.

Recommended direction:

- Make one `IngestConsole.astro` component and use it in both places, or make the
  dashboard console canonical and reduce `/ingest` to a focused full-page wrapper.
- Replace stale tokens with current theme tokens.

### P2 - Frontend Styling Needs Stronger Component Boundaries

Evidence:

- `src/styles/global.css` contains a useful core theme, but major pages still use
  many inline styles.
- Repeated card, rail, chip, toolbar, input, and metadata patterns are hand-coded
  in multiple pages.

Impact:

- Design consistency depends on manually remembering local inline styles.
- Responsive fixes are harder because layout rules are scattered.

Recommended direction:

- Add small Astro components for repeated structure:
  - `PageHeader.astro`
  - `MetricCard.astro`
  - `PageCard.astro`
  - `SourceCard.astro`
  - `RailSection.astro`
  - `IngestConsole.astro`
- Move page-specific styles into named CSS files imported by the page.
- Keep global CSS for tokens, shell layout, prose, and truly shared primitives.

### P2 - Frontend Contract Tests Are Missing

Evidence:

- Backend and pipeline tests are substantial.
- There are no focused tests for `src/lib/vault.mjs`, citation indexing, block
  derivation, or browser-side source-reader anchor logic.

Impact:

- The most custom frontend behavior can regress while `astro check` and
  `astro build` still pass.

Recommended direction:

- Add Node's built-in test runner or a small Vitest setup for pure helpers.
- Start with tests for:
  - frontmatter parsing
  - alias map and backlink generation over fixtures
  - `blocksForSource`
  - citation extraction/indexing
  - source-reader fragment parsing and duplicate-block resolution
- Add a small browser smoke test later for reader annotation load/render behavior.

### P3 - Sync Has Become A Multi-Stage Build Orchestrator

Evidence:

- `scripts/sync_content.py` snapshots content, publishes assets, writes sync
  metadata, runs fallback reading generation, and extracts source blocks.

Impact:

- The sync path is understandable, but it is becoming a critical orchestration
  path with multiple product responsibilities.

Recommended direction:

- Keep the script for now.
- Add a documented stage list and a `--check` or `--dry-run` mode before adding
  more responsibilities.
- If it grows again, replace the orchestration with a small Python or Node command
  while keeping the existing `npm run sync` interface.

## Target Layout

This is a target shape, not a mandatory big-bang rewrite.

```text
backend/app/
  main.py                    # app factory, middleware, router registration only
  settings.py                # env parsing and defaults
  auth.py                    # fail-closed auth dependency
  routers/
    health.py
    ingest.py
    study.py
    llm.py
    annotations.py
  services/
    ingest_runner.py
    llm.py
    promote.py
    fsrs.py
  store/
    db.py
    migrations.py

src/
  components/
    IngestConsole.astro
    PageHeader.astro
    PageCard.astro
    RailSection.astro
    SourceCard.astro
  lib/
    vault/
      frontmatter.mjs
      index.mjs
      graph.mjs
      sources.mjs
      blocks.mjs
      citations.mjs
  scripts/
    backend-client.ts
    ingest-client.ts
    language-reader.ts
    review.ts
    source-reader/
      api.ts
      anchors.ts
      annotations.ts
      commands.ts
      regions.ts
      rail.ts
  styles/
    global.css
    reader.css
    source-reader.css
```

## Phased Plan

### Phase 1 - Privacy And Immediate Contract Cleanup

Goal: make the current architecture internally consistent without broad refactors.

Tasks:

- Self-host or remove Google Fonts.
- Bundle Mermaid locally or pre-render diagrams.
- Add a restrictive CSP and `Referrer-Policy` in the app shell, then mirror it in
  the local server/deploy docs if headers are later served outside Astro.
- Add a static scan for unexpected runtime external URLs.
- Done immediately before the broader phases: update
  [`SOURCE-READER-DESIGN.md`](./SOURCE-READER-DESIGN.md) to reflect generated
  local `vault/.blocks` artifacts, fix the source-reader empty-state copy around
  block generation, and fix stale `--accent` / `--teal` usage in `/ingest`.

Validation:

- `npm run check`
- `npm run build`
- `rg -n "https?://|cdn" src public` with documented allowlist only
- Browser check: ordinary local pages do not load external scripts, styles,
  fonts, images, or `connect-src` targets under the CSP.

### Phase 2 - Backend Module Split

Goal: preserve API behavior while reducing change risk.

Tasks:

- Add `settings.py` and `auth.py`.
- Move routes into FastAPI routers by product area.
- Move annotation validators and ingest option normalization out of `main.py`.
- Convert JSON request bodies to Pydantic models where route contracts are stable.
- Keep existing route tests, then add a few model-validation tests for malformed
  request bodies.

Validation:

- `python -m compileall backend/app`
- `cd backend && python -m pytest`

### Phase 3 - Frontend Pure Contract Tests

Goal: protect the current behavior before extracting the highest-complexity UI.

Tasks:

- Add pure tests for `src/lib/vault` helpers before moving code:
  - frontmatter parsing
  - alias map and backlink generation over fixtures
  - `blocksForSource`
  - citation extraction/indexing
- Pull pure source-reader logic into small testable helpers without changing
  page behavior, then test:
  - fragment parsing
  - duplicate-block resolution
  - exact quote matching
  - fuzzy quote matching
- Add CI command, for example `npm run test:unit`.

Validation:

- `npm run test:unit`
- `npm run check`
- `npm run build`

### Phase 4 - Frontend Extraction

Goal: make the complex readers and ingest UI modular while keeping the Phase 3
tests green after every extraction step.

Tasks:

- Move backend settings/fetch helpers into `src/scripts/backend-client.ts`.
- Move `public/ingest-client.js` to a bundled `src/scripts` module.
- Extract `IngestConsole.astro` and reuse it from the dashboard and `/ingest`.
- Split source-reader client logic into modules.
- Move source-reader and language-reader CSS out of the Astro pages.

Validation:

- `npm run test:unit`
- `npm run check`
- `npm run build`
- Manual browser smoke:
  - dashboard loads
  - ingest console can ping backend
  - source reader loads annotations
  - language reader can save a word
  - review page loads queue/stats

### Phase 5 - Operational Polish

Goal: improve long-term local operation after the code layout is cleaner.

Tasks:

- Add a `doctor` command that checks content git cleanliness, backend auth,
  LLM command health, Node/Python versions, and expected ignored directories.
- Add a documented backup/restore command for `backend/data/study.db`.
- Consider persisting completed ingest job summaries if in-memory history becomes
  limiting.

Validation:

- `doctor` exits non-zero for missing auth, dirty content preflight blockers, and
  unavailable backend dependencies.

## Acceptance Criteria

- The browser app makes no third-party runtime requests during ordinary local use.
- A restrictive CSP and `Referrer-Policy` enforce that local-only posture and
  reduce backend-token exfiltration risk from accidental external script/style
  includes.
- `backend/app/main.py` only creates the app, configures middleware, and registers
  routers.
- Source-reader browser logic is in testable modules rather than a single large
  Astro page script.
- The docs agree on whether block artifacts are generated local build outputs or
  committed pipeline outputs.
- `/ingest` and the dashboard use the same ingest component and current design
  tokens.
- CI continues to run frontend build/check, backend tests, pipeline tests, and
  npm audit. Backend CI should continue running the full route suite currently at
  36 tests, and frontend CI should add the new `npm run test:unit` command before
  source-reader extraction proceeds.

## Non-Goals

- Do not move away from Astro or FastAPI.
- Do not commit private `content/`, generated `vault/`, full extracted book text,
  runtime DBs, or build outputs to the repo.
- Do not introduce a large frontend framework unless a specific interaction later
  requires it.
- Do not add a heavy database migration framework before SQLite migrations exceed
  the current simple additive pattern.
