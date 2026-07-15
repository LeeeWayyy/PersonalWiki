#!/usr/bin/env python3
"""
ingest.py — one-command ingest for the LLM-wiki (Python port of ingest.sh).

Behavior-preserving port of the bash orchestrator (§14 / "port to Python"):
same pipeline, same gates, same commit semantics. The logic-dense work
already lives in scripts/*.py (source-identity, extract, build-prompt,
apply-diff, lint gates, …); this is the orchestrator that sequences them.
Verified by scripts/tests/test_ingest_e2e.sh.

Usage:
  ./ingest.py [--section REGEX] [--section-label LABEL] [--limit N]
              [--images-only] <path-or-url>

Env: PW_LLM_PROVIDER=codex by default in app config; LLM_CMD is an advanced
     stdin/stdout command override. Also CAND_CAP (20), CAPTION_BACKEND/MODEL/
     LANG/LIMIT.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

# Layout: this orchestrator + scripts/ live in the pipeline tooling dir; the
# vault content usually lives at the project-level `content/` sibling, with a
# legacy fallback to `pipeline/content/`. We run with cwd = the content repo so
# ingest's relative `git apply/add/commit` operate there, while invoking helper
# scripts by absolute path. The cwd change + env export are side effects in
# main(), so `import ingest` never moves the caller's cwd.
INVOCATION_CWD = Path.cwd()  # process start cwd, to resolve relative inputs
TOOLING_ROOT = Path(__file__).resolve().parent
SCRIPTS_PATH = TOOLING_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_PATH))
from _util import (  # noqa: E402
    chapter_order_from_lines,
    default_vault_root,
    normalize_name,
    parse_log_line,
    sha256_of,
    split_frontmatter,
    today,
)
import llm_client  # noqa: E402
from source_citations import iter_source_citations  # noqa: E402

VAULT_ROOT = default_vault_root(TOOLING_ROOT)
SCRIPTS = str(SCRIPTS_PATH)
CHAPTERED_EXTS = (".epub", ".mobi", ".azw", ".azw3", ".pdf")
# Outline-less PDFs extract as `## Page N` sections; those aren't chapters.
PAGE_SECTION_RX = re.compile(r"^Page \d+$")
# Fallback only (books with no chapter markers): a `## ` section with fewer body
# chars than this is structural (cover/TOC/title page) and skipped.
CHAPTER_MIN_CHARS = int(os.environ.get("PW_CHAPTER_MIN_CHARS", "200"))
SECTION_LABEL_MAX_CHARS = int(os.environ.get("PW_SECTION_LABEL_MAX_CHARS", "200"))

# Distinguish an actual chapter heading from a section heading, so sections are
# ingested grouped under their parent chapter and out-of-chapter front/back
# matter (cover, preface, glossary, afterword) is excluded. Defaults cover CJK
# 第…章 / 第…节 and basic English; override per book/language via env.
_CJK_NUM = r"[\d〇零一二三四五六七八九十百千两]"


def _compile_env_rx(env_name: str, default: str) -> re.Pattern:
    return re.compile(os.environ.get(env_name) or default, re.IGNORECASE)


CHAPTER_HEADING_RX = _compile_env_rx(
    "PW_CHAPTER_HEADING_RX", rf"(?:第{_CJK_NUM}+章)|(?:^\s*(?:chapter|part)\b)")
SECTION_HEADING_RX = _compile_env_rx(
    "PW_SECTION_HEADING_RX", rf"(?:第{_CJK_NUM}+节)|(?:^\s*section\b)")
NONCONTENT_HEADING_RX = _compile_env_rx(
    "PW_NONCONTENT_HEADING_RX",
    r"(?:^|[/_.\-\s])(?:cover|copyright|contents?|toc|nav|title(?:page)?|"
    r"afterword|epilogue|acknowledg(?:e)?ments?|glossary|bibliography|"
    r"references?|index|colophon|about\s+the\s+author)(?:$|[/_.\-\s])|"
    r"^(?:版权(?:信息)?|目录|封面|后记|致谢|词汇表|参考文献|索引|译后记)$",
)

WIKI_PATHSPEC = [
    "wiki/entities/", "wiki/topics/", "wiki/_index/", "wiki/_maps/",
    "wiki/_taxonomy.md",
]
WIKI_PAGE_RX = re.compile(r"^wiki/(entities|topics)/.+\.md$")
TAXONOMY_PATH = "wiki/_taxonomy.md"
TAXONOMY_PLACEHOLDER = """# Taxonomy

## Domain
- `general/knowledge`

## Form
- `concept`

