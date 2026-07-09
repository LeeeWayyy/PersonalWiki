# Shell To Local App Migration Plan

Goal: shell scripts may remain as developer conveniences, but the product runtime
should not depend on `.sh` files. The local app should own startup, configuration,
sync, ingest orchestration, diagnostics, and backup flows through typed Python,
Node, or app-native code with absolute paths.

## Why Change

The current shell scripts worked for a local website, but they become fragile as
the architecture moves toward a local app:

- Relative paths depend on the caller's current working directory.
- Environment parsing has already needed custom safeguards.
- App startup needs richer state than shell is good at: selected wiki folder,
  auth token, background service state, port allocation, process supervision, and
  user-facing errors.
- Runtime behavior is harder to unit test when it lives in shell glue.

The target principle:

> Shell scripts are allowed for development and CI, but the installed local app
> must not require a shell script to run normal user workflows.

## Current Shell Inventory

| Script | Current role | Runtime critical? | Target owner |
|---|---:|---:|---|
| `scripts/app_start.py` | Starts backend, builds/serves site, frees ports | Yes | Local app startup / process manager |
| `run.sh` | Compatibility wrapper for app startup | Transitional | Remove after callers use typed command |
| `backend/app/serve.py` | Loads config, validates wiki folder, starts Uvicorn | Yes | Local app backend launcher |
| `backend/run.sh` | Creates/updates developer venv, delegates to `app.serve` | Transitional | Remove after app owns dependency setup |
| `scripts/sync_content.py` | Snapshots wiki folder into `vault/`, publishes assets, builds reader artifacts | Yes | Local app sync service |
| `scripts/app_config.py` | Env parsing, wiki-folder resolution, auth-token bootstrap | Yes | Local app config module |
| `scripts/doctor.py` | Local readiness checks | Useful | App diagnostics view + CLI command |
| `scripts/vendor_content.py` | One-time fallback copy/clone into `./content` | Transitional | App folder picker/import flow |
| `scripts/study_db.py` | Backup/restore study DB | Useful | App backup/restore flow |
| `scripts/check_runtime_external.py` | Static privacy scan | Dev/CI | App diagnostics / CI privacy scan |
| `pipeline/scripts/llm_client.py` | Shared LLM client and stdin/stdout CLI | Runtime for LLM-backed flows | Local app LLM adapter |
| `pipeline/scripts/tests/test_*.sh` | Pipeline integration tests | Dev/CI | Keep until migrated to Python tests |

Not shell, but part of the same runtime surface: `scripts/serve.mjs` serves the
built `dist/` site with security headers in production. The Python startup
manager and launchd site agent both call it directly. It is already Node, so it
is not shell debt; the open question is only whether a future desktop app embeds
static serving instead of running a sidecar Node process.

Phase 1 is done:

- `scripts/app_config.py` owns env-file parsing, wiki-folder resolution, and
  backend auth-token bootstrap.
- `scripts/sync_content.py` owns content snapshotting, asset publishing,
  provenance metadata, post-sync reader/block builders, and path-safety checks.

The `scripts/pw-env.sh` and `scripts/sync-content.sh` compatibility wrappers
have since been removed; `npm run sync` and the app startup path call the Python
modules directly.

Phase 2 has started with `backend/app/serve.py`, which owns backend runtime
configuration and Uvicorn launch while `backend/run.sh` remains as a developer
dependency-bootstrap wrapper.

Phase 3 has started with `scripts/app_start.py`, which owns local startup,
dependency checks, port cleanup, backend launch, build/static serve or dev
server launch, health checks, and shutdown while top-level `run.sh` remains as a
compatibility wrapper.

Phase 4 has started with `pipeline/scripts/llm_client.py`, which gives backend
and pipeline code one local Codex/API/custom-command client. `LLM_CMD` remains an
advanced override, and legacy `llm-codex.sh` values are treated as Codex provider
aliases instead of being executed through shell.

