#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
"""
Safely rename a wiki page and rewrite every wikilink that points to it.

A bare `git mv` would orphan every `[[OldName]]` in the vault. This
moves the file, preserves the old name as an alias on the moved page
(so historical references still resolve), rewrites every `[[OldName]]`
across `wiki/` to `[[NewName]]` (preserving `#anchor` and `|alt-text`),
and finally rebuilds the alias index.

Usage:
    scripts/rewire.py [--dry-run] <old_path> <new_path>

Both paths must be under `wiki/entities/` or `wiki/topics/` and end in
`.md`. The H1 of the moved page is rewritten to match the new stem.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import yaml

from _util import default_vault_root

TOOLING_ROOT = Path(__file__).resolve().parent.parent  # tooling repo (scripts/, schema.md)
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
WIKI_DIR = VAULT_ROOT / "wiki"

WIKILINK_RX = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
H1_RX = re.compile(r"^#\s+.+$", re.MULTILINE)
TYPE_LINE_RX = re.compile(r"^type:\s*.*$", re.MULTILINE)
FM_RX = re.compile(r"^---\n(.*?)\n---(?:\n|$)", re.DOTALL)


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    return re.sub(r"\s+", " ", s).strip()


def all_wiki_pages() -> list[Path]:
    """Inclusion list — entities/ + topics/ only.

    Shared with alias-index.py / autolink.py / add-page-id.py via the
    same convention. Do NOT add `_index/` here; MOCs lack `page_id`,
    have non-matching H1, etc. For rewire's link-rewriting scan use
    `_pages_to_rewrite()` below instead."""
    pages: list[Path] = []
    for sub in ("entities", "topics"):
        d = WIKI_DIR / sub
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))
    return pages


def _pages_to_rewrite() -> list[Path]:
    """Pages whose wikilinks must be scanned for rewriting on rename.
    Includes content pages plus generated MOCs and maps so generated links
    follow renames immediately instead of staying orphaned until regeneration.

    Implementation note: this is a local extension of `all_wiki_pages()`,
    NOT a modification. Other scripts (alias-index, add-page-id) still
    rely on the strict inclusion list and must keep their behavior
    unchanged."""
    pages = all_wiki_pages()
    for generated_dir in (WIKI_DIR / "_index", WIKI_DIR / "_maps"):
        if generated_dir.is_dir():
            pages.extend(sorted(generated_dir.rglob("*.md")))
    return pages


def rewrite_wikilinks(text: str, old_stem: str, new_stem: str) -> tuple[str, int]:
    """Return (new_text, count). Replaces `[[old]]`, `[[old#a]]`,
    `[[old|t]]`, `[[old#a|t]]` with the new stem; preserves anchor and
    alt-text segments verbatim."""
    old_norm = normalize(old_stem)
    count = 0

    def sub(m: re.Match) -> str:
        nonlocal count
        ref = m.group(1).strip()
        if normalize(ref) != old_norm:
            return m.group(0)
        count += 1
        # Everything after the original page-reference (anchor + alt + closing ]]).
        tail = m.group(0)[2 + len(m.group(1)):]
        return f"[[{new_stem}{tail}"

    return WIKILINK_RX.sub(sub, text), count


def _format_aliases_line(items: list[str]) -> str:
    """Flow-style: `aliases: [a, b, c]`. Quote any item that, left bare,
    would shift YAML semantics: `,` `[` `]` (flow-list delimiters), `:`
    (would be parsed as a mapping pair, e.g. `Chapter 1: Intro`), and
    surrounding whitespace. We use single quotes (escaping a literal `'`
    by doubling it per YAML spec) so the encoded form is unambiguous and
    cannot itself become invalid like a naive double-quote wrap of an
    alias containing `"`."""
    def needs_quote(s: str) -> bool:
        if not s:
            return True
        if s != s.strip():
            return True
        return any(c in s for c in ",[]:\"'#&*!|>%@`")

    def fmt(s: str) -> str:
        if not needs_quote(s):
            return s
        return "'" + s.replace("'", "''") + "'"

    return f"aliases: [{', '.join(fmt(a) for a in items)}]"


def _replace_aliases_entry(fm_text: str, new_line: str) -> str | None:
    """Find the existing `aliases:` entry (flow or block style) and replace
    it with `new_line`. Returns the new fm text, or None if no entry exists.

    Walks line by line so it handles cases the prior regex got wrong:
    - flow with inline comment: `aliases: [a, b] # canonical first`
    - block with interleaved comments / blank lines:
          aliases:
            # primary
            - Foo
            # secondary
            - Bar
    Continuation of a block entry = blank lines, comment lines, or lines
    indented relative to the parent. Stops at the next non-indented,
    non-blank, non-comment line (i.e. the next top-level YAML key)."""
    lines = fm_text.split("\n")
    out: list[str] = []
    i = 0
    found = False
    aliases_rx = re.compile(r"^aliases\s*:\s*(.*)$")
    while i < len(lines):
        line = lines[i]
        m = aliases_rx.match(line) if not found else None
        if m:
            value = m.group(1)
            # Strip inline comment from the value to detect flow vs block.
            val_no_comment = re.sub(r"\s+#.*$", "", value).rstrip()
            out.append(new_line)
            found = True
            i += 1
            if val_no_comment.strip():
                # Flow style or scalar on the same line — single-line entry.
                continue
            # Block style: skip continuation. Continuation lines are:
            #   - blank lines
            #   - comment lines
            #   - indented lines (standard block list/map style)
            #   - flush-left dash lines (YAML's compact block list, where
            #     dashes sit at the parent key's indent column 0).
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
    return "\n".join(out) if found else None


def add_alias(fm_text: str, new_alias: str) -> tuple[str, bool]:
    """Insert `new_alias` into the `aliases:` list if missing. Handles
    flow-style (`aliases: [a, b]`), flow with inline comment, block-style
    (`aliases:\\n  - a`), and block with interleaved comments. The result
    is always canonical flow style. Returns (new_fm, changed)."""
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        raise SystemExit("rewire: frontmatter is not valid YAML; aborting")

    raw_aliases = data.get("aliases") or []
    # If frontmatter has `aliases: Nick Lane` (a scalar, not a list), YAML
    # parses it as a string. Wrapping it in a list with `list(raw_aliases)`
    # would explode it into per-character entries. Treat scalars as a
    # single-element list instead.
    if isinstance(raw_aliases, str):
        raw_aliases = [raw_aliases]
    elif not isinstance(raw_aliases, list):
        raw_aliases = []
    items: list[str] = [str(a) for a in raw_aliases if isinstance(a, (str, int, float))]
    new_norm = normalize(new_alias)
    if any(normalize(a) == new_norm for a in items):
        return fm_text, False
    items.append(new_alias)

    new_line = _format_aliases_line(items)
    replaced = _replace_aliases_entry(fm_text, new_line)
    if replaced is not None:
        return replaced, True

    # No aliases entry yet — insert after `type:` if present, else append.
    if TYPE_LINE_RX.search(fm_text):
        return TYPE_LINE_RX.sub(
            lambda mm: f"{mm.group(0)}\n{new_line}", fm_text, count=1
        ), True
    return fm_text.rstrip("\n") + "\n" + new_line, True


def rewrite_h1(text: str, new_stem: str) -> tuple[str, bool]:
    m = H1_RX.search(text)
    if not m:
        return text, False
    new_h1 = f"# {new_stem}"
    if m.group(0) == new_h1:
        return text, False
    return text[:m.start()] + new_h1 + text[m.end():], True


def validate_path(p: Path, label: str) -> Path:
    p = p.resolve()
    try:
        rel = p.relative_to(VAULT_ROOT)
    except ValueError:
        sys.exit(f"rewire: {label} must be inside the vault: {p}")
    parts = rel.parts
    if len(parts) < 3 or parts[0] != "wiki" or parts[1] not in ("entities", "topics"):
        sys.exit(f"rewire: {label} must be under wiki/entities/ or wiki/topics/: {rel}")
    if p.suffix != ".md":
        sys.exit(f"rewire: {label} must end in .md: {rel}")
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("old_path")
    ap.add_argument("new_path")
    ap.add_argument("--dry-run", action="store_true", help="show changes, don't write")
    ap.add_argument("--no-git", action="store_true",
                    help="use plain mv instead of `git mv`")
    args = ap.parse_args()

    old = validate_path(Path(args.old_path), "old_path")
    new = validate_path(Path(args.new_path), "new_path")
    if not old.exists():
        sys.exit(f"rewire: not found: {old}")
    if new.exists() and old != new:
        sys.exit(f"rewire: destination exists: {new}")
    if old == new:
        sys.exit("rewire: old and new paths are identical")

    old_stem, new_stem = old.stem, new.stem
    print(f"rewire: '{old_stem}' → '{new_stem}'")

    # 1) Plan link rewrites across all pages (including the moving file
    # and MOCs under wiki/_index/, so MOC links follow the rename).
    rewrites: list[tuple[Path, str, int]] = []
    for page in _pages_to_rewrite():
        text = page.read_text(encoding="utf-8")
        new_text, n = rewrite_wikilinks(text, old_stem, new_stem)
        if n:
            rewrites.append((page, new_text, n))

    total_links = sum(n for _, _, n in rewrites)
    print(f"rewire: {total_links} wikilink rewrite(s) across {len(rewrites)} page(s)")
    for page, _, n in rewrites:
        print(f"  - {page.relative_to(VAULT_ROOT)} ({n})")

    # 2) Plan moved-file edits: H1 + add old_stem as alias.
    old_text = old.read_text(encoding="utf-8")
    fm_m = FM_RX.match(old_text)
    if not fm_m:
        sys.exit(f"rewire: {old.relative_to(VAULT_ROOT)} has no frontmatter")
    fm_body = fm_m.group(1)
    after_fm = old_text[fm_m.end():]

    new_fm, alias_added = add_alias(fm_body, old_stem)
    # Apply wikilink rewrites to both frontmatter and body of the moving
    # file. Other pages already had their full text scanned above; this
    # one we rebuilt from parts, so re-run the rewrite on each part.
    new_fm, _ = rewrite_wikilinks(new_fm, old_stem, new_stem)
    after_fm_new, h1_changed = rewrite_h1(after_fm, new_stem)
    after_fm_new, _ = rewrite_wikilinks(after_fm_new, old_stem, new_stem)
    # Update the rewrites entry for `old` so the destination path receives
    # the rebuilt text (rather than the original text's link rewrites).
    for i, (page, _, n) in enumerate(rewrites):
        if page == old:
            rewrites[i] = (old, f"---\n{new_fm}\n---\n{after_fm_new}", n)
            break
    moved_text = f"---\n{new_fm}\n---\n{after_fm_new}"

    if args.dry_run:
        print("rewire: --dry-run, no changes written")
        if alias_added:
            print(f"  + alias on moved page: '{old_stem}'")
        if h1_changed:
            print(f"  + H1 rewrite: '# {new_stem}'")
        return 0

    # 3) Execute. Move first so any link-rewrite that touches the moving
    # file lands at the new path.
    new.parent.mkdir(parents=True, exist_ok=True)
    if args.no_git:
        shutil.move(str(old), str(new))
    else:
        subprocess.run(["git", "mv", str(old), str(new)],
                       cwd=VAULT_ROOT, check=True)
    new.write_text(moved_text, encoding="utf-8")

    # 4) Apply link rewrites to every other page.
    for page, new_text, _ in rewrites:
        if page == old:
            continue  # already written via moved_text
        page.write_text(new_text, encoding="utf-8")

    # 5) Refresh alias index.
    subprocess.run(
        [str(TOOLING_ROOT / "scripts" / "alias-index.py"), "build"],
        cwd=VAULT_ROOT, check=True,
    )

    print("rewire: done. Run scripts/lint.py to verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
