#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""
Source-citation rewriter (expansion-plan §7.5) — repoint every citation of one
source_id to another across the wiki, preserving the anchor:
`[src:<old>#mm:ss]` → `[src:<new>#mm:ss]`, and the frontmatter `sources:` entry.

This is NOT `rewire.py` (which renames `[[wikilinks]]` on page moves and never
touches `[src:]`). Used by media supersession to migrate live citations to a
superseding source_id.

Both ids are 26-char ULIDs — globally unique tokens — so a plain text replace of
`<old>` → `<new>` within a page safely covers both the `[src:<old>#…]` body
anchors and the `sources: [<old>]` frontmatter (no other content matches a ULID).

Scope: wiki/{entities,topics,_index,_maps}/**.md (cwd-relative; honors
$VAULT_CONTENT_DIR). Transactional: stage every rewritten page to a temp file
first, then move them all into place — on any staging error, nothing is moved.

Usage:  rewrite-citations.py <old_source_id> <new_source_id>
stdout: `rewrote N page(s)`  (exit 0); errors prefixed `ingest:` (exit 1).
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ — for _util
from _util import die  # noqa: E402  (shared die-loud, single source of truth)

_ULID = re.compile(r"^[0-9A-Z]{26}$")


def wiki_pages() -> list[Path]:
    pages: list[Path] = []
    for sub in ("entities", "topics", "_index", "_maps"):
        d = Path("wiki", sub)
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))
    return pages


def main() -> int:
    if len(sys.argv) != 3:
        die("usage: rewrite-citations.py <old_source_id> <new_source_id>")
    old, new = sys.argv[1], sys.argv[2]
    if not (_ULID.match(old) and _ULID.match(new)):
        die("both ids must be 26-char ULIDs")
    if old == new:
        die("old and new source_id are identical")

    vcd = os.environ.get("VAULT_CONTENT_DIR")
    if vcd:
        os.chdir(vcd)

    # Stage: write every affected page's rewritten content to a temp file.
    staged: list[tuple[Path, str]] = []  # (page, temp_path)
    try:
        for page in wiki_pages():
            text = page.read_text(encoding="utf-8")
            if old not in text:
                continue
            fd, tmp = tempfile.mkstemp(prefix="rewrite-cit-", dir=str(page.parent))
            os.close(fd)
            Path(tmp).write_text(text.replace(old, new), encoding="utf-8")
            staged.append((page, tmp))
    except OSError as exc:
        for _, tmp in staged:
            try:
                os.remove(tmp)
            except OSError:
                pass
        die(f"staging failed (nothing moved): {exc}")

    # Commit: move each staged temp over its page (per-file atomic).
    for page, tmp in staged:
        os.replace(tmp, page)

    print(f"rewrote {len(staged)} page(s): [src:{old[:8]}…] → [src:{new[:8]}…]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
