#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Promote an entity page into a topic page.

This is a thin wrapper over `scripts/rewire.py` that adds the
type-flip: `type: Entity` → `type: Topic` in frontmatter, and moves
the file from `wiki/entities/<X>.md` to `wiki/topics/<X>.md` (or to a
new name if requested).

When an entity has grown past readability (lint flags >400 lines),
promotion converts it into a synthesis page where multiple sub-entity
links live alongside its own claims. The old entity name stays as an
alias on the new topic, so existing `[[OldName]]` links continue to
resolve via the alias index.

Usage:
    scripts/promote-entity.py [--dry-run] <entity_path> [<new_topic_name>]

If `<new_topic_name>` is omitted, the file keeps the same stem under
`wiki/topics/`.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from _util import default_vault_root

TOOLING_ROOT = Path(__file__).resolve().parent.parent  # tooling repo (scripts/, schema.md)
VAULT_ROOT = default_vault_root(TOOLING_ROOT)

# Match `type: Entity` with optional trailing whitespace and an optional
# inline YAML comment (`# …`). Without this tolerance, frontmatter that's
# valid YAML — e.g. `type: Entity # canonical entity` — would be rejected
# as "non-standard" and block promotion.
TYPE_LINE_RX = re.compile(r"^type:\s*Entity\s*(?:#[^\n]*)?$", re.MULTILINE)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("entity_path", help="wiki/entities/<X>.md")
    ap.add_argument("new_name", nargs="?",
                    help="optional new stem under wiki/topics/ (default: same stem)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.entity_path).resolve()
    try:
        rel = src.relative_to(VAULT_ROOT)
    except ValueError:
        sys.exit(f"promote: {src} not in vault")
    if rel.parts[:2] != ("wiki", "entities") or src.suffix != ".md":
        sys.exit(f"promote: must be wiki/entities/*.md, got {rel}")
    if not src.exists():
        sys.exit(f"promote: not found: {src}")

    new_stem = args.new_name or src.stem
    dst = VAULT_ROOT / "wiki" / "topics" / f"{new_stem}.md"
    if dst.exists() and dst != src:
        sys.exit(f"promote: destination exists: {dst.relative_to(VAULT_ROOT)}")

    text = src.read_text(encoding="utf-8")
    if not TYPE_LINE_RX.search(text):
        sys.exit(f"promote: {rel} does not declare 'type: Entity' "
                 "(already a Topic, or non-standard frontmatter)")

    if args.dry_run:
        print(f"promote (dry-run): {rel} → {dst.relative_to(VAULT_ROOT)}")
        print("  + invokes rewire.py to move file and rewrite wikilinks")
        print("  + frontmatter: type: Entity → type: Topic (after rewire)")
        return subprocess.call(
            [str(TOOLING_ROOT / "scripts" / "rewire.py"),
             "--dry-run", str(src), str(dst)],
            cwd=VAULT_ROOT,
        )

    # Order matters: rewire first (move + rewire links + rebuild index).
    # If rewire fails, the original file is intact and no type flip has
    # happened — no rollback needed. Only flip type after rewire succeeds,
    # at which point the file lives at `dst` and we know exactly where to
    # write.
    rc = subprocess.call(
        [str(TOOLING_ROOT / "scripts" / "rewire.py"), str(src), str(dst)],
        cwd=VAULT_ROOT,
    )
    if rc != 0:
        sys.exit("promote: rewire failed; original page unchanged")

    moved_text = dst.read_text(encoding="utf-8")
    new_text = TYPE_LINE_RX.sub("type: Topic", moved_text, count=1)
    if new_text == moved_text:
        # Should never happen — we verified `type: Entity` above.
        sys.exit("promote: type line not found post-rewire (page may be in unusual state)")
    dst.write_text(new_text, encoding="utf-8")

    # rewire.py already rebuilt the alias index, but it captured the
    # pre-flip type. Rebuild again so the index reflects `type: Topic`.
    subprocess.run(
        [str(TOOLING_ROOT / "scripts" / "alias-index.py"), "build"],
        cwd=VAULT_ROOT, check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
