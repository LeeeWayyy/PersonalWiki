"""Ingest control plane: run the wiki ingest pipeline from a web request.

Honors the operational realities from the plan (§2a):
- ONE lock serializes ingest + rebuild (avoids racing ingest.py's preflight).
- The ingest engine applies LLM diffs and commits through git (`git apply
  --index` + staged commits), so a wiki folder that is not a git repo is
  auto-initialized with a baseline commit; set PW_INGEST_NO_AUTO_GIT=1 to
  block instead of creating one. Reading/serving works without git regardless.
- Preflight refuses a dirty tree and reports the offending paths + leftover
  .rejected/.failed artifacts instead of failing opaquely.
- Runs async, streaming stdout over SSE, with idle timeout + cancel.
- LLM auth is the operator's responsibility (local Codex, custom LLM_CMD, or API
  fallback in the daemon env); set PW_INGEST_STUB=1 to exercise the whole flow
  without spending budget.
"""
from __future__ import annotations
import os
import re
import json
import shlex
import shutil
import signal
import sys
import uuid
import asyncio
import logging
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO
from urllib.parse import urlparse

from . import settings

REPO = Path(__file__).resolve().parents[2]  # personal_wiki root
SCRIPTS_DIR = REPO / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import app_config  # noqa: E402
from vendor_content import init_git_snapshot, init_empty_content  # noqa: E402


CONTENT_DIR = app_config.content_dir(REPO).expanduser().resolve(strict=False)      # local wiki folder
DEFAULT_INGEST_SCRIPT = REPO / "pipeline" / "ingest.py"
INGEST_CMD = os.environ.get("INGEST_CMD", f"python3 {shlex.quote(str(DEFAULT_INGEST_SCRIPT))}")
REBUILD_CMD = os.environ.get("REBUILD_CMD", "")  # e.g. "npm --prefix /path/to/personal_wiki run build"
JOB_TIMEOUT_S = 1800
PROCESS_CLEANUP_TIMEOUT_S = 10
PROCESS_TERMINATE_GRACE_S = 5
JOB_LOG_LIMIT = 2000
JOB_TTL_S = 86400
DATA_DIR = app_config.abs_path(REPO, os.environ.get("PW_DATA_DIR") or str(REPO / "backend" / "data")).resolve(strict=False)
JOB_LOG_DIR = DATA_DIR / "logs"
STUB = os.environ.get("PW_INGEST_STUB") == "1"
AUTO_INIT_GIT = os.environ.get("PW_INGEST_NO_AUTO_GIT") != "1"

LOCK = asyncio.Lock()
JOBS: dict[str, "Job"] = {}
TERMINAL_STATUSES = {"blocked", "done", "error", "canceled"}
LOGGER = logging.getLogger(__name__)

# Keep these backend preflight constants next to the guard while ingest.py lacks
# a stable --preflight CLI. They intentionally mirror ingest.py's dirty scopes.
_WATCHED = ("wiki/", "sources/", ".wiki/log.md")
_LANG_WATCHED = ("lang/",)
_LEFTOVER = re.compile(r"\.(rejected|failed(\.\d+)?|apply-err(\.\d+)?)$")
# ingest.py's ensure_wiki_scaffold creates wiki/_taxonomy.md and commits it in
# the same run; its own preflight allows the placeholder as untracked. A run
# interrupted after scaffold but before commit leaves it untracked, which would
# otherwise wedge every later job here. Mirror ingest's tolerance — the
# authoritative placeholder-vs-user-edit check stays in ingest.py's preflight.
_SCAFFOLD_UNTRACKED = frozenset({"wiki/_taxonomy.md"})


