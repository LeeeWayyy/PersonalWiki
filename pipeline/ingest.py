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
    parse_log_line,
    sha256_of,
    split_frontmatter,
    today,
)
import llm_client  # noqa: E402

VAULT_ROOT = default_vault_root(TOOLING_ROOT)
SCRIPTS = str(SCRIPTS_PATH)
CHAPTERED_EXTS = (".epub", ".mobi", ".azw", ".azw3")
# Fallback only (books with no chapter markers): a `## ` section with fewer body
# chars than this is structural (cover/TOC/title page) and skipped.
CHAPTER_MIN_CHARS = int(os.environ.get("PW_CHAPTER_MIN_CHARS", "200"))
SECTION_LABEL_MAX_CHARS = int(os.environ.get("PW_SECTION_LABEL_MAX_CHARS", "200"))
KEYWORD_SOURCE_HEAD_CHARS = 6_000

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

WIKI_PATHSPEC = ["wiki/entities/", "wiki/topics/", "wiki/_index/", "wiki/_taxonomy.md"]
WIKI_PAGE_RX = re.compile(r"^wiki/(entities|topics)/.+\.md$")
TAXONOMY_PLACEHOLDER = """# Taxonomy

## Domain
- `biology/cell`

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
_INGEST_LOCK_FH = None
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
    wanted: set[str] = set()
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


def _rollback_after_apply_failure() -> None:
    """Drop staged/worktree wiki/log changes created after git apply --index.

    Once source cleanup removes a newly-created source artifact, leaving staged
    wiki edits around can strand citations to a source_id that no longer exists.
    Preflight guarantees these paths were clean before ingest touched them.
    """
    if not _ROLLBACK_ON_FAILURE:
        return
    run_provenance = [
        ".wiki/log.md",
        SRC.get("SIDECAR", ""),
        SRC.get("AUDIT_JSON", ""),
    ]
    if SRC.get("DEST"):
        run_provenance.append(f"{SRC['DEST']}.assets/_manifest.md")
    paths = [
        p for p in [*WIKI_PATHSPEC, *run_provenance]
        if p and (Path(p).exists() or _tracked_under(p))
    ]
    if not paths:
        return
    subprocess.run(
        ["git", "-c", "core.quotepath=false", "restore", "--staged", "--worktree", "--", *paths],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


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
    _rollback_after_apply_failure()
    _cleanup_run_created_artifacts()
    _cleanup()
    sys.exit(1)


_TERMINATING = False


def _handle_termination(signum, _frame) -> None:
    """Let backend cancel/idle-timeout SIGTERM run normal failed-run cleanup."""
    global _TERMINATING
    if _TERMINATING:
        return
    _TERMINATING = True
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


def llm(prompt_text: str, *, soft: bool, model: str | None = None) -> str:
    """Invoke the shared LLM client; return stdout-like text.

    soft=True mirrors the keyword pre-pass (`… 2>/dev/null || true`): tolerate
    failure and return an empty string. soft=False dies on failure.
    """
    timeout = int(os.environ.get("PW_LLM_TIMEOUT_S", "1800"))
    try:
        out = llm_client.complete(prompt_text, timeout=timeout, model=model)
    except Exception as exc:
        if soft:
            return ""
        die(f"LLM call failed: {exc}")
    if out is None and not soft:
        die("LLM call failed: no local or API LLM provider is configured")
    return out or ""


def parse_shell_assignments(text: str) -> dict[str, str]:
    """Parse `KEY=<shell-quoted-value>` lines (source-identity.py output)."""
    out_: dict[str, str] = {}
    for line in text.splitlines():
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        parts = shlex.split(v)
        out_[k] = parts[0] if parts else ""
    return out_


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
def ensure_wiki_scaffold(profile: str = "wiki") -> list[str]:
    """Create the minimal wiki structure ingest needs in an empty content repo."""
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


def preflight(profile: str = "wiki", allowed_untracked: list[str] | None = None) -> None:
    allowed = set(allowed_untracked or [])
    staged = git_capture("diff", "--cached", "--name-only").strip()
    if staged:
        eout("refusing to run with a non-empty git index.")
        eout("  Currently staged:")
        for ln in staged.splitlines():
            print(f"    {ln}", file=sys.stderr)
        eout("  Either commit/stash these changes, or `git restore --staged .`")
        sys.exit(1)
    # The wiki-page dirty-guard is wiki-only (the LLM rewrites those pages); the
    # lang generator preserves its pages' human-zone from the working tree, so
    # it needs no such guard. (cwd is content/lang under --profile lang, where
    # wiki/ doesn't exist anyway.)
    if profile == "wiki":
        tracked = git_capture("diff", "--name-only", "--", *WIKI_PATHSPEC).strip()
        untracked = "\n".join(
            line
            for line in git_capture("ls-files", "--others", "--exclude-standard", "--", *WIKI_PATHSPEC).splitlines()
            if line and line not in allowed
        )
        if tracked or untracked:
            eout("refusing to run with local changes under wiki/.")
            eout("  Scope: wiki/entities/, wiki/topics/, wiki/_index/, wiki/_taxonomy.md")
            if tracked:
                eout("  Modified (tracked):")
                for ln in tracked.splitlines():
                    print(f"    {ln}", file=sys.stderr)
            if untracked:
                eout("  Untracked:")
                for ln in untracked.splitlines():
                    print(f"    {ln}", file=sys.stderr)
            eout("  Commit, stash, or remove these before running ingest — the")
            eout("  vault-wide autolink sweep + bulk `git add` would otherwise")
            eout("  pull them into the ingest commit.")
            sys.exit(1)
    # Provenance files the terminal `git add` sweeps up regardless of the LLM
    # diff: the ingest log + this run's source asset/sidecar. An unstaged edit
    # to an existing (tracked) one would be silently folded into the ingest
    # commit, corrupting provenance. (Only TRACKED-modified matters: ingest
    # adds specific paths, never a blanket `git add sources/`, so unrelated
    # untracked files — e.g. leftovers from a failed run — are never swept in.)
    prov_tracked = git_capture("diff", "--name-only", "--", ".wiki/log.md", "sources/").strip()
    if prov_tracked:
        eout("refusing to run with unstaged edits under .wiki/log.md or sources/.")
        eout("  Modified (tracked):")
        for ln in prov_tracked.splitlines():
            print(f"    {ln}", file=sys.stderr)
        eout("  The ingest commit `git add`s the log + source asset + sidecar;")
        eout("  commit, stash, or restore these first so they aren't swept in.")
        sys.exit(1)
    # The terminal commit blanket-`git add`s the source's <dest>.assets/ dir,
    # so stale UNTRACKED files left under a sources/*.assets/ dir by an aborted
    # prior run would be swept in. (Top-level untracked sources/*.md are added
    # by specific path, never swept, so they're allowed — a failed run stays
    # retryable.) Refuse only stale untracked asset files.
    stale_assets = [
        p for p in git_capture("ls-files", "--others", "--exclude-standard", "--", "sources/").splitlines()
        if ".assets/" in p
    ]
    if stale_assets:
        eout("refusing to run with untracked files under an existing sources/*.assets/ dir.")
        for p in stale_assets:
            print(f"    {p}", file=sys.stderr)
        eout("  The asset-dir `git add` would sweep these into the ingest commit;")
        eout("  remove or commit them first (likely leftovers from an aborted run).")
        sys.exit(1)


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
    fcntl.flock(_INGEST_LOCK_FH.fileno(), fcntl.LOCK_EX)


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


def collect_candidates(keywords_file: str, candidates_file: str, cap: int) -> None:
    pages = wiki_page_paths()
    total = len(pages)

    if total <= cap:
        write(candidates_file, "".join(p + "\n" for p in pages))
        out(f"{total} candidate pages (full vault, cap={cap})")
        return

    out("rebuilding alias index...")
    run_stream([f"{SCRIPTS}/alias-index.py", "build"])

    hits: list[str] = []
    kw_text = read(keywords_file)
    # (a) alias-index lookup (tab-separated; column 2 = path)
    r = subprocess.run([f"{SCRIPTS}/alias-index.py", "lookup"], input=kw_text,
                       text=True, capture_output=True)
    for ln in r.stdout.splitlines():
        cols = ln.split("\t")
        if len(cols) >= 2 and cols[1]:
            hits.append(cols[1])
    # (b) rg -F body search per cleaned keyword
    for raw in kw_text.splitlines():
        kw = re.sub(r"^[ \t]*[-*][ \t]*", "", raw)
        kw = re.sub(r"^[0-9]+\.[ \t]*", "", kw)
        if not kw:
            continue
        rr = subprocess.run(["rg", "-l", "--type", "md", "-F", "--", kw,
                             "wiki/entities/", "wiki/topics/"],
                            text=True, capture_output=True)
        hits.extend(ln for ln in rr.stdout.splitlines() if ln)

    counts = Counter(hits)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    chosen = [path for path, _ in ranked[:cap]]
    write(candidates_file, "".join(p + "\n" for p in chosen))
    out(f"{len(chosen)} candidate pages (keyword-ranked, cap={cap})")


# ── globals populated from source-identity (§1) ──────────────────────────────
SRC: dict[str, str] = {}


def _audit_extra() -> list[str]:
    """The media `.transcript.json` audit artifact (media path only), so every
    commit that stages DEST/SIDECAR also stages it — else the hash-guarded JSON
    is left untracked (§7.2). Empty list on the document path."""
    a = SRC.get("AUDIT_JSON")
    return [a] if a else []


def build_prompt(expand_file: str, out_file: str, all_source_ids: str,
                 text_file: str, candidates_file: str, source_terms_file: str,
                 section_label: str, operation: str = "digest") -> None:
    with open(out_file, "w", encoding="utf-8") as f:
        r = subprocess.run(
            [f"{SCRIPTS}/build-prompt.py",
             "--source-id", SRC["SOURCE_ID"], "--sha256", SRC["SHA256"],
             "--added", SRC["ADDED"], "--origin-type", SRC["ORIGIN_TYPE"],
             "--origin-ref", SRC["ORIGIN_REF"], "--basename", SRC["DEST_BASENAME"],
             "--section-label", section_label, "--all-source-ids", all_source_ids,
             "--source-terms-file", source_terms_file,
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


def handle_no_changes_or_continue(raw_file: str, section_label: str) -> None:
    """If the LLM response is a NO_CHANGES (and not a diff), log + commit the
    no-change run and exit 0. Otherwise return."""
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
    # Invariant: a supersede always mints a FRESH source (every SUPERSEDES emit pairs with
    # EXISTING_SIDECAR=""), so the two are mutually exclusive. If a future front-door path
    # ever broke that, the EXISTING_SIDECAR branch below would commit a new superseding
    # sidecar WITHOUT its source blob (silent provenance violation) — refuse loud instead.
    if superseded and SRC.get("EXISTING_SIDECAR"):
        die("internal invariant violated: SUPERSEDES set together with EXISTING_SIDECAR "
            "(a supersede must mint a fresh source, not reuse one)")
    if superseded:
        out(f"superseding {superseded} → {SRC['SOURCE_ID']}: migrating live citations...")
        run_stream([f"{SCRIPTS}/rewrite-citations.py", superseded, SRC["SOURCE_ID"]])
        git_run("add", "--", "wiki/")
        out("re-validating migrated citations (lint --gate=media-anchors)...")
        run_stream([f"{SCRIPTS}/lint.py", "--gate=media-anchors"])
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
    _cleanup()
    sys.exit(0)


def run_lang(dest: str) -> int:
    """Language profile: run the generator (it owns the chapter loop), then
    stage EXACT paths, assert the staged set ⊆ lang/, and commit (or no-op
    exit). The generator writes pages + appends the log AFTER rendering."""
    out("generating language study/vocab/grammar pages...")
    manifest_file = mktemp()
    r = subprocess.run(
        ["uv", "run", f"{SCRIPTS}/generate-language-pages.py",
         "--source-id", SRC["SOURCE_ID"], "--manifest-out", manifest_file],
        text=True,
    )
    if r.returncode != 0:
        die("language generator failed")

    try:
        manifest = json.loads(read(manifest_file))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"language generator wrote an invalid manifest: {exc}")
    if not isinstance(manifest, list) or not all(isinstance(p, str) for p in manifest):
        die("language generator wrote an invalid manifest: expected a JSON list of paths")

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
        return 0
    git_run("commit", "-m", f"lang: {SRC['SOURCE_ID']} ({SRC['DEST_BASENAME']})")
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


def _diff_existing_modify_targets(diff_file: str) -> list[str]:
    dt = subprocess.run([f"{SCRIPTS}/diff-paths.py", diff_file, "--mode=modify-targets"],
                        text=True, capture_output=True)
    if dt.returncode != 0:
        if dt.stderr:
            sys.stderr.write(dt.stderr)
        return []
    return sorted({
        p for p in dt.stdout.splitlines()
        if WIKI_PAGE_RX.match(p) and Path(p).is_file()
    })


def _merge_retry_targets(candidates_file: str, expand_file: str, retry_set: list[str]) -> None:
    if not retry_set:
        return
    for fpath in (candidates_file, expand_file):
        existing = [ln for ln in read(fpath).splitlines() if ln]
        merged = sorted(set(existing) | set(retry_set))
        write(fpath, "".join(x + "\n" for x in merged))


def _citation_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for inner in re.findall(r"\[([^\[\]]*src:[^\[\]]*)\]", text):
        for part in inner.split(","):
            m = re.search(r"src:([A-Z0-9]{26})(#[^\]]*)?", part.strip())
            if not m:
                continue
            keys.add(m.group(1) + (m.group(2) or ""))
    return keys


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
    """Fallback (no chapter markers): one ingest per distinct section title."""
    return [(title, f"^{re.escape(title)}$") for title in dict.fromkeys(titles)]


def _group_by_chapter(sections: list[tuple[str, int]]) -> list[tuple[str, list[str]]]:
    """Group section titles under their parent chapter.

    A title matching CHAPTER_HEADING_RX opens a chapter; subsequent titles
    matching SECTION_HEADING_RX belong to it; any other title (front/back matter,
    or a section appearing before the first chapter) is dropped — only content
    inside a chapter is ingestable. Returns [(chapter_label, [member titles])],
    or [] when no chapter heading is found (caller falls back to per-section)."""
    groups: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for title, _size in sections:
        if CHAPTER_HEADING_RX.search(title):
            current = [title]
            groups.append((title, current))
        elif current is not None and SECTION_HEADING_RX.search(title):
            current.append(title)
        else:
            current = None  # front/back matter or stray section → excluded
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
    if args.images_only or args.rerender or _is_http_url(args.input):
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
    section keyword pre-pass (empty text) and abort the whole book."""
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
    if not sources_dir.is_dir():
        return None
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
        if values.get("sha256") == sha and values.get("source_id"):
            return values["source_id"]
    return None


