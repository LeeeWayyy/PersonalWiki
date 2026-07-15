#!/usr/bin/env python3
"""Shared LLM client for pipeline and backend runtime.

Built-in Codex, Claude, and Agy providers are invoked directly as argv with the
prompt on stdin. `LLM_CMD` remains an advanced custom stdin/stdout override; the
old `llm-codex.sh` command name maps to the direct Codex provider.
"""
from __future__ import annotations

import json
import hashlib
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
import urllib.parse
from pathlib import Path

TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_API_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_MODEL = "gpt-4o-mini"
CODEX_ARGS = ("exec", "--skip-git-repo-check", "--color", "never")
API_PROVIDERS = {"api", "openai"}
CODEX_PROVIDERS = {"codex"}
CLI_PROVIDER_ALIASES = {
    "claude": "claude-cli",
    "claude-cli": "claude-cli",
    "agy": "agy-cli",
    "agy-cli": "agy-cli",
}
_STDOUT_DONE = object()
_CLI_ENV_KEYS = {
    "HOME", "PATH", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "NO_COLOR", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY",
    "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
}
_CLI_PROVIDER_ENV_KEYS = {
    "codex": {"CODEX_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL"},
    "claude": {
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
    },
    "agy": {
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
    },
}


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


def _claude_bin() -> str:
    return _env("PW_CLAUDE_BIN", "claude")


def _agy_bin() -> str:
    return _env("PW_AGY_BIN", "agy")


def _requested_cli_provider() -> str | None:
    choice = _provider_choice()
    if choice in CODEX_PROVIDERS:
        return "codex"
    if choice in CLI_PROVIDER_ALIASES:
        return CLI_PROVIDER_ALIASES[choice]
    if _legacy_codex_bridge(raw_command()):
        return "codex"
    return None


def _cli_bin(provider_name: str) -> str:
    return {
        "codex": _codex_bin,
        "claude-cli": _claude_bin,
        "agy-cli": _agy_bin,
    }[provider_name]()


def _llm_workdir() -> str:
    return _env("PW_LLM_WORKDIR") or _env("PW_CODEX_WORKDIR")


def _codex_home() -> Path:
    home = _env("CODEX_HOME")
    return Path(home).expanduser() if home else Path.home() / ".codex"


def _effective_codex_setting(env_name: str, default: str) -> str:
    return _env(env_name) or default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUTHY


def cli_env(provider_name: str) -> dict[str, str]:
    """Minimal environment for built-in agent CLIs; never expose app secrets."""
    allowed = _CLI_ENV_KEYS | _CLI_PROVIDER_ENV_KEYS.get(provider_name, set())
    return {key: os.environ[key] for key in allowed if key in os.environ}


def codex_requested() -> bool:
    if _api_requested():
        return False
    return _requested_cli_provider() == "codex"


def _provider_error() -> str | None:
    choice = _provider_choice()
    if choice and choice not in API_PROVIDERS | CODEX_PROVIDERS | set(CLI_PROVIDER_ALIASES):
        return (
            f"unsupported PW_LLM_PROVIDER={choice!r}; "
            "use 'codex', 'claude-cli', 'agy-cli', or 'api'/'openai'"
        )
    if _api_requested() and not _api_key():
        return "PW_LLM_PROVIDER=api/openai requires PW_LLM_API_KEY"
    return None


def codex_configured() -> bool:
    return codex_requested() and shutil.which(_codex_bin()) is not None


def _cli_provider() -> str | None:
    selected = _requested_cli_provider()
    return selected if selected and shutil.which(_cli_bin(selected)) is not None else None


def command_configured() -> bool:
    if _api_requested():
        return False
    return bool(command() or _cli_provider())


def configured() -> bool:
    return bool(_provider_error() is None and (command_configured() or (_api_enabled() and _api_key())))


def provider() -> str | None:
    if _provider_error() and not _api_requested():
        return None
    if _api_requested():
        return "api"
    if command():
        return "command"
    if selected := _cli_provider():
        return selected
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
    if _cli_provider():
        # The CLI's compiled default is not discoverable without running a
        # completion. Leave it unset and bind the cache to the binary fingerprint.
        return None
    if _api_enabled() and _api_key():
        return DEFAULT_API_MODEL
    return None