class Job:
    def __init__(self, job_id: str):
        self.id = job_id
        self.status = "queued"          # queued|running|blocked|done|error|canceled
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.lines: deque[tuple[int, str]] = deque(maxlen=JOB_LOG_LIMIT)
        self.next_seq = 0
        self.result: dict = {}
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.cancel_requested = False
        self.ended = False
        self.log_path = JOB_LOG_DIR / f"{job_id}.log"
        self.log_failed = False
        self._lock = threading.RLock()
        self._log_fh: TextIO | None = None

    def emit(self, line: str):
        with self._lock:
            if self.ended:
                return
            self.updated_at = time.time()
            self.lines.append((self.next_seq, line))
            self.next_seq += 1
            self._append_log_line(line)

    def _append_log_line(self, line: str) -> None:
        try:
            if self._log_fh is None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_fh = self.log_path.open("a", encoding="utf-8", buffering=1)
            self._log_fh.write(f"{_log_timestamp()} {line}\n")
        except Exception as e:  # noqa
            if not self.log_failed:
                LOGGER.warning(
                    "failed to append job log job_id=%s path=%s: %s",
                    self.id,
                    self.log_path,
                    e,
                )
                self.log_failed = True

    def _close_log_handle(self) -> None:
        with self._lock:
            if self._log_fh is None:
                return
            try:
                self._log_fh.close()
            finally:
                self._log_fh = None

    @property
    def dropped_lines(self) -> int:
        with self._lock:
            return max(0, self.next_seq - len(self.lines))

    def visible_lines(self) -> list[str]:
        with self._lock:
            lines = [line for _, line in self.lines]
            dropped = max(0, self.next_seq - len(self.lines))
        if dropped:
            return [f"... {dropped} earlier log line(s) truncated ...", *lines]
        return lines

    def events_after(self, cursor: int) -> list[tuple[int, str]]:
        with self._lock:
            lines = list(self.lines)
            dropped = max(0, self.next_seq - len(self.lines))
        if lines and cursor < lines[0][0]:
            first_seq = lines[0][0]
            marker = f"... {dropped} earlier log line(s) truncated ..."
            return [(first_seq - 1, marker), *lines]
        return [(seq, line) for seq, line in lines if seq >= cursor]

    def finish_canceled(self) -> None:
        if self.ended:
            return
        self.status = "canceled"
        self.result = {"status": "canceled"}
        LOGGER.info("ingest job terminal job_id=%s status=canceled", self.id)
        self.emit("canceled by request")
        self.emit("__END__")
        with self._lock:
            self.ended = True
        self._close_log_handle()

    def finish_terminal(self, status: str, result: dict, lines: list[str] | None = None) -> None:
        if self.ended:
            return
        self.status = status
        self.result = result
        LOGGER.info("ingest job terminal job_id=%s status=%s", self.id, status)
        for line in lines or []:
            self.emit(line)
        self.emit("__END__")
        with self._lock:
            self.ended = True
        self._close_log_handle()