def _source_log_progress(lines: list[str], source_id: str) -> tuple[set[str], bool]:
    labels = set(chapter_order_from_lines(lines, source_id))
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
    return whole_done or _chapter_done(section_label, done)


def _label_matches_title(title: str, label: str) -> bool:
    if title == label:
        return True
    if title.startswith(label):
        tail = title[len(label):]
        return not tail or not (tail[0].isalnum() and tail[0].isascii())
    return False


def _chapter_done(title: str, done_labels: set[str]) -> bool:
    return any(_label_matches_title(title, label) for label in done_labels)


def _run_one_chapter(args, section: str | None, label: str | None, *, skip_assets: bool = False) -> int:
    """Spawn a fresh single-section ingest (own process → own preflight, clean
    index, and git commit). Reuses the fully-tested single-run path unchanged."""
    argv = [sys.executable, str(Path(__file__).resolve()),
            "--limit", args.limit, "--profile", args.profile]
    if section is not None:
        argv += ["--section", section]
    if label is not None:
        argv += ["--section-label", label]
    if args.model:
        argv += ["--model", args.model]
    argv.append(str(_resolve_input_path(args.input)))
    env = os.environ.copy()
    env["PW_INGEST_NO_AUTOCHAPTER"] = "1"   # child is a single-section run; never re-chapter
    if skip_assets:
        env["PW_INGEST_SKIP_ASSETS"] = "1"
    else:
        env.pop("PW_INGEST_SKIP_ASSETS", None)
    return subprocess.run(argv, env=env).returncode


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
                     ("--kind", bool(args.kind)),
                     ("--rerender", args.rerender)):
        if on:
            die(f"--chapters cannot be combined with {flag}")
    if args.profile != "wiki":
        die("--chapters is only supported for the wiki profile")

    sections = _enumerate_sections(input_path)
    groups = _group_by_chapter(sections)
    if groups:
        chapters = [(label, _anchored_regex(members)) for label, members in groups]
        grouped = sum(len(members) for _, members in groups)
        out(f"detected {len(chapters)} chapter(s); {grouped} section(s) grouped "
            f"under them, {len(sections) - grouped} front/back-matter section(s) "
            f"excluded")
    else:
        # No chapter markers → one ingest per substantial section (structural
        # cover/TOC/title pages, which would crash the keyword pre-pass, dropped).
        substantial = [title for title, size in sections if size >= CHAPTER_MIN_CHARS]
        thin = [title for title, size in sections if size < CHAPTER_MIN_CHARS]
        if thin:
            preview = ", ".join(thin[:6]) + ("…" if len(thin) > 6 else "")
            out(f"no chapter markers; per-section ingest, skipping {len(thin)} "
                f"empty/structural section(s): {preview}")
        chapters = _group_chapters(substantial)
    if not chapters:
        out("no ingestable chapters detected — ingesting as a single unit")
        return _run_one_chapter(args, section=None, label=None)

    # Resume: find this source (by sha256) and the chapters already committed.
    src_sha = sha256_of(input_path)
    source_id = _source_id_for_sha(VAULT_ROOT / "sources", src_sha)
    done: set[str] = set()
    whole_done = False
    log_path = VAULT_ROOT / ".wiki" / "log.md"
    if source_id and log_path.is_file():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        done, whole_done = _source_log_progress(lines, source_id)

    total = len(chapters)
    if whole_done:
        out(f"chaptered ingest: {total} chapter(s), source already logged as a single unit, 0 to do")
        return 0

    remaining = sum(1 for label, _ in chapters if not _chapter_done(label, done))
    out(f"chaptered ingest: {total} chapter(s), {total - remaining} already done, "
        f"{remaining} to do")
    new = 0
    asset_pass_done = False
    for i, (label, section) in enumerate(chapters, start=1):
        if _chapter_done(label, done):
            out(f"[{i}/{total}] skip (done): {label}")
            continue
        out(f"[{i}/{total}] ingesting: {label}")
        rc = _run_one_chapter(args, section=section, label=label, skip_assets=asset_pass_done)
        if rc != 0:
            eout(f"[{i}/{total}] chapter failed (rc={rc}): {label}")
            eout("stopped — re-run the same command to resume from this chapter.")
            return rc
        new += 1
        asset_pass_done = True
    out(f"chaptered ingest complete: {new} new chapter(s), {total - remaining} skipped.")
    return 0


