#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
"""
Maintain `wiki/.alias-index.json`: a multimap from normalized aliases
to the page(s) that declare them.

The vault's keyword pre-pass uses this to route LLM-extracted keywords
directly to canonical pages even when the alias appears only in
frontmatter and not in body text. Lint and rewire use it for graph
integrity.

Subcommands:
    build               Scan all wiki pages, write the index. Idempotent.
    lookup              Read keywords from stdin (one per line), emit
                        TSV `<keyword>\\t<path>` for each match. Unknown
                        keywords produce no output. Ambiguous keywords
                        (>1 matching page) emit one line per matching
                        path — useful as candidate-finding for ingest's
                        ranking pre-pass.
    check               Exit 1 if any normalized alias maps to >1 page,
                        or any page is missing aliases entirely.

Normalization: NFKC + casefold + collapse internal whitespace + strip.
Identity for CJK; collapses width/case variants for ASCII.

Index format (JSON):

    {
      "version": 2,
      "generated_at": "<ISO8601>",
      "aliases": { "<normalized>": ["<page_id>", ...] },
      "pages":   {
        "<page_id>": {
          "path": "...",
          "aliases": [...],
          "type": "...",
          "domains": ["biology/cell", ...]   // intersection of `tags:` with
                                             // wiki/_taxonomy.md ## Domain
        }
      }
    }

Version history:
- 2: added per-page `domains` (taxonomy-Domain-intersected `tags:`).
     scripts/autolink.py uses this for cross-domain bare-token
     suppression. `autolink.py` requires version >= 2 and refuses to
     load v1 indices (which have no `domains` and would cause every
     page to look "no Domain", reverting autolink to its old
     false-positive behavior).
- 1: initial schema (aliases + pages with path/aliases/type).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from _util import default_vault_root
from media_resolver import parse_frontmatter

TOOLING_ROOT = Path(__file__).resolve().parent.parent  # tooling repo (scripts/, schema.md)
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
WIKI_DIR = VAULT_ROOT / "wiki"
INDEX_PATH = WIKI_DIR / ".alias-index.json"
TAXONOMY_PATH = WIKI_DIR / "_taxonomy.md"

TAXONOMY_BULLET_RX = re.compile(r"^- `([^`]+)`")


def domain_tags_from_taxonomy() -> set[str]:
    """Parse `wiki/_taxonomy.md` and return the set of bullets under
    the `## Domain` H2. Used to compute each page's `domains:` (the
    intersection of its `tags:` with the Domain set).

    Mirrors lint.py's stricter taxonomy parser without all the
    cross-section / duplicate guards — those are lint's job. If the
    taxonomy file is missing, returns an empty set; alias-index then
    stores `domains: []` for every page and autolink falls back to
    its v1 behavior. lint.py's `check_tags()` is what enforces
    taxonomy presence and well-formedness."""
    if not TAXONOMY_PATH.is_file():
        return set()
    out: set[str] = set()
    in_domain = False
    in_fence = False
    for line in TAXONOMY_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.startswith(">"):
            continue
        if line.startswith("## "):
            in_domain = line[3:].strip() == "Domain"
            continue
        if not in_domain:
            continue
        m = TAXONOMY_BULLET_RX.match(line)
        if m:
            out.add(m.group(1))
    return out


def normalize(s: str) -> str:
    """NFKC + casefold + whitespace collapse. Identity for CJK characters."""
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def all_wiki_pages() -> list[Path]:
    """Inclusion list — entities/ + topics/ only.

    `wiki/_index/` (MOC files) is intentionally excluded: MOCs are
    tool-owned, lack `page_id`, and their H1 is a tag slug like
    `# biology/cell` which would pollute the alias multimap. Do NOT
    add `_index/` here. If a future caller needs to walk MOCs, use a
    separate helper local to that script."""
    pages: list[Path] = []
    for sub in ("entities", "topics"):
        d = WIKI_DIR / sub
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))
    return pages


def build_index() -> dict:
    aliases: dict[str, list[str]] = {}
    pages: dict[str, dict] = {}
    domain_tags = domain_tags_from_taxonomy()
    for path in all_wiki_pages():
        fm = parse_frontmatter(path)
        pid = fm.get("page_id")
        if not pid:
            # Not fatal here — `lint.py` flags it. Skip in the index so
            # lookups remain deterministic.
            continue
        raw_aliases = fm.get("aliases") or []
        # `aliases: Nick Lane` (scalar) parses as a string — wrapping with
        # `list(...)` would split it per character. Coerce scalars to a
        # one-element list so a degenerate frontmatter doesn't pollute the
        # alias index with single letters.
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        elif not isinstance(raw_aliases, list):
            raw_aliases = []
        page_aliases = list(raw_aliases)
        # Always include filename stem as an implicit alias so
        # `[[Title]]` resolves even if the page omits its own title.
        stem = path.stem
        if stem not in page_aliases:
            page_aliases.append(stem)

        # Per-page `domains`: intersection of `tags:` with the
        # taxonomy's Domain section. autolink.py uses this to suppress
        # cross-domain bare-token false positives (e.g., the economic
        # entity `分工` vs. the biological-prose use of the same word).
        # Computed via EXPLICIT taxonomy intersection (not "tags minus
        # Form") so Reserved tags like `taxonomy-gap` and any future
        # custom tags don't leak through as pseudo-domains.
        raw_tags = fm.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags]
        elif not isinstance(raw_tags, list):
            raw_tags = []
        page_domains = sorted(t for t in raw_tags if isinstance(t, str) and t in domain_tags)

        rel = str(path.relative_to(VAULT_ROOT))
        pages[pid] = {
            "path": rel,
            "aliases": page_aliases,
            "type": fm.get("type"),
            "domains": page_domains,
        }

        for a in page_aliases:
            if not isinstance(a, str) or not a.strip():
                continue
            key = normalize(a)
            if not key:
                continue
            bucket = aliases.setdefault(key, [])
            if pid not in bucket:
                bucket.append(pid)

    return {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "aliases": aliases,
        "pages": pages,
    }


def write_atomic(path: Path, data: dict) -> None:
    """Write JSON atomically: temp file in same dir, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def cmd_build(args: argparse.Namespace) -> int:
    idx = build_index()
    write_atomic(INDEX_PATH, idx)
    n_pages = len(idx["pages"])
    n_aliases = len(idx["aliases"])
    n_ambiguous = sum(1 for v in idx["aliases"].values() if len(v) > 1)
    print(
        f"alias-index: {n_pages} page(s), {n_aliases} alias(es)"
        + (f", {n_ambiguous} ambiguous" if n_ambiguous else "")
    )
    return 0