def identity() -> dict[str, str | None]:
    return {"provider": provider(), "model": model()}


def _effective_model(model_override: str | None = None) -> str | None:
    override = (model_override or "").strip()
    return override or model()


def _resolved_file(token: str, *, cwd: str | None = None) -> Path | None:
    candidate = Path(token).expanduser()
    if not candidate.is_absolute() and cwd:
        candidate = Path(cwd) / candidate
    if candidate.is_file():
        return candidate.resolve()
    resolved = shutil.which(token)
    return Path(resolved).resolve() if resolved else None


def _file_fingerprint(path: Path) -> dict[str, int | str]:
    stat = path.stat()
    return {
        "name": path.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _content_fingerprint(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _command_fingerprint() -> str | None:
    """Hash the effective custom command without persisting command secrets."""
    cmd = command()
    if not cmd:
        return None
    cwd = _command_cwd()
    try:
        argv = shlex.split(cmd)
    except ValueError:
        argv = [cmd]
    files: list[dict[str, int | str]] = []
    for index, token in enumerate(argv):
        # The executable and explicit script/file arguments affect behavior.
        # Flags and inline values are already covered by the command hash.
        if index > 0 and token.startswith("-"):
            continue
        path = _resolved_file(token, cwd=cwd)
        if path is not None:
            files.append(_file_fingerprint(path))
    payload = json.dumps(
        {"cmd": cmd, "cwd": cwd or "", "files": files},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _binary_fingerprint(binary: str) -> str | None:
    path = _resolved_file(binary)
    if path is None:
        return None
    payload = json.dumps(
        _file_fingerprint(path), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _codex_binary_fingerprint() -> str | None:
    return _binary_fingerprint(_codex_bin())


def _codex_automation_profile() -> dict[str, object]:
    """Return every non-secret Codex automation input that can affect output."""
    profile: dict[str, object] = {
        "ignore_user_config": True,
        "ignore_rules": _env_bool("PW_CODEX_IGNORE_RULES", True),
        "disable_shell": _env_bool("PW_CODEX_DISABLE_SHELL", True),
    }
    if not profile["ignore_rules"]:
        rules_dir = _codex_home() / "rules"
        profile["rules"] = [
            {
                "path": path.relative_to(rules_dir).as_posix(),
                "sha256": _content_fingerprint(path),
            }
            for path in sorted(rules_dir.rglob("*"))
            if path.is_file()
        ] if rules_dir.is_dir() else []
    workdir = _llm_workdir()
    configured_workdir = Path(workdir).expanduser() if workdir else None
    root = (
        configured_workdir.resolve()
        if configured_workdir is not None and configured_workdir.is_dir()
        else Path(tempfile.gettempdir()).resolve()
    )
    profile["workspace_instructions"] = [
        {
            "path": str(candidate),
            "sha256": _content_fingerprint(candidate),
        }
        for candidate in (parent / "AGENTS.md" for parent in (root, *root.parents))
        if candidate.is_file()
    ]
    return profile


def _codex_automation_fingerprint() -> str:
    payload = json.dumps(
        _codex_automation_profile(), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _public_api_base_url() -> str | None:
    if provider() != "api":
        return None
    raw = _env("PW_LLM_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def execution_identity(model_override: str | None = None) -> dict[str, str | None]:
    """Return non-secret settings that can change a completion's output."""
    selected_provider = provider()
    return {
        "provider": selected_provider,
        "model": _effective_model(model_override),
        "reasoning": (
            _effective_codex_setting(
                "PW_CODEX_REASONING_EFFORT", "medium"
            )
            if selected_provider == "codex"
            else None
        ),
        "verbosity": (
            _effective_codex_setting(
                "PW_CODEX_VERBOSITY", "low"
            )
            if selected_provider == "codex"
            else None
        ),
        "api_base_url": _public_api_base_url(),
        "command_fingerprint": (
            _command_fingerprint() if selected_provider == "command" else None
        ),
        "binary_fingerprint": (
            _binary_fingerprint(_cli_bin(selected_provider))
            if selected_provider in {"claude-cli", "agy-cli"}
            else None
        ),
        "codex_binary_fingerprint": (
            _codex_binary_fingerprint() if selected_provider == "codex" else None
        ),
        "codex_config_fingerprint": None,
        "codex_automation_fingerprint": (
            _codex_automation_fingerprint()
            if selected_provider == "codex"
            else None
        ),
    }


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


def _run_local(argv_or_cmd: list[str] | str, prompt: str, timeout: int, *, shell: bool,
               cwd: str | None, env: dict[str, str] | None = None) -> str | None:
    proc = subprocess.Popen(
        argv_or_cmd,
        shell=shell,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
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


def _run_plain_cli(provider_name: str, prompt: str, timeout: int,
                   model_override: str | None) -> str | None:
    short_name = provider_name.removesuffix("-cli")
    configured = _llm_workdir()
    own = not (configured and Path(configured).is_dir())
    workdir = tempfile.mkdtemp(prefix=f"pw-{short_name}-") if own else configured
    model_name = _effective_model(model_override)
    if provider_name == "claude-cli":
        argv = [
            _claude_bin(), "--safe-mode", "--print", "--output-format", "text",
            "--no-session-persistence", "--permission-mode", "plan", "--tools", "",
        ]
    else:
        argv = [
            _agy_bin(), "--print", "--mode", "plan", "--sandbox",
            "--print-timeout", f"{timeout}s",
        ]
    if model_name:
        argv += ["--model", model_name]
    try:
        return _run_local(
            argv, prompt, timeout, shell=False, cwd=workdir,
            env=cli_env(short_name),
        )
    finally:
        if own:
            shutil.rmtree(workdir, ignore_errors=True)


def _codex_heartbeat_interval() -> float:
    raw = _env("PW_CODEX_HEARTBEAT_S", "30")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 30.0


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
    def fmt(*parts: str) -> str:
        return " · ".join(["codex", *[part for part in parts if part]])
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
    if t == "agent_message" and p.get("message", "").strip():
        return fmt(state.get("ctx"), " ".join(p["message"].split())[:120])
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
    argv = [*base_argv, "--json", "-o", diff_file, "-"]
    proc = None
    stdout_thread = None
    stdin_thread = None
    stdout_lines: queue.Queue[object] = queue.Queue()
    state: dict = {}
    heartbeat_s = _codex_heartbeat_interval()

    def write_stdin() -> None:
        try:
            assert proc is not None and proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

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
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf, text=True,
                cwd=cwd, env=cli_env("codex"), start_new_session=True)
        assert proc.stdout is not None
        stdin_thread = threading.Thread(
            target=write_stdin,
            daemon=True,
            name="codex-stdin-write",
        )
        stdout_thread = threading.Thread(
            target=_enqueue_stdout_lines,
            args=(proc.stdout, stdout_lines),
            daemon=True,
            name="codex-stdout-drain",
        )
        stdin_thread.start()
        stdout_thread.start()
        try:
            started = time.monotonic()
            deadline = time.monotonic() + timeout
            next_heartbeat = started + heartbeat_s
            while proc.poll() is None:
                flush_stdout_lines()
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                if show and heartbeat_s > 0 and now >= next_heartbeat:
                    elapsed = int(now - started)
                    print(
                        f"  codex · still running ({elapsed}s elapsed, timeout {timeout}s)",
                        file=sys.stderr,
                        flush=True,
                    )
                    next_heartbeat = now + heartbeat_s
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
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if proc.stdout is not None:
                proc.stdout.close()
            if stdin_thread is not None:
                stdin_thread.join(timeout=1)
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
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()
        for p in (diff_file, err_file):
            try:
                os.remove(p)
            except OSError:
                pass


def complete_command(prompt: str, timeout: int = 60, *, model: str | None = None) -> str | None:
    """Run a custom command or one of the built-in local CLI providers."""
    cmd = command()
    if cmd:
        env = None
        if model and model.strip():
            env = os.environ.copy()
            env["PW_LLM_MODEL"] = model.strip()
        return _run_local(
            cmd, prompt, timeout, shell=True, cwd=_command_cwd(), env=env
        )
    selected_provider = _cli_provider()
    if selected_provider in {"claude-cli", "agy-cli"}:
        return _run_plain_cli(selected_provider, prompt, timeout, model)
    if selected_provider == "codex":
        # Isolate codex from the content repo: it runs in a small working root
        # (-C) holding ONLY the pages the prompt references — never the whole
        # vault. That starves the agentic repo-exploration that otherwise piles
        # ~200k tokens onto a book-chapter prompt and overflows the window, and
        # keeps codex from dirtying the real tree (it edits copies; ingest
        # applies the emitted diff). PW_LLM_WORKDIR, when set, is that seeded
        # dir — ingest owns/cleans it and pre-populates the candidate pages so
        # codex can MODIFY existing entries (an empty root stalls it: it can't
        # find the file it's told to edit). Unset (e.g. the keyword pre-pass,
        # which needs no files) → a throwaway empty scratch. workspace-write
        # lets codex assemble large diffs without the read-only thrash-spiral
        # that wedged prior runs.
        # ponytail: seed = candidate pages only; widen only if a diff legitimately
        # needs a page outside the candidate window (the expand loop covers that).
        # PW_LLM_MODEL pins the ingest/text model; unset → codex's own default.
        seeded = _llm_workdir()
        own = not (seeded and Path(seeded).is_dir())
        workdir = tempfile.mkdtemp(prefix="pw-codex-") if own else seeded
        argv = [_codex_bin(), *CODEX_ARGS]
        automation = _codex_automation_profile()
        argv.append("--ignore-user-config")
        if automation["ignore_rules"]:
            argv.append("--ignore-rules")
        if automation["disable_shell"]:
            argv += ["--disable", "shell_tool"]
        reasoning = _effective_codex_setting(
            "PW_CODEX_REASONING_EFFORT", "medium"
        )
        if reasoning:
            argv += ["-c", f"model_reasoning_effort={json.dumps(reasoning)}"]
        verbosity = _effective_codex_setting(
            "PW_CODEX_VERBOSITY", "low"
        )
        if verbosity:
            argv += ["-c", f"model_verbosity={json.dumps(verbosity)}"]
        argv += ["-C", workdir, "--sandbox", "workspace-write"]
        model_name = _effective_model(model)
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


def _twice(call):
    """Retry one provider failure/empty response once."""
    for attempt in range(2):
        try:
            result = call()
        except Exception as exc:
            if attempt or "timed out" in str(exc).lower():
                raise
            continue
        if result is not None or attempt:
            return result
    return None


def complete(prompt: str, timeout: int = 60, *, model: str | None = None) -> str | None:
    """Run one completion.

    `PW_LLM_PROVIDER=codex`, `claude-cli`, or `agy-cli` selects a built-in local
    CLI. `PW_LLM_PROVIDER=api` or `openai` is non-agentic: one chat completion
    over the OpenAI-compatible API. For compatibility,
    `PW_LLM_API_ENABLED=1` still acts as an API fallback when no local provider
    is configured, and `LLM_CMD` remains an advanced local command override.
    """
    err = _provider_error()
    if err:
        raise RuntimeError(err)
    if _api_requested():
        return _twice(lambda: _complete_api(prompt, timeout, model=model))
    if command_configured():
        try:
            result = _twice(lambda: complete_command(prompt, timeout=timeout, model=model))
        except Exception:
            if _api_enabled() and _api_key():
                return _twice(lambda: _complete_api(prompt, timeout, model=model))
            raise
        if result is not None or not (_api_enabled() and _api_key()):
            return result
        return _twice(lambda: _complete_api(prompt, timeout, model=model))
    if _api_enabled() and _api_key():
        return _twice(lambda: _complete_api(prompt, timeout, model=model))
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
