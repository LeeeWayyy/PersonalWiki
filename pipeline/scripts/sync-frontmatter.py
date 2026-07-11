#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Sync `sources:` and `last_ingested:` frontmatter on wiki pages from
the [src:<id>...] citations that actually appear in the body.

Usage:
    scripts/sync-frontmatter.py [--date YYYY-MM-DD] <path>...
    scripts/sync-frontmatter.py [--date YYYY-MM-DD] --all

What it does, per file:
  - Scans the body for every `src:<ULID>` citation (strips any `#anchor`).
  - Rewrites the `sources:` line to the sorted unique set.
  - Sets `last_ingested: <date>` if any change happened (or forced via --date).

Surgical: only the two lines change. Ordering of other frontmatter
fields, comments, and body content are preserved.

Called automatically by ingest.py after `git apply`. Also safe to run
by hand for cleanup.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date as _date
from pathlib import Path

from _util import default_vault_root, split_frontmatter
from source_citations import SOURCE_ID_RX, iter_source_citations

TOOLING_ROOT = Path(__file__).resolve().parent.parent  # tooling repo (scripts/, schema.md)
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
WIKI_DIR = VAULT_ROOT / "wiki"

# Two-step citation extraction: find `[...]` segments containing `src:`,
# then pull `src:<ULID>` out of each. Tolerates interior whitespace (e.g.
# `[src:a, src:b]`) and any anchor form, while ignoring `src:…` mentions
# outside brackets (e.g. headings like `### From src:01K…`).
# Strip fenced code blocks before citation scanning so example citations
# inside ``` … ``` blocks don't pollute `sources:`. Mirrors lint.py so
# both tools agree on what counts as a citation; otherwise sync writes a
# bogus source_id and lint immediately flags frontmatter drift.
_FENCED_CODE_RX = re.compile(r"```.*?```", re.DOTALL)
def citations_in(body: str) -> list[str]:
    seen: dict[str, None] = {}
    body = _FENCED_CODE_RX.sub("", body)
    for citation in iter_source_citations(body):
        if SOURCE_ID_RX.fullmatch(citation.source_id):
            seen.setdefault(citation.source_id, None)
    return list(seen.keys())


def _format_list(ids: list[str]) -> str:
    """Flow-style YAML list: [a, b, c]."""
    return "[" + ", ".join(ids) + "]"


def _upsert_line(fm_body: str, key: str, new_value: str) -> tuple[str, bool]:
    """Replace the `key:` entry with a single `key: <new_value>` line.
    Handles both flow-style (`key: foo`) and block-style:
        key:
          - a
          - b
    by skipping any continuation lines (indented or blank/comment) that
    belong to the entry. If the entry is absent, append at end. Returns
    (new_body, changed) — second value is True iff the body actually
    changed.

    Returns whether the frontmatter body changed so callers can avoid
    unnecessary writes."""
    new_line = f"{key}: {new_value}"
    lines = fm_body.split("\n")
    out: list[str] = []
    i = 0
    found = False
    key_rx = re.compile(rf"^{re.escape(key)}\s*:\s*(.*)$")
    while i < len(lines):
        line = lines[i]
        m = key_rx.match(line) if not found else None
        if m:
            value = m.group(1)
            val_no_comment = re.sub(r"\s+#.*$", "", value).rstrip()
            out.append(new_line)
            found = True
            i += 1
            if val_no_comment.strip():
                # Single-line entry (flow scalar / flow list).
                continue
            # Block style: skip continuation. Continuation lines are:
            #   - blank lines
            #   - comment lines (`# …`)
            #   - indented lines (`  - foo`, `  key: …`)
            #   - flush-left dash lines (`- foo`) — YAML's compact block
            #     list style where dashes sit at the parent key's indent.
            while i < len(lines):
                cont = lines[i]
                if (cont == ""
                        or re.match(r"^\s*#", cont)
                        or re.match(r"^[ \t]+\S", cont)
                        or re.match(r"^-(\s|$)", cont)):
                    i += 1
                    continue
                break
            continue
        out.append(line)
        i += 1
    if found:
        new_body = "\n".join(out)
        return new_body, new_body != fm_body
    # Not present — append at end.
    sep = "" if fm_body.endswith("\n") else "\n"
    return fm_body + sep + new_line, True


def sync_file(path: Path, today: str | None) -> tuple[bool, str]:
    """Return (changed, message)."""
    text = path.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    if not split:
        return False, f"  skip (no frontmatter): {path.relative_to(VAULT_ROOT)}"
    before, fm_body, after = split

    cites = citations_in(after)
    # When citations become empty (a regen removed them all), still rewrite
    # the frontmatter to `sources: []` rather than silently leaving stale
    # entries. Lint check #4 will then flag this as an unused-sources case.
    sources_value = _format_list(sorted(cites))
    new_fm, changed_src = _upsert_line(fm_body, "sources", sources_value)

    if today is None:
        # Only bump last_ingested if we actually changed sources.
        if not changed_src:
            return False, f"  ok: {path.relative_to(VAULT_ROOT)}"
        today = _date.today().isoformat()

    new_fm, changed_date = _upsert_line(new_fm, "last_ingested", today)

    if not (changed_src or changed_date):
        return False, f"  ok: {path.relative_to(VAULT_ROOT)}"

    new_text = f"---\n{new_fm}\n---\n{after}"
    path.write_text(new_text, encoding="utf-8")

    changes = []
    if changed_src:
        changes.append(f"sources={sources_value}")
    if changed_date:
        changes.append(f"last_ingested={today}")
    return True, f"  updated {path.relative_to(VAULT_ROOT)}: {', '.join(changes)}"


def collect_all_pages() -> list[Path]:
    pages: list[Path] = []
    for sub in ("entities", "topics"):
        d = WIKI_DIR / sub
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))
    return pages


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", help="wiki pages to sync")
    ap.add_argument("--all", action="store_true", help="sync every page under wiki/")
    ap.add_argument(
        "--date",
        default=None,
        help="YYYY-MM-DD to force as last_ingested (default: today when anything changes)",
    )
    args = ap.parse_args()

    if args.all:
        targets = collect_all_pages()
    else:
        targets = [Path(p).resolve() for p in args.paths]

    if not targets:
        print("sync-frontmatter: nothing to do", file=sys.stderr)
        return 0

    for path in targets:
        if not path.is_file():
            print(f"  skip (missing): {path}", file=sys.stderr)
            continue
        _changed, msg = sync_file(path, args.date)
        print(msg)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
