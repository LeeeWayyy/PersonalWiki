# Project Issues Audit

Date: 2026-07-07
Scope: current working tree, including the command-first LLM changes and the
`/health/llm` probe work. This is an issue inventory, not a fix log.

## Summary

The project is now in good functional shape after the review/fix loop. Backend
tests, Astro check, production build, npm audit, sync guard checks, and the full
pipeline shell test suite pass on this working tree.

The main risk areas found during the loop were private-route auth, local LLM
command/API fallback behavior, ingest control-plane robustness, SQLite
migrations/concurrency, reader rendering trust boundaries, and web-ingest
option drift. Each actionable issue documented below is now fixed.

Audit loop status: passes 2 through 14 each produced at least one new distinct
finding. Pass 15 reran targeted static scans plus the full verification suite and
found no new actionable issue categories. This is the current stopping point for
the requested review loop.

Fix progress:
- Fixed in current working tree: backend auth now fails closed for private,
  mutating, spend-capable, study, job-log, and preflight routes. `/health`
  remains open.
- Fixed in current working tree: ingest log streaming now uses header-auth
  `fetch()` instead of URL query tokens, and the backend event route no longer
  accepts `?token=`.
- Fixed in current working tree: build-time fallback reader translations now
  require `PW_BUILD_TRANSLATE=1`; normal `npm run build` does not invoke an LLM.
- Fixed in current working tree: ingest file uploads now stream to disk with a
  `PW_MAX_UPLOAD_MB` size limit and partial-file cleanup on rejection.
- Fixed in current working tree: ingest job logs are bounded by
  `PW_JOB_LOG_LIMIT`, completed jobs are reaped after `PW_JOB_TTL_S`, and
  truncated streams emit a marker.
- Fixed in current working tree: timed-out ingest/rebuild subprocesses run in
  process groups, are killed as groups, and are awaited after kill.
- Fixed in current working tree: ingest jobs can be canceled through
  `POST /jobs/{id}/cancel`, and both ingest UIs expose a Cancel control.
- Fixed in current working tree: annotation API inputs now validate color, tags,
  and supported link objects before storing JSON that the reader renders.
- Fixed in current working tree: manually marking vocabulary as `known` now
  clears its due date and removes it from the review queue.
- Fixed in current working tree: review grading now rejects values outside
  `1..4` instead of silently clamping them.
- Fixed in current working tree: `/export` now rejects unsupported formats with
  `400` instead of silently returning CSV.
- Fixed in current working tree: backend CORS now defaults to local Astro origins
  instead of wildcard; broader origins must be configured explicitly.
- Fixed in current working tree: ingest preflight is profile-aware, reports git
  failures loudly, and the pipeline's safety-critical git capture helper no
  longer treats git errors as clean state.
- Fixed in current working tree: `scripts/sync_content.py` refuses
  source/destination path overlap before deleting generated vault output.
- Fixed in current working tree: SQLite connections enable foreign keys and a
  busy timeout; review tables now have queue/history indexes and review rows
  reference existing items.
- Fixed in current working tree: duplicate vocab saves merge newer context while
  preserving review scheduling.
- Fixed in current working tree: raw vault Markdown now rejects arbitrary raw
  HTML before rendering, while pipeline control comments remain allowed.
- Fixed in current working tree: the dashboard and ingest page share one ingest
  client helper for validation, header-auth log streaming, and cancellation.
- Fixed in current working tree: sample config/docs no longer hardcode this
  machine's repo path.
- Fixed in current working tree: backend JSON routes now reject malformed JSON,
  non-object bodies, and bad nested field shapes with `400` instead of leaking
  server errors.
- Fixed in current working tree: on-demand translation cache rows now store and
  return the actual target language used by the prompt.
- Fixed in current working tree: source-reader annotation rendering now treats
  legacy stored annotation fields as untrusted before interpolating them into
  classes, attributes, and links.
- Fixed in current working tree: promoting annotations into wiki human zones now
  escapes note text/title as Markdown text and validates `wiki_rel` before
  writing.
- Fixed in current working tree: `LLM_CMD` timeouts now kill the whole local
  command process group instead of only the shell wrapper.
- Fixed in current working tree: web ingest options now validate kind/section
  shapes and the UI no longer sends the invalid `--kind media` pipeline value.
- Fixed in current working tree: existing study databases now migrate legacy
  `reviews` tables to the foreign-key schema instead of only protecting new DBs.

## Findings

### P0 - Mutating and spend-capable routes fail open when `PW_AUTH_TOKEN` is unset

