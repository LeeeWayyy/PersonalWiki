#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Vision-captioner for image manifests written by extract.py.

Reads `<source>.assets/_manifest.md` (frontmatter-only YAML), and for
every image entry that's neither captioned nor marked terminally
failed:
  1. Run a pre-LLM heuristic for "obviously decorative" images
     (cover/logo/qr/spacer/tiny). Decorative → mark, skip LLM.
  2. Otherwise call a vision-capable CLI (gemini, codex, or claude)
     to produce a 1–3 sentence caption.
  3. If the model output is the literal token `DECORATIVE`, mark the
     entry decorative and skip persisting a caption.
  4. Else trim and store as `caption`.

After each entry, the manifest is rewritten atomically (`.tmp` + replace)
so a crash mid-loop leaves the manifest consistent for re-runs.

Usage:
    scripts/caption.py <manifest-path-or-assets-dir>
                       [--backend gemini|codex|claude|agy]
                       [--model MODEL]   # caption model, independent of the
                                         # ingest model (PW_LLM_MODEL)
                       [--source-lang Chinese|English|...]
                       [--recaption]              # re-caption all entries
                       [--retry-errors]           # retry terminal-error entries
                       [--dry-run]                # don't call the LLM
                       [--limit N]                # cap calls per run
                       [--jobs N]                 # parallel calls (default 4)

Backend defaults to the same CLI the pipeline's LLM uses (codex when the LLM
is codex), so there is one tool to authenticate. Override with CAPTION_BACKEND.

Env vars (override CLI defaults):
    CAPTION_BACKEND, CAPTION_MODEL, CAPTION_LANG, CAPTION_LIMIT, CAPTION_JOBS
    GEMINI_BIN (gemini backend only; point at a renamed CLI such as `agy`)

Captioning is the slow part of an image-heavy ingest (one vision call per
non-decorative image). Calls run in a thread pool (--jobs, default 4) since each
is an independent blocking subprocess; only the manifest write is serialized.

See plan/image-ingest-plan.md §4–§5.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date
from pathlib import Path

# Local helper module (sibling file).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from asset_manifest import ImageEntry, read_manifest, write_manifest  # noqa: E402

PROMPT_TEMPLATE = """\
Describe this figure in 1-3 sentences for a knowledge-base index.

Rules:
- Open with the figure type: "A bar chart of...", "A scatter plot
  showing...", "A photograph of...", "A diagram illustrating..."
- Mention axes/labels if it's a chart, or the central object if a
  photo, or the relationship being depicted if a diagram.
- If the figure is decorative (chapter opener, ornamental, publisher
  logo, ISBN/QR code, copyright page), output exactly: DECORATIVE
- Do NOT speculate about what the surrounding text claims. Describe
  only what's visually in the image.
- Output language: {lang}.
"""

DEFAULT_BACKEND = "gemini"
DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "codex": "gpt-5-mini",
    "claude": "claude-haiku-4-5-20251001",
    "agy": "Gemini 3.5 Flash (Low)",  # cheap vision model; agy fronts Gemini/Claude/GPT
}


def _default_backend() -> str:
    """Caption with the same CLI the pipeline's LLM uses, so there is a single
    tool to authenticate and manage. An explicit CAPTION_BACKEND always wins;
    otherwise use codex whenever llm_client resolves the LLM to codex (via
    PW_LLM_PROVIDER=codex or a legacy llm-codex.sh LLM_CMD), else gemini."""
    explicit = os.environ.get("CAPTION_BACKEND")
    if explicit:
        return explicit
    try:
        import llm_client
        if llm_client.codex_requested() or llm_client.provider() == "codex":
            return "codex"
    except Exception:
        pass
    return DEFAULT_BACKEND

# Filename patterns that strongly suggest a decorative image (cover,
# copyright, TOC, logo, QR). Includes Chinese / Japanese terms common
# in CN-published EPUBs. NFKC + lowercase before matching.
DECORATIVE_FILENAME_RX = re.compile(
    r"(cover|title[-_]?page|toc|frontispiece|publisher|logo|qr|"
    r"封面|版权|扉页|目录|出版社)",
    re.IGNORECASE)

