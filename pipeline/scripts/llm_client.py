#!/usr/bin/env python3
"""Shared LLM client for pipeline and backend runtime.

The default local provider is Codex, invoked directly as argv with the prompt as
a positional argument. `LLM_CMD` remains supported as an advanced override for
custom stdin/stdout commands, but the old `llm-codex.sh` command name is treated
as a legacy command string for the direct Codex provider so existing backend/.env
files stop depending on shell.
"""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_API_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_MODEL = "gpt-4o-mini"
DEFAULT_CODEX_MODEL = "codex"
CODEX_ARGS = ("exec", "--skip-git-repo-check", "--color", "never")
API_PROVIDERS = {"api", "openai"}
CODEX_PROVIDERS = {"codex"}
_STDOUT_DONE = object()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _provider_choice() -> str:
    return _env("PW_LLM_PROVIDER").lower()


def _api_requested() -> bool:
    return _provider_choice() in API_PROVIDERS


def _api_enabled() -> bool:
    return _api_requested() or _env("PW_LLM_API_ENABLED").lower() in TRUTHY


def _api_key() -> str:
    return _env("PW_LLM_API_KEY")


def raw_command() -> str:
    return _env("LLM_CMD")


def _legacy_codex_bridge(cmd: str) -> bool:
    if not cmd:
        return False
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return False
    return bool(argv and Path(argv[0]).name == "llm-codex.sh")


def command() -> str:
    cmd = raw_command()
    return "" if _legacy_codex_bridge(cmd) else cmd


def _codex_bin() -> str:
    return _env("PW_CODEX_BIN", "codex")


def codex_requested() -> bool:
    if _api_requested():
        return False
    return _provider_choice() in CODEX_PROVIDERS or _legacy_codex_bridge(raw_command())


def _provider_error() -> str | None:
    choice = _provider_choice()
    if choice and choice not in API_PROVIDERS | CODEX_PROVIDERS:
        return (
            f"unsupported PW_LLM_PROVIDER={choice!r}; "
            "use 'codex' for agentic Codex or 'api'/'openai' for API single-completion mode"
        )
    if _api_requested() and not _api_key():
        return "PW_LLM_PROVIDER=api/openai requires PW_LLM_API_KEY"
    return None


def codex_configured() -> bool:
    return codex_requested() and shutil.which(_codex_bin()) is not None


def command_configured() -> bool:
    if _api_requested():
        return False
    return bool(command() or codex_configured())


def configured() -> bool:
    return bool(_provider_error() is None and (command_configured() or (_api_enabled() and _api_key())))


def provider() -> str | None:
    if _provider_error() and not _api_requested():
        return None
    if _api_requested():
        return "api"
    if command():
        return "command"
    if codex_configured():
        return "codex"
    if _api_enabled() and _api_key():
        return "api"
    return None


def model() -> str | None:
    configured_model = _env("PW_LLM_MODEL")
    if configured_model:
        return configured_model
    if _api_requested():
        return DEFAULT_API_MODEL
    cmd = command()
    if cmd:
        try:
            argv = shlex.split(cmd)
        except ValueError:
            return "local-command"
        return Path(argv[0]).name if argv else "local-command"
    if codex_configured():
        return DEFAULT_CODEX_MODEL
    if _api_enabled() and _api_key():
        return DEFAULT_API_MODEL
    return None


def identity() -> dict[str, str | None]:
    return {"provider": provider(), "model": model()}


def _command_cwd() -> str | None:
    base = _env("PW_LLM_CMD_BASE_DIR")
    if not base:
        return None
    path = Path(base).expanduser()
    return str(path) if path.exists() else None


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        proc.kill()


def _enqueue_stdout_lines(stdout, lines: "queue.Queue[object]") -> None:
    try:
        for line in stdout:
            lines.put(line)
    except Exception:
        pass
    finally:
        lines.put(_STDOUT_DONE)


