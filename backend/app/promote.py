"""Promote an annotation into a wiki page's human-zone (Source Reader P3).

The vault convention (remark-zones) delimits the person's own zone with HTML
comments: `<!-- human-zone -->` … `<!-- /human-zone -->`. Promoting a note writes
it *only* inside that zone — never touching the LLM synthesis — and is idempotent:
each promoted note is wrapped in `<!-- anno:<id> -->` … `<!-- /anno:<id> -->`, so
re-promoting the same annotation updates its block in place instead of duplicating.

The write is committed in the configured local wiki git repo (under the ingest lock,
so it can't race an ingest commit). Pure helpers (`render_note`, `insert_note`)
are kept side-effect-free so they can be unit-tested without a repo.
"""
from __future__ import annotations
import contextlib
import fcntl
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote as _q

HUMAN_OPEN = "<!-- human-zone -->"
HUMAN_CLOSE = "<!-- /human-zone -->"
COMMENT_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
HUMAN_OPEN_RE = re.compile(r"(?m)^" + re.escape(HUMAN_OPEN) + r"[ \t]*(?:\r?$)")
HUMAN_CLOSE_RE = re.compile(r"(?m)^" + re.escape(HUMAN_CLOSE) + r"[ \t]*(?:\r?$)")
LOGGER = logging.getLogger(__name__)


def _comment_token(value: str) -> str:
    return COMMENT_TOKEN_RE.sub("_", str(value or "")).strip("_")[:120] or "annotation"