## Reserved
- `taxonomy-gap`
"""
SCAFFOLD_PATHS: list[str] = []

# ── temp-file management (mirrors the bash cleanup trap: remove the working
#    temps + DIFF_FILE.raw on exit; PRESERVE on-failure artifacts) ──────────
_TEMPS: list[str] = []
_DIFF_RAW: list[str] = []
_TEMP_DIRS: list[str] = []
_RUN_CREATED_FILES: set[str] = set()
_RUN_CREATED_DIRS: set[str] = set()
_PREEXISTING_SOURCE_PATHS: set[str] = set()
_ROLLBACK_EXTRA_PATHS: set[str] = set()
_INGEST_LOCK_FH = None
_ACTIVE_SOURCE_IDENTITY: subprocess.Popen[str] | None = None
_ROLLBACK_ON_FAILURE = False


def _log_prefix() -> str:
    run_id = os.environ.get("PW_RUN_ID", "").strip()
    return f"ingest[{run_id}]" if run_id else "ingest"


def mktemp() -> str:
    fd, p = tempfile.mkstemp()
    os.close(fd)
    _TEMPS.append(p)
    return p


def mktempdir() -> str:
    d = tempfile.mkdtemp(prefix="pw-ingest-workset-")
    _TEMP_DIRS.append(d)
    return d


def _seed_workset(workdir: str, candidates_file: str, expand_file: str) -> None:
    """Copy the candidate/expanded wiki pages into codex's isolated workdir so
    it can read/modify the exact files the prompt references — without seeing
    the whole vault (context overflow) or dirtying the real tree (it edits
    copies; ingest applies the emitted diff). Idempotent: mirrors the CURRENT
    candidate+expand set (both grow across the expand/retry loop). Paths are
    cwd-relative (cwd == the content repo)."""
    wanted: set[str] = {TAXONOMY_PATH}
    for f in (candidates_file, expand_file):
        if Path(f).is_file():
            wanted.update(ln.strip() for ln in read(f).splitlines() if ln.strip())
    for rel in wanted:
        src = Path(rel)
        if not src.is_file():
            continue
        dst = Path(workdir) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


_TAXONOMY_ADDITION_RX = re.compile(
    r"^- `([a-z0-9]+(?:[-/][a-z0-9]+)*)`$"
)


def _taxonomy_additions(before: str, after: str) -> list[str]:
    """Return added tags; reject taxonomy deletions, rewrites, and prose edits."""
    old = before.splitlines()
    old_index = 0
    section = ""
    additions: list[str] = []
    seen = set(old)
    for line in after.splitlines():
        if line.startswith("## "):
            section = line[3:]
        if old_index < len(old) and line == old[old_index]:
            old_index += 1
            continue
        match = _TAXONOMY_ADDITION_RX.fullmatch(line)
        if not match:
            raise ValueError(f"only taxonomy bullet additions are allowed, got {line!r}")
        if section not in {"Domain", "Form"}:
            raise ValueError("taxonomy bullets may be added only under Domain or Form")
        if line in seen:
            raise ValueError(f"duplicate taxonomy bullet: {line}")
        seen.add(line)
        additions.append(match.group(1))
    if old_index != len(old):
        raise ValueError("existing taxonomy lines may not be deleted, rewritten, or reordered")
    return additions


def _cleanup() -> None:
    for p in _TEMPS + _DIFF_RAW:
        try:
            os.remove(p)
        except OSError:
            pass
    for d in _TEMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)


def _source_path_snapshot() -> set[str]:
    """Existing sources/ paths before this run creates any new source artifacts."""
    root = Path("sources")
    if not root.exists():
        return set()
    return {p.as_posix() for p in root.rglob("*")}


def _tracked_file(path: str) -> bool:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "--error-unmatch", "--", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _tracked_under(path: str) -> bool:
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "--", path],
        text=True,
        capture_output=True,
        check=False,
    )
    return bool(r.stdout.strip())


def _register_run_created_file(path: str) -> None:
    rel = Path(path).as_posix()
    if rel not in _PREEXISTING_SOURCE_PATHS and not _tracked_file(rel):
        _RUN_CREATED_FILES.add(rel)


def _register_run_created_dir(path: str) -> None:
    rel = Path(path).as_posix()
    if rel not in _PREEXISTING_SOURCE_PATHS and not _tracked_under(rel):
        _RUN_CREATED_DIRS.add(rel)


def _register_new_source_artifacts(dest: str) -> None:
    _register_run_created_file(dest)
    _register_run_created_file(SRC["SIDECAR"])
    if SRC.get("AUDIT_JSON"):
        _register_run_created_file(SRC["AUDIT_JSON"])
    _register_run_created_dir(f"{dest}.assets")


def _register_untracked_under(path: str) -> None:
    """Register untracked files produced below a clean, tool-owned directory."""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "--others",
         "--exclude-standard", "--", path],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return
    for rel in result.stdout.splitlines():
        if rel:
            _register_run_created_file(rel)


def _git_path_status(paths: list[str]) -> tuple[bool, str]:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "status", "--porcelain", "--", *paths],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return False, detail or f"git status exited {result.returncode}"
    return not result.stdout.strip(), result.stdout.strip()


def _rollback_after_apply_failure() -> bool:
    """Drop staged/worktree wiki/log changes created after git apply --index.

    Once source cleanup removes a newly-created source artifact, leaving staged
    wiki edits around can strand citations to a source_id that no longer exists.
    Preflight guarantees these paths were clean before ingest touched them.
    """
    if not _ROLLBACK_ON_FAILURE:
        return True
    run_provenance = [
        ".wiki/log.md",
        SRC.get("DEST", ""),
        SRC.get("SIDECAR", ""),
        SRC.get("AUDIT_JSON", ""),
    ]
    if SRC.get("DEST"):
        run_provenance.extend([
            f"{SRC['DEST']}.assets",
            f"{SRC['DEST']}.assets/_manifest.md",
        ])
    paths = [
        p for p in [*WIKI_PATHSPEC, *run_provenance, *_ROLLBACK_EXTRA_PATHS]
        if p and (Path(p).exists() or _tracked_under(p))
    ]
    if not paths:
        return True

    # `git restore --staged --worktree` in one command fails for newly-added
    # paths because they do not exist in HEAD, leaving the entire index dirty.
    # Record additions, unstage first, then restore only paths tracked in HEAD
    # and remove the now-untracked additions from this clean-baseline run.
    staged_result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "diff", "--cached",
         "--name-only", "--", *paths],
        text=True,
        capture_output=True,
        check=False,
    )
    added_result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "diff", "--cached",
         "--name-only", "--diff-filter=A", "--", *paths],
        text=True,
        capture_output=True,
        check=False,
    )
    for operation, result in (("inspect staged paths", staged_result),
                              ("inspect staged additions", added_result)):
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            eout(f"rollback failed — could not {operation}"
                 + (f": {detail}" if detail else ""))
            return False
    staged = staged_result.stdout.splitlines()
    added = set(added_result.stdout.splitlines())
    if staged:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "restore", "--staged", "--", *staged],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            eout("rollback failed — could not restore the Git index"
                 + (f": {detail}" if detail else ""))
            return False
    tracked_result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "--", *paths],
        text=True,
        capture_output=True,
        check=False,
    )
    if tracked_result.returncode != 0:
        detail = (tracked_result.stderr or tracked_result.stdout).strip()
        eout("rollback failed — could not enumerate tracked paths"
             + (f": {detail}" if detail else ""))
        return False
    tracked = tracked_result.stdout.splitlines()
    if tracked:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "restore", "--worktree", "--", *tracked],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            eout("rollback failed — could not restore the working tree"
                 + (f": {detail}" if detail else ""))
            return False
    wiki_additions = {
        rel for rel in added
        if WIKI_PAGE_RX.match(rel) or rel.startswith("wiki/_index/")
        or rel == "wiki/_taxonomy.md" or rel == ".wiki/log.md"
    }
    for rel in sorted(wiki_additions, reverse=True):
        path = Path(rel)
        if path.is_file() and not _tracked_file(rel):
            try:
                path.unlink()
            except OSError as exc:
                eout(f"warn — failed to remove rolled-back added path {rel}: {exc}")
                return False
    verification_paths = [*WIKI_PATHSPEC, ".wiki/log.md", *_ROLLBACK_EXTRA_PATHS]
    asset_dir = f"{SRC.get('DEST', '')}.assets"
    verification_paths.extend(
        path for path in run_provenance
        if path and path != asset_dir and _tracked_under(path)
    )
    clean, detail = _git_path_status(sorted(set(verification_paths)))
    if not clean:
        eout("rollback failed — wiki/provenance paths remain dirty"
             + (f": {detail}" if detail else ""))
        return False
    return True


def _cleanup_run_created_artifacts() -> None:
    """Remove only untracked source artifacts known to have been created here."""
    for rel in sorted(_RUN_CREATED_FILES, reverse=True):
        p = Path(rel)
        if p.is_file() and not _tracked_file(rel):
            try:
                p.unlink()
            except OSError as exc:
                eout(f"warn — failed to remove run-created file {rel}: {exc}")
    for rel in sorted(_RUN_CREATED_DIRS, key=lambda s: s.count("/"), reverse=True):
        p = Path(rel)
        if p.is_dir() and not _tracked_under(rel):
            shutil.rmtree(p, ignore_errors=True)


def out(msg: str) -> None:
    print(f"{_log_prefix()}: {msg}", flush=True)


def eout(msg: str) -> None:
    print(f"{_log_prefix()}: {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> None:
    print(f"{_log_prefix()}: {msg}", file=sys.stderr, flush=True)
    if SRC.get("DEST"):
        _register_untracked_under(f"{SRC['DEST']}.assets")
    rollback_ok = _rollback_after_apply_failure()
    if rollback_ok:
        _cleanup_run_created_artifacts()
    else:
        eout("rollback was incomplete; preserving source provenance so any remaining "
             "wiki citations cannot be orphaned")
    _cleanup()
    sys.exit(1)


_TERMINATING = False


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM a child, escalating to SIGKILL if it ignores a 5s grace period."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _handle_termination(signum, _frame) -> None:
    """Let backend cancel/idle-timeout SIGTERM run normal failed-run cleanup."""
    global _TERMINATING
    if _TERMINATING:
        return
    _TERMINATING = True
    proc = _ACTIVE_SOURCE_IDENTITY
    if proc is not None and proc.poll() is None:
        _terminate(proc)
    name = signal.Signals(signum).name
    die(f"terminated by {name}")


signal.signal(signal.SIGTERM, _handle_termination)
signal.signal(signal.SIGINT, _handle_termination)


# ── subprocess helpers ────────────────────────────────────────────────────────
def git_capture(*args: str) -> str:
    """git with core.quotepath=false, stdout captured; fail loud on git errors."""
    r = subprocess.run(["git", "-c", "core.quotepath=false", *args],
                       text=True, capture_output=True)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        die(f"git {' '.join(args)} failed" + (f": {detail}" if detail else ""))
    return r.stdout


def git_run(*args: str, check: bool = True) -> int:
    r = subprocess.run(["git", "-c", "core.quotepath=false", *args])
    if check and r.returncode != 0:
        die(f"git {' '.join(args)} failed")
    return r.returncode


def run_stream(cmd: list[str], *, env: dict | None = None, check: bool = True) -> int:
    """Run a command inheriting stdio (so its output streams to the user)."""
    r = subprocess.run(cmd, env=env)
    if check and r.returncode != 0:
        die(f"command failed: {' '.join(cmd)}")
    return r.returncode


def analyzer_env() -> dict[str, str]:
    """Isolate cheap analyzer reasoning from the main renderer process."""
    env = os.environ.copy()
    env["PW_CODEX_REASONING_EFFORT"] = (
        os.environ.get("PW_ANALYZE_REASONING_EFFORT", "low").strip() or "low"
    )
    return env


def llm(prompt_text: str, *, soft: bool, model: str | None = None) -> str:
    """Invoke the shared LLM client; return stdout-like text.

    soft=True tolerates failure and returns an empty string. Main ingest calls
    use soft=False so a missing or failed completion aborts before mutation.
    """
    os.environ.setdefault("PW_CODEX_DISABLE_SHELL", "1")
    timeout = int(os.environ.get("PW_LLM_TIMEOUT_S", "1800"))
    try:
        out = llm_client.complete(prompt_text, timeout=timeout, model=model)
    except Exception as exc:
        if soft:
            return ""
        die(f"LLM call failed: {exc}")
    if out is None and not soft:
        detail = (
            "configured provider returned empty output"
            if llm_client.configured()
            else "no local or API LLM provider is configured"
        )
        die(f"LLM call failed: {detail}")
    return out or ""


def parse_shell_assignments(text: str) -> dict[str, str]:
    """Parse `KEY=<shell-quoted-value>` lines (source-identity.py output)."""
    out_: dict[str, str] = {}
    for line in text.splitlines():
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        try:
            parts = shlex.split(v)
        except ValueError as exc:
            raise RuntimeError(f"malformed source identity output for {k!r}: {exc}") from exc
        out_[k] = parts[0] if parts else ""
    return out_


def run_source_identity(src_input: str) -> subprocess.CompletedProcess[str]:
    """Resolve a document source through source-identity's two-phase protocol.

    The child stages no vault files before ``IDENTITY_READY=new``. We register
    every future destination before replying ``PUBLISH``, so cancellation can
    never strand a source file that the orchestrator does not know how to clean.
    """
    global _ACTIVE_SOURCE_IDENTITY
    proc = subprocess.Popen(
        [f"{SCRIPTS}/source-identity.py", "--reserve-handshake", src_input],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _ACTIVE_SOURCE_IDENTITY = proc
    lines: list[str] = []
    ready = ""
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            parsed = parse_shell_assignments("".join(lines))
            ready = parsed.get("IDENTITY_READY", "")
            if ready:
                if ready == "new":
                    required = ("DEST", "SIDECAR")
                    if any(not parsed.get(key) for key in required):
                        proc.kill()
                        raise RuntimeError("source identity reservation omitted DEST or SIDECAR")
                    _register_run_created_file(parsed["DEST"])
                    _register_run_created_file(parsed["SIDECAR"])
                    _register_run_created_dir(f"{parsed['DEST']}.assets")
                    assert proc.stdin is not None
                    proc.stdin.write("PUBLISH\n")
                    proc.stdin.flush()
                break
        stdout_rest, stderr = proc.communicate()
        lines.append(stdout_rest)
    except BaseException:
        if proc.poll() is None:
            _terminate(proc)
        raise
    finally:
        _ACTIVE_SOURCE_IDENTITY = None
    return subprocess.CompletedProcess(proc.args, proc.returncode, "".join(lines), stderr)


def write(path: str, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


def final_newline(text: str) -> str:
    return text if text.endswith("\n") else f"{text}\n"


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


# ── profile root resolution ────────────────────────────────────────────────
def resolve_vault_root(profile: str) -> Path:
    """Active child root for the profile. `$VAULT_CONTENT_DIR` (already folded
    into VAULT_ROOT at import) names the BASE content root; `--profile lang`
    selects the `lang/` subtree. Guard against a base that's already a lang
    subtree (would double-nest to lang/lang)."""
    if profile == "lang":
        if VAULT_ROOT.name == "lang":
            die("$VAULT_CONTENT_DIR already points at a 'lang' subtree; it must "
                "name the BASE content root (the 'lang/' subtree is selected by --profile)")
        return VAULT_ROOT / "lang"
    return VAULT_ROOT  # wiki: true no-op (guards the e2e oracle)


# ── preflight ─────────────────────────────────────────────────────────────────
CHAPTER_INTELLIGENCE_CACHE_IGNORE = ".wiki/chapter-intelligence-cache/"
INGEST_LOCK_IGNORE = ".wiki/ingest.lock"
_LEFTOVER_RX = re.compile(r"\.(rejected|failed(\.\d+)?|apply-err(\.\d+)?)$")


def ensure_local_cache_ignore() -> None:
    """Keep local runtime state out of existing custom vault repos."""
    exclude_raw = git_capture("rev-parse", "--git-path", "info/exclude").strip()
    if not exclude_raw:
        die("git did not report an info/exclude path for the content repo")
    exclude = Path(exclude_raw)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
    rules = {line.strip() for line in current.splitlines()}
    repo_root = Path(git_capture("rev-parse", "--show-toplevel").strip())
    prefix = Path.cwd().resolve().relative_to(repo_root.resolve())
    local = "" if prefix == Path(".") else f"{prefix.as_posix()}/"
    missing = [
        rule for rule in (
            f"{local}{CHAPTER_INTELLIGENCE_CACHE_IGNORE}",
            f"{local}{INGEST_LOCK_IGNORE}",
        )
        if rule not in rules
    ]
    if not missing:
        return
    separator = "" if not current or current.endswith("\n") else "\n"
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(separator + "".join(f"{rule}\n" for rule in missing))


def ensure_wiki_scaffold(profile: str = "wiki") -> list[str]:
    """Create the minimal wiki structure ingest needs in an empty content repo."""
    ensure_local_cache_ignore()
    if profile != "wiki":
        return []
    for rel in ("wiki/entities", "wiki/topics", "wiki/_index", "sources", ".wiki"):
        Path(rel).mkdir(parents=True, exist_ok=True)
    taxonomy = Path("wiki/_taxonomy.md")
    if taxonomy.exists():
        untracked = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard", "--", taxonomy.as_posix()],
            text=True,
            capture_output=True,
            check=False,
        ).stdout.splitlines()
        if taxonomy.as_posix() in untracked and taxonomy.read_text(encoding="utf-8", errors="replace") == TAXONOMY_PLACEHOLDER:
            return [taxonomy.as_posix()]
        return []
    taxonomy.write_text(TAXONOMY_PLACEHOLDER, encoding="utf-8")
    return [taxonomy.as_posix()]


def _status_paths(line: str) -> list[str]:
    path = line[3:].strip()
    return [part.strip() for part in path.split(" -> ", 1) if part.strip()]


def _expand_status_path(content: Path, path: str) -> list[str]:
    target = content / path
    if not path.endswith("/") or not target.is_dir():
        return [path]
    files = [p.relative_to(content).as_posix() for p in target.rglob("*") if p.is_file()]
    return sorted(files) or [path]


def preflight_report(profile: str = "wiki", allowed_untracked: list[str] | None = None,
                     content_root: Path | None = None) -> tuple[bool, str, list[str]]:
    """Structured dirty-tree gate shared by CLI ingest and the web backend."""
    content = (content_root or (VAULT_ROOT.parent if profile == "lang" else VAULT_ROOT)).resolve()
    if not content.is_dir():
        return False, f"wiki folder not found at {content}", []
    if not (content / ".git").exists():
        return False, f"wiki folder is not a git repo ({content})", []
    try:
        result = subprocess.run(
            ["git", "-C", str(content), "-c", "core.quotepath=false", "status",
             "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except Exception as exc:
        return False, f"git status failed: {exc}", []
    if result.returncode:
        detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip().splitlines()[-1]
        return False, f"git status failed: {detail}", []

    prefix = "lang/" if profile == "lang" else ""
    allowed = set(allowed_untracked or [])
    taxonomy = content / TAXONOMY_PATH
    if (not allowed_untracked and taxonomy.is_file()
            and taxonomy.read_text(encoding="utf-8", errors="replace") == TAXONOMY_PLACEHOLDER):
        allowed.add(TAXONOMY_PATH)
    offending: list[str] = []
    leftovers: list[str] = []
    for line in result.stdout.splitlines():
        status = line[:2]
        for path in _status_paths(line):
            staged = status[0] not in {" ", "?"}
            untracked = status == "??"
            wiki_dirty = profile == "wiki" and any(
                path.startswith(scope) or path == scope.rstrip("/") for scope in WIKI_PATHSPEC
            )
            generated_dirty = profile == "lang" and (
                path.startswith("lang/_reading/") or path == "lang/_reading"
            )
            provenance_dirty = not untracked and (
                path == f"{prefix}.wiki/log.md" or path.startswith(f"{prefix}sources/")
            )
            stale_asset = untracked and path.startswith(f"{prefix}sources/") and ".assets/" in path
            ignored = untracked and (path == f"{prefix}{INGEST_LOCK_IGNORE}" or path in allowed)
            if not ignored and (staged or wiki_dirty or generated_dirty or provenance_dirty or stale_asset):
                offending.extend(_expand_status_path(content, path))
            if _LEFTOVER_RX.search(path):
                leftovers.append(path)
    offending = list(dict.fromkeys(offending))
    leftovers = list(dict.fromkeys(leftovers))
    if offending or leftovers:
        message = "Vault tree is not clean — ingest preflight would refuse."
        if leftovers:
            message += f" Leftover artifacts: {', '.join(leftovers)}."
        return False, message, offending + leftovers
    return True, "clean", []


def preflight(profile: str = "wiki", allowed_untracked: list[str] | None = None) -> None:
    ok, message, offending = preflight_report(profile, allowed_untracked)
    if ok:
        return
    eout(message)
    for path in offending:
        print(f"    {path}", file=sys.stderr)
    raise SystemExit(1)


def check_deps() -> None:
    for tool in ("rg", "git", "shasum", "python3", "uv"):
        if shutil.which(tool) is None:
            die(f"{tool} required")


def acquire_content_ingest_lock() -> None:
    """Serialize content mutations across CLI/backend ingest processes."""
    global _INGEST_LOCK_FH
    lock_path = Path(".wiki") / "ingest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _INGEST_LOCK_FH = open(lock_path, "a+", encoding="utf-8")
    out(f"waiting for ingest lock: {lock_path}")
    while True:
        try:
            fcntl.flock(_INGEST_LOCK_FH.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(30)
            out(f"still waiting for ingest lock: {lock_path}")


def release_content_ingest_lock() -> None:
    """Release the process-level ingest lock before delegating to child runs."""
    global _INGEST_LOCK_FH
    if _INGEST_LOCK_FH is None:
        return
    fcntl.flock(_INGEST_LOCK_FH.fileno(), fcntl.LOCK_UN)
    _INGEST_LOCK_FH.close()
    _INGEST_LOCK_FH = None


# ── candidate collection ──────────────────────────────────────────────────────
def wiki_page_count() -> int:
    return sum(
        1 for sub in ("entities", "topics")
        for _ in Path("wiki", sub).rglob("*.md")
    ) if Path("wiki").is_dir() else 0


def wiki_page_paths() -> list[str]:
    return sorted(
        str(p) for sub in ("entities", "topics")
        for p in Path("wiki", sub).rglob("*.md")
    ) if Path("wiki").is_dir() else []


def _intelligence_search_terms(intelligence_file: str) -> list[dict]:
    """Return deduplicated retrieval terms ordered by editorial importance."""
    try:
        artifact = json.loads(read(intelligence_file))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"invalid chapter intelligence for candidate retrieval: {exc}")
    if not isinstance(artifact, dict):
        die("invalid chapter intelligence for candidate retrieval: expected object")

    page_decisions: dict[tuple[str, str], tuple[int, bool]] = {}
    for candidate in artifact.get("page_candidates", []):
        if not isinstance(candidate, dict):
            continue
        page_type = str(candidate.get("page_type") or "")
        name = str(candidate.get("name") or "").strip()
        importance = candidate.get("importance")
        if name and page_type in {"entity", "topic"} and isinstance(importance, int):
            page_decisions[(page_type, normalize_name(name))] = (
                importance,
                candidate.get("required") is True,
            )

    terms: dict[str, dict] = {}

    def add(term: object, *, importance: int, required: bool) -> None:
        if not isinstance(term, str):
            return
        value = " ".join(term.split())
        if not value or len(value) > 512:
            return
        key = normalize_name(value)
        existing = terms.get(key)
        row = {
            "term": value,
            "importance": max(1, min(5, importance)),
            "required": required,
        }
        if existing is None or (row["required"], row["importance"]) > (
            existing["required"], existing["importance"]
        ):
            terms[key] = row

    for key, page_type in (("entities", "entity"), ("topics", "topic")):
        for item in artifact.get(key, []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            importance = item.get("importance")
            if not isinstance(importance, int):
                importance = 1
            candidate_importance, required = page_decisions.get(
                (page_type, normalize_name(name)), (0, False)
            )
            merged_importance = max(importance, candidate_importance)
            add(name, importance=merged_importance, required=required)
            for alias in item.get("aliases", []):
                add(alias, importance=merged_importance, required=required)

    return sorted(
        terms.values(),
        key=lambda row: (-int(row["required"]), -row["importance"], row["term"].casefold()),
    )


def collect_candidates(intelligence_file: str, candidates_file: str, cap: int) -> None:
    pages = wiki_page_paths()
    total = len(pages)

    if total <= cap:
        write(candidates_file, "".join(p + "\n" for p in pages))
        out(f"{total} candidate pages (full vault, cap={cap})")
        return

    out("rebuilding alias index...")
    run_stream([f"{SCRIPTS}/alias-index.py", "build"])

    search_terms = _intelligence_search_terms(intelligence_file)
    term_by_value = {row["term"]: row for row in search_terms}
    query_text = "".join(f"{row['term']}\n" for row in search_terms)
    scores: Counter[str] = Counter()
    pinned: set[str] = set()

    # (a) exact normalized alias lookup (tab-separated term + path). Required
    # matches are pinned before the optional-context cap is applied.
    r = subprocess.run([f"{SCRIPTS}/alias-index.py", "lookup"], input=query_text,
                       text=True, capture_output=True)
    for ln in r.stdout.splitlines():
        cols = ln.split("\t")
        if len(cols) >= 2 and cols[1]:
            term, path = cols[0], cols[1]
            row = term_by_value.get(term, {"importance": 1, "required": False})
            scores[path] += 100 + 10 * int(row["importance"])
            if row["required"]:
                pinned.add(path)

    # (b) body search adds optional context and ranking evidence.
    for row in search_terms:
        term = row["term"]
        rr = subprocess.run(["rg", "-l", "--type", "md", "-F", "--", term,
                             "wiki/entities/", "wiki/topics/"],
                            text=True, capture_output=True)
        for path in (ln for ln in rr.stdout.splitlines() if ln):
            scores[path] += int(row["importance"])

    ranked = [path for path, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]
    chosen = sorted(pinned)
    for path in ranked:
        if path in pinned:
            continue
        if len(chosen) >= max(cap, len(pinned)):
            break
        chosen.append(path)
    write(candidates_file, "".join(p + "\n" for p in chosen))
    out(f"{len(chosen)} candidate pages ({len(pinned)} required exact match(es) pinned; cap={cap})")


def _renderer_intelligence_with_existing_types(
    intelligence_file: str, candidates_file: str
) -> str:
    """Resolve analyzer Entity/Topic suggestions to the vault's owned identity.

    The strict analyzer artifact remains unchanged for validation and reuse.
    This renderer-only projection prevents a historical Topic from provoking a
    duplicate Entity (or vice versa) when the alias index proves one owner.
    """
    if not _candidate_paths(candidates_file):
        return intelligence_file
    build = subprocess.run(
        [f"{SCRIPTS}/alias-index.py", "build"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if build.returncode != 0:
        detail = build.stderr.strip()
        die("cannot reconcile candidate page types: alias-index build failed"
            + (f": {detail}" if detail else ""))
    try:
        index = json.loads(Path("wiki/.alias-index.json").read_text(encoding="utf-8"))
        artifact = json.loads(read(intelligence_file))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot reconcile candidate page types: {exc}")
    aliases = index.get("aliases", {}) if isinstance(index, dict) else {}
    pages = index.get("pages", {}) if isinstance(index, dict) else {}
    if not isinstance(aliases, dict) or not isinstance(pages, dict):
        die("cannot reconcile candidate page types: malformed alias index")

    entity_aliases: dict[str, list[str]] = {}
    for entity in artifact.get("entities", []):
        if not isinstance(entity, dict) or not isinstance(entity.get("name"), str):
            continue
        entity_aliases[normalize_name(entity["name"])] = [
            alias for alias in entity.get("aliases", []) if isinstance(alias, str)
        ]

    changed: list[tuple[str, str, str]] = []
    for candidate in artifact.get("page_candidates", []):
        if not isinstance(candidate, dict):
            continue
        name = candidate.get("name")
        analyzer_type = candidate.get("page_type")
        if not isinstance(name, str) or analyzer_type not in {"entity", "topic"}:
            continue
        identity_names = [name, *entity_aliases.get(normalize_name(name), [])]
        page_ids = sorted({
            page_id
            for identity_name in identity_names
            for page_id in aliases.get(normalize_name(identity_name), [])
            if isinstance(page_id, str)
        })
        owners = []
        for page_id in page_ids:
            page = pages.get(page_id)
            if not isinstance(page, dict):
                continue
            path = page.get("path")
            page_type = str(page.get("type") or "").casefold()
            if isinstance(path, str) and page_type in {"entity", "topic"}:
                owners.append((path, page_type))
        owners = sorted(set(owners))
        if len(owners) != 1 or owners[0][1] == analyzer_type:
            continue
        path, existing_type = owners[0]
        candidate["page_type"] = existing_type
        changed.append((name, analyzer_type, existing_type))

    if not changed:
        return intelligence_file
    projected = mktemp()
    write(projected, json.dumps(artifact, ensure_ascii=False, separators=(",", ":")) + "\n")
    for name, analyzer_type, existing_type in changed:
        out(f"candidate identity type reconciled: {name} "
            f"{analyzer_type} → {existing_type} (existing vault owner)")
    return projected


# ── globals populated from source-identity (§1) ──────────────────────────────
SRC: dict[str, str] = {}


def _audit_extra() -> list[str]:
    """The media `.transcript.json` audit artifact (media path only), so every
    commit that stages DEST/SIDECAR also stages it — else the hash-guarded JSON
    is left untracked (§7.2). Empty list on the document path."""
    a = SRC.get("AUDIT_JSON")
    return [a] if a else []


def build_prompt(expand_file: str, out_file: str, all_source_ids: str,
                 text_file: str, candidates_file: str, source_intelligence_file: str,
                 section_label: str, operation: str = "digest") -> None:
    with open(out_file, "w", encoding="utf-8") as f:
        r = subprocess.run(
            [f"{SCRIPTS}/build-prompt.py",
             "--source-id", SRC["SOURCE_ID"], "--sha256", SRC["SHA256"],
             "--added", SRC["ADDED"], "--origin-type", SRC["ORIGIN_TYPE"],
             "--origin-ref", SRC["ORIGIN_REF"], "--basename", SRC["DEST_BASENAME"],
             f"--section-label={section_label}", "--all-source-ids", all_source_ids,
             "--source-intelligence-file", source_intelligence_file,
             "--text-file", text_file, "--candidates-file", candidates_file,
             "--expand-file", expand_file, "--dest", SRC["DEST"],
             "--operation", operation],
            stdout=f)
    if r.returncode != 0:
        die("build-prompt failed")


def validate_section_label(label: str) -> str:
    if not label:
        return ""
    if len(label) > SECTION_LABEL_MAX_CHARS:
        die(f"--section-label must be <= {SECTION_LABEL_MAX_CHARS} characters")
    bad = [c for c in label if ord(c) < 32 or ord(c) == 127]
    if bad:
        die("--section-label must not contain control characters or newlines")
    return label


def whole_source_already_logged(source_id: str) -> bool:
    log_path = Path(".wiki") / "log.md"
    if not log_path.is_file():
        return False
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _log_line_marks_whole_source(line, source_id):
            return True
    return False


def _log_line_marks_whole_source(line: str, source_id: str) -> bool:
    parsed = parse_log_line(line)
    if not parsed or parsed[0] != source_id or parsed[1]:
        return False
    return "pages: (images-only)" not in line


def _candidate_paths(candidates_file: str) -> list[str]:
    return sorted({
        line.strip() for line in read(candidates_file).splitlines()
        if line.strip() and Path(line.strip()).is_file()
    })


def _run_quality_gate(intelligence_file: str, candidates_file: str,
                      section_label: str, modified: list[str], *,
                      allow_no_changes: bool = False) -> tuple[int, dict]:
    """Run the same deterministic coverage policy for diffs and NO_CHANGES."""
    command = [
        f"{SCRIPTS}/verify-ingest-quality.py",
        "--intelligence", intelligence_file,
        "--source-id", SRC["SOURCE_ID"],
        f"--section-label={section_label}",
    ]
    if modified:
        command += ["--modified", *modified]
    if allow_no_changes:
        command.append("--allow-no-changes")
    modified_set = set(modified)
    existing = [path for path in _candidate_paths(candidates_file)
                if path not in modified_set]
    if existing:
        command += ["--existing", *existing]
    result = subprocess.run(command, capture_output=True, text=True)
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        die(f"ingest quality gate returned malformed JSON: {exc}")
    return result.returncode, receipt


def _report_quality_receipt(receipt: dict) -> None:
    summary = receipt.get("summary", {})
    out(
        "quality coverage: "
        f"{summary.get('represented_candidates', 0)}/"
        f"{summary.get('required_candidates', 0)} planned page candidate(s), "
        f"{summary.get('already_covered_candidates', 0)} already covered, "
        f"{summary.get('modified_substantive_paragraphs', 0)} changed paragraph(s)"
    )
    for warning in receipt.get("warnings", []):
        if isinstance(warning, dict):
            eout(f"quality warning [{warning.get('code', '?')}]: "
                 f"{warning.get('message', '')}")
    for error in receipt.get("errors", []):
        if isinstance(error, dict):
            detail = error.get("path") or error.get("candidate") or ""
            suffix = f" ({detail})" if detail else ""
            eout(f"quality error [{error.get('code', '?')}]: "
                 f"{error.get('message', '')}{suffix}")


def _iter_source_sidecar_ids(sources_dir: Path):
    """Yield (source_id, sha256) from each sources/*.md sidecar's frontmatter."""
    if not sources_dir.is_dir():
        return
    for sidecar in sorted(sources_dir.glob("*.md")):
        if sidecar.name == "README.md":
            continue
        split = split_frontmatter(sidecar.read_text(encoding="utf-8", errors="replace"))
        if not split:
            continue
        values: dict[str, str] = {}
        for line in split[1].splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip() in {"source_id", "sha256"}:
                values[key.strip()] = _yaml_scalar(value)
        yield values.get("source_id"), values.get("sha256")


def _source_sha_for_id(source_id: str) -> str | None:
    matches = [
        sha for sid, sha in _iter_source_sidecar_ids(Path("sources"))
        if sid == source_id and sha
    ]
    return matches[0] if len(set(matches)) == 1 else None


def _supersede_coverage_proven(superseded: str) -> bool:
    """Only bypass re-synthesis when committed wiki pages cite the predecessor
    AND the predecessor's committed text-artifact hash matches this run's."""
    predecessor_sha = _source_sha_for_id(superseded)
    current_sha = SRC.get("SHA256", "")
    if not (predecessor_sha and current_sha and predecessor_sha == current_sha):
        return False
    result = subprocess.run(
        ["git", "grep", "-l", "-F", f"src:{superseded}", "HEAD", "--",
         "wiki/entities/", "wiki/topics/"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        _head, separator, rel = line.partition(":")
        if not separator or not rel:
            continue
        page = subprocess.run(
            ["git", "show", f"HEAD:{rel}"], text=True, capture_output=True,
            check=False,
        )
        if page.returncode == 0 and any(
            citation.source_id == superseded
            for citation in iter_source_citations(page.stdout)
        ):
            return True
    return False


def handle_no_changes_or_continue(raw_file: str, section_label: str,
                                  intelligence_file: str, candidates_file: str) -> None:
    """If the LLM response is a NO_CHANGES (and not a diff), log + commit the
    no-change run and exit 0. Otherwise return."""
    global _ROLLBACK_ON_FAILURE
    raw = read(raw_file)
    if re.search(r"^diff --git ", raw, re.MULTILINE):
        return
    # [^\S\n] = any whitespace except newline — matches bash grep's per-line
    # `[[:space:]]` (which includes \r\f\v) without letting the class span lines.
    m = re.search(r"^[^\S\n]*(?:>+[^\S\n]*)*NO_CHANGES:.*$", raw, re.MULTILINE)
    if not m:
        return
    no_changes_line = re.sub(r"^[^\S\n]*(?:>+[^\S\n]*)*", "", m.group(0))
    out("LLM returned NO_CHANGES:")
    print(f"  {no_changes_line}")
    reason = re.sub(r"^NO_CHANGES:[^\S\n]*", "", no_changes_line) or "no reason given"
    # A supersede run can legitimately produce NO new wiki pages — especially --add-frames,
    # whose transcript is BYTE-IDENTICAL to the predecessor, so the LLM almost always has
    # nothing to add. But the predecessor's live citations STILL must migrate to the new
    # source: otherwise B commits as the head while old pages keep citing the superseded A
    # (orphaned). Mirror the main-path supersede here so the migrated pages land in this same
    # no-changes commit. (Same as §9: rewrite → stage wiki/ → re-validate media anchors.)
    superseded = SRC.get("SUPERSEDES")
    identical_supersede = bool(superseded and _supersede_coverage_proven(superseded))
    if not identical_supersede:
        quality_rc, quality_receipt = _run_quality_gate(
            intelligence_file, candidates_file, section_label, [],
            allow_no_changes=True,
        )
        _report_quality_receipt(quality_receipt)
        if quality_rc != 0 or not quality_receipt.get("ok"):
            die("NO_CHANGES did not satisfy chapter-intelligence coverage; "
                "the section was not logged as complete")
    elif superseded:
        out("supersede text artifact is byte-identical to its predecessor; "
            "coverage migration may proceed without a new wiki diff")
    # Invariant: a supersede always mints a FRESH source (every SUPERSEDES emit pairs with
    # EXISTING_SIDECAR=""), so the two are mutually exclusive. If a future front-door path
    # ever broke that, the EXISTING_SIDECAR branch below would commit a new superseding
    # sidecar WITHOUT its source blob (silent provenance violation) — refuse loud instead.
    if superseded and SRC.get("EXISTING_SIDECAR"):
        die("internal invariant violated: SUPERSEDES set together with EXISTING_SIDECAR "
            "(a supersede must mint a fresh source, not reuse one)")
    if superseded:
        # Citation rewriting mutates and stages tracked wiki pages. Arm rollback
        # before the first mutation so a rewrite/lint/git failure cannot leave
        # migrated citations staged while failed-run cleanup removes provenance.
        _ROLLBACK_ON_FAILURE = True
        out(f"superseding {superseded} → {SRC['SOURCE_ID']}: migrating live citations...")
        run_stream([f"{SCRIPTS}/rewrite-citations.py", superseded, SRC["SOURCE_ID"]])
        git_run("add", "--", "wiki/")
        out("re-validating migrated citations (lint --gate=media-anchors)...")
        run_stream([f"{SCRIPTS}/lint.py", "--gate=media-anchors"])
    # Everything below mutates the log, sidecar, assets, or Git index.  Keep the
    # transaction armed for ordinary NO_CHANGES runs too, not just supersedes.
    _ROLLBACK_ON_FAILURE = True
    sup_note = f" (supersedes {superseded})" if superseded else ""
    Path(".wiki").mkdir(exist_ok=True)
    section_tag = f"#{section_label}" if section_label else ""
    with open(".wiki/log.md", "a", encoding="utf-8") as f:
        f.write(f"{SRC['ADDED']}  {SRC['SOURCE_ID']}{section_tag}  pages: (no-changes: {reason}){sup_note}\n")
    if subprocess.run([f"{SCRIPTS}/update-sidecar-progress.py", SRC["SIDECAR"]],
                      stdout=subprocess.DEVNULL).returncode != 0:
        eout("warn — sidecar progress update failed (continuing)")
    assets_dir = f"{SRC['DEST']}.assets"
    if Path(assets_dir).is_dir():
        git_run("add", "--", assets_dir)
    if not SRC.get("EXISTING_SIDECAR"):
        git_run("add", ".wiki/log.md", SRC["DEST"], SRC["SIDECAR"], *SCAFFOLD_PATHS, *_audit_extra())
        git_run("commit", "-m", f"ingest (no-changes): {SRC['SOURCE_ID']}{section_tag} ({SRC['DEST_BASENAME']}){sup_note}")
        out("committed source asset+sidecar with no wiki changes" + (" (citations migrated)" if superseded else ""))
    else:
        git_run("add", ".wiki/log.md", SRC["SIDECAR"], *SCAFFOLD_PATHS, *_audit_extra())
        git_run("commit", "-m", f"ingest (no-changes): {SRC['SOURCE_ID']}{section_tag} ({SRC['DEST_BASENAME']}){sup_note}")
        out("committed sidecar progress update for no-change run")
    _ROLLBACK_ON_FAILURE = False
    _cleanup()
    sys.exit(0)


def run_lang(dest: str) -> int:
    """Language profile: run the generator (it owns the chapter loop), then
    stage EXACT paths, assert the staged set ⊆ lang/, and commit (or no-op
    exit). The generator writes pages + appends the log AFTER rendering."""
    global _ROLLBACK_ON_FAILURE
    out("generating language study/vocab/grammar pages...")
    _ROLLBACK_ON_FAILURE = True
    _ROLLBACK_EXTRA_PATHS.update({"_reading", ".wiki/log.md"})
    log_preexisting = Path(".wiki/log.md").exists()
    manifest_file = mktemp()
    r = subprocess.run(
        ["uv", "run", f"{SCRIPTS}/generate-language-pages.py",
         "--source-id", SRC["SOURCE_ID"], "--manifest-out", manifest_file],
        text=True,
    )
    _register_untracked_under("_reading")
    if not SRC.get("EXISTING_SIDECAR"):
        cache_dir = Path(".wiki/lang-cache")
        for cache_path in cache_dir.glob(f"{SRC['SOURCE_ID']}.*"):
            if cache_path.is_file():
                _register_run_created_file(cache_path.as_posix())
    if not log_preexisting:
        _register_run_created_file(".wiki/log.md")
    if r.returncode != 0:
        die("language generator failed")

    try:
        manifest = json.loads(read(manifest_file))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"language generator wrote an invalid manifest: {exc}")
    if not isinstance(manifest, list) or not all(isinstance(p, str) for p in manifest):
        die("language generator wrote an invalid manifest: expected a JSON list of paths")
    for path in manifest:
        _ROLLBACK_EXTRA_PATHS.add(path)
        if Path(path).is_dir():
            _register_run_created_dir(path)
        else:
            _register_run_created_file(path)

    # Lint gate (cwd/env already point at content/lang, so lint resolves the
    # lang roots): source drift + duplicate ids + conflicts + citation orphans.
    out("linting language pages (lint --profile lang)...")
    run_stream([f"{SCRIPTS}/lint.py", "--profile", "lang"])

    # Stage EXACT paths (never a directory pathspec — wouldn't sweep stray
    # untracked files). Paths are cwd-relative (cwd == content/lang).
    assets_dir = f"{dest}.assets"
    if Path(assets_dir).is_dir():
        git_run("add", "--", assets_dir)
    stage = [dest, SRC["SIDECAR"], *_audit_extra(), *manifest]
    git_run("add", "--", *[p for p in stage if Path(p).exists()])

    # Subset assert: `git diff --cached --name-only` yields repo-root-relative
    # names (content/ is the repo root), so every staged lang path is `lang/…`.
    staged = [s for s in git_capture("diff", "--cached", "--name-only").splitlines() if s]
    outside = [s for s in staged if not s.startswith("lang/")]
    if outside:
        die(f"refusing to commit: staged paths outside lang/: {outside}")

    # No-op exit (nothing changed on a reused source).
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        out("nothing to commit (lang pages already up to date)")
        _ROLLBACK_ON_FAILURE = False
        return 0
    git_run("commit", "-m", f"lang: {SRC['SOURCE_ID']} ({SRC['DEST_BASENAME']})")
    _ROLLBACK_ON_FAILURE = False
    out(f"committed lang pages for {SRC['SOURCE_ID']}")
    return 0


def scope_check(diff_file: str, *, retry: bool) -> None:
    ok, stdout, stderr = _scope_check_result(diff_file)
    if ok:
        return
    _reject_scoped_diff(diff_file, retry=retry, stdout=stdout, stderr=stderr)


def _scope_check_result(diff_file: str) -> tuple[bool, str, str]:
    r = subprocess.run([f"{SCRIPTS}/diff-paths.py", diff_file, "--mode=scope"],
                       text=True, capture_output=True)
    return r.returncode == 0, r.stdout, r.stderr


def _reject_scoped_diff(diff_file: str, *, retry: bool, stdout: str, stderr: str) -> None:
    print(stdout, file=sys.stderr)
    if stderr:  # surface a diff-paths.py crash/traceback (bash dropped it)
        sys.stderr.write(stderr)
    shutil.copyfile(diff_file, diff_file + ".rejected")
    label = "retry diff" if retry else "LLM diff"
    die(f"{label} touched forbidden paths (above). raw at {diff_file}.rejected")


def _scope_error_retryable(stdout: str, stderr: str) -> bool:
    return "no `diff --git` headers" in (stdout + stderr)


def _ingest_diff_path(path: str) -> bool:
    return path == TAXONOMY_PATH or bool(WIKI_PAGE_RX.match(path))


def _diff_existing_modify_targets(diff_file: str) -> list[str]:
    dt = subprocess.run([f"{SCRIPTS}/diff-paths.py", diff_file, "--mode=modify-targets"],
                        text=True, capture_output=True)
    if dt.returncode != 0:
        if dt.stderr:
            sys.stderr.write(dt.stderr)
        return []
    return sorted({
        p for p in dt.stdout.splitlines()
        if _ingest_diff_path(p) and Path(p).is_file()
    })


def _merge_retry_targets(candidates_file: str, expand_file: str, retry_set: list[str]) -> None:
    retry_set = [path for path in retry_set if WIKI_PAGE_RX.match(path)]
    if not retry_set:
        return
    for fpath in (candidates_file, expand_file):
        existing = [ln for ln in read(fpath).splitlines() if ln]
        merged = sorted(set(existing) | set(retry_set))
        write(fpath, "".join(x + "\n" for x in merged))


def _citation_keys(text: str) -> set[str]:
    return {
        citation.source_id + (
            f"#{citation.raw_anchor}" if citation.raw_anchor else ""
        )
        for citation in iter_source_citations(text)
    }


def _citation_key_still_present(old_key: str, new_keys: set[str]) -> bool:
    if old_key in new_keys:
        return True
    # Allow a formerly bare source citation to become anchored. The reverse
    # would lose chapter/section precision and is intentionally rejected.
    if "#" not in old_key and any(k.startswith(old_key + "#") for k in new_keys):
        return True
    return False


def _assert_existing_citation_anchors_preserved() -> None:
    changed = git_capture(
        "diff", "--cached", "--name-only", "--", "wiki/entities/", "wiki/topics/"
    ).splitlines()
    failures: list[str] = []
    for rel in changed:
        if not WIKI_PAGE_RX.match(rel):
            continue
        head = subprocess.run(["git", "cat-file", "-e", f"HEAD:{rel}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if head.returncode != 0 or not Path(rel).is_file():
            continue
        old = subprocess.run(["git", "show", f"HEAD:{rel}"], text=True,
                             capture_output=True)
        if old.returncode != 0:
            continue
        old_keys = _citation_keys(old.stdout)
        if not old_keys:
            continue
        new_keys = _citation_keys(read(rel))
        missing = sorted(
            key for key in old_keys
            if not _citation_key_still_present(key, new_keys)
        )
        if missing:
            failures.append(f"  {rel}: removed {', '.join(missing)}")
    if failures:
        for line in failures:
            eout(line)
        die("LLM diff removed existing citation anchors; preserve prior "
            "source/chapter evidence and re-run")


def _anchored_regex(titles: list[str]) -> str:
    """`--section` regex matching exactly the given `## ` heading titles."""
    return "^(?:" + "|".join(re.escape(t) for t in dict.fromkeys(titles)) + ")$"


def _group_chapters(titles: list[str]) -> list[tuple[str, str]]:
    """Fallback (no chapter markers): one ingest per section occurrence."""
    return [(title, f"^{re.escape(title)}$") for title in titles]


_OCCURRENCE_LABEL_RX = re.compile(r" \[occurrence \d+/\d+\]$")


def _stable_chapter_instances(
    chapters: list[tuple[str, str]],
) -> list[tuple[str, str, str, int | None]]:
    """Attach stable identities without merging duplicate source headings.

    Returns (log/analysis label, section regex, source heading, occurrence).
    Unique headings retain their historical labels. Repeated headings receive
    a deterministic occurrence suffix and are selected separately after extract.
    """
    totals = Counter(normalize_name(label) for label, _ in chapters)
    seen: Counter[str] = Counter()
    source_seen: Counter[str] = Counter()
    result: list[tuple[str, str, str, int | None]] = []
    for label, section in chapters:
        key = normalize_name(label)
        seen[key] += 1
        source_seen[label] += 1
        total = totals[key]
        suffix = f" [occurrence {seen[key]}/{total}]" if total > 1 else ""
        clean = re.sub(r"[\x00-\x1f\x7f]", " ", label).strip() or "Untitled chapter"
        stable = f"{clean[:SECTION_LABEL_MAX_CHARS - len(suffix)]}{suffix}"
        result.append((stable, section, label, source_seen[label] if total > 1 else None))
    return result


def _heading_starts(lines: list[str], title: str | None = None) -> list[int]:
    """Line indices of ``## `` headings, optionally only those titled ``title``."""
    return [
        index for index, line in enumerate(lines)
        if (match := _HEADING_RX.match(line.rstrip("\r\n")))
        and (title is None or match.group(1) == title)
    ]


def _select_section_occurrence(text: str, title: str, occurrence: int) -> str:
    """Select one repeated `## title` span from extractor output."""
    lines = text.splitlines(keepends=True)
    starts = _heading_starts(lines, title)
    if occurrence < 1 or occurrence > len(starts):
        die(f"extractor returned {len(starts)} occurrence(s) of {title!r}; "
            f"cannot select occurrence {occurrence}")
    start = starts[occurrence - 1]
    end = starts[occurrence] if occurrence < len(starts) else len(lines)
    return "".join(lines[start:end])


def _select_heading_range(text: str, start: int, end: int) -> str:
    """Select a half-open range of ordered ``##`` section occurrences."""
    lines = text.splitlines(keepends=True)
    starts = _heading_starts(lines)
    if start < 0 or end <= start or end > len(starts):
        die(f"invalid chapter heading range {start}:{end} for {len(starts)} headings")
    first_line = starts[start]
    last_line = starts[end] if end < len(starts) else len(lines)
    return "".join(lines[first_line:last_line])


def _grouped_chapter_ranges(
    sections: list[tuple[str, int]],
) -> list[tuple[str, list[str], int, int]]:
    """Group chapters and preserve their exact ordered heading boundaries.

    Title-regex unions are insufficient because common subsection titles can
    repeat in different chapters.  The returned ``start``/``end`` ordinals
    select one contiguous range from the extractor's full ordered output.
    """
    groups: list[tuple[str, list[str], int, int]] = []
    current_label: str | None = None
    current_members: list[str] = []
    current_start = 0

    def finish(end: int) -> None:
        nonlocal current_label, current_members
        if current_label is not None:
            groups.append((current_label, current_members, current_start, end))
        current_label = None
        current_members = []

    for index, (title, size) in enumerate(sections):
        if CHAPTER_HEADING_RX.search(title):
            finish(index)
            current_label = title
            current_members = [title]
            current_start = index
        elif current_label is None:
            continue
        elif NONCONTENT_HEADING_RX.search(title):
            finish(index)
        elif SECTION_HEADING_RX.search(title) or size >= CHAPTER_MIN_CHARS:
            current_members.append(title)
    finish(len(sections))
    return groups


def _is_http_url(value: str) -> bool:
    return bool(re.match(r"^https?://", value, flags=re.IGNORECASE))


def _resolve_input_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (INVOCATION_CWD / path).resolve()


def _should_auto_chapter(args) -> bool:
    if args.chapters:
        return True
    # Children spawned by run_chaptered carry this marker so a source with no
    # `## ` sections (→ the whole-unit fallback, which passes no --section) can't
    # re-enter chapter mode and fork-bomb.
    if os.environ.get("PW_INGEST_NO_AUTOCHAPTER") == "1":
        return False
    if args.profile != "wiki" or args.kind or args.section or args.section_label:
        return False
    if args.images_only or _is_http_url(args.input):
        return False
    input_path = _resolve_input_path(args.input)
    return input_path.is_file() and input_path.suffix.lower() in CHAPTERED_EXTS


_HEADING_RX = re.compile(r"^##\s+(.+?)\s*$")
_EXTRACTION_TRUNCATION_RX = re.compile(
    r"(?:^|\n)\[\.\.\. truncated at \d+ chars \.\.\.\]\s*$")


def _section_sizes(full_text: str) -> list[tuple[str, int]]:
    """[(section title, body char-count)] per `## ` heading, in document order.

    Body excludes the heading line and whitespace, so cover / copyright / TOC /
    chapter-title pages score ~0 and can be dropped — extract.py emits one `## `
    per EPUB spine item, and those structural pages otherwise crash the per-
    chapter analysis (empty text) and abort the whole book."""
    sizes: list[tuple[str, int]] = []
    title: str | None = None
    count = 0
    for line in full_text.splitlines():
        m = _HEADING_RX.match(line)
        if m:
            if title is not None:
                sizes.append((title, count))
            title, count = m.group(1), 0
        elif title is not None:
            count += len(line.strip())
    if title is not None:
        sizes.append((title, count))
    return sizes


def _has_extraction_truncation_marker(text: str) -> bool:
    return bool(_EXTRACTION_TRUNCATION_RX.search(text))


def _require_complete_extraction(text: str, *, sliced: bool) -> None:
    if not _has_extraction_truncation_marker(text):
        return
    scope = "selected section" if sliced else "whole source"
    die(f"extractor hit the text limit for the {scope}; raise --limit or select "
        "a smaller range so truncated text is never logged as complete")


def _require_one_selected_heading(text: str, selector: str) -> None:
    count = sum(1 for line in text.splitlines() if _HEADING_RX.match(line))
    if count == 1:
        return
    if count == 0:
        die(f"--section {selector!r} matched no section heading")
    die(f"--section {selector!r} matched {count} section headings; refine the "
        "selector or ingest the source in automatic chapter mode")


def _enforce_selected_limit(text: str, limit: str, *, scope: str) -> None:
    text_limit = int(limit)
    if text_limit > 0 and len(text) > text_limit:
        die(f"{scope} contains {len(text)} characters, exceeding --limit "
            f"{text_limit}; raise --limit or select a smaller explicit section")


def _validate_section_contract(section: str, label: str, internal_range: str) -> None:
    if internal_range and section:
        die("internal ordered section range cannot be combined with --section")
    if internal_range and not label:
        die("internal ordered section range requires --section-label")
    if section and not label:
        die("--section requires --section-label so a partial ingest cannot be logged "
            "as whole-source completion")
    if label and not section and not internal_range:
        die("--section-label requires --section; a label alone would attach one "
            "chapter citation to the whole source")


def _enumerate_sections(input_path: Path) -> list[tuple[str, int]]:
    """One full-book extraction → [(section title, body size)]. A single pass
    yields both the section list and the sizes used to drop empty front/back
    matter, so no section is enumerated that would later crash on empty text."""
    extract_py = SCRIPTS_PATH / "extract.py"
    try:
        res = subprocess.run(
            [str(extract_py), str(input_path), "--limit", "0"],  # 0 = no truncation
            capture_output=True, text=True,
        )
    except FileNotFoundError as exc:
        die(f"extract.py failed: {exc}")
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        detail = detail[-500:] if detail else f"exit code {res.returncode}"
        die(f"extract.py failed for {input_path.name}: {detail}")
    if res.stderr:
        sys.stderr.write(res.stderr)
        sys.stderr.flush()
    return _section_sizes(res.stdout)


def _yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _source_id_for_sha(sources_dir: Path, sha: str) -> str | None:
    for source_id, sidecar_sha in _iter_source_sidecar_ids(sources_dir):
        if sidecar_sha == sha and source_id:
            return source_id
    return None


def _source_log_progress(lines: list[str], source_id: str) -> tuple[set[str], bool]:
    completion_lines = [
        line for line in lines if "pages: (images-only)" not in line
    ]
    labels = set(chapter_order_from_lines(completion_lines, source_id))
    whole_done = False
    for line in lines:
        if _log_line_marks_whole_source(line, source_id):
            whole_done = True
            break
    return labels, whole_done


def section_already_logged(source_id: str, section_label: str) -> bool:
    if not section_label:
        return False
    log_path = Path(".wiki") / "log.md"
    if not log_path.is_file():
        return False
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    done, whole_done = _source_log_progress(lines, source_id)
    return whole_done or section_label in done


def _label_matches_title(title: str, label: str) -> bool:
    if title == label:
        return True
    if title.startswith(label):
        tail = title[len(label):]
        return not tail or not (tail[0].isalnum() and tail[0].isascii())
    return False


def _resolved_done_labels(current_labels: list[str], done_labels: set[str]) -> set[str]:
    """Resolve historical abbreviated labels only when the match is unique.

    Current logs use exact stable labels. Older logs sometimes stored a chapter
    prefix such as ``第1章``. Preserve that migration path without allowing one
    short legacy label to mark multiple similarly named chapters complete.
    """
    resolved = set(done_labels) & set(current_labels)
    for legacy in done_labels - resolved:
        matches = [
            label for label in current_labels
            if not _OCCURRENCE_LABEL_RX.search(label)
            and _label_matches_title(label, legacy)
        ]
        if len(matches) == 1:
            resolved.add(matches[0])
    return resolved


def _run_one_chapter(args, section: str | None, label: str | None, *,
                     skip_assets: bool = False, source_title: str | None = None,
                     section_occurrence: int | None = None,
                     section_range: tuple[int, int] | None = None) -> int:
    """Spawn a fresh single-section ingest (own process → own preflight, clean
    index, and git commit). Reuses the fully-tested single-run path unchanged."""
    argv = [sys.executable, str(Path(__file__).resolve()),
            "--limit", args.limit, "--profile", args.profile]
    if section is not None and section_range is None:
        argv += ["--section", section]
    if label is not None:
        argv += [f"--section-label={label}"]
    if args.model:
        argv += ["--model", args.model]
    if getattr(args, "analyze_model", ""):
        argv += ["--analyze-model", args.analyze_model]
    argv.append(str(_resolve_input_path(args.input)))
    env = os.environ.copy()
    env["PW_INGEST_NO_AUTOCHAPTER"] = "1"   # child is a single-section run; never re-chapter
    chapter_outline = getattr(args, "chapter_outline", None)
    if chapter_outline:
        env["PW_SOURCE_CHAPTER_OUTLINE"] = json.dumps(chapter_outline, ensure_ascii=False)
    if section_occurrence is not None and section_range is None:
        env["PW_SECTION_SOURCE_TITLE"] = source_title or ""
        env["PW_SECTION_OCCURRENCE"] = str(section_occurrence)
    if section_range is not None:
        env["PW_SECTION_RANGE"] = f"{section_range[0]}:{section_range[1]}"
    if skip_assets:
        env["PW_INGEST_SKIP_ASSETS"] = "1"
    else:
        env.pop("PW_INGEST_SKIP_ASSETS", None)
    return subprocess.run(argv, env=env).returncode


def generate_argument_map(source_id: str) -> int:
    """Generate and commit the source's derived argument map, if needed."""
    if (
        os.environ.get("PW_INGEST_NO_AUTOCHAPTER") == "1"
        or os.environ.get("PW_INGEST_SKIP_ARGUMENT_MAP") == "1"
    ):
        return 0
    out(f"generating argument map for {source_id}...")
    env = os.environ.copy()
    env.setdefault("PW_CODEX_DISABLE_SHELL", "1")
    rc = run_stream(
        [f"{SCRIPTS}/generate-mindmap.py", "--source-id", source_id],
        env=env,
        check=False,
    )
    if rc != 0:
        eout("argument-map generation failed; source ingestion is committed. "
             "Re-run the same ingest command to retry only the missing map.")
        return rc
    git_run("add", "--", "wiki/_maps/")
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        out("argument map unchanged")
        return 0
    git_run("commit", "-m", f"mindmap: {source_id}")
    out(f"committed argument map for {source_id}")
    return 0


def finish_argument_map(source_id: str) -> int:
    """Generate a book map after chapter children release their individual locks."""
    acquire_content_ingest_lock()
    try:
        preflight("wiki")
        return generate_argument_map(source_id)
    finally:
        release_content_ingest_lock()


def run_chaptered(args) -> int:
    """Ingest a whole book chapter-by-chapter.

    Enumerate the source's `## ` chapters, then run the normal single-section
    ingest once per chapter — each its own commit. Chapters already recorded in
    .wiki/log.md for this source (matched by the file's sha256 → source_id) are
    skipped, so re-running the exact same command resumes where a failure/stop
    left off. Captioning happens once (inside the first new chapter's run; later
    chapters skip the asset/caption pass)."""
    input_path = _resolve_input_path(args.input)
    if not input_path.is_file():
        die("--chapters needs a local file, not a URL")
    for flag, on in (("--section", bool(args.section)),
                     ("--section-label", bool(args.section_label)),
                     ("--images-only", args.images_only),
                     ("--kind", bool(args.kind))):
        if on:
            die(f"--chapters cannot be combined with {flag}")
    if args.profile != "wiki":
        die("--chapters is only supported for the wiki profile")

    sections = _enumerate_sections(input_path)
    grouped_ranges = _grouped_chapter_ranges(sections)
    if grouped_ranges:
        groups = [(label, members) for label, members, _start, _end in grouped_ranges]
        chapters = [(label, _anchored_regex(members)) for label, members in groups]
        chapter_ranges: list[tuple[int, int] | None] = [
            (start, end) for _label, _members, start, end in grouped_ranges
        ]
        grouped = sum(len(members) for _, members in groups)
        out(f"detected {len(chapters)} chapter(s); {grouped} section(s) grouped "
            f"under them, {len(sections) - grouped} front/back-matter section(s) "
            f"excluded")
    elif sections and all(PAGE_SECTION_RX.match(t) for t, _ in sections):
        # Outline-less PDF: page-numbered sections aren't content chapters —
        # per-page ingest would be a commit storm. Ingest as a single unit.
        out("page-numbered sections only (PDF without an outline) — "
            "ingesting as a single unit")
        chapters = []
        chapter_ranges = []
    else:
        # No chapter markers → one ingest per substantial section (structural
        # cover/TOC/title pages, which would produce empty chapter analysis, dropped).
        substantial = [title for title, size in sections if size >= CHAPTER_MIN_CHARS]
        thin = [title for title, size in sections if size < CHAPTER_MIN_CHARS]
        if thin:
            preview = ", ".join(thin[:6]) + ("…" if len(thin) > 6 else "")
            out(f"no chapter markers; per-section ingest, skipping {len(thin)} "
                f"empty/structural section(s): {preview}")
        chapters = _group_chapters(substantial)
        chapter_ranges = [None] * len(chapters)
    if not chapters:
        out("no ingestable chapters detected — ingesting as a single unit")
        rc = _run_one_chapter(args, section=None, label=None)
        if rc != 0:
            return rc
        source_id = _source_id_for_sha(VAULT_ROOT / "sources", sha256_of(input_path))
        if not source_id:
            eout("single-unit ingest completed but its source_id could not be resolved")
            return 1
        return finish_argument_map(source_id)

    chapter_instances = _stable_chapter_instances(chapters)

    # Child processes analyze one section at a time. The ordered labels let a
    # child include compact prior-chapter spines without resending prior text.
    args.chapter_outline = [label for label, _section, _title, _occurrence
                            in chapter_instances]

    # Resume: find this source (by sha256) and the chapters already committed.
    src_sha = sha256_of(input_path)
    source_id = _source_id_for_sha(VAULT_ROOT / "sources", src_sha)
    done: set[str] = set()
    whole_done = False
    log_path = VAULT_ROOT / ".wiki" / "log.md"
    if source_id and log_path.is_file():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        done, whole_done = _source_log_progress(lines, source_id)

    total = len(chapter_instances)
    if whole_done:
        out(f"chaptered ingest: {total} chapter(s), source already logged as a single unit, 0 to do")
        return finish_argument_map(source_id)

    resolved_done = _resolved_done_labels(
        [label for label, _section, _title, _occurrence in chapter_instances],
        done,
    )
    remaining = sum(
        1 for label, _section, _title, _occurrence in chapter_instances
        if label not in resolved_done
    )
    out(f"chaptered ingest: {total} chapter(s), {total - remaining} already done, "
        f"{remaining} to do")
    new = 0
    asset_pass_done = False
    for i, ((label, section, source_title, occurrence), section_range) in enumerate(
        zip(chapter_instances, chapter_ranges, strict=True), start=1
    ):
        if label in resolved_done:
            out(f"[{i}/{total}] skip (done): {label}")
            continue
        out(f"[{i}/{total}] ingesting: {label}")
        rc = _run_one_chapter(
            args,
            section=section,
            label=label,
            skip_assets=asset_pass_done,
            source_title=source_title,
            section_occurrence=occurrence,
            section_range=section_range,
        )
        if rc != 0:
            eout(f"[{i}/{total}] chapter failed (rc={rc}): {label}")
            eout("stopped — re-run the same command to resume from this chapter.")
            return rc
        new += 1
        asset_pass_done = True
    out(f"chaptered ingest complete: {new} new chapter(s), {total - remaining} skipped.")
    source_id = source_id or _source_id_for_sha(VAULT_ROOT / "sources", src_sha)
    if not source_id:
        eout("chaptered ingest completed but its source_id could not be resolved")
        return 1
    return finish_argument_map(source_id)


def main() -> int:
    global _ROLLBACK_ON_FAILURE
    os.environ.setdefault("PW_LLM_PROVIDER", "codex")
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--preflight", action="store_true",
                    help="print the structured dirty-tree report as JSON and exit")
    ap.add_argument("--section", default="")
    ap.add_argument("--section-label", default="")
    ap.add_argument("--limit", default="100000")
    ap.add_argument("--model", default=os.environ.get("PW_LLM_MODEL", ""),
                    help="LLM model for ingest text (codex -m / API model). "
                         "Defaults to PW_LLM_MODEL, else the CLI's own default. "
                         "Separate from the caption model (CAPTION_MODEL), so "
                         "ingest can run on a cheaper model than captioning.")
    ap.add_argument("--analyze-model", default=os.environ.get("PW_ANALYZE_MODEL", ""),
                    help="Optional model for structured chapter analysis. Defaults "
                         "to PW_ANALYZE_MODEL or the main provider model.")
    ap.add_argument("--images-only", action="store_true")
    ap.add_argument("--chapters", action="store_true",
                    help="Ingest a whole book chapter-by-chapter: enumerate its "
                         "`## ` chapters and run one ingest per chapter, each its "
                         "own commit. Skips chapters already in .wiki/log.md, so "
                         "re-running resumes where it stopped. (wiki profile only)")
    ap.add_argument("--kind", choices=["video", "audio", "image_note"], default="",
                    help="media front door (externalized extraction via the remote service); §7/§8")
    ap.add_argument("--platform", choices=["youtube", "podcast", "rednote", "unknown"],
                    default="youtube",
                    help="media platform discriminator (§8.1); podcast/image_note → extract-remote")
    ap.add_argument("--title", default="", help="media title fallback")
    ap.add_argument("--retranscribe", action="store_true", help="media: force re-ASR + supersede")
    ap.add_argument("--force", action="store_true",
                    help="media: accept a low chars/s coverage-gate result (forwarded to media-identity)")
    # podcast (audio_extraction) selectors — forwarded to media-identity → extract-remote
    ap.add_argument("--feed-url", default="")
    ap.add_argument("--episode-guid", default="")
    ap.add_argument("--episode-url", default="")
    ap.add_argument("--episode-title", default="")
    ap.add_argument("--episode-published", default="")
    ap.add_argument("--enclosure-url", default="")
    # image_note (§8.2) selectors
    ap.add_argument("--post-id", default="")
    ap.add_argument("--reocr", action="store_true", help="image_note: re-OCR + supersede")
    # video frames (§8.3)
    ap.add_argument("--frames", action="store_true", help="video: also extract keyframes")
    ap.add_argument("--cadence", default="", help="video frames: fixed cadence (seconds)")
    ap.add_argument("--profile", choices=["wiki", "lang"], default="wiki",
                    help="ingest destination: wiki synthesis (default) or isolated "
                         "language study/vocab/grammar pages under content/lang/")
    ap.add_argument("input", nargs="?")
    args = ap.parse_args()
    if args.preflight:
        active_root = resolve_vault_root(args.profile)
        content_root = active_root.parent if args.profile == "lang" else active_root
        ok, message, offending = preflight_report(args.profile, content_root=content_root)
        print(json.dumps({"ok": ok, "message": message, "offending": offending}, ensure_ascii=False))
        return 0
    if not args.input:
        ap.error("input is required unless --preflight is used")
    section_label = validate_section_label(args.section_label)
    args.section_label = section_label
    try:
        parsed_limit = int(args.limit)
    except ValueError:
        die("--limit must be a non-negative integer")
    if parsed_limit < 0:
        die("--limit must be a non-negative integer")
    args.limit = str(parsed_limit)
    internal_section_range = os.environ.get("PW_SECTION_RANGE", "").strip()
    if internal_section_range and os.environ.get("PW_SECTION_OCCURRENCE", "").strip():
        die("internal section range and occurrence selectors are mutually exclusive")
    _validate_section_contract(args.section, section_label, internal_section_range)
    if args.kind and (args.section or section_label):
        die("media ingest does not support --section/--section-label; media text "
            "would otherwise be cited as a section without being sliced")

    # Pin the ingest model for every downstream LLM call (llm_client reads this
    # from the env). Kept out of the caption path, which has its own model knob.
    if args.model:
        os.environ["PW_LLM_MODEL"] = args.model

    # ── profile destination: reassign the module root BEFORE the env export so
    #    every child inherits the active child root (content/lang for lang).
    global VAULT_ROOT
    VAULT_ROOT = resolve_vault_root(args.profile)
    chapter_mode = _should_auto_chapter(args)

    # lang: reject incompatible flags BEFORE any source write (identity copies
    # the source/sidecar), and create the vault dirs BEFORE chdir (the tree may
    # not exist yet, so chdir would fail).
    if args.profile == "lang":
        # The lang generator owns the chapter loop (it sections internally and
        # caps each chapter at its own limit), so the wiki-path slicing flags
        # would be silently ignored — reject them rather than no-op surprisingly.
        # --platform has a truthy default ("youtube"), so detect EXPLICIT use
        # from argv rather than its value.
        platform_explicit = any(a == "--platform" or a.startswith("--platform=")
                                for a in sys.argv[1:])
        forbidden = [f for f, on in (
            ("--kind", bool(args.kind)),
            ("--images-only", args.images_only), ("--retranscribe", args.retranscribe),
            ("--reocr", args.reocr), ("--frames", args.frames),
            ("--chapters", args.chapters),
            ("--section", bool(args.section)), ("--section-label", bool(args.section_label)),
            ("--limit", args.limit != "100000"),
            # media/podcast/image-note selectors — only ever consumed by the
            # media path (--kind), which is itself rejected; flag them too so a
            # lang run can't silently swallow them.
            ("--force", args.force), ("--title", bool(args.title)),
            ("--cadence", bool(args.cadence)), ("--feed-url", bool(args.feed_url)),
            ("--episode-guid", bool(args.episode_guid)), ("--episode-url", bool(args.episode_url)),
            ("--episode-title", bool(args.episode_title)),
            ("--episode-published", bool(args.episode_published)),
            ("--enclosure-url", bool(args.enclosure_url)), ("--post-id", bool(args.post_id)),
            ("--platform", platform_explicit),
        ) if on]
        if forbidden:
            die(f"--profile lang does not support {', '.join(forbidden)} "
                "(v1 = text/document sources only, generator owns chapter slicing; "
                "audio/video transcription is v2)")
        # A URL under --profile lang would be web-scraped as an HTML page (the
        # source-identity default), turning a YouTube link into 700KB of page
        # chrome tokenized as "Japanese". Media URLs go through the transcription
        # front door instead; refuse the scrape so it can't silently commit junk.
        if _is_http_url(args.input):
            die("--profile lang does not ingest URLs directly (a YouTube/media URL "
                "would be scraped as a web page, not transcribed). Run "
                "scripts/fetch-transcript.py <url> to transcribe first; it feeds the "
                "resulting .transcript.json back into this path.")
        for d in (VAULT_ROOT, VAULT_ROOT / "sources", VAULT_ROOT / ".wiki"):
            d.mkdir(parents=True, exist_ok=True)

    # cwd + env side effects live here (not module scope) so importing this
    # module is side-effect-free. All relative git ops below run in content/.
    # PW_CONTENT_DIR is the public override; VAULT_CONTENT_DIR remains an
    # internal/legacy alias for scripts that have not moved to _util.py.
    os.environ["PW_CONTENT_DIR"] = str(VAULT_ROOT)
    os.environ["VAULT_CONTENT_DIR"] = str(VAULT_ROOT)
    os.chdir(VAULT_ROOT)

    acquire_content_ingest_lock()
    global SCAFFOLD_PATHS
    SCAFFOLD_PATHS = ensure_wiki_scaffold(args.profile)
    preflight(args.profile, SCAFFOLD_PATHS)
    check_deps()
    global _PREEXISTING_SOURCE_PATHS
    _PREEXISTING_SOURCE_PATHS = _source_path_snapshot()

    # Whole-book mode loops the normal single-section ingest once per chapter.
    # It runs after the common setup gates so missing tools and dirty vault state
    # produce the same diagnostics as a normal ingest.
    if chapter_mode:
        release_content_ingest_lock()
        return run_chaptered(args)

    # ── §1 fetch / canonicalize identity ──
    if args.kind:
        # Media front door (§7): delegate ASR to the remote via media-identity.py
        # (a sibling; source-identity.py stays untouched). It emits the same
        # contract PLUS TEXT_FILE (the prompt copy — extract.py is skipped) and
        # AUDIT_JSON (the committed .transcript.json, added at commit time).
        # A local bundle path (image_note zip/tar) is relative to the caller's cwd, not
        # the content/ dir we chdir'd into — resolve it like the document path, else
        # origin_ref/OCR would target VAULT_ROOT/<path>. URLs pass through untouched.
        media_input = args.input
        if not _is_http_url(media_input) and not os.path.isabs(media_input):
            media_input = str(INVOCATION_CWD / media_input)
        media_args = [f"{SCRIPTS}/media-identity.py", media_input,
                      "--kind", args.kind, "--limit", args.limit,
                      "--platform", args.platform]
        if args.title:
            media_args += ["--title", args.title]
        if args.retranscribe:
            media_args += ["--retranscribe"]
        if args.force:
            media_args += ["--force"]
        if args.reocr:
            media_args += ["--reocr"]
        if args.frames:
            media_args += ["--frames"]
        if args.cadence:
            media_args += ["--cadence", args.cadence]
        # forward podcast (audio_extraction) + image_note selectors when set
        for flag, val in (("--feed-url", args.feed_url), ("--episode-guid", args.episode_guid),
                          ("--episode-url", args.episode_url), ("--episode-title", args.episode_title),
                          ("--episode-published", args.episode_published),
                          ("--enclosure-url", args.enclosure_url), ("--post-id", args.post_id)):
            if val:
                media_args += [flag, val]
        r = subprocess.run(media_args, text=True, capture_output=True)
    else:
        # Resolve a relative FILE input against the caller's original cwd, not the
        # content/ dir we chdir'd into at import. URLs pass through untouched.
        src_input = args.input
        if not _is_http_url(src_input) and not os.path.isabs(src_input):
            src_input = str(INVOCATION_CWD / src_input)
        try:
            r = run_source_identity(src_input)
        except RuntimeError as exc:
            die(f"source identity failed: {exc}")
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        die("media identity failed" if args.kind else "source identity failed")
    SRC.update(parse_shell_assignments(r.stdout))
    dest = SRC["DEST"]
    if not SRC.get("EXISTING_SIDECAR"):
        _register_new_source_artifacts(dest)
    elif SRC.get("AUDIT_JSON"):
        _register_run_created_file(SRC["AUDIT_JSON"])

    if (
        args.profile == "wiki"
        and not args.kind
        and not args.section
        and not section_label
        and not args.images_only
        and whole_source_already_logged(SRC["SOURCE_ID"])
    ):
        out(f"whole-source ingest already logged for {SRC['SOURCE_ID']}; checking argument map")
        return generate_argument_map(SRC["SOURCE_ID"])
    if (
        args.profile == "wiki"
        and section_label
        and section_already_logged(SRC["SOURCE_ID"], section_label)
    ):
        out(f"section ingest already logged for {SRC['SOURCE_ID']}#{section_label}; nothing to do")
        return 0

    # ── §lang: skip all wiki synthesis; run the language generator instead ──
    if args.profile == "lang":
        return run_lang(dest)

    # ── §4 extract text ──
    # Extraction and captioning can update tracked files in a reused asset
    # directory, so the transaction starts before either helper can mutate it.
    _ROLLBACK_ON_FAILURE = True
    text_file = mktemp()
    if args.kind:
        # The transcript is already clean text; media-identity.py rendered the
        # (sampled) prompt copy. Skip extract.py and use it directly. It is a temp
        # file media-identity.py created (outside our mktemp registry), so register
        # it for _cleanup() — otherwise the prompt copy leaks in /tmp.
        text_file = SRC["TEXT_FILE"]
        _TEMPS.append(text_file)
    else:
        write_assets = args.images_only or os.environ.get("PW_INGEST_SKIP_ASSETS") != "1"
        range_raw = os.environ.get("PW_SECTION_RANGE", "").strip()
        occurrence_raw = os.environ.get("PW_SECTION_OCCURRENCE", "").strip()
        select_after_extract = bool(range_raw or occurrence_raw)
        extract_args = [dest, "--limit", "0" if select_after_extract else args.limit]
        if args.section and not range_raw:
            extract_args = [
                dest, "--section", args.section,
                "--limit", "0" if occurrence_raw else args.limit,
            ]
        if write_assets:
            extract_args += ["--write-assets"]
        if SRC["ORIGIN_TYPE"] == "url":
            extract_args += ["--base-url", SRC["ORIGIN_REF"]]
        out(f"extracting text{f' (section={args.section})' if args.section else ''}...")
        with open(text_file, "w", encoding="utf-8") as f:
            if subprocess.run([f"{SCRIPTS}/extract.py", *extract_args], stdout=f).returncode != 0:
                die("extract failed")
        if range_raw:
            try:
                start_raw, end_raw = range_raw.split(":", 1)
                start, end = int(start_raw), int(end_raw)
            except (ValueError, TypeError):
                die("PW_SECTION_RANGE must be two integer heading ordinals: START:END")
            selected = _select_heading_range(read(text_file), start, end)
            _enforce_selected_limit(selected, args.limit, scope="selected chapter")
            write(text_file, selected)
        if occurrence_raw:
            source_title = os.environ.get("PW_SECTION_SOURCE_TITLE", "")
            try:
                occurrence = int(occurrence_raw)
            except ValueError:
                die("PW_SECTION_OCCURRENCE must be a positive integer")
            selected = _select_section_occurrence(read(text_file), source_title, occurrence)
            _enforce_selected_limit(selected, args.limit, scope="selected section")
            write(text_file, selected)

    if args.section:
        selected_text = read(text_file)
        _require_one_selected_heading(selected_text, args.section)
        _require_complete_extraction(selected_text, sliced=True)

    # caption new images (soft-fail)
    assets_dir = f"{dest}.assets"
    if (os.environ.get("PW_INGEST_SKIP_ASSETS") != "1"
            and Path(assets_dir).is_dir() and Path(assets_dir, "_manifest.md").is_file()):
        cap_args = [f"{SCRIPTS}/caption.py", assets_dir]
        for env_k, flag in (("CAPTION_BACKEND", "--backend"), ("CAPTION_MODEL", "--model"),
                            ("CAPTION_LANG", "--source-lang"), ("CAPTION_LIMIT", "--limit")):
            if os.environ.get(env_k):
                cap_args += [flag, os.environ[env_k]]
        out(f"captioning new images (backend={os.environ.get('CAPTION_BACKEND') or 'auto (matches LLM CLI)'})...")
        if subprocess.run(cap_args).returncode != 0:
            eout("warn — captioning failed (continuing)")

    section_tag = f"#{section_label}" if section_label else ""

    # ── §5(images-only) short-circuit ──
    if args.images_only:
        out("--images-only — skipping chapter analysis and main LLM diff")
        _ROLLBACK_ON_FAILURE = True
        Path(".wiki").mkdir(exist_ok=True)
        with open(".wiki/log.md", "a", encoding="utf-8") as f:
            f.write(f"{SRC['ADDED']}  {SRC['SOURCE_ID']}{section_tag}  pages: (images-only)\n")
        if subprocess.run([f"{SCRIPTS}/update-sidecar-progress.py", SRC["SIDECAR"]],
                          stdout=subprocess.DEVNULL).returncode != 0:
            eout("warn — sidecar progress update failed (continuing)")
        if Path(assets_dir).is_dir():
            git_run("add", "--", assets_dir)
        if not SRC.get("EXISTING_SIDECAR"):
            git_run("add", ".wiki/log.md", dest, SRC["SIDECAR"], *SCAFFOLD_PATHS, *_audit_extra())
        else:
            git_run("add", ".wiki/log.md", SRC["SIDECAR"], *SCAFFOLD_PATHS, *_audit_extra())
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            out("--images-only nothing to commit (assets already up to date)")
            _ROLLBACK_ON_FAILURE = False
            return 0
        git_run("commit", "-m", f"images-only: {SRC['SOURCE_ID']}{section_tag} ({SRC['DEST_BASENAME']})")
        _ROLLBACK_ON_FAILURE = False
        out("committed images-only update")
        return 0

    text = read(text_file)
    out(f"extracted {len(text)} characters")
    if len(text) == 0:
        die("extractor produced empty text")
    _require_complete_extraction(
        text, sliced=bool(args.section or internal_section_range)
    )

    # ── §5 reusable chapter analysis / §6 collect candidates ──
    cap = int(os.environ.get("CAND_CAP", "20"))
    source_intelligence_file = mktemp()
    candidates_file = mktemp()
    cache_dir = Path(".wiki") / "chapter-intelligence-cache"
    analyze_cmd = [
        f"{SCRIPTS}/analyze-chapter.py",
        "--text-file", text_file,
        "--source-id", SRC["SOURCE_ID"],
        "--source-sha256", SRC["SHA256"],
        f"--section-label={section_label}",
        "--cache-dir", str(cache_dir),
        "--output", source_intelligence_file,
    ]
    chapter_outline_json = os.environ.get("PW_SOURCE_CHAPTER_OUTLINE", "").strip()
    if chapter_outline_json:
        analyze_cmd += ["--chapter-outline-json", chapter_outline_json]
    if args.analyze_model:
        analyze_cmd += ["--model", args.analyze_model]
    out("analyzing chapter intelligence...")
    if run_stream(analyze_cmd, env=analyzer_env(), check=False) != 0:
        die("chapter-intelligence analysis failed; no wiki diff was attempted")
    try:
        intelligence = json.loads(read(source_intelligence_file))
    except json.JSONDecodeError as exc:
        die(f"chapter-intelligence output is not valid JSON: {exc}")
    out(
        "chapter intelligence: "
        f"{len(intelligence.get('claims', []))} claims, "
        f"{len(intelligence.get('entities', []))} entities, "
        f"{len(intelligence.get('topics', []))} topics, "
        f"{len(intelligence.get('page_candidates', []))} page candidate(s)"
    )
    for candidate in intelligence.get("page_candidates", []):
        if isinstance(candidate, dict):
            print(
                f"  - {candidate.get('page_type', '?')} "
                f"{candidate.get('name', '?')} (importance={candidate.get('importance', '?')})"
            )

    collect_candidates(source_intelligence_file, candidates_file, cap)
    renderer_intelligence_file = _renderer_intelligence_with_existing_types(
        source_intelligence_file, candidates_file
    )

    # ── §7 main ingest call ──
    # Sidecars only. git's `*` matches `/`, so "sources/*.md" also catches
    # sources/<asset>.assets/_manifest.md — whose source_id could differ from
    # its sidecar mid-ingest and leak a phantom citable id into the prompt.
    all_source_ids = "\n".join(sorted({
        m.group(1)
        for f in git_capture("ls-files", "--cached", "--", "sources/*.md").splitlines()
        if Path(f).is_file() and not f.endswith("/_manifest.md")
        for m in re.finditer(r"^source_id:\s*(\S+)", read(f), re.MULTILINE)
    }))
    expand_file = mktemp()
    write(expand_file, "")
    diff_file = mktemp()
    diff_raw = diff_file + ".raw"
    _DIFF_RAW.append(diff_raw)
    prompt_file = mktemp()

    # codex runs isolated in this dir (see llm_client): seed it with the exact
    # candidate pages so it can modify existing entries, not the whole vault.
    codex_workdir = mktempdir()
    os.environ["PW_CODEX_WORKDIR"] = codex_workdir

    build_prompt(expand_file, prompt_file, all_source_ids, text_file, candidates_file,
                 renderer_intelligence_file, section_label, "digest")
    out(f"calling LLM (digest mode, {Path(prompt_file).stat().st_size} bytes)...")
    _seed_workset(codex_workdir, candidates_file, expand_file)
    write(diff_raw, final_newline(llm(read(prompt_file), soft=False)))

    run_stream([f"{SCRIPTS}/apply-diff.py", "detect-expand", diff_raw, expand_file])
    if Path(expand_file).stat().st_size > 0:
        n = read(expand_file).count("\n")
        out(f"LLM requested expansion of {n} file(s):")
        for ln in read(expand_file).splitlines():
            print(f"  - {ln}")
        build_prompt(expand_file, prompt_file, all_source_ids, text_file, candidates_file,
                     renderer_intelligence_file, section_label, "expand")
        out(f"re-calling LLM with expanded content ({Path(prompt_file).stat().st_size} bytes)...")
        _seed_workset(codex_workdir, candidates_file, expand_file)
        write(diff_raw, final_newline(llm(read(prompt_file), soft=False)))

    handle_no_changes_or_continue(
        diff_raw, section_label, source_intelligence_file, candidates_file
    )
    run_stream([f"{SCRIPTS}/apply-diff.py", "strip-fences", diff_raw, diff_file])
    ok, scope_stdout, scope_stderr = _scope_check_result(diff_file)
    if not ok:
        if _scope_error_retryable(scope_stdout, scope_stderr):
            print(scope_stdout, file=sys.stderr)
            if scope_stderr:
                sys.stderr.write(scope_stderr)
            shutil.copyfile(diff_file, diff_file + ".rejected")
            retry_set = _diff_existing_modify_targets(diff_file)
            _merge_retry_targets(candidates_file, expand_file, retry_set)
            if retry_set:
                eout(f"diff format invalid; auto-retry with {len(retry_set)} expanded path(s)...")
            else:
                eout("diff format invalid; auto-retry with git-format instructions...")
            build_prompt(expand_file, prompt_file, all_source_ids, text_file,
                         candidates_file, renderer_intelligence_file, section_label, "retry")
            _seed_workset(codex_workdir, candidates_file, expand_file)
            write(diff_raw, final_newline(llm(read(prompt_file), soft=False)))
            handle_no_changes_or_continue(
                diff_raw, section_label, source_intelligence_file, candidates_file
            )
            run_stream([f"{SCRIPTS}/apply-diff.py", "strip-fences", diff_raw, diff_file])
            scope_check(diff_file, retry=True)
        else:
            _reject_scoped_diff(diff_file, retry=False,
                                stdout=scope_stdout, stderr=scope_stderr)

    # ── §8 apply + auto-retry ──
    taxonomy_before = read(TAXONOMY_PATH)
    # A fresh scaffold is untracked. Put the prompt-visible baseline in the
    # index so `git apply --index` can modify it in the same diff as wiki pages.
    if TAXONOMY_PATH in SCAFFOLD_PATHS:
        git_run("add", "--", TAXONOMY_PATH)
    apply_err = mktemp()
    retry_count = 0
    max_retries = 1
    apply_env = {**os.environ, "LC_ALL": "C"}
    while True:
        out("applying diff (with --recount)...")  # bash prints to stderr; keep visible
        with open(apply_err, "w", encoding="utf-8") as ef:
            applied = subprocess.run(
                ["git", "-c", "core.quotepath=false", "apply", "--index",
                 "--recount", "--whitespace=nowarn", diff_file],
                env=apply_env, stderr=ef).returncode == 0
        if applied:
            _ROLLBACK_ON_FAILURE = True
            try:
                taxonomy_added = _taxonomy_additions(
                    taxonomy_before, read(TAXONOMY_PATH)
                )
            except ValueError as exc:
                reversed_ok = subprocess.run(
                    ["git", "-c", "core.quotepath=false", "apply", "--reverse",
                     "--index", "--recount", "--whitespace=nowarn", diff_file],
                    env=apply_env, stderr=subprocess.DEVNULL,
                ).returncode == 0
                if not reversed_ok:
                    die(f"taxonomy update rejected and rollback failed: {exc}")
                write(apply_err, f"error: patch failed: {TAXONOMY_PATH}: {exc}\n")
            else:
                if taxonomy_added:
                    out("taxonomy: added " + ", ".join(taxonomy_added))
                _assert_existing_citation_anchors_preserved()
                break

        err_text = read(apply_err)
        if retry_count >= max_retries:
            sys.stderr.write(err_text)
            shutil.copyfile(diff_file, f"{diff_file}.failed.{retry_count}")
            shutil.copyfile(apply_err, f"{diff_file}.apply-err.{retry_count}")
            die(f"patch rejected after {retry_count} auto-retry(ies). "
                f"Artifacts at {diff_file}.failed.* / {diff_file}.apply-err.*")
        if re.search(r"corrupt patch|No valid patches|patch fragment without header|"
                     r"patch with only garbage|unrecognized input|"
                     r"git diff header lacks filename information", err_text):
            sys.stderr.write(err_text)
            shutil.copyfile(diff_file, f"{diff_file}.failed.{retry_count}")
            shutil.copyfile(apply_err, f"{diff_file}.apply-err.{retry_count}")
            die(f"patch rejected (non-retryable error class). "
                f"Artifacts at {diff_file}.failed.{retry_count} / {diff_file}.apply-err.{retry_count}")

        # 1. parsed failing paths (validated strictly)
        parsed_file = mktemp()
        with open(parsed_file, "w", encoding="utf-8") as pf:
            subprocess.run([f"{SCRIPTS}/apply-diff.py", "parse-failed-paths", apply_err], stdout=pf)
        for p in read(parsed_file).splitlines():
            if not p:
                continue
            if not _ingest_diff_path(p):
                die(f"patch rejected: parsed path out-of-scope: {p}")
            if not Path(p).is_file():
                if re.search(rf"^error: {re.escape(p)}: does not exist in index", err_text, re.MULTILINE):
                    die(f"patch rejected: parsed path does not exist on disk OR in index: {p}")
                die(f"patch rejected: parsed path does not exist on disk: {p}")

        # 2. modify-targets from the rejected diff (lenient filter)
        diff_targets = _diff_existing_modify_targets(diff_file)

        # 3. combined retry set (line-based, like bash `cat … | sort -u` —
        #    splitlines(), NOT split(), so page names containing spaces survive)
        retry_set = sorted({p for p in read(parsed_file).splitlines() if p} | set(diff_targets))
        if not retry_set:
            sys.stderr.write(err_text)
            die("patch rejected; no retry paths derivable")

        # 4. index-clean check
        dirty = git_capture("diff", "--cached", "--name-only", "--",
                            "wiki/entities/", "wiki/topics/", "wiki/_index/").strip()
        if dirty:
            die(f"post-failure index dirty (unexpected): {dirty}")

        # 5. append to candidates + expand (dedup)
        _merge_retry_targets(candidates_file, expand_file, retry_set)

        eout(f"patch failed; auto-retry with {len(retry_set)} expanded path(s)...")
        build_prompt(expand_file, prompt_file, all_source_ids, text_file,
                     candidates_file, renderer_intelligence_file, section_label, "retry")
        _seed_workset(codex_workdir, candidates_file, expand_file)
        write(diff_raw, final_newline(llm(read(prompt_file), soft=False)))  # bash: || die "LLM call failed (retry)"
        handle_no_changes_or_continue(
            diff_raw, section_label, source_intelligence_file, candidates_file
        )
        run_stream([f"{SCRIPTS}/apply-diff.py", "strip-fences", diff_raw, diff_file])
        scope_check(diff_file, retry=True)
        retry_count += 1

    try:
        os.remove(prompt_file)
    except OSError:
        pass

    # ── post-apply: page_id, frontmatter sync, gates, autolink, MOCs ──
    today_str = today()
    modified = [ln for ln in git_capture("diff", "--cached", "--name-only", "--relative").splitlines()
                if WIKI_PAGE_RX.match(ln)]
    if taxonomy_added and not modified:
        die("taxonomy update rejected: the same diff must create or modify a content page")
    if modified:
        out("normalizing llm-zone formatting...")
        run_stream([
            f"{SCRIPTS}/format-llm-zone.py",
            f"--source-id={SRC['SOURCE_ID']}",
            f"--section-label={section_label}",
            *modified,
        ])

        out("validating chapter-intelligence coverage and prose quality...")
        quality_rc, quality_receipt = _run_quality_gate(
            source_intelligence_file, candidates_file, section_label, modified
        )
        _report_quality_receipt(quality_receipt)
        if quality_rc != 0 or not quality_receipt.get("ok"):
            die("chapter-intelligence quality gate failed; staged wiki edits were rolled back")

        out("ensuring page_id on modified pages...")
        run_stream([f"{SCRIPTS}/add-page-id.py", *modified])
        out("syncing frontmatter on modified pages...")
        run_stream([f"{SCRIPTS}/sync-frontmatter.py", "--date", today_str, *modified])
        out("validating alias uniqueness before vault-wide rewrites...")
        if subprocess.run([f"{SCRIPTS}/alias-index.py", "check"],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode != 0:
            eout("ABORT — alias-uniqueness check failed:")
            subprocess.run([f"{SCRIPTS}/alias-index.py", "check"])
            eout("  Resolve the collision in the generated page identities, then re-run.")
            eout("  This failed run will roll back its staged wiki/source edits.")
            die("alias-uniqueness check failed")
        out("validating tags (lint --gate=tags)...")
        run_stream([f"{SCRIPTS}/lint.py", "--gate=tags"])
        out("validating image embeds (lint --gate=images)...")
        run_stream([f"{SCRIPTS}/lint.py", "--gate=images"])
        # Run this for documents too: the quality gate permits only recognized
        # structured anchors, and this lint proves the cited source has the
        # matching timestamp/card/frame capability.
        out("validating media anchors (lint --gate=media-anchors)...")
        run_stream([f"{SCRIPTS}/lint.py", "--gate=media-anchors"])
        subprocess.run([f"{SCRIPTS}/alias-index.py", "build"], stdout=subprocess.DEVNULL)
        out("auto-linking entity mentions across the vault...")
        run_stream([f"{SCRIPTS}/autolink.py", "--all"])
        out("regenerating MOCs (wiki/_index/)...")
        run_stream([f"{SCRIPTS}/generate-mocs.py"])
        git_run("add", "--", "wiki/entities/", "wiki/topics/", "wiki/_index/", *SCAFFOLD_PATHS)

    out("git diff --cached (review now):")
    subprocess.run(["git", "--no-pager", "diff", "--cached", "--stat"])

    # ── §8b gates ──
    if subprocess.run([f"{SCRIPTS}/lint.py", "--gate=page-id"]).returncode != 0:
        eout("  Resolve the duplicate/malformed page_id cause and re-run")
        eout("  (the LLM should not emit page_id; add-page-id.py injects one).")
        eout("  This failed run will roll back its staged wiki/source edits.")
        die("page-id lint failed")

    # ── supersede (media --reocr / --retranscribe): migrate any live citations of the
    #    superseded predecessor to the new source so nothing is orphaned. The LLM's new
    #    pages already cite the new source_id; this repoints OLD pages that still cite the
    #    predecessor. Runs after the LLM pipeline + before the commit, so the rewritten
    #    pages land in the same atomic commit. The old source stays in sources/ (immutable;
    #    the new sidecar's `supersedes:` makes the resolver pick the new head).
    superseded = SRC.get("SUPERSEDES")
    if superseded:
        out(f"superseding {superseded} → {SRC['SOURCE_ID']}: migrating live citations...")
        run_stream([f"{SCRIPTS}/rewrite-citations.py", superseded, SRC["SOURCE_ID"]])
        git_run("add", "--", "wiki/")
        # rewrite-citations preserves the anchor verbatim, so a re-OCR/re-transcribe that
        # changed card/frame/segment coverage can turn a once-valid old anchor into an
        # out-of-range new one (e.g. [src:A#card-3] → [src:B#card-3] when B has 2 cards).
        # The media-anchor gate ran BEFORE the rewrite — re-run it on the migrated citations.
        out("re-validating migrated citations (lint --gate=media-anchors)...")
        run_stream([f"{SCRIPTS}/lint.py", "--gate=media-anchors"])

    # ── §9 log + commit ──
    touched = git_capture("diff", "--cached", "--name-only").replace("\n", " ")
    Path(".wiki").mkdir(exist_ok=True)
    sup_note = f" (supersedes {superseded})" if superseded else ""
    with open(".wiki/log.md", "a", encoding="utf-8") as f:
        f.write(f"{SRC['ADDED']}  {SRC['SOURCE_ID']}{section_tag}  pages: {touched}{sup_note}\n")
    if subprocess.run([f"{SCRIPTS}/update-sidecar-progress.py", SRC["SIDECAR"]]).returncode != 0:
        eout("warn — sidecar progress update failed (continuing)")
    git_run("add", ".wiki/log.md", dest, SRC["SIDECAR"], *SCAFFOLD_PATHS, *_audit_extra())
    if Path(assets_dir).is_dir():
        git_run("add", "--", assets_dir)
    commit_suffix = f" — {section_label}" if section_label else ""
    git_run("commit", "-m",
            f"ingest: {SRC['SOURCE_ID']}{section_tag} ({SRC['DEST_BASENAME']}{commit_suffix}){sup_note}")
    _ROLLBACK_ON_FAILURE = False
    out(f"committed {SRC['SOURCE_ID']}{section_tag}")
    if args.profile == "wiki" and not section_label:
        map_rc = generate_argument_map(SRC["SOURCE_ID"])
        if map_rc != 0:
            return map_rc
    out("done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        _cleanup()