Status: fixed in current working tree. `require_auth()` now returns `503` when
`PW_AUTH_TOKEN` is unset, rejects missing/wrong tokens with `401`, and route
tests cover the auth matrix.

Evidence:
- `require_auth()` only rejects when `AUTH_TOKEN` is set:
  `backend/app/main.py:43`.
- Expensive or mutating routes use `require_auth()`, not the fail-closed guard:
  `/ingest` at `backend/app/main.py:122`, `/vocab` at
  `backend/app/main.py:188`, review grading at `backend/app/main.py:258`,
  `/translate` at `backend/app/main.py:336`, and `/assist` at
  `backend/app/main.py:381`.
- Previously, `.env.example` and the local generated `.env` left
  `PW_AUTH_TOKEN=` blank by default. `run.sh` / `backend/run.sh` now generate one
  automatically when it is empty.

Impact:
If the backend is reachable by any other host on the network and the token was
not configured, a caller can run ingest jobs, spend LLM/ASR budget, mutate the
study DB, and use AI assist endpoints. This conflicts with the README claim
that mutating routes require an auth token.

Recommended fix:
Make all mutating, private, or spend-capable endpoints fail closed when
`PW_AUTH_TOKEN` is unset. Keep only low-risk probes like `/health` open. Add tests
that import the app with `PW_AUTH_TOKEN` unset and assert `503` on `/ingest`,
`/vocab`, review grading, `/translate`, `/assist`, and `/health/llm`.

### P0 - Study data read routes are open

Status: fixed in current working tree. Study read routes now require
`X-Auth-Token` and fail closed when `PW_AUTH_TOKEN` is unset.

Evidence:
- `/vocab`, `/review/queue`, `/review/stats`, and `/export` have no auth guard:
  `backend/app/main.py:234`, `backend/app/main.py:246`,
  `backend/app/main.py:291`, and `backend/app/main.py:304`.

Impact:
The word bank, review queue, examples, and export can contain private learning
history and source excerpts. Any tailnet peer or local process that can reach the
backend can read them.

Recommended fix:
Use a fail-closed auth guard for all study routes, including reads. If open reads
are intentional for a dashboard, make that explicit with a separate
`PW_ALLOW_OPEN_STUDY_READS=1` opt-in.

### P1 - SSE job auth puts the backend token in the URL

Status: fixed in current working tree. The dashboard and ingest page now stream
logs with `fetch()` and `X-Auth-Token`; `/jobs/{id}/events` no longer accepts a
query-string token.

Evidence:
- Frontend creates `EventSource(...?token=...)`:
  `src/pages/index.astro:244` and `src/pages/ingest.astro:93`.
- Backend reads the token from a query parameter:
  `backend/app/main.py:157`.

Impact:
Query tokens are easier to leak through browser history, logs, screenshots,
reverse-proxy access logs, and copied URLs. Native `EventSource` cannot send
custom headers, so this is a common trap.

Recommended fix:
Replace `EventSource` with a streaming `fetch()` reader that sends
`X-Auth-Token`, or issue a short-lived, job-scoped stream token from `/ingest`
and store only that in the event URL. Also redact query strings from any future
request logging.

### P1 - `npm run build` can make local LLM calls unintentionally

Status: fixed in current working tree. `scripts/build-reading.py` now translates
only when `PW_BUILD_TRANSLATE=1` and still honors `PW_NO_TRANSLATE=1` as an
override.

Evidence:
- `scripts/sync_content.py` always invokes `scripts/build-reading.py` when `uv`
  or Python is present.
- `build-reading.py` loads `backend/.env`, checks `llm_client.configured()`, and
  translates when configured unless `PW_NO_TRANSLATE=1`:
  `scripts/build-reading.py:45` and `scripts/build-reading.py:51`.
- `.env.example` now defaults to `PW_LLM_PROVIDER=codex`, and the shared LLM
  client treats legacy `llm-codex.sh` values as direct Codex provider aliases.

Impact:
A normal site build can trigger local LLM prompts, adding latency and surprising
users who expected a pure static build. It is less costly than an API key, but it
still depends on local model auth and can hang or fail in CI-like contexts if the
environment differs.

Recommended fix:
Invert the default: do not translate during build unless `PW_BUILD_TRANSLATE=1`.
Keep `PW_NO_TRANSLATE=1` as an emergency override if desired. Document this in
README and `.env.example`.

### P1 - Ingest uploads read the full file into memory and have no size limit

Status: fixed in current working tree. Uploads now write in chunks, reject files
over `PW_MAX_UPLOAD_MB` with `413`, and remove partial staged files.

Evidence:
- Upload handling uses `dest.write_bytes(await file.read())`:
  `backend/app/main.py:137`.