The legacy `pipeline/ingest.sh` and `pipeline/scripts/llm-codex.sh` wrappers have
been removed. `pipeline/ingest.py` is the ingest entrypoint, and legacy
`LLM_CMD=.../llm-codex.sh` strings are still interpreted by `llm_client.py`
without executing a shell bridge.

Phase 5 is implemented at the typed utility layer with `scripts/doctor.py`,
`scripts/study_db.py`, `scripts/vendor_content.py`, and
`scripts/check_runtime_external.py`. The first wrapper-removal pass deleted the
old maintenance shell entrypoints after package scripts and docs moved to the
Python commands. The future local app can now call these Python modules directly
or replace their thin CLI surfaces with app-native UI actions.

Phase 6 is implemented for the replaced maintenance utilities with Python tests
covering the privacy scan, study DB backup/restore, and fallback wiki import.
The final wrapper-removal pass deleted `scripts/pw-env.sh`,
`scripts/sync-content.sh`, and `scripts/tests/test_pw_env.sh`. The pipeline
shell tests remain only for end-to-end coverage that has not yet moved to typed
modules.

## Target Architecture

```text
Local app
  App config store
    selected wiki folder
    generated auth token
    backend port
    runtime/cache dirs

  Runtime manager
    ensure Python/Node runtime
    start/stop backend or embedded service
    serve or embed the built static site (replaces scripts/serve.mjs)
    preserve current security headers or equivalent webview policy
    supervise background jobs
    surface logs/errors in UI

  Sync/index service
    read local wiki folder
    write generated vault/cache artifacts
    build reader blocks and reading JSON
    run Pagefind or replacement indexer

  Backend service
    auth, ingest queue, annotations, study DB
    calls pipeline through importable Python APIs or subprocesses

  Diagnostics and maintenance
    doctor checks
    study DB backup/restore
    privacy/external URL scan
```

The selected wiki folder remains the source of truth. Generated artifacts remain
outside it unless they are part of the wiki schema.

## Migration Phases

### Phase 1 - Keep Scripts, Extract App-Callable Modules

Purpose: separate logic from shell without changing user behavior.

Tasks:

- Create a Python or Node config module that owns:
  - local config path
  - selected wiki folder
  - auth token generation/reuse
  - absolute path resolution
- Replace `scripts/pw-env.sh` internals with calls to that module where possible,
  while keeping the script as a compatibility wrapper.
- Move sync orchestration out of `scripts/sync-content.sh` into the app-callable
  `scripts/sync_content.py` command:

```text
scripts/sync_content.py
  sync_content(source_dir, dest_dir, public_asset_dir, options)
```

- Keep `npm run sync` working by calling the Python command directly; keep
  `scripts/sync-content.sh` as a thin wrapper for older manual workflows.

Validation:

- Existing `npm run test:unit`
- `npm run build`
- New unit tests for config, sync path safety, asset publishing, and provenance
- Existing nested-path rejection tests still pass

### Phase 2 - Replace Backend Startup Shell

Purpose: make backend startup independent of `backend/run.sh`.

Tasks:

- Add a Python entrypoint, for example `backend/app/serve.py`, that:
  - loads app config
  - ensures auth token exists
  - validates wiki folder
  - starts Uvicorn
- Move dependency creation decisions out of shell. Prefer one of:
  - app bundle ships Python environment
  - app-managed venv setup
  - documented developer-only install step
- Keep `backend/run.sh` as a developer dependency-bootstrap wrapper temporarily.
- Update launchd backend templates and deploy docs to call the new entrypoint
  before removing the wrapper.

Validation:

- `python -m app.serve` from `backend/`, or `python -m backend.app.serve` from
  the repo root, starts the backend
- Backend route tests still pass
- `/health` reports resolved content folder and auth enabled

### Phase 3 - Replace Top-Level App Startup Shell

Purpose: remove `run.sh` from normal use.

Tasks:

- Local app startup should:
  - load or create config
  - ask for wiki folder if missing
  - generate auth token
  - allocate/free ports or use embedded backend
  - start backend
  - run sync/build/index if needed
  - open the UI
- Keep `run.sh` as a developer convenience wrapper that calls the same app-managed
  startup path.