def _run_local(argv_or_cmd: list[str] | str, prompt: str, timeout: int, *, shell: bool, cwd: str | None) -> str | None:
    proc = subprocess.Popen(
        argv_or_cmd,
        shell=shell,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        proc.communicate()
        raise RuntimeError(f"LLM command timed out after {timeout}s") from exc
    if proc.returncode != 0:
        err = (stderr or "").strip()
        detail = err[-500:] if err else f"exit code {proc.returncode}"
        raise RuntimeError(f"LLM command failed: {detail}")
    return (stdout or "").strip() or None


# ── codex live progress ──────────────────────────────────────────────────────
# The codex call blocks for minutes; without this the ingest job stream goes
# silent and we can't tell "working" from "wedged". `codex exec --json` streams
# its events (token_count, agent messages, tool calls) to stdout as JSONL, so we
# read them in the main thread and emit concise heartbeats to stderr, which the
# backend already merges into the job stream. Because stdout now carries events,
# the diff (codex's final message) comes from `-o <file>` instead.
def _codex_progress_line(event: dict, state: dict) -> str | None:
    """Turn one codex event into a one-line heartbeat, or None to skip."""
    p = event.get("payload", event)
    t = p.get("type")
    fmt = lambda *parts: " · ".join(["codex", *[s for s in parts if s]])  # drop empty segments
    if t == "task_started":
        state["window"] = p.get("model_context_window")
        return None
    if t == "token_count":
        lt = (p.get("info") or {}).get("last_token_usage") or {}
        inp = lt.get("input_tokens")
        state["turn"] = state.get("turn", 0) + 1
        if inp is None:
            return None
        w = state.get("window")
        state["ctx"] = f"{inp // 1000}k/{w // 1000}k ctx" if w else f"{inp // 1000}k ctx"
        return fmt(f"turn {state['turn']}", state["ctx"])
    if t == "message" and p.get("role") == "assistant":
        for c in p.get("content", []):
            if c.get("type") == "output_text" and c.get("text", "").strip():
                return fmt(state.get("ctx"), " ".join(c["text"].split())[:120])
    if t in ("function_call", "local_shell_call", "custom_tool_call") or p.get("name"):
        name = p.get("name") or t
        args = p.get("arguments") or p.get("action") or ""
        brief = ""
        if isinstance(args, str):
            m = re.search(r"(Add|Update|Delete) File: (\S+)", args)
            if m:
                brief = f"{m.group(1)} {m.group(2)}"
            else:
                try:
                    cmd = json.loads(args).get("cmd", "")
                    brief = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                except Exception:
                    brief = args
        return fmt(state.get("ctx"), f"▸ {name} {brief.strip()[:70]}".rstrip())
    return None


def _run_codex(base_argv: list[str], prompt: str, timeout: int, cwd: str) -> str | None:
    """Run codex with --json (events → stdout → live progress) and -o (final
    message = the diff → a file). Streams heartbeats to stderr in the main
    thread — no rollout-file tailing. PW_CODEX_PROGRESS=0 silences the prints
    (events are still drained so the pipe can't stall). A helper thread drains
    stdout so the caller can enforce proc.wait(timeout=...) even when Codex is
    silent; this keeps backend asyncio.to_thread workers from hanging forever."""
    show = _env("PW_CODEX_PROGRESS") != "0"
    fd, diff_file = tempfile.mkstemp(prefix="pw-codex-msg-")
    os.close(fd)
    efd, err_file = tempfile.mkstemp(prefix="pw-codex-err-")
    os.close(efd)
    argv = [*base_argv, "--json", "-o", diff_file, prompt]
    proc = None
    stdout_thread = None
    stdout_lines: queue.Queue[object] = queue.Queue()
    state: dict = {}

    def flush_stdout_lines() -> bool:
        stdout_done = False
        while True:
            try:
                item = stdout_lines.get_nowait()
            except queue.Empty:
                return stdout_done
            if item is _STDOUT_DONE:
                stdout_done = True
                continue
            line = str(item)
            if show:
                try:
                    msg = _codex_progress_line(json.loads(line), state)
                except Exception:
                    msg = None
                if msg:
                    print(f"  {msg}", file=sys.stderr, flush=True)

    try:
        with open(err_file, "w") as errf:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=errf, text=True,
                cwd=cwd, start_new_session=True)
        assert proc.stdout is not None
        stdout_thread = threading.Thread(
            target=_enqueue_stdout_lines,
            args=(proc.stdout, stdout_lines),
            daemon=True,
            name="codex-stdout-drain",
        )
        stdout_thread.start()
        try:
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                flush_stdout_lines()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    proc.wait(timeout=min(0.2, remaining))
                except subprocess.TimeoutExpired:
                    pass
            if stdout_thread is not None:
                stdout_thread.join(timeout=1)
            flush_stdout_lines()
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(proc)
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
            if stdout_thread is not None:
                stdout_thread.join(timeout=1)
            flush_stdout_lines()
            raise RuntimeError(f"LLM command timed out after {timeout}s") from exc
        if proc.returncode != 0:
            err = Path(err_file).read_text(errors="replace").strip()
            detail = err[-500:] if err else f"exit code {proc.returncode}"
            raise RuntimeError(f"LLM command failed: {detail}")
        return Path(diff_file).read_text(encoding="utf-8", errors="replace").strip() or None
    finally:
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()
        for p in (diff_file, err_file):
            try:
                os.remove(p)
            except OSError:
                pass


