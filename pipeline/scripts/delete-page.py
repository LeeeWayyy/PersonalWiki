#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
"""Delete a wiki page without leaving orphan wikilinks.

True delete de-links every alias of the page to its visible plain text.
`--merge-into TARGET` instead redirects `[[old stem]]` to TARGET and moves
the doomed page's aliases there. Merge evidence and `sources:` into TARGET
first; this command refuses a merge that would knowingly drop either source
metadata or human notes.

Pages must be Git-tracked unless `--no-git` is used. Alias ambiguity is left
to `scripts/lint.py`; run it after every deletion.

Usage:
    scripts/delete-page.py [--dry-run] [--no-git] PAGE
    scripts/delete-page.py [--dry-run] [--no-git] --merge-into TARGET PAGE
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

from rewire import (
    FM_RX,
    TOOLING_ROOT,
    VAULT_ROOT,
    _pages_to_rewrite,
    add_alias,
    normalize,
    rewrite_wikilinks,
    validate_path,
)

WIKILINK_PARTS_RX = re.compile(
    r"!?\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|([^\]]*))?\]\]"
)
HUMAN_ZONE_RX = re.compile(
    r"<!-- human-zone -->(.*?)<!-- /human-zone -->", re.DOTALL
)


def frontmatter(text: str, path: Path) -> tuple[re.Match[str], dict]:
    match = FM_RX.match(text)
    if not match:
        sys.exit(f"delete-page: {path.relative_to(VAULT_ROOT)} has no frontmatter")
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        sys.exit(f"delete-page: invalid YAML: {path.relative_to(VAULT_ROOT)}")
    if not isinstance(data, dict):
        sys.exit(f"delete-page: invalid frontmatter: {path.relative_to(VAULT_ROOT)}")
    return match, data


def string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def page_aliases(path: Path, data: dict) -> list[str]:
    aliases = [path.stem, *string_list(data.get("aliases"))]
    return list(dict.fromkeys(normalize(alias) for alias in aliases if normalize(alias)))


def unlink_wikilinks(text: str, aliases: list[str]) -> tuple[str, int]:
    """Replace links to any doomed alias with their visible plain text."""
    doomed = set(aliases)
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        ref = match.group(1).strip()
        if normalize(ref) not in doomed:
            return match.group(0)
        count += 1
        return match.group(2) if match.group(2) is not None else ref

    return WIKILINK_PARTS_RX.sub(replace, text), count


def add_aliases(text: str, path: Path, aliases: list[str]) -> tuple[str, list[str]]:
    match, _ = frontmatter(text, path)
    fm = match.group(1)
    added: list[str] = []
    for alias in aliases:
        fm, changed = add_alias(fm, alias)
        if changed:
            added.append(alias)
    return f"---\n{fm}\n---\n{text[match.end():]}", added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("page")
    parser.add_argument("--merge-into", metavar="TARGET")
    parser.add_argument("--dry-run", action="store_true", help="show changes, don't write")
    parser.add_argument("--no-git", action="store_true", help="use unlink instead of `git rm`")
    args = parser.parse_args()

    doomed = validate_path(Path(args.page), "page")
    if not doomed.is_file():
        sys.exit(f"delete-page: not found: {doomed}")
    target = validate_path(Path(args.merge_into), "merge target") if args.merge_into else None
    if target and (not target.is_file() or target == doomed):
        sys.exit("delete-page: merge target must be a different existing page")

    doomed_text = doomed.read_text(encoding="utf-8")
    _, doomed_fm = frontmatter(doomed_text, doomed)
    aliases = [doomed.stem, *string_list(doomed_fm.get("aliases"))]
    alias_keys = page_aliases(doomed, doomed_fm)

    planned: dict[Path, tuple[str, int]] = {}
    for page in _pages_to_rewrite():
        if page == doomed:
            continue
        text = page.read_text(encoding="utf-8")
        changed, count = (
            rewrite_wikilinks(text, doomed.stem, target.stem)
            if target else unlink_wikilinks(text, alias_keys)
        )
        if count:
            planned[page] = (changed, count)

    added_aliases: list[str] = []
    if target:
        target_text = planned.get(target, (target.read_text(encoding="utf-8"), 0))[0]
        _, target_fm = frontmatter(target_text, target)
        missing_sources = sorted(
            set(string_list(doomed_fm.get("sources")))
            - set(string_list(target_fm.get("sources")))
        )
        if missing_sources:
            sys.exit(
                "delete-page: merge evidence and sources into the target first; "
                f"missing source(s): {', '.join(missing_sources)}"
            )
        human = HUMAN_ZONE_RX.search(doomed_text)
        if human and human.group(1).strip() and human.group(1).strip() not in target_text:
            sys.exit("delete-page: move the doomed page's human-zone notes into the target first")
        target_text, added_aliases = add_aliases(target_text, target, aliases)
        planned[target] = (target_text, planned.get(target, ("", 0))[1])

    total = sum(count for _, count in planned.values())
    action = f"merge into {target.relative_to(VAULT_ROOT)}" if target else "true delete"
    print(f"delete-page: {doomed.relative_to(VAULT_ROOT)} ({action})")
    print(f"delete-page: {total} wikilink(s) across {sum(count > 0 for _, count in planned.values())} file(s)")
    for page, (_, count) in planned.items():
        if count:
            print(f"  - {page.relative_to(VAULT_ROOT)} ({count})")
    for alias in added_aliases:
        print(f"  + target alias: {alias}")

    if args.dry_run:
        print("delete-page: --dry-run, no changes written")
        return 0

    if args.no_git:
        doomed.unlink()
    else:
        subprocess.run(["git", "rm", str(doomed)], cwd=VAULT_ROOT, check=True)
    for page, (text, _) in planned.items():
        page.write_text(text, encoding="utf-8")

    subprocess.run(
        [str(TOOLING_ROOT / "scripts" / "alias-index.py"), "build"],
        cwd=VAULT_ROOT,
        check=True,
    )
    subprocess.run(
        [str(TOOLING_ROOT / "scripts" / "generate-mocs.py")],
        cwd=VAULT_ROOT,
        check=True,
    )
    print("delete-page: done. Run scripts/lint.py to verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