def _plain_md_text(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _link_label(value: str) -> str:
    return _plain_md_text(value).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def reader_deeplink(anno: dict) -> str:
    """Fine anchor back into the source reader for this annotation's exact block."""
    t = anno.get("target") or {}
    ctx = t.get("context") or {}
    sel = t.get("selector") or {}
    params = {
        "b": t.get("block_id") or "",
        "s": t.get("section_id") or "",
        "prev": ctx.get("prev_block_id") or "",
        "next": ctx.get("next_block_id") or "",
        "q": sel.get("quote") or "",
    }
    frag = "&".join(f"{k}={_q(str(v), safe='')}" for k, v in params.items() if v)
    source_id = _q(str(anno.get("source_id") or ""), safe="")
    base = f"/sources/{source_id}/read"
    return base + ("#" + frag if frag else "")


def render_note(anno: dict, source_title: str) -> str:
    """Markdown block for one promoted annotation, tagged for idempotent replace."""
    aid = _comment_token(anno["id"])
    sel = (anno.get("target") or {}).get("selector") or {}
    quote = _plain_md_text((sel.get("quote") or "").strip())
    body = _plain_md_text((anno.get("body") or "").strip())
    link = reader_deeplink(anno)
    title = _link_label((source_title or "source").strip())
    lines = [f"<!-- anno:{aid} -->"]
    if quote:
        lines.append(f"> “{quote}” — [{title}]({link})")
    else:
        lines.append(f"> [{title}]({link})")
    if body:
        lines.append(">")
        for para in body.splitlines() or [body]:
            lines.append(f"> {para}".rstrip())
    lines.append(f"<!-- /anno:{aid} -->")
    return "\n".join(lines)


def _anno_block_re(aid: str) -> re.Pattern:
    return re.compile(
        r"\n?<!--\s*anno:" + re.escape(aid) + r"\s*-->"
        r"(?:(?!<!--\s*/?anno:).)*"
        r"<!--\s*/anno:" + re.escape(aid) + r"\s*-->\n?",
        re.DOTALL,
    )


def insert_note(page_text: str, aid: str, note_md: str) -> str:
    """Idempotently place `note_md` inside the page's human-zone.

    - If this annotation was already promoted, replace its block in place.
    - Else insert just before `<!-- /human-zone -->`.
    - Else (no human-zone yet) append a fresh human-zone at the end of the page.
    """
    existing = _anno_block_re(aid)
    if existing.search(page_text):
        return existing.sub(lambda _m: "\n" + note_md + "\n", page_text)

    close = HUMAN_CLOSE_RE.search(page_text)
    if close:
        idx = close.start()
        head = page_text[:idx].rstrip("\n")
        return head + "\n\n" + note_md + "\n\n" + page_text[idx:]
    if HUMAN_CLOSE in page_text:
        raise ValueError("malformed human-zone close marker: expected it on its own line")

    body = page_text.rstrip("\n")
    zone = f"{HUMAN_OPEN}\n\n{note_md}\n\n{HUMAN_CLOSE}"
    return body + "\n\n" + zone + "\n"


def read_zone(page_text: str) -> str | None:
    """Inner markdown of the human-zone, or None when the page has no zone."""
    o = HUMAN_OPEN_RE.search(page_text)
    c = HUMAN_CLOSE_RE.search(page_text)
    if not o or not c or c.start() < o.end():
        return None
    return page_text[o.end():c.start()].strip("\n")


def replace_zone(page_text: str, inner: str) -> str:
    """Replace the human-zone's inner markdown (append a zone if none exists)."""
    inner = inner.strip("\n")
    block = f"{HUMAN_OPEN}\n\n{inner}\n\n{HUMAN_CLOSE}" if inner else f"{HUMAN_OPEN}\n{HUMAN_CLOSE}"
    o = HUMAN_OPEN_RE.search(page_text)
    c = HUMAN_CLOSE_RE.search(page_text)
    if o and c and c.start() >= o.end():
        return page_text[:o.start()] + block + page_text[c.end():]
    if o or c:
        raise ValueError("malformed human-zone markers: expected a matched open/close pair, each on its own line")
    return page_text.rstrip("\n") + "\n\n" + block + "\n"


def get_zone(content_dir: Path, wiki_rel: str) -> dict:
    """Read a wiki page's human-zone. Raises ValueError for a bad path / missing page."""
    path = _safe_page_path(content_dir.resolve(), wiki_rel)
    if not path.exists():
        raise ValueError(f"wiki page not found: wiki/{wiki_rel}.md")
    text = read_zone(path.read_text(encoding="utf-8"))
    return {"wiki_rel": wiki_rel, "text": text or "", "exists": text is not None}


def set_zone(content_dir: Path, wiki_rel: str, text: str) -> dict:
    """Replace a wiki page's human-zone and git-commit it (same guarantees as promote)."""
    content_dir = content_dir.resolve()
    with _content_ingest_lock(content_dir):
        path = _safe_page_path(content_dir, wiki_rel)
        if not path.exists():
            raise ValueError(f"wiki page not found: wiki/{wiki_rel}.md")
        _ensure_clean_target_page(content_dir, path)
        original = path.read_text(encoding="utf-8")
        updated = replace_zone(original, text)
        committed = False
        if updated != original:
            index_snapshot = _git_index_snapshot(content_dir, path)
            _atomic_write_text(path, updated)
            try:
                committed = _git_commit(content_dir, path, f"human-zone: edit wiki/{wiki_rel}")
            except Exception:
                _atomic_write_text(path, original)
                _restore_git_index_snapshot(content_dir, path, index_snapshot)
                raise
    return {"ok": True, "wiki_rel": wiki_rel, "committed": committed}


def _safe_page_path(content_dir: Path, wiki_rel: str) -> Path:
    rel = (wiki_rel or "").strip().lstrip("/")
    if rel.endswith(".md"):
        rel = rel[:-3]
    wiki_root = (content_dir / "wiki").resolve()
    path = (wiki_root / f"{rel}.md").resolve()
    if not str(path).startswith(str(wiki_root) + "/"):
        raise ValueError(f"refusing to write outside wiki/: {wiki_rel}")
    return path


def promote_to_page(anno: dict, source_title: str, content_dir: Path, wiki_rel: str) -> dict:
    """Write the note into <wiki-folder>/wiki/<wiki_rel>.md and git-commit it.

    Returns {ok, wiki_rel, href, created_zone, committed}. Raises ValueError for a
    bad path / missing page, RuntimeError for git failures.
    """
    content_dir = content_dir.resolve()
    with _content_ingest_lock(content_dir):
        return _promote_to_page_locked(anno, source_title, content_dir, wiki_rel)


def _promote_to_page_locked(anno: dict, source_title: str, content_dir: Path, wiki_rel: str) -> dict:
    path = _safe_page_path(content_dir, wiki_rel)
    if not path.exists():
        raise ValueError(f"wiki page not found: wiki/{wiki_rel}.md")
    _ensure_clean_target_page(content_dir, path)
    original = path.read_text(encoding="utf-8")
    aid = _comment_token(anno["id"])
    note_md = render_note(anno, source_title)
    created_zone = HUMAN_CLOSE_RE.search(original) is None and not _anno_block_re(aid).search(original)
    updated = insert_note(original, aid, note_md)
    committed = False
    if updated != original:
        index_snapshot = _git_index_snapshot(content_dir, path)
        _atomic_write_text(path, updated)
        try:
            committed = _git_commit(content_dir, path, f"human-zone: promote annotation {aid} → wiki/{wiki_rel}")
        except Exception:
            _atomic_write_text(path, original)
            _restore_git_index_snapshot(content_dir, path, index_snapshot)
            raise
    href = "/wiki/" + wiki_rel.strip("/").removesuffix(".md")
    return {"ok": True, "wiki_rel": wiki_rel, "href": href,
            "created_zone": created_zone, "committed": committed}


@contextlib.contextmanager
def _content_ingest_lock(content_dir: Path):
    lock_path = content_dir / ".wiki" / "ingest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _git_commit(content_dir: Path, path: Path, message: str) -> bool:
    """Best-effort commit in the content repo; returns True on success. If the dir
    isn't a git repo the file is still written (returns False)."""
    if not (content_dir / ".git").exists():
        return False
    rel = str(path.relative_to(content_dir))
    try:
        subprocess.run(["git", "-C", str(content_dir), "add", rel],
                       check=True, capture_output=True, text=True, timeout=30)
        # Nothing staged (identical content) → treat as no-op success.
        st = subprocess.run(["git", "-C", str(content_dir), "diff", "--cached", "--quiet", "--", rel],
                            capture_output=True, text=True, timeout=30)
        if st.returncode == 0:
            return False
        subprocess.run(["git", "-C", str(content_dir), "commit", "-m", message, "--", rel],
                       check=True, capture_output=True, text=True, timeout=30)
        return True
    except subprocess.CalledProcessError as e:  # noqa
        raise RuntimeError((e.stderr or e.stdout or str(e)).strip())


def _ensure_clean_target_page(content_dir: Path, path: Path) -> None:
    if not (content_dir / ".git").exists():
        return
    rel = str(path.relative_to(content_dir))
    result = subprocess.run(
        ["git", "-C", str(content_dir), "-c", "core.quotepath=false", "status", "--porcelain", "--", rel],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or "git status failed")
    if result.stdout.strip():
        raise RuntimeError(f"target wiki page has uncommitted changes: {rel}")


def _git_index_snapshot(content_dir: Path, path: Path) -> tuple[str, str] | None:
    if not (content_dir / ".git").exists():
        return None
    rel = str(path.relative_to(content_dir))
    result = subprocess.run(
        ["git", "-C", str(content_dir), "ls-files", "-s", "--", rel],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    fields = result.stdout.split(maxsplit=3)
    if len(fields) < 2:
        return None
    return fields[0], fields[1]


def _restore_git_index_snapshot(content_dir: Path, path: Path, snapshot: tuple[str, str] | None) -> None:
    if not (content_dir / ".git").exists():
        return
    rel = str(path.relative_to(content_dir))
    try:
        if snapshot is None:
            subprocess.run(
                ["git", "-C", str(content_dir), "rm", "--cached", "--ignore-unmatch", "-q", "--", rel],
                check=False, capture_output=True, text=True, timeout=30,
            )
            return
        mode, blob = snapshot
        subprocess.run(
            ["git", "-C", str(content_dir), "update-index", "--add", "--cacheinfo", mode, blob, rel],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        # The page content has already been restored. If index restoration fails,
        # surface the original commit error rather than masking it.
        LOGGER.warning(
            "failed to restore git index snapshot content_dir=%s path=%s: %s",
            content_dir,
            path,
            (exc.stderr or exc.stdout or str(exc)).strip(),
        )
        return