- Move port cleanup/supervision logic into app code with visible errors instead
  of silent shell process killing.
- Update launchd site templates and deploy docs so resident deployment no longer
  depends on shell startup paths.

Validation:

- Fresh checkout/start path works without manual `backend/.env` edits
- Existing configured wiki folder starts without prompts
- Failure states are visible: missing folder, dirty tree, backend port conflict,
  missing runtime dependency
- The site-serving replacement preserves `X-Frame-Options`, `nosniff`,
  `Referrer-Policy`, and equivalent isolation behavior from `scripts/serve.mjs`

### Phase 4 - Replace LLM Shell Adapter

Purpose: remove the legacy Codex shell bridge from runtime.

Tasks:

- Add a Python or Node LLM adapter with the same stdin/stdout contract or direct
  function API.
- Make pipeline call an importable LLM client where practical.
- Keep `LLM_CMD` only as an advanced/developer override.
- Prefer absolute command paths when subprocesses are unavoidable.

Validation:

- Wiki ingest and language ingest work when launched from a wiki folder cwd
- Existing `PW_LLM_CMD_BASE_DIR` regression test remains covered or is no longer
  needed because relative shell commands are gone
- `/health/llm` still tests the configured LLM path

### Phase 5 - Move Diagnostics And Backup Into The App

Purpose: replace user-facing maintenance scripts.

Tasks:

- Diagnostics are owned by `scripts/doctor.py`:
  - app diagnostics page
  - optional `python -m personal_wiki doctor`
- Study DB backup/restore is owned by `scripts/study_db.py` and future app
  backup/restore actions.
- Fallback wiki import is owned by `scripts/vendor_content.py` and future:
  - folder picker
  - import/copy flow
  - explicit explanation of source and destination
- Runtime external URL scanning is owned by `scripts/check_runtime_external.py`
  for CI and diagnostics.

Validation:

- Diagnostics catches same failures as current doctor
- Backup/restore is tested against a temp DB
- Import flow refuses unsafe nested paths
- Runtime external URL scanning is covered by Python tests

### Phase 6 - Test Migration

Purpose: keep CI strong while removing shell from product runtime.

Tasks:

- Convert shell tests that cover pure logic to Python tests where their logic has
  typed replacements.
- Keep high-value end-to-end shell tests until Python alternatives exist.
- Mark remaining shell scripts as developer-only in docs.
- Remove shell wrappers only after no app/runtime docs reference them.

Validation:

- CI still covers:
  - backend API
  - frontend contracts
  - sync path safety
  - ingest pipeline fixtures
  - LLM command/path behavior or replacement LLM adapter behavior

## Proposed New File Layout

This is a target shape, not a required single refactor:

```text
backend/app/
  serve.py
  config.py
  runtime.py

scripts/
  sync_content.py
  doctor.py
  study_db.py
  vendor_content.py
  check_runtime_external.py

pipeline/
  ingest.py
  services/
    llm_client.py
    source_extract.py
    language_pages.py

local-app/
  runtime/
    config.ts
    process-manager.ts
    sync.ts
    diagnostics.ts
```

If the app becomes Tauri-based, prefer app-native TypeScript/Rust for process
supervision and filesystem selection, while keeping pipeline-heavy content logic
in Python modules.

## Compatibility Policy

- Do not delete a shell script until all documented normal workflows use the new
  path.
- Shell wrappers should become one-liners around typed commands before deletion.
- CI may keep shell for orchestration longer than the local app runtime.
- All script replacements must preserve privacy behavior: no external network
  calls unless explicitly configured, no private wiki content committed to this
  app repo, and no generated full-text artifacts in git.

## Success Criteria

The migration is complete when:

- A user can install/open the local app without running `./run.sh`.
- A user can select a wiki folder without editing `backend/.env`.
- The app can ingest, sync, search, annotate, and review without invoking a `.sh`
  file as part of normal runtime.
- Remaining `.sh` files are clearly developer/CI-only or removed.
- Relative path bugs like `LLM_CMD=../pipeline/...` failing from a wiki cwd are no
  longer possible in the product path.
