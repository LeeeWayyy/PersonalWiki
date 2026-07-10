# Personal Wiki

A private, **local-first** personal-learning website over an LLM-wiki vault —
built on **Astro** (bespoke modern-dashboard design), with a **FastAPI backend**
that runs the ingest pipeline from the page and hosts a **word/grammar bank +
FSRS spaced repetition** and a **source reader with annotations**. The vault
content lives in a configurable local wiki folder (`PW_CONTENT_DIR`), so each
machine/user can use different private content while sharing the same app code.
Served locally on your Mac, reached only by you over Tailscale. See
[`pipeline/schema.md`](./pipeline/schema.md) for the current vault and ingest
contracts, and [`docs/SHELL-TO-LOCAL-APP-MIGRATION.md`](./docs/SHELL-TO-LOCAL-APP-MIGRATION.md)
for the runtime migration notes.

## What works today

- **Reading site** — renders your real vault: entities, topics, tag indexes
  (MOCs), the Mermaid argument map, and source pages. Custom rendering for
  `[src:]` citation chips (→ source pages), `[[wikilinks]]` + aliases, backlinks,
  `> [!AI]` synthesis callouts, human/LLM zones, the two-tier Synthesis/Evidence
  structure, `![[…assets/…]]` embeds, and `==CONFLICT==` highlights.
- **Dashboard** — counts, recently updated, sources shelf, tag explorer, open
  conflicts.
- **Search** — Pagefind, full-text, with built-in CJK segmentation (verified on
  the Chinese vault).
- **Ingest console** (dashboard/root page, `/`) — add a file or URL, pick
  options, run the pipeline, watch live logs over SSE, auto-rebuild on success.
- **Language reader** (`/reader`) — Miraa-style immersion reading: sentence-aligned
  bilingual text, furigana ruby, tap-a-word dictionary sheet with save-to-bank,
  per-sentence grammar + on-demand translation, new-word highlighting, and a reading
  toolbar (furigana / translation-mode / font size). Renders from a structured
  `_reading/<id>.reading.json` — emitted by the ingest pipeline
  (`generate-language-pages.py`).
- **Review** (`/reader/review`) — FSRS spaced repetition over your saved items.
- **Source reader + annotations** (`/sources/<id>/read`) — read the original
  source in-app (epub/mobi/pdf extracted into blocks, or language-source text), select
  text to highlight/comment, a marginalia rail, and `[src:]` citation chips that
  deep-link into the exact chapter. Notes live in the backend (private, fail-closed
  auth). The reader block/citation contract is covered by
  [`pipeline/schema.md`](./pipeline/schema.md).
- **Backend** — ingest control plane (preflight dirty-tree guard, single
  serialized lock, leftover-artifact detection, per-job timeout, SSE), the
  SQLite word bank, FSRS scheduler, annotations store, CSV/Anki export, on-demand
  translation.

## Local wiki folder

The application reads a local wiki folder. By default that folder is this repo's
gitignored `content/`, but production/local-private use can point at any absolute
path with `PW_CONTENT_DIR`:

- `pipeline/` — the ingest tooling (committed code).
- `PW_CONTENT_DIR` — your local wiki folder, containing `wiki/`, `sources/`,
  `lang/`, and `.wiki/`.
- `content/` — the fallback local wiki folder inside this repo, gitignored so the
  full book text / epub / private notes stay out of git history.

Recommended setup:

```sh
cp backend/.env.example backend/.env
# then set:
# PW_CONTENT_DIR=/absolute/path/to/your/wiki-content
```

Optional one-time fallback setup (copies or clones an existing vault into
gitignored `./content`; a git source lets the ingest console commit into it):

```sh
python3 scripts/vendor_content.py ~/Documents/DailyNotes/content
# or:  PW_CONTENT_SOURCE=/path/to/content python3 scripts/vendor_content.py
```

## Quick start

Everything in one shot (site + backend; Ctrl-C stops both):

```sh
python3 scripts/app_start.py        # build the site, serve it, start the backend
python3 scripts/app_start.py --dev  # hot-reload dev server (no Pagefind search) + backend
# or: ./run.sh                      # compatibility wrapper
```

Then open http://localhost:4321 (backend on http://localhost:8787). On first run it
installs deps, creates `backend/.env`, enables the local Codex provider, and
generates `PW_AUTH_TOKEN` automatically. If you are not using `./content`, set
`PW_CONTENT_DIR` to your wiki folder. Use `--open` or
`PW_OPEN_UI=1` to open the site automatically after startup.

Or run the pieces separately:

```sh
# 1) Frontend (reading site). Requires Node 22.12+.
npm install
npm run check      # Astro + TypeScript diagnostics
npm run test:unit  # frontend contract tests against ci-fixtures/content
npm run build      # sync content → astro build → pagefind index
npm run preview    # serve dist locally

# 2) Backend (ingest + study). Requires Python 3.11+.
cd backend
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m app.serve   # http://127.0.0.1:8787
# or: bash run.sh                 # developer wrapper that creates/updates .venv
```

After the first run creates `backend/.env`, run `npm run doctor` from the repo root
for local readiness/preflight checks.

