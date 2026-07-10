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
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote as _q

HUMAN_OPEN = "<!-- human-zone -->"
HUMAN_CLOSE = "<!-- /human-zone -->"
COMMENT_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


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
        r"\n?<!--\s*anno:" + re.escape(aid) + r"\s*-->.*?<!--\s*/anno:" + re.escape(aid) + r"\s*-->\n?",
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

    if HUMAN_CLOSE in page_text:
        idx = page_text.index(HUMAN_CLOSE)
        head = page_text[:idx].rstrip("\n")
        return head + "\n\n" + note_md + "\n\n" + page_text[idx:]

    body = page_text.rstrip("\n")
    zone = f"{HUMAN_OPEN}\n\n{note_md}\n\n{HUMAN_CLOSE}"
    return body + "\n\n" + zone + "\n"


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
    path = _safe_page_path(content_dir, wiki_rel)
    if not path.exists():
        raise ValueError(f"wiki page not found: wiki/{wiki_rel}.md")
    original = path.read_text(encoding="utf-8")
    aid = _comment_token(anno["id"])
    note_md = render_note(anno, source_title)
    created_zone = HUMAN_CLOSE not in original and not _anno_block_re(aid).search(original)
    updated = insert_note(original, aid, note_md)
    committed = False
    if updated != original:
        _atomic_write_text(path, updated)
        committed = _git_commit(content_dir, path, aid, wiki_rel)
    href = "/wiki/" + wiki_rel.strip("/").removesuffix(".md")
    return {"ok": True, "wiki_rel": wiki_rel, "href": href,
            "created_zone": created_zone, "committed": committed}


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


def _git_commit(content_dir: Path, path: Path, aid: str, wiki_rel: str) -> bool:
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
        subprocess.run(["git", "-C", str(content_dir), "commit", "-m",
                        f"human-zone: promote annotation {aid} → wiki/{wiki_rel}", "--", rel],
                       check=True, capture_output=True, text=True, timeout=30)
        return True
    except subprocess.CalledProcessError as e:  # noqa
        raise RuntimeError((e.stderr or e.stdout or str(e)).strip())