def load_index() -> dict:
    if not INDEX_PATH.exists():
        return build_index()
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return build_index()


def resolve(idx: dict, term: str) -> tuple[list[str], bool]:
    """Return (page_paths, ambiguous). Empty list = unknown."""
    key = normalize(term)
    pids = idx["aliases"].get(key, [])
    paths = [idx["pages"][p]["path"] for p in pids if p in idx["pages"]]
    return paths, len(pids) > 1


def cmd_lookup(args: argparse.Namespace) -> int:
    idx = load_index()
    for line in sys.stdin:
        term = line.strip()
        if not term:
            continue
        # Strip list markers (- or *) and numbered-list prefixes (`1. `).
        # Crucially: only strip leading digits when followed by a dot, so
        # entity names like "2024 Election" stay intact.
        term = re.sub(r"^[\s\-*]+", "", term)
        term = re.sub(r"^\d+\.\s+", "", term)
        term = term.strip()
        if not term:
            continue
        paths, _ambiguous = resolve(idx, term)
        for p in paths:
            print(f"{term}\t{p}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    idx = build_index()
    bad = {k: v for k, v in idx["aliases"].items() if len(v) > 1}
    no_alias_pages = [info["path"] for info in idx["pages"].values()
                      if not info["aliases"]]
    if not bad and not no_alias_pages:
        print(f"  ✓ {len(idx['pages'])} page(s); all aliases unique")
        return 0
    if bad:
        print(f"  ✗ {len(bad)} ambiguous alias(es):")
        for key, pids in sorted(bad.items()):
            paths = [idx["pages"][p]["path"] for p in pids]
            print(f"    '{key}' → {paths}")
    if no_alias_pages:
        print(f"  ✗ {len(no_alias_pages)} page(s) with no aliases:")
        for p in no_alias_pages:
            print(f"    {p}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build", help="rebuild wiki/.alias-index.json")

    sub.add_parser("lookup", help="resolve stdin terms to paths")

    sub.add_parser("check", help="exit 1 if any alias is ambiguous")

    args = ap.parse_args()
    return {
        "build": cmd_build,
        "lookup": cmd_lookup,
        "check": cmd_check,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
