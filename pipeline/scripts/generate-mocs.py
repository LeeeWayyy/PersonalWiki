#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
"""
Generate Map-of-Content (MOC) pages under `wiki/_index/`.

One MOC per `tags:` value carried by content pages. Each MOC lists
member pages grouped by their Form tag. Re-runs are idempotent: only
write when the rendered bytes differ from on-disk content.

Run from vault root:
    scripts/generate-mocs.py            # write missing/changed MOCs
    scripts/generate-mocs.py --dry-run  # show what would change

Three input cases per tag:
  1. In taxonomy AND ≥1 carrier → write/refresh MOC.
  2. In taxonomy AND 0 carriers → rewrite existing MOC to Members(0).
  3. Not in taxonomy AND 0 carriers → emit orphan warning, no change.
  4. Not in taxonomy AND ≥1 carrier → impossible if lint ran first.
     Hard-fail.

The generator NEVER deletes MOC files. Orphans are warned-on.

MOC file structure (canonical; humans must NOT add custom frontmatter
fields — they'll be erased on next regeneration):
    ---
    type: index
    tag: <tag>
    member_count: <n>
    last_generated: <YYYY-MM-DD>
    member_hash: <16-hex>
    ---
    # <tag>

    <!-- human-zone -->
    _Optional human commentary; preserved verbatim across regenerations._
    <!-- /human-zone -->

    <!-- moc-zone -->
    ## Members (<n>)

    ### <form-tag-1>
    - [[stem|h1]]
    ...

    ### <form-tag-2>
    - [[stem|h1]]
    ...
    <!-- /moc-zone -->
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import NamedTuple

import derived_lib as dl
from _util import default_vault_root

TOOLING_ROOT = Path(__file__).resolve().parent.parent  # tooling repo (scripts/, schema.md)
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
WIKI_DIR = VAULT_ROOT / "wiki"
INDEX_DIR = WIKI_DIR / "_index"
TAXONOMY_PATH = WIKI_DIR / "_taxonomy.md"

H1_RX = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
TAXONOMY_BULLET_RX = re.compile(r"^- `([^`]+)`")


class Member(NamedTuple):
    page_id: str
    stem: str
    h1: str
    form_tag: str  # the page's single Form tag
    tags: list[str]  # full tag list


def parse_taxonomy() -> dict[str, set[str]]:
    """Parse `wiki/_taxonomy.md`. Mirrors lint.py's parser shape but
    returns minimal output: {section: set}. The full strictness checks
    (overlap, duplicates, malformed bullets) live in lint.py and have
    already passed by the time this generator runs (it's invoked AFTER
    the tag-gate)."""
    if not TAXONOMY_PATH.is_file():
        sys.exit(f"generate-mocs: taxonomy file missing: {TAXONOMY_PATH.relative_to(VAULT_ROOT)}")
    sections: dict[str, set[str]] = {"Domain": set(), "Form": set(), "Reserved": set()}
    current: str | None = None
    in_fence = False
    for line in TAXONOMY_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.startswith(">"):
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
            current = heading if heading in sections else None
            continue
        if current is None:
            continue
        m = TAXONOMY_BULLET_RX.match(line)
        if m:
            sections[current].add(m.group(1))
    return sections


def collect_members(form_tags: set[str]) -> dict[str, list[Member]]:
    """Walk content pages, return {tag: [members]}. Each member appears
    under every tag it carries. Members must have valid frontmatter
    including page_id, h1, and exactly 1 Form tag — lint.py would have
    caught missing pieces before this runs."""
    out: dict[str, list[Member]] = {}
    for sub in ("entities", "topics"):
        d = WIKI_DIR / sub
        if not d.is_dir():
            continue
        for path in sorted(d.rglob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            fm = dl.parse_frontmatter(text)
            if not fm:
                continue
            tags = fm.get("tags")
            if not isinstance(tags, list) or not tags:
                continue
            page_id = fm.get("page_id")
            if not isinstance(page_id, str):
                continue
            h1_m = H1_RX.search(text)
            h1 = h1_m.group(1).strip() if h1_m else path.stem
            page_form_tags = [t for t in tags if t in form_tags]
            if len(page_form_tags) != 1:
                # Should never reach here if lint --gate=tags ran first.
                continue
            form_tag = page_form_tags[0]
            member = Member(page_id, path.stem, h1, form_tag, list(tags))
            for t in tags:
                out.setdefault(t, []).append(member)
    return out


def existing_moc_tags() -> set[str]:
    """Return tags for which a MOC file already exists in `_index/`.
    Read each MOC's frontmatter `tag:` field — that's authoritative
    over the slug, since hand-renames could desync them."""
    if not INDEX_DIR.is_dir():
        return set()
    out: set[str] = set()
    for path in sorted(INDEX_DIR.rglob("*.md")):
        fm = dl.parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if not fm:
            continue
        tag = fm.get("tag")
        if isinstance(tag, str):
            out.add(tag)
    return out


def slug_for(tag: str) -> str:
    """tag → MOC filename stem. `/` → `__`. Tag regex forbids `_`,
    so the slug is collision-free."""
    return tag.replace("/", "__")


def compute_member_hash(members: list[Member]) -> str:
    """sha256 over (page_id, stem, h1, form_tag) for each member,
    sorted by page_id. Captures every render-affecting input."""
    payload = json.dumps(
        [(m.page_id, m.stem, m.h1, m.form_tag) for m in
         sorted(members, key=lambda m: m.page_id)],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def render_moc(
    tag: str,
    members: list[Member],
    member_hash: str,
    last_generated: str,
    existing_text: str | None,
) -> str:
    """Build the full canonical MOC text. `last_generated` is whatever
    the caller decides to stamp (see main()'s two-pass logic for the
    "preserve prior date when nothing changed" semantics)."""
    fm = (
        "---\n"
        "type: index\n"
        f"tag: {tag}\n"
        f"member_count: {len(members)}\n"
        f"last_generated: {last_generated}\n"
        f"member_hash: {member_hash}\n"
        "---\n\n"
    )
    h1 = f"# {tag}\n\n"
    human = dl.render_human_zone(existing_text, "moc-zone")
    moc_zone_lines = [
        "<!-- moc-zone -->",
        f"## Members ({len(members)})",
        "",
    ]
    if members:
        # Group by form_tag, deterministic order alphabetical by form.
        by_form: dict[str, list[Member]] = {}
        for m in members:
            by_form.setdefault(m.form_tag, []).append(m)
        for form in sorted(by_form):
            moc_zone_lines.append(f"### {form}")
            for m in sorted(by_form[form], key=lambda x: x.h1):
                moc_zone_lines.append(f"- [[{m.stem}|{m.h1}]]")
            moc_zone_lines.append("")
    moc_zone_lines.append("<!-- /moc-zone -->")
    moc_zone = "\n".join(moc_zone_lines) + "\n"
    return fm + h1 + human + "\n" + moc_zone


def write_moc(path: Path, content: str, dry_run: bool) -> bool:
    """Write content to path atomically (temp + rename). Skip if file
    already equals content. Returns True if a write happened.

    Caller is responsible for choosing the right `last_generated:`
    date in `content` (see render_moc(): preserves prior date on
    same-content re-runs to keep this byte comparison stable across
    day boundaries; bumps to today on real changes)."""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--dry-run", action="store_true", help="show changes, don't write")
    args = ap.parse_args()

    if not WIKI_DIR.is_dir():
        print("generate-mocs: run from vault root", file=sys.stderr)
        return 2

    taxonomy = parse_taxonomy()
    domain_tags = taxonomy["Domain"]
    form_tags = taxonomy["Form"]
    reserved_tags = taxonomy["Reserved"]
    valid_tags = domain_tags | form_tags | reserved_tags

    members_by_tag = collect_members(form_tags)
    page_carried_tags = set(members_by_tag.keys())
    moc_existing_tags = existing_moc_tags()

    # Tags to consider: union of page-carried + existing MOCs.
    union = page_carried_tags | moc_existing_tags

    today = date.today().isoformat()
    written = 0
    skipped = 0
    orphans: list[str] = []
    invalid: list[str] = []

    for tag in sorted(union):
        in_taxonomy = tag in valid_tags
        carriers = members_by_tag.get(tag, [])

        if not in_taxonomy and not carriers:
            # Orphan: tag dropped from taxonomy AND no page carries it.
            # Warn, leave file alone.
            slug = slug_for(tag)
            orphans.append(f"wiki/_index/{slug}.md (tag={tag!r})")
            continue

        if not in_taxonomy and carriers:
            # Should not happen if lint --gate=tags ran first.
            invalid.append(f"tag {tag!r} carried by {len(carriers)} page(s) but not in taxonomy")
            continue

        # in_taxonomy: write/refresh (carriers may be 0 → Members(0)).
        slug = slug_for(tag)
        path = INDEX_DIR / f"{slug}.md"
        existing_text = path.read_text(encoding="utf-8") if path.exists() else None
        member_hash = compute_member_hash(carriers)
        # Two-pass to keep last_generated stable across day boundaries:
        # 1. Render preserving the existing file's last_generated. If
        #    that matches the existing bytes, nothing meaningful
        #    changed → skip.
        # 2. Otherwise re-render with today's date — a real content
        #    change deserves a fresh stamp. Both passes share the
        #    same existing_text so human-zone is preserved.
        prior_date = dl.existing_last_generated(existing_text)
        date_for_compare = prior_date if prior_date else today
        content_preserved = render_moc(
            tag, carriers, member_hash, date_for_compare, existing_text
        )
        if existing_text is not None and existing_text == content_preserved:
            skipped += 1
            continue
        content = render_moc(tag, carriers, member_hash, today, existing_text)
        if write_moc(path, content, args.dry_run):
            written += 1
            verb = "would write" if args.dry_run else "wrote"
            print(f"  {verb} {path.relative_to(VAULT_ROOT)} ({len(carriers)} member(s))")
        else:
            skipped += 1

    if orphans:
        print()
        print(f"  ⚠ {len(orphans)} orphaned MOC(s) — tag no longer in taxonomy and no page carries it:")
        for o in orphans:
            print(f"    {o}")
        print("  Delete by hand if desired; generator will not auto-delete.")

    if invalid:
        print()
        print("  ✗ FAIL: tag(s) carried by pages but not in taxonomy (lint should have caught this):")
        for x in invalid:
            print(f"    {x}")
        return 1

    print()
    if args.dry_run:
        print(f"generate-mocs: dry-run — {written} MOC(s) would change, {skipped} unchanged.")
    else:
        print(f"generate-mocs: {written} MOC(s) written/updated, {skipped} unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