Impact:
Large PDFs, EPUBs, audio, or video files can allocate the full upload in memory.
An accidental huge file or malicious local caller can exhaust memory before the
pipeline even starts.

Recommended fix:
Stream uploads to disk in chunks, enforce a maximum size with
`PW_MAX_UPLOAD_MB`, and reject over-limit files with `413`. Add tests around the
size guard.

### P1 - Ingest job state and logs are unbounded

Status: fixed in current working tree. Jobs now retain a bounded, sequenced log
buffer, expose `dropped_lines`, stream a truncation marker when needed, and reap
terminal jobs after `PW_JOB_TTL_S`.

Evidence:
- `JOBS` is an in-memory process-wide dict with no TTL or cap:
  `backend/app/ingest_runner.py:30`.
- Every emitted line is appended forever:
  `backend/app/ingest_runner.py:43`.
- `/jobs/{job_id}` returns the full `lines` list:
  `backend/app/main.py:149`.

Impact:
Repeated jobs or verbose pipeline output can grow backend memory until restart.
The issue is especially visible for long ASR/LLM jobs and repeated web UI use.

Recommended fix:
Add a bounded log buffer per job, job expiration/reaping, and a persisted summary
for completed jobs if history matters. Consider returning log slices instead of
the entire line list.

### P1 - Timeout paths kill subprocesses without awaiting process exit

Status: fixed in current working tree. Ingest and rebuild subprocesses now start
in their own process groups, timeout cleanup kills the group, and the backend
awaits exit with `PW_PROCESS_CLEANUP_TIMEOUT_S`.

Evidence:
- Ingest timeout calls `proc.kill()` and returns:
  `backend/app/ingest_runner.py:140`.
- Rebuild timeout does the same with `rp.kill()`:
  `backend/app/ingest_runner.py:162`.

Impact:
The OS process may remain briefly unreaped, and child processes spawned by the
pipeline may survive if they are in a separate process group. That can leave
orphaned LLM/ASR/transcription work running after the UI reports a kill.

Recommended fix:
Start jobs in a process group when possible, kill the group on timeout, then
`await proc.wait()` with a short cleanup timeout. Emit a clearer result that
distinguishes parent kill from confirmed child cleanup.

### P1 - No cancel endpoint despite the ingest control-plane contract

Status: fixed in current working tree. Jobs track the active subprocess, expose
an authenticated `POST /jobs/{id}/cancel`, transition to `canceled`, and the
dashboard/dedicated ingest pages show a Cancel button while a job is active.

Evidence:
- `backend/app/ingest_runner.py` documents "timeout + cancel" in the module
  header.
- No route exposes cancellation, and `Job` has no process handle after launch:
  `backend/app/ingest_runner.py:36`.

Impact:
Once a long ingest starts, the user must wait for timeout or restart the backend.
This is painful for accidental media jobs or stuck LLM calls.

Recommended fix:
Store the active process handle on the job, add `POST /jobs/{id}/cancel`, and
reuse the same process-group cleanup as timeout handling. Add frontend cancel UI
next to the log stream.

### P1 - Annotation fields are not validated before rendering into `innerHTML`

Status: fixed in current working tree. Annotation create/patch now accepts only
known colors, string tag arrays, and safe `human-zone` link objects; unsafe link
hrefs and unsupported link shapes are rejected with `400`.

Evidence:
- Backend accepts arbitrary annotation `color`, `tags`, and `links` in create and
  patch routes: `backend/app/main.py:445` and `backend/app/main.py:485`.
- Source reader interpolates `color`, `links.href`, and `links.wiki_rel` into
  HTML strings: `src/pages/sources/[id]/read.astro:384` and
  `src/pages/sources/[id]/read.astro:388`.

Impact:
Most normal text is escaped, and promoted links are normally generated by the
backend. However, an authenticated caller can patch arbitrary `links` JSON and
turn the source reader into an XSS surface. Because the backend token is stored
in `localStorage`, any frontend XSS also becomes a backend-token exposure.

Recommended fix:
Validate `color` against `note|question|important`, validate `links` objects
against known shapes, and render annotation cards with DOM APIs or quote-safe
attribute escaping. Consider a basic CSP that blocks inline event handlers once
inline scripts are refactored.

### P2 - Backend CORS defaults to wildcard

Status: fixed in current working tree. The backend now defaults to
`http://localhost:4321,http://127.0.0.1:4321`, and `.env.example` documents
adding Tailnet/custom origins explicitly.

Evidence:
- `allow_origins` defaults to `*`: `backend/app/main.py:38`.