class _JobLogHandler(logging.Handler):
    """Tee backend records emitted during one job into that job's durable log."""

    def __init__(self, job: Job):
        super().__init__(level=logging.INFO)
        self.job = job
        self._emitting = False
        self.setFormatter(logging.Formatter("backend %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if self._emitting or self.job.ended:
            return
        try:
            self._emitting = True
            self.job.emit(self.format(record))
        finally:
            self._emitting = False


def _log_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _idle_sleep_guarded_argv(argv: list[str]) -> list[str]:
    """Wrap a macOS ingest command in an idle-sleep power assertion."""
    if sys.platform != "darwin":
        return argv
    caffeinate = shutil.which("caffeinate")
    if not caffeinate:
        return argv
    return [caffeinate, "-i", *argv]


def reap_jobs(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for job_id, job in list(JOBS.items()):
        if job.status in TERMINAL_STATUSES and now - job.updated_at > JOB_TTL_S:
            # Durable job logs are intentionally kept as an audit trail after
            # in-memory job metadata is reaped.
            JOBS.pop(job_id, None)


def get_job(job_id: str) -> Job | None:
    reap_jobs()
    return JOBS.get(job_id)


def _expand_status_path(content: Path, path: str) -> list[str]:
    target = content / path
    if not path.endswith("/") or not target.is_dir():
        return [path]
    files = [p.relative_to(content).as_posix() for p in target.rglob("*") if p.is_file()]
    return sorted(files) or [path]


def _porcelain_paths(line: str) -> list[str]:
    path = line[3:].strip()
    if " -> " not in path:
        return [path] if path else []
    return [part.strip() for part in path.split(" -> ", 1) if part.strip()]


def ensure_content_git(content: Path) -> tuple[bool, str]:
    """Guarantee the wiki folder exists and is a git repo ingest can commit into.

    The engine applies diffs via `git apply --index` and commits each run, so a
    missing folder or a plain (non-git) folder can't be ingested. When the folder
    is absent we create a fresh empty vault; when it exists but isn't a repo we
    snapshot it. Both auto-init unless PW_INGEST_NO_AUTO_GIT=1. Returns
    (ok, message); ok=False means ingest should block. Existing repos are left
    untouched.
    """
    if (content / ".git").exists():
        return True, ""
    if not AUTO_INIT_GIT:
        missing = not content.exists()
        return False, (
            f"wiki folder {'does not exist' if missing else 'is not a git repo'} ({content}) — "
            "ingest needs a git repo so it can commit changes. Unset PW_INGEST_NO_AUTO_GIT to "
            "let ingest create one automatically."
        )
    # Absent folder → make a fresh empty vault (dir + .gitignore + baseline
    # commit); ingest.py's ensure_wiki_scaffold fills the wiki structure on run.
    if not content.exists() or not any(content.iterdir()):
        ok, detail = init_empty_content(content)
        if not ok:
            return False, f"failed to create an empty vault at {content}: {detail}"
        return True, f"no wiki folder at {content}; {detail}"
    ok, detail = init_git_snapshot(
        content,
        user_email="ingest@personal-wiki.local",
        user_name="ingest",
        commit_message="ingest: initial vault snapshot",
        allow_empty=True,
    )
    if not ok:
        if detail == "git is not installed":
            return False, f"wiki folder is not a git repo ({content}) and git is not installed to create one"
        return False, f"failed to initialize git in {content}: {detail or 'unknown error'}"
    suffix = " with a baseline commit" if detail == "committed" else ""
    return True, f"wiki folder was not a git repo; initialized one at {content}{suffix}"


def preflight(options: dict | None = None) -> tuple[bool, str, list[str]]:
    """Return (ok, message, offending_paths)."""
    content = CONTENT_DIR
    if not content.exists():
        return False, f"wiki folder not found at {content} - set PW_CONTENT_DIR or run python3 scripts/vendor_content.py", []
    if not (content / ".git").exists():
        return False, f"wiki folder is not a git repo ({content}) — ingest needs git so it can commit changes", []
    try:
        result = subprocess.run(
            ["git", "-C", str(content), "-c", "core.quotepath=false", "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except Exception as e:  # noqa
        return False, f"git status failed: {e}", []
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        detail = detail.splitlines()[-1] if detail else f"exit code {result.returncode}"
        return False, f"git status failed: {detail}", []
    out = result.stdout
    watched = _LANG_WATCHED if (options or {}).get("kind") == "lang" else _WATCHED
    offending, leftover = [], []
    for line in out.splitlines():
        for path in _porcelain_paths(line):
            if any(path.startswith(w) or path == w.rstrip("/") for w in watched):
                offending.extend(_expand_status_path(content, path))
            if _LEFTOVER.search(path):
                leftover.append(path)
    offending = [p for p in offending if p not in _SCAFFOLD_UNTRACKED]
    if offending or leftover:
        msg = "Vault tree is not clean — ingest preflight would refuse."
        if leftover:
            msg += f" Leftover artifacts: {', '.join(leftover)}."
        return False, msg, offending + leftover
    return True, "clean", []


def _build_argv(target: str, options: dict) -> list[str]:
    argv = shlex.split(INGEST_CMD)
    kind = (options or {}).get("kind", "auto")
    if kind == "lang":
        argv += ["--profile", "lang"]
    elif kind in {"video", "audio", "image_note"}:
        argv += ["--kind", kind]
    section_heading = (options or {}).get("section_heading")
    if section_heading:
        argv += [
            "--section",
            rf"^{re.escape(section_heading)}$",
            "--section-label",
            section_heading,
        ]
    argv.append(target)
    return argv


async def _stream(proc: asyncio.subprocess.Process, job: Job, *, idle_timeout_s: int = JOB_TIMEOUT_S):
    assert proc.stdout
    while True:
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=idle_timeout_s)
        if not raw:
            break
        job.emit(raw.decode(errors="replace").rstrip("\n"))


async def _stream_until_exit(proc: asyncio.subprocess.Process, job: Job) -> int:
    await _stream(proc, job)
    try:
        return await asyncio.wait_for(proc.wait(), timeout=PROCESS_CLEANUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        await _kill_process_group(proc, job, "process with closed output")
        raise


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_process_group_exit(pgid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while _process_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.05, remaining))
    return True


async def _reap_process(proc: asyncio.subprocess.Process, job: Job, label: str) -> None:
    if proc.returncode is not None:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=PROCESS_CLEANUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        job.emit(f"warn: {label} top-level process was not reaped after process-group cleanup")


async def _kill_process_group(proc: asyncio.subprocess.Process, job: Job, label: str) -> None:
    # All controlled children are launched with start_new_session=True, so the
    # initial PID remains the process-group ID after the group leader exits.
    # Capturing it this way lets us terminate descendants even when SIGTERM
    # makes the top-level process exit before an ignoring child does.
    pgid = proc.pid
    if proc.returncode is not None and not _process_group_exists(pgid):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        await _reap_process(proc, job, label)
        return
    except OSError:
        if proc.returncode is None:
            proc.terminate()
    if await _wait_for_process_group_exit(pgid, PROCESS_TERMINATE_GRACE_S):
        await _reap_process(proc, job, label)
        return
    job.emit(f"warn: {label} process group still active after SIGTERM; escalating to SIGKILL")
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        if proc.returncode is None:
            proc.kill()
    if not await _wait_for_process_group_exit(pgid, PROCESS_CLEANUP_TIMEOUT_S):
        job.emit(f"warn: {label} process group still active after SIGKILL")
    await _reap_process(proc, job, label)


async def cancel_job(job_id: str) -> Job | None:
    job = get_job(job_id)
    if not job:
        return None
    if job.status in {"blocked", "done", "error", "canceled"}:
        return job
    job.cancel_requested = True
    proc = job.process
    if proc and proc.returncode is None:
        await _kill_process_group(proc, job, "cancel")
    job.finish_canceled()
    return job


def _staged_target_path(target: str) -> Path | None:
    if urlparse(target).scheme:
        return None
    target_path = Path(target)
    if not target_path.is_absolute():
        return None
    try:
        target_resolved = target_path.resolve(strict=False)
        stage_resolved = settings.STAGE_DIR.resolve(strict=False)
        target_resolved.relative_to(stage_resolved)
    except (OSError, RuntimeError, ValueError):
        return None
    return target_path


def _cleanup_staged_target(target: str, job_id: str) -> None:
    target_path = _staged_target_path(target)
    if target_path is None:
        return
    try:
        if target_path.is_file() or target_path.is_symlink():
            target_path.unlink(missing_ok=True)
    except Exception as e:  # noqa
        LOGGER.warning(
            "failed to remove staged ingest upload job_id=%s path=%s: %s",
            job_id,
            target_path,
            e,
        )


def sweep_stage_dir() -> None:
    """Remove staged uploads left behind by a previous crashed backend."""
    try:
        entries = list(settings.STAGE_DIR.iterdir())
    except FileNotFoundError:
        return
    except Exception as e:  # noqa
        LOGGER.warning("failed to inspect staged ingest upload dir path=%s: %s", settings.STAGE_DIR, e)
        return
    for path in entries:
        try:
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
        except Exception as e:  # noqa
            LOGGER.warning("failed to remove stale staged ingest upload path=%s: %s", path, e)


async def shutdown_jobs() -> None:
    """Terminate non-terminal ingest/rebuild processes before the app exits."""
    active = [job for job in JOBS.values() if job.status not in TERMINAL_STATUSES]
    for job in active:
        job.cancel_requested = True
        proc = job.process
        if proc and proc.returncode is None:
            job.emit("backend shutdown: terminating active process")
            await _kill_process_group(proc, job, "shutdown")
        if job.task and not job.task.done():
            job.task.cancel()
    if active:
        await asyncio.gather(
            *(job.task for job in active if job.task),
            return_exceptions=True,
        )
    for job in active:
        if not job.ended:
            job.finish_terminal("canceled", {"status": "canceled", "shutdown": True}, ["canceled by backend shutdown"])


async def run_job(job: Job, target: str, options: dict):
    job_handler: _JobLogHandler | None = None
    try:
        async with LOCK:
            job_handler = _JobLogHandler(job)
            LOGGER.addHandler(job_handler)
            if job.cancel_requested:
                job.finish_canceled()
                return
            job.status = "running"
            LOGGER.info("ingest job start job_id=%s target=%s options=%s", job.id, target, options)
            git_ok, git_msg = await asyncio.to_thread(ensure_content_git, CONTENT_DIR)
            if job.cancel_requested:
                job.finish_canceled()
                return
            if not git_ok:
                job.finish_terminal("blocked", {"status": "blocked", "offending": []}, ["BLOCKED: " + git_msg])
                return
            if git_msg:
                job.emit(git_msg)
            ok, msg, offending = await asyncio.to_thread(preflight, options)
            if job.cancel_requested:
                job.finish_canceled()
                return
            if not ok:
                job.finish_terminal(
                    "blocked",
                    {"status": "blocked", "offending": offending},
                    ["BLOCKED: " + msg, *[f"  · {p}" for p in offending]],
                )
                return

            if job.cancel_requested:
                job.finish_canceled()
                return

            job.emit(f"preflight: clean · target={target} · opts={json.dumps(options)}")

            if STUB:
                for step in ["sidecar", "extract text", "keyword pre-pass",
                             "LLM diff", "lint", "commit"]:
                    if job.cancel_requested:
                        job.finish_canceled()
                        return
                    await asyncio.sleep(0.2)
                    job.emit(f"[stub] {step} ✓")
                job.finish_terminal("done", {"status": "done", "stub": True})
                return

            argv = _build_argv(target, options)
            job.emit("$ " + " ".join(shlex.quote(a) for a in argv))
            launch_argv = _idle_sleep_guarded_argv(argv)
            if launch_argv is not argv:
                job.emit("power: preventing idle system sleep via caffeinate")
            proc_env = {
                **os.environ,
                "PW_CONTENT_DIR": str(CONTENT_DIR),
                "VAULT_CONTENT_DIR": str(CONTENT_DIR),
                "PYTHONUNBUFFERED": "1",
                "PW_RUN_ID": job.id,
            }
            try:
                proc = await asyncio.create_subprocess_exec(
                    *launch_argv, cwd=str(CONTENT_DIR),  # pipeline runs with cwd = the local wiki repo
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env=proc_env,
                    start_new_session=True,
                    limit=2**20,
                )
                job.process = proc
                if job.cancel_requested:
                    await _kill_process_group(proc, job, "cancel")
                    job.finish_canceled()
                    return
            except FileNotFoundError as e:
                job.finish_terminal("error", {"status": "error"}, [f"error: cannot launch ingest ({e})"])
                return
            try:
                rc = await _stream_until_exit(proc, job)
            except asyncio.TimeoutError:
                await _kill_process_group(proc, job, "ingest")
                job.finish_terminal(
                    "error",
                    {"status": "error", "timeout": True},
                    [f"error: ingest produced no output for {JOB_TIMEOUT_S}s - killed"],
                )
                return
            finally:
                if job.process is proc:
                    job.process = None

            if job.cancel_requested:
                job.finish_canceled()
                return

            if rc != 0:
                job.finish_terminal("error", {"status": "error", "code": rc}, [f"ingest exited with code {rc}"])
                return

            job.emit("ingest committed ✓")
            if REBUILD_CMD:
                job.emit("$ " + REBUILD_CMD)
                rp = await asyncio.create_subprocess_shell(
                    REBUILD_CMD, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                    limit=2**20)
                job.process = rp
                if job.cancel_requested:
                    await _kill_process_group(rp, job, "cancel")
                    job.finish_canceled()
                    return
                try:
                    rebuild_rc = await _stream_until_exit(rp, job)
                except asyncio.TimeoutError:
                    await _kill_process_group(rp, job, "rebuild")
                    job.finish_terminal(
                        "error",
                        {"status": "error", "rebuild_timeout": True},
                        [f"error: rebuild produced no output for {JOB_TIMEOUT_S}s - killed"],
                    )
                    return
                finally:
                    if job.process is rp:
                        job.process = None
                if job.cancel_requested:
                    job.finish_canceled()
                    return
                if rebuild_rc != 0:
                    job.finish_terminal(
                        "error",
                        {"status": "error", "rebuild_code": rebuild_rc},
                        [f"rebuild exited with code {rebuild_rc}"],
                    )
                    return
                job.emit("site rebuilt ✓")
            job.finish_terminal("done", {"status": "done"})
    except asyncio.CancelledError:
        LOGGER.info("ingest job cancelled job_id=%s", job.id)
        proc = job.process
        if proc and proc.returncode is None:
            await _kill_process_group(proc, job, "cancel")
        job.process = None
        if not job.ended:
            job.finish_terminal("canceled", {"status": "canceled", "shutdown": True}, ["canceled by backend shutdown"])
        raise
    except Exception as e:  # noqa
        LOGGER.exception("ingest job failed job_id=%s", job.id)
        proc = job.process
        if proc and proc.returncode is None:
            await _kill_process_group(proc, job, "ingest")
        job.process = None
        job.finish_terminal("error", {"status": "error", "error": str(e)}, [f"error: {e}"])
    finally:
        if job_handler is not None:
            LOGGER.removeHandler(job_handler)
        if job.status in TERMINAL_STATUSES:
            await asyncio.to_thread(_cleanup_staged_target, target, job.id)


def start_job(target: str, options: dict) -> str:
    reap_jobs()
    job_id = uuid.uuid4().hex[:12]
    job = Job(job_id)
    JOBS[job_id] = job
    job.task = asyncio.create_task(run_job(job, target, options))
    return job_id