def main() -> int:
    global _ROLLBACK_ON_FAILURE
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--section", default="")
    ap.add_argument("--section-label", default="")
    ap.add_argument("--limit", default="100000")
    ap.add_argument("--model", default=os.environ.get("PW_LLM_MODEL", ""),
                    help="LLM model for ingest text (codex -m / API model). "
                         "Defaults to PW_LLM_MODEL, else the CLI's own default. "
                         "Separate from the caption model (CAPTION_MODEL), so "
                         "ingest can run on a cheaper model than captioning.")
    ap.add_argument("--keyword-model", default=os.environ.get("PW_KEYWORD_MODEL", ""),
                    help="Optional cheaper model for the keyword/entity pre-pass "
                         "(codex/API providers only). Defaults to PW_KEYWORD_MODEL.")
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
    ap.add_argument("--rerender", action="store_true",
                    help="media: re-render markdown from committed JSON after a render_format_version bump (no ASR)")
    ap.add_argument("--profile", choices=["wiki", "lang"], default="wiki",
                    help="ingest destination: wiki synthesis (default) or isolated "
                         "language study/vocab/grammar pages under content/lang/")
    ap.add_argument("input")
    args = ap.parse_args()
    section_label = validate_section_label(args.section_label)
    args.section_label = section_label

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
            ("--kind", bool(args.kind)), ("--rerender", args.rerender),
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
    if args.profile == "wiki":
        _PREEXISTING_SOURCE_PATHS = _source_path_snapshot()

    # Whole-book mode loops the normal single-section ingest once per chapter.
    # It runs after the common setup gates so missing tools and dirty vault state
    # produce the same diagnostics as a normal ingest.
    if chapter_mode:
        release_content_ingest_lock()
        return run_chaptered(args)

    # ── media --rerender (§7.5): a self-contained migration (no ASR, no LLM
    #    diff) — re-render from the committed JSON + supersede + rewire citations.
    if args.rerender:
        if not args.kind:
            die("--rerender requires --kind video|audio")
        sys.exit(subprocess.run([f"{SCRIPTS}/rerender-media.py", args.input]).returncode)

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
        r = subprocess.run([f"{SCRIPTS}/source-identity.py", src_input],
                           text=True, capture_output=True)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        sys.exit(1)
    SRC.update(parse_shell_assignments(r.stdout))
    dest = SRC["DEST"]
    if args.profile == "wiki" and not SRC.get("EXISTING_SIDECAR"):
        _register_new_source_artifacts(dest)
    elif args.profile == "wiki" and SRC.get("AUDIT_JSON"):
        _register_run_created_file(SRC["AUDIT_JSON"])

    if (
        args.profile == "wiki"
        and not args.kind
        and not args.section
        and not section_label
        and not args.images_only
        and whole_source_already_logged(SRC["SOURCE_ID"])
    ):
        out(f"whole-source ingest already logged for {SRC['SOURCE_ID']}; nothing to do")
        return 0
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
        extract_args = [dest, "--limit", args.limit]
        if args.section:
            extract_args = [dest, "--section", args.section, "--limit", args.limit]
        if write_assets:
            extract_args += ["--write-assets"]
        if SRC["ORIGIN_TYPE"] == "url":
            extract_args += ["--base-url", SRC["ORIGIN_REF"]]
        out(f"extracting text{f' (section={args.section})' if args.section else ''}...")
        with open(text_file, "w", encoding="utf-8") as f:
            if subprocess.run([f"{SCRIPTS}/extract.py", *extract_args], stdout=f).returncode != 0:
                die("extract failed")

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
        out("--images-only — skipping keyword pre-pass and main LLM diff")
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
            return 0
        git_run("commit", "-m", f"images-only: {SRC['SOURCE_ID']}{section_tag} ({SRC['DEST_BASENAME']})")
        out("committed images-only update")
        return 0

    text = read(text_file)
    out(f"extracted {len(text)} characters")
    if len(text) == 0:
        die("extractor produced empty text")
    if not args.section and _has_extraction_truncation_marker(text):
        die("extractor hit the text limit during a whole-source ingest; use "
            "--chapters for chaptered ingest or raise --limit so the source is "
            "not logged as complete from truncated text")

    # ── §5 source-term pre-pass / §6 collect candidates ──
    cap = int(os.environ.get("CAND_CAP", "20"))
    source_terms_file = mktemp()
    candidates_file = mktemp()
    pages = wiki_page_paths()
    source_head = text[:KEYWORD_SOURCE_HEAD_CHARS]
    kw_prompt = (
        "Extract all salient entity and concept names (people, products,\n"
        "projects, concepts, methods, organisms, enzymes, proteins,\n"
        "hypotheses, mechanisms, organizations, places) from the text below.\n"
        "Include every distinct, non-trivial name or concept that is central,\n"
        "recurring, or likely to deserve a reusable wiki node; do not stop at\n"
        "a fixed count. Output a newline-separated list with no numbering, no\n"
        "commentary, just one name per line. Use the same language as the\n"
        "source text (do not translate).\n\n"
        f"---\n{source_head}\n---\n"
    )
    out("extracting source key terms...")
    write(source_terms_file, llm(kw_prompt, soft=True, model=args.keyword_model or None))
    kw = read(source_terms_file)
    nonws = sum(1 for c in kw if not c.isspace())
    klines = kw.split("\n")
    nonempty = sum(1 for ln in klines if ln.strip())
    maxline = max((len(ln) for ln in klines), default=0)
    kw_valid = nonws >= 5 and nonempty >= 2 and maxline <= 100
    if not kw_valid:
        eout("source key-term pre-pass output looks like an error/prose, not keywords.")
        eout(f"  non-whitespace chars: {nonws} (need >=5)")
        eout(f"  non-empty lines:      {nonempty} (need >=2)")
        eout(f"  longest line:         {maxline} chars (need <=100)")
        eout("  raw output:")
        for ln in kw.splitlines():
            print(f"  | {ln}", file=sys.stderr)
        write(source_terms_file, "")
        if len(pages) > cap:
            die("source key-term pre-pass produced no usable output (LLM call failed?). "
                "Aborting before candidate selection would run blind.")
        eout("warn — continuing without source key terms because the full vault fits in the candidate cap")
    else:
        out("source key terms:")
        for ln in kw.splitlines():
            print(f"  - {ln}")

    if len(pages) <= cap:
        write(candidates_file, "".join(f"{p}\n" for p in pages))
        out(f"{len(pages)} candidate pages (full vault, cap={cap})")
    else:
        collect_candidates(source_terms_file, candidates_file, cap)

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

    build_prompt(expand_file, prompt_file, all_source_ids, text_file, candidates_file, source_terms_file, section_label, "digest")
    out(f"calling LLM (digest mode, {Path(prompt_file).stat().st_size} bytes)...")
    _seed_workset(codex_workdir, candidates_file, expand_file)
    write(diff_raw, final_newline(llm(read(prompt_file), soft=False)))

    run_stream([f"{SCRIPTS}/apply-diff.py", "detect-expand", diff_raw, expand_file])
    if Path(expand_file).stat().st_size > 0:
        n = read(expand_file).count("\n")
        out(f"LLM requested expansion of {n} file(s):")
        for ln in read(expand_file).splitlines():
            print(f"  - {ln}")
        build_prompt(expand_file, prompt_file, all_source_ids, text_file, candidates_file, source_terms_file, section_label, "expand")
        out(f"re-calling LLM with expanded content ({Path(prompt_file).stat().st_size} bytes)...")
        _seed_workset(codex_workdir, candidates_file, expand_file)
        write(diff_raw, final_newline(llm(read(prompt_file), soft=False)))

    handle_no_changes_or_continue(diff_raw, section_label)
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
                         candidates_file, source_terms_file, section_label, "retry")
            _seed_workset(codex_workdir, candidates_file, expand_file)
            write(diff_raw, final_newline(llm(read(prompt_file), soft=False)))
            handle_no_changes_or_continue(diff_raw, section_label)
            run_stream([f"{SCRIPTS}/apply-diff.py", "strip-fences", diff_raw, diff_file])
            scope_check(diff_file, retry=True)
        else:
            _reject_scoped_diff(diff_file, retry=False,
                                stdout=scope_stdout, stderr=scope_stderr)

    # ── §8 apply + auto-retry ──
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
            if not WIKI_PAGE_RX.match(p):
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
        build_prompt(expand_file, prompt_file, all_source_ids, text_file, candidates_file, source_terms_file, section_label, "retry")
        _seed_workset(codex_workdir, candidates_file, expand_file)
        write(diff_raw, final_newline(llm(read(prompt_file), soft=False)))  # bash: || die "LLM call failed (retry)"
        handle_no_changes_or_continue(diff_raw, section_label)
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
    if modified:
        out("normalizing llm-zone formatting...")
        run_stream([f"{SCRIPTS}/format-llm-zone.py", *modified])
        out("ensuring page_id on modified pages...")
        run_stream([f"{SCRIPTS}/add-page-id.py", *modified])
        out("syncing frontmatter on modified pages...")
        run_stream([f"{SCRIPTS}/sync-frontmatter.py", "--date", today_str, *modified])
        out("validating tags (lint --gate=tags)...")
        run_stream([f"{SCRIPTS}/lint.py", "--gate=tags"])
        out("validating image embeds (lint --gate=images)...")
        run_stream([f"{SCRIPTS}/lint.py", "--gate=images"])
        if args.kind:
            # Every media ingest runs the COMBINED media-anchor gate (timestamp §7.4 +
            # card §8.2 + frame §8.3 + citation orphans). Running only the kind-matching
            # validator would let a wrong-CAPABILITY anchor the LLM emitted slip past the
            # front door (e.g. a [src:<id>#mm:ss] on an image_note, or a [src:<id>#frame-N]
            # on a plain video) — each validator flags such mismatches as a capability
            # error, but only if it actually runs.
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
    if subprocess.run([f"{SCRIPTS}/alias-index.py", "check"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        eout("ABORT — alias-uniqueness check failed:")
        subprocess.run([f"{SCRIPTS}/alias-index.py", "check"])
        eout("  Resolve the collision in the vault or candidate set, then re-run.")
        eout("  This failed run will roll back its staged wiki/source edits.")
        die("alias-uniqueness check failed")
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
    out("done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        _cleanup()