Impact:
With token auth this is less severe than cookie-based auth, but it increases the
blast radius of any fail-open route and makes it easy for arbitrary web pages to
call unauthenticated endpoints from the browser.

Recommended fix:
Default `PW_CORS` to the local Astro origins (`http://localhost:4321`,
`http://127.0.0.1:4321`) and document adding Tailnet origins explicitly.

### P2 - Review grade validation is misleading

Status: fixed in current working tree. The route now rejects non-integer and
out-of-range grades before scheduling, with tests for `0`, `5`, and invalid
strings.

Evidence:
- The route only validates integer parsing and says the grade must be 1 to 4:
  `backend/app/main.py:262`.
- The scheduler silently clamps any integer into range:
  `backend/app/fsrs.py:64`.

Impact:
Bad clients can send `0`, `999`, or negative grades and receive a successful
review. The scheduler clamps them, so the data is not catastrophic, but the API
contract and recorded `reviews.grade` value become misleading.

Recommended fix:
Reject grades outside `1..4` in the route before calling `schedule()`. Add tests
for `0` and `5`.

### P2 - Export format parameter is unused

Status: fixed in current working tree. `format=csv` remains supported and any
other format now returns `400`.

Evidence:
- `/export` accepts `format` but always returns CSV:
  `backend/app/main.py:304`.

Impact:
Small API correctness issue. A user or future UI may expect Anki or another
format and receive CSV silently.

Recommended fix:
Either remove the parameter or reject unsupported formats with `400`. If Anki
export is intended, add explicit `format=anki` behavior and tests.

### P2 - Raw/static content trust boundary is not explicit enough

Status: fixed in current working tree. Vault Markdown is now checked by a remark
guard that rejects arbitrary raw HTML while allowing pipeline comments, and the
README documents the content trust boundary.

Evidence:
- The site renders vault-derived Markdown and uses raw HTML-producing remark
  plugins for zones and highlights.
- Frontend stores the backend URL and token in `localStorage`:
  `src/pages/index.astro:206`, `src/pages/index.astro:213`,
  `src/pages/ingest.astro:56`, and `src/pages/ingest.astro:58`.

Impact:
The current model is "personal vault content is trusted." That is reasonable for
a local private site, but the pipeline also accepts LLM-generated Markdown. If
unsafe HTML ever lands in rendered content, browser JavaScript can read the token
from localStorage and call backend mutation routes.

Recommended fix:
Document the trust boundary. Longer term, sanitize rendered Markdown or add lint
rules that reject raw HTML outside known zone markers. Prefer session storage or
an in-memory token for high-risk operations if the site is ever exposed beyond a
single-user Tailnet.

## Additional Findings From Pass 2

### P1 - Marking vocabulary as known does not remove it from the review queue

Status: fixed in current working tree. `PATCH /vocab/{id}` now updates FSRS
state when manually marking an item `known`, clears `due`, and has regression
coverage verifying the item is absent from `/review/queue`.

Evidence:
- Reader "Mark known" first creates a vocab row and then patches only
  `status=known`: `src/pages/reader/[slug].astro:185`.
- The backend status patch only updates the `status` column:
  `backend/app/main.py:219`.
- Review queue and stats ignore `status` and select `state=0` cards:
  `backend/app/main.py:246` and `backend/app/main.py:291`.

Impact:
A word marked known in the reader still appears in the review queue as a new
card, and review stats continue counting it as new/due. This makes the "known"
action misleading and weakens the study workflow.

Recommended fix:
Decide whether `status` is the source of truth or only a display label. If
`known` should suppress review, update `state`, `due`, and `last_review` in the
PATCH route, or change queue/stats queries to exclude `status='known'`. Add a
test that saves a known item and verifies it is absent from `/review/queue`.

### P1 - Web ingest preflight does not fully match lang-profile dirty-state risk

Status: fixed in current working tree. `/preflight?kind=lang` now checks dirty
paths under `lang/`, and web ingest passes the requested kind into preflight
before launching the job.

Evidence:
- The web preflight watches only `wiki/`, `sources/`, and `.wiki/log.md`:
  `backend/app/ingest_runner.py:32`.
- The same runner can launch `--profile lang`: `backend/app/ingest_runner.py:76`.
- The pipeline's lang path later stages and asserts paths under `lang/`:
  `pipeline/ingest.py:368`.

Impact:
Dirty or leftover files under `content/lang/` can get past the web preflight.
The underlying pipeline has a later subset check, so this is unlikely to commit
outside the lang subtree, but the web UI can report "preflight: clean" before a
less obvious downstream failure or before stale lang artifacts affect generation.

