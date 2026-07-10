# Dependencies & Installation Guide

Tooling the vault's pipeline relies on, by category and rollout phase.
Last updated: 2026-05-28 (Phase 2 prep).

## Philosophy

Two kinds of dependency, handled differently:

1. **Python script libraries** are declared **inline** in each script's
   `# /// script` header and resolved automatically by `uv run`
   (`scripts/*.py` are `#!/usr/bin/env -S uv run --script`). You do
   **not** `pip install` these — `uv` fetches them per-script into an
   isolated, cached env. Versions that affect output bytes are **pinned**
   for reproducibility (see `scripts/extract.py`, which pins
   `pdfplumber==0.11.9` / `pillow==12.2.0` so rasterized images
   re-hash identically).
2. **External CLI tools** (git, ripgrep, an LLM CLI, yt-dlp, …) are
   installed once on the machine and found on `PATH`.

When an adapter shells out to a CLI it should also record the tool +
version in the source sidecar (`schema.md` §7.1: `transcript_tool`,
`transcript_model`, `ocr_tool`, …) so a transcript/OCR run is
reproducible and supersession churn is explainable.

---

## Core toolchain (required today)

Used by `ingest.py` and the `scripts/`. All present on this machine.

| Tool | Purpose | Install (macOS) | Verify |
|---|---|---|---|
| `git` | version control / audit trail | `brew install git` | `git --version` |
| `ripgrep` (`rg`) | keyword pre-pass, conflict scan | `brew install ripgrep` | `rg --version` |
| `curl` | URL fetch | (system) | `curl --version` |
| `shasum` | sha256 of sources | (system) | `shasum -a 256 <f>` |
| `python3` | scripts runtime (≥3.11) | `brew install python@3.11` / pyenv | `python3 --version` |
| `uv` | runs `scripts/*.py` + manages their inline deps | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `uv --version` |
| an **LLM provider** | ingest diff + mind-map extraction | see below | `codex --version` |

### LLM provider

`ingest.py`, language-page generation, and `scripts/generate-mindmap.py` use the
shared Python LLM client. Default local provider: `PW_LLM_PROVIDER=codex`, which
invokes `codex exec` directly without a shell adapter. This is the agentic,
subscription-backed mode: Codex can inspect and edit the isolated workdir seeded
by ingest.

For non-agentic single-completion mode, set `PW_LLM_PROVIDER=api` (or `openai`)
with `PW_LLM_API_KEY`; `PW_LLM_MODEL` selects the chat-completions model and
`PW_LLM_BASE_URL` can point at another OpenAI-compatible endpoint. The older
fallback switch, `PW_LLM_API_ENABLED=1`, still works when no local provider is
configured, but the provider value is the clearer way to choose API mode.

`LLM_CMD` is still available as an advanced custom stdin-to-stdout command:

```bash
LLM_CMD="gemini -p" python3 ingest.py <path-or-url>
```

Ingest's expansion pass and its single apply-failure retry rebuild and re-send
the full prompt today. That keeps every provider stateless and makes the retry
see the exact current candidate/expanded page set, but it spends the full prompt
budget again. Keep the retry count low unless that contract changes.

### Script libraries (auto-installed by `uv`, no action needed)

Declared inline in the relevant script headers; listed here for
reference only:

- `pyyaml` — `lint.py`, `generate-mocs.py`, `generate-mindmap.py`
- `ebooklib`, `beautifulsoup4`, `markdownify`, `trafilatura`, `pypdf`,
  `requests`, `mobi==0.4.1` (pinned; GPL-3 KindleUnpack-based conversion),
  `pdfplumber==0.11.9` (pinned), `pillow==12.2.0` (pinned)
  — `extract.py`

---

## Phase 1 — Mind maps ✅ (shipped)

`scripts/generate-mindmap.py` needs only `pyyaml` (inline) + the shared LLM
provider. Nothing extra to install beyond Codex or your chosen custom provider.

```bash
scripts/generate-mindmap.py            # all sources in the log
scripts/generate-mindmap.py --refresh  # re-call the LLM
```

---

## Phase 2 — YouTube (media ingest) — **externalized ASR**

Transcription is **delegated to the external `script_generation` service**
(`~/Documents/SourceCode/script_generation`, WhisperX large-v3 + pyannote
diarization on a remote GPU host). **The local wiki holds no media bytes and needs
NO local `yt-dlp`/`ffmpeg`/`whisper`** — those live on the remote host. The
vault side only needs the thin client + env. (expansion-plan §7.)

- **Client:** `transcript-remote` (the `transcript[client]` extra — needs only
  `requests`). `scripts/media-identity.py` shells out to it.

```bash
pip install -e "$HOME/Documents/SourceCode/script_generation[client]"
export TRANSCRIPT_SERVER=http://10.0.0.161:8000      # the remote GPU host
export TRANSCRIPT_TOKEN=<bearer token>               # if the server requires auth
transcript-remote "https://www.youtube.com/watch?v=<id>" -f json -o out.json   # smoke test
```

- **Env (required at ingest time):** `TRANSCRIPT_SERVER`, `TRANSCRIPT_TOKEN`.
- **Override hook:** `TRANSCRIPT_REMOTE_CMD` (default `transcript-remote`) lets
  you point `media-identity.py` at a specific client/binary; tests set it to a
  stub (`scripts/tests/stub-transcript-remote`) so the media e2e needs no
  network. Mirrors the custom-command pattern used by `LLM_CMD`.
- **Provenance:** the recipe is recorded **from the service's `-f json` `meta`
  payload** (`model`/`device`/`compute_type`/`diarized` + `language`), never
  hardcoded (schema §7.1). Fields the service doesn't yet emit (job_id, engine
  versions, download recipe) are recorded `null` until a recommended service
  enhancement lands (expansion-plan §7.1/§7.8).

> Network access to `TRANSCRIPT_SERVER` is required at ingest time; ASR jobs are
> async and can take minutes (the client polls; `--timeout` bounds the wait).

---

## Phase 3 — Podcast / RedNote (ASR + OCR) — NOT yet installed

Install these only when starting Phase 3.

### ffmpeg (audio handling for ASR)

```bash
brew install ffmpeg
ffmpeg -version
```

### ASR (speech → transcript) — pick one

| Option | Install | Notes |
|---|---|---|
| `whisper.cpp` | `brew install whisper-cpp` (+ download a model) | fast, local, no Python; record model in sidecar |
| `openai-whisper` | `uv tool install openai-whisper` | Python; needs ffmpeg + a model download |
| `faster-whisper` | inline `uv` dep in the adapter | CTranslate2 backend, faster |

Record `transcript_tool`+version, `transcript_model`, `transcript_params`
in the sidecar (`schema.md` §7.1).

### OCR (RedNote image-card notes → `.cards.md`) — pick one

| Option | Install | Notes |
|---|---|---|
| `tesseract` | `brew install tesseract tesseract-lang` | needs `chi_sim` for Chinese cards |
| `paddleocr` | inline `uv` dep | better on CJK; heavier |

Record `ocr_tool`+version, `ocr_model`, `ocr_params` in the sidecar.

### RedNote (小红书) fetch

No stable public CLI; expect login/anti-scraping friction. Phase 3 plans
a **manual-export fallback** (user supplies exported text/images/audio),
which needs none of the above fetchers — only the OCR/ASR step.

---

## Quick verification

```bash
for t in git rg curl shasum python3 uv codex yt-dlp; do
  printf '%-10s ' "$t"; command -v "$t" >/dev/null && echo OK || echo MISSING
done
```

`ffmpeg`, a whisper backend, and an OCR engine will show `MISSING` until
Phase 3 — expected.
