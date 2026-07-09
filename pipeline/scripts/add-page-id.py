#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Ensure every wiki page has a `page_id: <ULID>` line in its frontmatter.

`page_id` is an immutable, rename-safe identity for the page. Tools
(rewire, lint, alias index) reference pages by it so renames and moves
don't rot incoming pointers.

Usage:
    scripts/add-page-id.py <path>...        # backfill specific pages
    scripts/add-page-id.py --all            # every page under wiki/
    scripts/add-page-id.py --check          # exit 1 if any page is missing one

Surgical: only inserts the line if absent. Existing `page_id` values
are preserved verbatim. Other frontmatter ordering is untouched.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ — for _util
from _util import default_vault_root, new_ulid  # noqa: E402

TOOLING_ROOT = Path(__file__).resolve().parent.parent  # tooling repo (scripts/, schema.md)
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
WIKI_DIR = VAULT_ROOT / "wiki"

FM_RX = re.compile(r"^---\n(.*?)\n---(?:\n|$)", re.DOTALL)
PAGE_ID_RX = re.compile(r"^page_id:\s*\S+", re.MULTILINE)
TYPE_LINE_RX = re.compile(r"^type:\s*.*$", re.MULTILINE)

def all_wiki_pages() -> list[Path]:
    pages: list[Path] = []
    for sub in ("entities", "topics"):
        d = WIKI_DIR / sub
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))
    return pages


def add_page_id(path: Path) -> tuple[bool, str]:
    """Return (changed, message)."""
    text = path.read_text(encoding="utf-8")
    m = FM_RX.match(text)
    if not m:
        return False, f"  skip (no frontmatter): {path.relative_to(VAULT_ROOT)}"
    fm = m.group(1)
    if PAGE_ID_RX.search(fm):
        return False, f"  ok: {path.relative_to(VAULT_ROOT)}"

    pid = new_ulid()
    new_line = f"page_id: {pid}"
    # Insert immediately after the `type:` line so frontmatter stays grouped:
    # type → page_id → aliases → sources → last_ingested.
    if TYPE_LINE_RX.search(fm):
        new_fm = TYPE_LINE_RX.sub(
            lambda m: f"{m.group(0)}\n{new_line}", fm, count=1
        )
    else:
        new_fm = fm.rstrip("\n") + "\n" + new_line

    new_text = f"---\n{new_fm}\n---\n{text[m.end():]}"
    path.write_text(new_text, encoding="utf-8")
    return True, f"  added page_id={pid}: {path.relative_to(VAULT_ROOT)}"


def check_only(targets: list[Path]) -> int:
    missing: list[Path] = []
    for path in targets:
        text = path.read_text(encoding="utf-8")
        m = FM_RX.match(text)
        if not m or not PAGE_ID_RX.search(m.group(1)):
            missing.append(path)
    if not missing:
        print(f"  ✓ all {len(targets)} page(s) have page_id")
        return 0
    print(f"  ✗ {len(missing)} page(s) missing page_id:")
    for p in missing:
        print(f"    {p.relative_to(VAULT_ROOT)}")
    print("  → fix: scripts/add-page-id.py --all")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", help="wiki pages")
    ap.add_argument("--all", action="store_true", help="all pages under wiki/")
    ap.add_argument("--check", action="store_true", help="exit 1 if any page is missing page_id")
    args = ap.parse_args()

    if args.all:
        targets = all_wiki_pages()
    else:
        targets = [Path(p).resolve() for p in args.paths]

    if not targets:
        print("add-page-id: nothing to do", file=sys.stderr)
        return 0

    targets = [p for p in targets if p.is_file()]

    if args.check:
        return check_only(targets)

    for path in targets:
        _, msg = add_page_id(path)
        print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