def complete_command(prompt: str, timeout: int = 60, *, model: str | None = None) -> str | None:
    """Run a local LLM provider only: custom `LLM_CMD`, then direct Codex."""
    cmd = command()
    if cmd:
        return _run_local(cmd, prompt, timeout, shell=True, cwd=_command_cwd())
    if codex_configured():
        # Isolate codex from the content repo: it runs in a small working root
        # (-C) holding ONLY the pages the prompt references — never the whole
        # vault. That starves the agentic repo-exploration that otherwise piles
        # ~200k tokens onto a book-chapter prompt and overflows the window, and
        # keeps codex from dirtying the real tree (it edits copies; ingest
        # applies the emitted diff). PW_CODEX_WORKDIR, when set, is that seeded
        # dir — ingest owns/cleans it and pre-populates the candidate pages so
        # codex can MODIFY existing entries (an empty root stalls it: it can't
        # find the file it's told to edit). Unset (e.g. the keyword pre-pass,
        # which needs no files) → a throwaway empty scratch. workspace-write
        # lets codex assemble large diffs without the read-only thrash-spiral
        # that wedged prior runs.
        # ponytail: seed = candidate pages only; widen only if a diff legitimately
        # needs a page outside the candidate window (the expand loop covers that).
        # PW_LLM_MODEL pins the ingest/text model; unset → codex's own default.
        seeded = _env("PW_CODEX_WORKDIR")
        own = not (seeded and Path(seeded).is_dir())
        workdir = tempfile.mkdtemp(prefix="pw-codex-") if own else seeded
        argv = [_codex_bin(), *CODEX_ARGS, "-C", workdir, "--sandbox", "workspace-write"]
        model_name = (model or "").strip() or _env("PW_LLM_MODEL")
        if model_name:
            argv += ["-m", model_name]
        try:
            return _run_codex(argv, prompt, timeout, cwd=workdir)
        finally:
            if own:
                shutil.rmtree(workdir, ignore_errors=True)
    return None


def _complete_api(prompt: str, timeout: int, *, model: str | None) -> str | None:
    base_url = _env("PW_LLM_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")
    model_name = (model or "").strip() or _env("PW_LLM_MODEL", DEFAULT_API_MODEL)
    body = json.dumps({
        "model": model_name,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_api_key()}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read())
    try:
        return (data["choices"][0]["message"]["content"] or "").strip() or None
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("unexpected LLM API response shape") from exc


def complete(prompt: str, timeout: int = 60, *, model: str | None = None) -> str | None:
    """Run one completion.

    `PW_LLM_PROVIDER=codex` is the agentic, subscription-backed path used by the
    app default. `PW_LLM_PROVIDER=api` or `openai` is non-agentic: one chat
    completion over the OpenAI-compatible API. For compatibility,
    `PW_LLM_API_ENABLED=1` still acts as an API fallback when no local provider
    is configured, and `LLM_CMD` remains an advanced local command override.
    """
    err = _provider_error()
    if err:
        raise RuntimeError(err)
    if _api_requested():
        return _complete_api(prompt, timeout, model=model)
    if command_configured():
        return complete_command(prompt, timeout=timeout, model=model)
    if _api_enabled() and _api_key():
        return _complete_api(prompt, timeout, model=model)
    return None


def main() -> int:
    prompt = sys.stdin.read()
    timeout = int(_env("PW_LLM_TIMEOUT_S", "60"))
    try:
        out = complete(prompt, timeout=timeout)
    except Exception as exc:
        print(exc, file=sys.stderr)
        return 1
    if not out:
        print("LLM is not configured", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