DECORATIVE_TOKEN = "DECORATIVE"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("target",
                    help="Path to a `_manifest.md` or to its containing "
                         "<source>.assets/ directory.")
    ap.add_argument("--backend", default=_default_backend(),
                    choices=("gemini", "codex", "claude", "agy"))
    ap.add_argument("--model", default=os.environ.get("CAPTION_MODEL"))
    ap.add_argument("--source-lang", default=os.environ.get("CAPTION_LANG",
                                                            "match the source "
                                                            "document's primary "
                                                            "language"))
    ap.add_argument("--recaption", action="store_true",
                    help="Re-caption every entry, even those already captioned.")
    ap.add_argument("--retry-errors", action="store_true",
                    help="Re-attempt entries flagged caption_error_kind=terminal.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be processed without calling the LLM.")
    ap.add_argument("--limit", type=int,
                    default=int(os.environ.get("CAPTION_LIMIT", "0")),
                    help="Cap the number of LLM calls this run (0 = unlimited).")
    ap.add_argument("--jobs", type=int,
                    default=int(os.environ.get("CAPTION_JOBS", "4")),
                    help="Parallel caption calls (default 4, or CAPTION_JOBS). "
                         "Each is an independent, blocking CLI/API subprocess, so "
                         "the pool cuts wall-clock roughly linearly.")
    args = ap.parse_args()

    target = Path(args.target).expanduser().resolve()
    if target.is_dir():
        assets_dir = target
    elif target.is_file() and target.name == "_manifest.md":
        assets_dir = target.parent
    else:
        print(f"caption: not a manifest or assets dir: {target}", file=sys.stderr)
        return 2

    if not (assets_dir / "_manifest.md").is_file():
        print(f"caption: no _manifest.md under {assets_dir}", file=sys.stderr)
        return 2

    source_id, entries = read_manifest(assets_dir)
    if not entries:
        print(f"caption: no entries in manifest at {assets_dir}", file=sys.stderr)
        return 0

    backend = args.backend
    model = args.model or DEFAULT_MODELS[backend]
    model_label = model or "default"
    if not args.dry_run and shutil.which(backend) is None:
        print(f"caption: backend CLI '{backend}' not found in PATH", file=sys.stderr)
        return 3

    prompt = PROMPT_TEMPLATE.format(lang=args.source_lang)

    todo = [e for e in entries if _needs_caption(e, args.recaption,
                                                 args.retry_errors)]
    if not todo:
        print("caption: nothing to do", file=sys.stderr)
        return 0

    today = _date.today().isoformat()

    # Phase A (serial, cheap — no LLM): resolve missing files and the decorative
    # heuristic; collect everything that still needs a real vision call.
    to_dispatch: list[tuple[ImageEntry, Path]] = []
    heuristic_hits = False
    for entry in todo:
        file_path = assets_dir / entry.file
        if not file_path.is_file():
            print(f"caption: missing image file {file_path}; skip", file=sys.stderr)
            continue
        if _is_decorative_heuristic(entry, file_path):
            entry.decorative = True
            # Clear any stale caption fields — entry may have been captioned
            # previously and is now reclassified (e.g. file shrunk after a
            # re-encode).
            entry.caption = None
            entry.caption_source = None
            entry.caption_model = None
            entry.caption_error = None
            entry.caption_error_kind = None
            entry.caption_at = today
            print(f"caption: heuristic-decorative {entry.file}", file=sys.stderr)
            heuristic_hits = True
            continue
        to_dispatch.append((entry, file_path))
    if heuristic_hits:
        write_manifest(assets_dir, source_id, entries)

    if args.dry_run:
        for entry, _ in to_dispatch:
            print(f"caption: would caption {entry.file} via {backend}/{model_label}",
                  file=sys.stderr)
        return 0

    if args.limit and len(to_dispatch) > args.limit:
        print(f"caption: reached --limit={args.limit}", file=sys.stderr)
        to_dispatch = to_dispatch[:args.limit]
    if not to_dispatch:
        return 0

    # Phase B: dispatch in parallel. Each call is an independent, blocking
    # subprocess (I/O-bound), so a thread pool gives near-linear speedup. The
    # slow _dispatch runs lock-free; only the field-commit + manifest write is
    # serialized, so an entry is never serialized half-updated and the manifest
    # stays crash-consistent for re-runs.
    jobs = max(1, min(args.jobs, len(to_dispatch)))
    lock = threading.Lock()

    def work(pair: tuple[ImageEntry, Path]) -> None:
        entry, file_path = pair
        output: str | None = None
        exc: BaseException | None = None
        try:
            output = _dispatch(backend, model, prompt, file_path)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError) as e:
            exc = e
        with lock:
            status = _apply_caption_result(entry, output, exc, backend,
                                           model_label, today)
            write_manifest(assets_dir, source_id, entries)
            print(f"caption: {status} {entry.file}", file=sys.stderr)

    if jobs == 1:
        for pair in to_dispatch:
            work(pair)
    else:
        # `with` guarantees the pool is shut down (threads joined) before we
        # return; map() surfaces any worker exception when iterated.
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            for _ in ex.map(work, to_dispatch):
                pass

    return 0