Recommended fix:
Make the web preflight profile-aware. For `kind=lang`, check tracked and
untracked changes under `lang/` plus lang-local leftovers before launching the
pipeline, and report those paths directly in the UI.

### P2 - Git preflight helpers can treat git failures as clean state

Status: fixed in current working tree. Backend preflight now blocks on non-zero
`git status`, and pipeline `git_capture()` fails loud with the git stderr.

Evidence:
- Backend preflight reads `git status --porcelain` stdout but does not check
  `returncode`: `backend/app/ingest_runner.py:54`.
- Pipeline `git_capture()` documents that git errors become an empty string:
  `pipeline/ingest.py:90`.
- Several preflight checks depend on `git_capture()` output:
  `pipeline/ingest.py:169`.

Impact:
A git failure caused by a corrupted repository, unsafe directory configuration,
missing permissions, or an unexpected git error can look identical to "no dirty
files." Later commands may fail, but the safety check that should stop the run
does not fail loud at the earliest point.

Recommended fix:
Use checked git capture helpers for all safety-critical preflight calls. Return
stderr and exit code in the blocked-job message so the operator can fix the
content repo before ingest starts.

### P2 - SQLite migration failures are swallowed too broadly

Status: fixed in current working tree. Additive migrations now ignore only
duplicate-column errors and re-raise other `OperationalError`s with the failed
statement.

Evidence:
- Additive migrations catch every `sqlite3.OperationalError` and assume the
  column already exists: `backend/app/db.py:100`.

Impact:
Real migration failures such as a missing table, malformed schema, permissions
problem, or locked database are hidden during connection setup. The app can then
fail later in unrelated routes, making diagnosis harder and risking partially
initialized state.

Recommended fix:
Only ignore duplicate-column errors. Re-raise every other `OperationalError`
with the failed statement included in the message.

### P2 - Sync can delete the source when misconfigured

Status: fixed in current working tree. `scripts/sync_content.py` resolves the
content, vault, and asset-output paths and refuses any nested or identical pair
before deleting generated output.

Evidence:
- The vault destination is always generated under the app root, while
  `PW_CONTENT_DIR` can be overridden by machine-local config.
- Sync deletes the generated vault before copying from the configured content
  source.

Impact:
If `PW_CONTENT_DIR` is accidentally set to `vault` or a path inside the
destination tree, `npm run sync` can delete the source it is about to read. The
default path is safe, but the override is explicitly supported and has no guard.

Recommended fix:
Resolve both paths with `realpath` before deletion. Refuse when `CONTENT == DEST`,
when `CONTENT` is inside `DEST`, or when `DEST` is inside `CONTENT` unless a
deliberate force flag is set.

### P2 - Duplicate vocab saves drop newer source context

Status: fixed in current working tree. Duplicate saves now refresh reading,
gloss, part of speech, example, source, and anchor with non-empty incoming
values while leaving scheduling fields untouched.

Evidence:
- `/vocab` upserts on `(kind,norm_key)` but only refreshes `reading` and `gloss`
  on conflict: `backend/app/main.py:201`.
- Newer `pos`, `example`, `source_id`, `anchor`, and scheduling fields from the
  incoming save are ignored on conflict: `backend/app/main.py:208`.

Impact:
Saving the same word from a better sentence or a new source returns the existing
item id but leaves stale context attached to the card. This can make later
reviews show old or low-quality examples even though the reader action appeared
successful.

Recommended fix:
Define the merge policy explicitly. A conservative approach is to update empty
fields only, append or track multiple examples separately, and keep scheduling
fields untouched unless the user explicitly resets the card.

### P2 - SQLite writes have no busy timeout

Status: fixed in current working tree. `db.connect()` now enables
`PRAGMA busy_timeout=5000` and `PRAGMA foreign_keys=ON`.

Evidence:
- `db.connect()` enables WAL but does not configure `busy_timeout`:
  `backend/app/db.py:94`.
- Multiple routes open independent SQLite connections for writes, including
  ingest-adjacent annotations, vocab saves, reviews, and translation caching.

Impact:
Concurrent reader actions or a translation cache write during another write can
raise `database is locked` immediately instead of waiting briefly. This is more
likely from the browser because annotation autosave, vocab saves, and AI assist
can overlap.

Recommended fix:
Set a short `PRAGMA busy_timeout`, keep write transactions small, and add tests
or a small stress script around concurrent writes to confirm the backend fails
gracefully.

### P3 - Ingest UI code is duplicated and already drifting

Status: fixed in current working tree. Shared browser code now lives in
`public/ingest-client.js`; both ingest UIs call the same helper for backend
settings, validation, submission, streaming, and cancellation.