In the site, open **Ingest console → Backend** and set the URL + auth token once
(stored in your browser). Or exercise the ingest flow with no LLM/ASR spend:
`PW_INGEST_STUB=1 python3 scripts/app_start.py`.

`npm run dev` runs the site with hot reload (search needs a production build).

## Architecture

```
pipeline/ingest.py ──► PW_CONTENT_DIR (local wiki repo — private, never pushed by this app)
        ▲                        │
        │            scripts/sync_content.py (snapshot → ./vault + .blocks, read-only)
        │                        ▼
   FastAPI backend        Astro build ──► dist/ + Pagefind
   (ingest + study +             │
    annotations)                 │
        └── SSE · word bank · annotations ──┘
                 │
        both served locally ──► Tailscale ──► only your devices
```

- The site is a **read-only consumer** of `PW_CONTENT_DIR`. `scripts/sync_content.py`
  snapshots it into `vault/` (gitignored) and extracts epub/mobi/pdf sources into
  `vault/.blocks/`.
- Vault Markdown is treated as trusted text plus pipeline zone comments, not as
  arbitrary HTML. The Astro build rejects raw HTML tags in vault Markdown before
  rendering; UI-level HTML belongs in Astro components.
- The backend is the only component that writes: it runs the vendored
  `pipeline/ingest.py` (spending LLM/ASR budget) and, for the Source Reader, stores
  private annotations and can **promote** a note into a wiki page's `human-zone`
  (committed in the configured wiki folder). Hence the auth token and the
  preflight/lock safeguards.

## Still needs your Mac / credentials (plan boundaries)

1. **Language `_reading` sidecar** — the pipeline's structured
   `_reading/<id>.reading.json` is the authoritative reader contract. Legacy
   `_vocab`/`_grammar` markdown is no longer used by the app; re-ingest old
   language sources with `--profile lang` to generate current reader JSON.
2. **codex / LLM auth** — `PW_LLM_PROVIDER=codex` is the agentic,
   subscription-backed default and must run non-interactively under a daemon. To
   force non-agentic single-completion mode, set `PW_LLM_PROVIDER=api` (or
   `openai`) with `PW_LLM_API_KEY`. If local Codex auth is not viable, you can
   also set `LLM_CMD` to a custom stdin-to-stdout command or keep the legacy
   OpenAI-compatible backup path with `PW_LLM_API_ENABLED=1` and
   `PW_LLM_API_KEY`. Debug daemon auth with
   `GET /health/llm` using `X-Auth-Token`; it probes only the local provider,
   never the API fallback. Without one configured, real ingest and `/translate`
   won't run (the UI degrades gracefully).
3. **Private hosting** — `tailscale serve` in front of `scripts/serve.mjs` (site)
   and `python -m app.serve` (backend), plus `launchd` agents to keep both
   resident. Run the daemon as your login user so git ownership matches the vault.

## Tests & CI

- **Pipeline** — `python3 -m unittest discover -s pipeline/scripts/tests -p 'test_*.py'`
  and `for t in pipeline/scripts/tests/test_*.sh; do bash "$t" || break; done`
  (self-contained fixtures; includes the alias-index and orphan-allowlist checks).
- **Local config helpers** — `python3 -m unittest scripts/tests/test_app_config.py`
  covers the typed config module.
- **Sync helper** — `python3 -m unittest scripts/tests/test_sync_content.py`
  covers path-safety and generated asset/provenance behavior.
- **Maintenance helpers** —
  `python3 -m unittest scripts/tests/test_check_runtime_external.py scripts/tests/test_study_db.py scripts/tests/test_vendor_content.py`
  covers the typed privacy scan, study DB backup/restore, and fallback wiki import.
- **Backend** — `pip install -r backend/requirements-dev.txt && (cd backend && python -m pytest)`
  covers health, fail-closed annotation auth, CRUD, image-region round-trip, promote,
  backend startup config, and the AI-assist / translate graceful paths.
- **Frontend** — `npm run test:unit`, `npm run check` (Astro + tsc), and
  `npm run build`.
- **CI** — `.github/workflows/ci.yml` runs all of the above on push/PR. Since real
  wiki folders are private, the frontend jobs build against `ci-fixtures/content`
  (a tiny fixture vault). `npm audit --audit-level=moderate` runs too.

The **alias index** (`wiki/.alias-index.json`) is a derived artifact: `ingest.py`
rebuilds it every run and `vendor_content.py` builds it on first setup, so it's never
committed. Intentionally-unreferenced source assets (e.g. book figures shown only in the
Source Reader) are listed in `sources/.orphan-assets-allow` so lint's orphan-asset check
stays quiet for them while still flagging new ones.

## Notes

- A clean `npm run build` produces the full static site + search index. (If you
  build inside a sandboxed/over-mounted filesystem you may see a harmless `EPERM
  unlink` on Astro's temp cache; it doesn't occur on a normal macOS filesystem.)
- Study data lives in `backend/data/study.db` (gitignored). Use
  `npm run study:backup` to create `backups/study-db/*.db`, and restore with
  `npm run study:restore -- <backup-file>` after stopping/restarting the backend.
  CSV/Anki export is still available at `/export`.