def _apply_caption_result(entry: ImageEntry, output: str | None,
                          exc: BaseException | None, backend: str,
                          model_label: str, today: str) -> str:
    """Classify one dispatch outcome onto the entry (no manifest write — the
    caller owns that under a lock). Returns a short status string for logging."""
    entry.caption_at = today
    entry.caption_model = f"{backend}:{model_label}"
    if isinstance(exc, subprocess.CalledProcessError):
        err = (exc.stderr or exc.stdout or "").strip()[:200]
        kind = _classify_error(err, exc.returncode)
        entry.caption = None
        entry.caption_source = None
        entry.caption_error = f"exit {exc.returncode}: {err}"
        entry.caption_error_kind = kind
        return f"{kind} error: {err[:80]}"
    if exc is not None:                          # TimeoutExpired / FileNotFoundError
        entry.caption = None
        entry.caption_source = None
        entry.caption_error = str(exc)[:200]
        entry.caption_error_kind = "transient"
        return f"transient error: {exc}"

    text = (output or "").strip()
    if text.upper() == DECORATIVE_TOKEN:
        entry.decorative = True
        entry.caption = None
        entry.caption_source = None
        entry.caption_error = None
        entry.caption_error_kind = None
        return "LLM-decorative"
    if not text:
        entry.caption = None
        entry.caption_source = None
        entry.caption_error = "empty output"
        entry.caption_error_kind = "transient"
        return "empty output"
    entry.caption = text
    entry.caption_source = "vision"
    # A successful caption resets BOTH the error AND a stale decorative flag
    # (an earlier heuristic may no longer apply).
    entry.decorative = False
    entry.caption_error = None
    entry.caption_error_kind = None
    return f"ok ({len(text)} chars)"


def _needs_caption(entry: ImageEntry, recaption: bool,
                   retry_errors: bool) -> bool:
    if recaption:
        return True
    if entry.decorative:
        return False
    if entry.caption is not None:
        return False
    if entry.caption_error_kind == "terminal" and not retry_errors:
        return False
    return True


def _is_decorative_heuristic(entry: ImageEntry, file_path: Path) -> bool:
    """Cheap-test: is this entry obviously decorative without a vision call?"""
    # Tiny dimensions → decorative.
    w, h = (entry.dimensions + [0, 0])[:2]
    if w and h and (w < 200 or h < 200):
        return True
    # Small file, not SVG → likely an icon.
    if entry.bytes < 20_000:
        ext = file_path.suffix.lower()
        if ext != ".svg":
            return True
    # Filename hint (slug, image filename, OR origin_ref item path).
    candidates: list[str] = [file_path.name]
    for ref in entry.origin_refs:
        if ref.item:
            candidates.append(ref.item)
        if ref.url:
            candidates.append(ref.url)
    for cand in candidates:
        norm = unicodedata.normalize("NFKC", cand).lower()
        if DECORATIVE_FILENAME_RX.search(norm):
            return True
    return False


def _classify_error(stderr: str, exit_code: int) -> str:
    """Sort an error message/exit-code into transient or terminal."""
    text = (stderr or "").lower()
    transient_signals = (
        "rate limit", "rate-limit", "ratelimit",
        "timed out", "timeout",
        "429",                     # HTTP 429 Too Many Requests
        "too many requests",
        "503", "502", "504",
        "temporarily unavailable",
        "service unavailable",
        "connection reset",
        "connection refused",
        "no capacity",
        "resource_exhausted",
        "econnreset", "etimedout", "esocket",   # Node-style errno labels
    )
    if any(sig in text for sig in transient_signals):
        return "transient"
    return "terminal"