Evidence:
- Dashboard ingest logic lives in `src/pages/index.astro:205`.
- Dedicated ingest page logic lives separately in `src/pages/ingest.astro:55`.
- The dashboard trims and rejects an empty URL before submitting:
  `src/pages/index.astro:238`.
- The dedicated page submits the URL input directly:
  `src/pages/ingest.astro:88`.

Impact:
The two pages can develop inconsistent validation, streaming behavior, and token
handling. The current drift is small, but this is the same area that needs a
query-token SSE replacement and future cancel controls.

Recommended fix:
Move shared backend settings, ingest submission, and log streaming into one
client helper or Astro component. Then implement URL validation, stream auth, and
cancel UI once.

## Additional Findings From Pass 3

### P1 - Ingest operational read routes are open

Status: fixed in current working tree. Job status, job event streams, and
preflight now require auth, and job event streams accept `X-Auth-Token` only.

Evidence:
- `/jobs/{job_id}` returns job status and all collected log lines with no auth
  guard: `backend/app/main.py:149`.
- `/preflight` exposes content-repo cleanliness and offending paths with no auth
  guard: `backend/app/main.py:181`.
- The SSE route does call `require_auth()`, but only on a query token and still
  inherits the fail-open behavior when `PW_AUTH_TOKEN` is unset:
  `backend/app/main.py:157`.

Impact:
Even when a backend token is configured, any caller who can guess or obtain a job
id can read ingest logs through `/jobs/{job_id}`. Logs can include source URLs,
file names, command output, and failure details. `/preflight` can also reveal
private content paths and dirty filenames.

Recommended fix:
Apply the same fail-closed auth policy to `/jobs/{job_id}`, `/jobs/{job_id}/events`,
and `/preflight`. If unauthenticated status is useful for a local dashboard, gate
it behind an explicit development-only flag and redact paths/log lines.

### P2 - Backend route tests do not cover the study and ingest control-plane surfaces

Status: fixed in current working tree. Focused backend tests now cover the auth
matrix, job status/log authorization, preflight behavior, known-vocab review
suppression, grade validation, export format rejection, upload limits, SQLite
pragmas/indexes, and duplicate vocab context merging.

Evidence:
- The backend test module describes its own current scope as health,
  annotations, promotion, AI assist, and translate:
  `backend/tests/test_api.py:1`.
- There are no tests in `backend/tests/test_api.py` for `/ingest`,
  `/jobs/{job_id}`, `/preflight`, `/vocab`, `/review/queue`,
  `/review/{item_id}/grade`, `/review/stats`, or `/export`.

Impact:
The highest-risk surfaces identified in this audit are exactly the ones with the
least route coverage. That is why fail-open auth, stale `known` status, grade
clamping, upload limits, and open job logs can regress without CI catching them.

Recommended fix:
Add focused route tests before larger refactors. Start with auth matrix tests
for configured and unconfigured tokens, then add workflow tests for known vocab
suppression, grade validation, export format rejection, upload-size rejection,
and preflight/job-log authorization.

## Additional Finding From Pass 4

### P3 - Study DB lacks indexes and constraints for review history

Status: fixed in current working tree. The schema now adds a `(state, due)` item
index, a `reviews(item_id)` index, and a foreign key from reviews to items.

Evidence:
- The schema creates only `idx_items_key` and `idx_annotations_source`:
  `backend/app/db.py:37` and `backend/app/db.py:66`.
- Review queue and stats filter by `state` and `due`:
  `backend/app/main.py:251` and `backend/app/main.py:296`.
- `reviews.item_id` is stored as an integer but has no foreign key or index:
  `backend/app/db.py:38`.

Impact:
This is not urgent for a small personal dataset, but review queue and stats will
become full scans as the bank grows. Review history can also become orphaned if
items are ever deleted or migrated because SQLite is not enforcing the
relationship.

Recommended fix:
Add indexes for queue/stats access, such as `(state, due)` or a partial due
index, plus an index on `reviews(item_id)`. If item deletion becomes supported,
enable `PRAGMA foreign_keys=ON` and add an explicit `REFERENCES items(id)`
policy.

## Additional Finding From Passes 5-6

### P2 - Sample configs and docs hardcode one local machine path

Status: fixed in current working tree. `.env.example` uses a relative rebuild
command, launchd examples use `/ABS/PATH/TO/personal_wiki` placeholders, and the
media dependency doc uses `$HOME` for the sibling editable install.

Evidence:
- `backend/.env.example` sets `REBUILD_CMD` to an absolute path under the current
  developer's home directory: `backend/.env.example:40`.
- Launchd examples also embed `/Users/leeewayyy/Documents/SourceCode/personal_wiki`:
  `deploy/com.personalwiki.backend.plist.example:11` and
  `deploy/com.personalwiki.site.plist.example:14`.
- Media dependency docs include an absolute editable install path for a sibling
  project: `pipeline/docs/DEPENDENCIES.md:89`.

Impact:
New checkouts on another machine can copy sample commands that point at
nonexistent paths. If the repo is shared externally, it also exposes a local
username/path unnecessarily. This is especially relevant for launchd/headless
setup, where users tend to copy plist examples directly.

Recommended fix:
Make the sample path portable, for example
`REBUILD_CMD="npm --prefix .. run build"` when loaded from `backend/run.sh`, or
leave it commented with a relative example. Replace plist examples with
placeholders like `/ABS/PATH/TO/personal_wiki`, and make the dependency docs use
`$HOME/...` or a generic path.

## Additional Finding From Pass 8

### P2 - Malformed JSON route bodies can raise server errors

Status: fixed in current working tree. Shared backend request helpers now parse
JSON object bodies and form-encoded ingest options consistently, and routes use
small string/object validators before reading nested fields.

Evidence:
- Several private routes called `await request.json()` and immediately used
  `.get()` on the result, including `/ingest`, `/vocab`, `/translate`,
  `/assist`, and annotation routes.
- Multipart ingest parsed `options` with `json.loads(options)` without catching
  malformed JSON or rejecting non-object values.

Impact:
Malformed JSON, a top-level JSON array, or nested values with the wrong shape
could become `500` responses in local/headless logs instead of actionable client
errors. Some bad nested values could also reach SQLite or cache hashing before
failing.

Recommended fix:
Parse JSON through a shared helper that rejects malformed or non-object bodies
with `400`. Validate form `options` as a JSON object, and validate nested object
and string fields before using `.get()`, `.strip()`, cache hashing, or database
writes. Add route tests for each category.

## Additional Finding From Pass 9

### P2 - Translation cache language metadata can record the wrong language

Status: fixed in current working tree. `/translate` now stores
`PW_TRANSLATE_LANG` in translation cache rows and returns it as `target_lang`
for both fresh and cached responses.

Evidence:
- `/translate` used `PW_TRANSLATE_LANG` for the cache hash and prompt but stored
  `b.get("lang")` in the `translations.lang` column.
- The reader calls `/translate` with `lang: 'ja'` while the backend prompt asks
  for the configured target language, so cache audit rows could say `ja` for a
  Simplified Chinese translation.

Impact:
Cache auditability and invalidation become confusing: the row's language field
can describe the source-language hint from the caller instead of the target
language that actually shaped the prompt and output.

Recommended fix:
Treat `PW_TRANSLATE_LANG` as the translation target for this endpoint until a
separate explicit target-language API exists. Store that target in the cache row
and expose it in responses so cached and fresh translation metadata match.

## Additional Finding From Pass 10

### P2 - Legacy annotation rows can still reach unsafe reader HTML interpolation

Status: fixed in current working tree. The source reader now normalizes
annotation colors, only renders promoted links with `/wiki/` hrefs, escapes
dynamic attribute values, and escapes annotation ids before selector lookups.

Evidence:
- `renderRail()` interpolated stored annotation fields into HTML strings for
  `href`, `class`, `data-aid`, `data-promote`, and `data-del`.
- New annotation writes now validate these fields, but existing local SQLite
  rows created before that validation can still be returned by the backend and
  rendered by the browser.

Impact:
A malformed legacy annotation row could break the reader rail markup or, in the
worst case, turn a stored promoted link/id/color field into executable HTML when
the source-reader page renders annotations.

Recommended fix:
Keep backend validation, but also harden the renderer against stored data:
normalize enum-like class values, allow-list promoted hrefs, escape attribute
values separately from text nodes, and use `CSS.escape()` for selector
construction.

## Additional Finding From Pass 11

### P2 - Promoted annotation text is written into Markdown without escaping

Status: fixed in current working tree. Promotion rendering now treats quote,
body, and source title as plain Markdown text, escapes raw HTML/comment
delimiters, URL-encodes source IDs in reader links, and uses sanitized annotation
tokens for idempotency comments.

Evidence:
- `backend/app/promote.py` inserted annotation quote, body, and source title
  directly into Markdown blockquotes and link labels.
- The promote API relied on path resolution for safety but did not reject
  malformed `wiki_rel` values before calling the writer.

Impact:
A note body containing raw HTML or zone-like comments could make the next Astro
build fail under the raw HTML guard or interfere with human-zone/annotation
markers. A source title with Markdown link delimiters could also break the
promoted backlink label.