def _dispatch(backend: str, model: str, prompt: str, image: Path) -> str:
    """Call the configured vision CLI. Returns stdout string."""
    if backend == "gemini":
        return _dispatch_gemini(model, prompt, image)
    if backend == "codex":
        return _dispatch_codex(model, prompt, image)
    if backend == "claude":
        return _dispatch_claude(model, prompt, image)
    if backend == "agy":
        return _dispatch_agy(model, prompt, image)
    raise ValueError(f"unknown backend: {backend}")


def _dispatch_gemini(model: str, prompt: str, image: Path) -> str:
    """Gemini CLI: prompt is argv (`-p`), image attached via `@<path>`.

    Asset paths are guaranteed space-free by §3.1 naming conventions
    (slug-sanitized dir + `<sha12>.<ext>` filename). If a path with
    spaces ever sneaks through, refuse rather than guess at escaping.
    """
    abs_path = str(image)
    if " " in abs_path:
        raise RuntimeError(f"unexpected space in asset path: {abs_path}")
    # GEMINI_BIN lets you point at a renamed CLI (e.g. `agy`) without code changes.
    cmd = [os.environ.get("GEMINI_BIN", "gemini"), "-m", model, "-p", f"{prompt} @{abs_path}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True,
                          timeout=120)
    return proc.stdout


def _dispatch_codex(model: str | None, prompt: str, image: Path) -> str:
    """Codex CLI: `codex exec [-m <model>] -i <path> -`, prompt on stdin.

    Mirrors the text LLM path sandbox/color flags. The caller passes a mini
    default unless CAPTION_MODEL/--model overrides it."""
    cmd = ["codex", "exec", "--skip-git-repo-check",
           "--sandbox", "read-only", "--color", "never"]
    if model:
        cmd += ["-m", model]
    cmd += ["-i", str(image), "-"]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          check=True, timeout=180)
    return proc.stdout


def _dispatch_claude(model: str, prompt: str, image: Path) -> str:
    """Claude CLI: stream-json with base64-encoded image content block."""
    mime_by_ext = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }
    mime = mime_by_ext.get(image.suffix.lower(), "application/octet-stream")
    payload = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": mime,
                    "data": base64.b64encode(image.read_bytes()).decode(),
                }},
                {"type": "text", "text": prompt},
            ],
        },
    }
    # Current Claude Code requires stream-json OUT when IN is stream-json, and
    # rejects --bare (it drops user auth). Run from a neutral cwd so the repo's
    # project hooks / CLAUDE.md don't load; user-level auth is unaffected.
    cmd = ["claude", "--print", "--input-format", "stream-json",
           "--output-format", "stream-json", "--verbose", "--model", model]
    proc = subprocess.run(cmd, input=json.dumps(payload),
                          capture_output=True, text=True, check=True,
                          timeout=180, cwd=tempfile.gettempdir())
    return _parse_claude_result(proc.stdout)


def _parse_claude_result(stdout: str) -> str:
    """Extract the final text from Claude Code's stream-json events: prefer the
    terminal `result` event, else concatenate assistant text blocks."""
    result = ""
    texts: list[str] = []
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") == "result" and obj.get("subtype") == "success":
            result = obj.get("result") or result
        elif obj.get("type") == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
    return (result or "\n".join(texts)).strip()


def _dispatch_agy(model: str, prompt: str, image: Path) -> str:
    """agy CLI (agentic, multi-model gateway): no image flag, so the prompt
    points it at the absolute path and it reads the file with its own tools.
    --dangerously-skip-permissions auto-approves that local read in non-
    interactive mode; run from a neutral cwd so project context doesn't load."""
    abs_path = str(image.resolve())
    full = f"{prompt}\n\nDescribe the image file at this exact path: {abs_path}"
    cmd = ["agy", "-p", full, "--model", model, "--dangerously-skip-permissions"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True,
                          timeout=180, cwd=tempfile.gettempdir())
    return proc.stdout


if __name__ == "__main__":
    raise SystemExit(main())