Recommended fix:
Render promoted annotation text as plain text inside Markdown: escape `&`, `<`,
and `>`, escape Markdown link-label delimiters for source titles, URL-encode
source IDs in generated reader links, sanitize annotation comment tokens, and
reject invalid `wiki_rel` at the API boundary.

## Additional Finding From Pass 12

### P2 - Timed-out local LLM command can leave child processes running

Status: fixed in current working tree. The shared LLM client now launches custom
local commands in a new process group, kills that group on timeout, waits for the
process, and raises a clear timeout error.

Evidence:
- `backend/app/llm.py` used `subprocess.run(..., shell=True, timeout=...)` for
  custom `LLM_CMD`.
- `subprocess.run` kills and waits for the direct shell process on timeout, but
  shell-launched children can survive unless the whole process group is killed.

Impact:
A hung `claude`, `codex`, bridge script, or helper subprocess could continue
running after `/translate`, `/assist`, or `/health/llm` had already failed. In a
launchd/headless setup, repeated probes could accumulate stale local LLM
processes.

Recommended fix:
Use `subprocess.Popen(..., start_new_session=True)` for the local command path,
call `communicate()` with the timeout, and on timeout kill the process group
before returning an explicit local-LLM timeout error. Add a regression that
spawns a child process and verifies it is no longer running after timeout.

## Additional Finding From Pass 13

### P1 - Web ingest `media` kind maps to an invalid pipeline flag

Status: fixed in current working tree. Ingest options are normalized at the API
boundary, `media` is retained only as a legacy alias for `video`, invalid kinds
and non-string `section_label` values return `400`, and the UI now sends
`video` directly.

Evidence:
- The ingest UIs sent `kind: media`.
- `backend/app/ingest_runner.py` converted that to `--kind media`.
- `pipeline/ingest.py` only accepts `--kind video|audio|image_note`, so web
  media jobs failed at argument parsing.
- The `options` object accepted arbitrary field types, so a list-valued
  `section_label` could reach subprocess argument construction.

Impact:
Selecting media ingest from the web UI launched a job that could not pass the
pipeline parser. Bad option shapes also produced confusing downstream failures
instead of immediate client errors.

Recommended fix:
Validate and normalize ingest options in the backend before job creation. Use
explicit pipeline kinds (`video`, `audio`, `image_note`), preserve `media` only
as a compatibility alias, reject unsupported option shapes with `400`, and make
the frontend submit a real pipeline kind.

## Additional Finding From Pass 14

### P2 - Reviews foreign key only applies to newly created databases

Status: fixed in current working tree. `db.connect()` now detects legacy
`reviews` tables without a foreign key, rebuilds them with
`ON DELETE CASCADE`, copies only rows whose item still exists, and recreates the
review history index.

Evidence:
- The schema added `FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE`
  inside `CREATE TABLE IF NOT EXISTS reviews`.
- Existing SQLite tables are not altered by that statement, so any personal DB
  created before the schema change kept the unconstrained table.

Impact:
The code and audit implied review rows were protected by a foreign key, but
existing deployments would still allow orphan reviews and would not cascade
review history if item deletion/migration occurs later.

Recommended fix:
On connection, inspect `PRAGMA foreign_key_list(reviews)`. If no FK exists,
rename the legacy table, create the current table, copy valid rows joined to
existing items, drop the legacy table, and recreate `idx_reviews_item`.

## Existing Strengths

- Annotation routes already fail closed with `require_configured_auth()`.
- Pipeline tests cover many destructive ingest and supersession paths.
- `content/`, `vault/`, generated search output, backend DBs, and local env files
  are ignored rather than committed.
- The shared LLM client now prefers local command execution and only uses the API
  fallback when explicitly enabled.
- Translation and assist cache rows now record prompt version and LLM identity.

## Current State

No open actionable issues were found in the final review pass.

Verification completed:
- `python -m pytest` in `backend/`
- `npm run check`
- `npm run build`
- `npm audit --audit-level=moderate`
- `bash -n scripts/sync-content.sh`
- Python compile checks for backend, ingest, and build helper scripts
- `git diff --check`
- `PW_CONTENT_DIR="$PWD/vault" python3 scripts/sync_content.py` rejects generated
  output as a source path
- all `pipeline/scripts/tests/test_*.sh`

Non-blocking improvement ideas:
- Put the verification commands above into CI or a local `make verify` target.
- Add a browser smoke test for the ingest dashboard and source reader controls.
- Replace Pagefind's default UI integration with its newer component UI when
  search UI work is next touched.
- Add an explicit backup/restore command for `backend/data/study.db`.
