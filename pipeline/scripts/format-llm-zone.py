#!/usr/bin/env python3
"""
Normalize wiki page llm-zones into the required Obsidian AI callout shape.

The ingest LLM occasionally emits valid content under `<!-- llm-zone -->` but
forgets the callout blockquote wrapper. This script is intentionally
mechanical: it does not rewrite prose or synthesis structure, it only ensures
the zone body is one `> [!AI] LLM Synthesis` callout with every content line
quoted.

Usage:
  scripts/format-llm-zone.py <page>...
  scripts/format-llm-zone.py --check <page>...
  scripts/format-llm-zone.py --all
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _util import default_vault_root  # noqa: E402
from ingest_quality import (  # noqa: E402
    PageInput,
    expected_citation,
    has_exact_citation,
    modified_paragraphs,
    parse_page,
)


TOOLING_ROOT = Path(__file__).resolve().parent.parent
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
LLM_OPEN = "<!-- llm-zone -->"
LLM_CLOSE = "<!-- /llm-zone -->"
ZONE_RX = re.compile(
    re.escape(LLM_OPEN) + r"(.*?)" + re.escape(LLM_CLOSE),
    re.DOTALL,
)
AI_CALLOUT_RX = re.compile(r"^\s*>\s*\[!AI\]\s+LLM Synthesis\s*$")
FROM_SRC_HEADING_RX = re.compile(r"^\s*###\s+From\s+src:[A-Z0-9]{26}\b")


def _all_content_lines_quoted(lines: list[str]) -> bool:
    content = [ln for ln in lines if ln.strip()]
    return bool(content) and bool(AI_CALLOUT_RX.match(content[0])) and all(
        ln.startswith(">") for ln in content
    )


def _unquote_one_callout_level(line: str) -> str:
    if line == ">":
        return ""
    if line.startswith("> "):
        return line[2:]
    if line.startswith(">"):
        return line[1:].lstrip()
    return line


def normalize_zone_body(body: str) -> tuple[str, bool]:
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    unquoted = [_unquote_one_callout_level(ln).rstrip() for ln in lines]
    content = _strip_source_metadata_headings(unquoted)

    if _all_content_lines_quoted(lines) and content == unquoted:
        normalized = "\n" + "\n".join(lines) + "\n"
        return normalized, normalized != body

    while content and not content[0].strip():
        content.pop(0)
    if content and re.match(r"^\s*\[!AI\]\s+LLM Synthesis\s*$", content[0]):
        content.pop(0)
    while content and not content[0].strip():
        content.pop(0)
    while content and not content[-1].strip():
        content.pop()

    out = ["> [!AI] LLM Synthesis"]
    if content:
        out.append(">")
        for line in content:
            out.append(">" if not line.strip() else f"> {line}")
    normalized = "\n" + "\n".join(out) + "\n"
    return normalized, normalized != body


def _strip_source_metadata_headings(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if FROM_SRC_HEADING_RX.match(line):
            if out and out[-1] == "":
                out.pop()
            continue
        out.append(line)
    return out


def normalize_text(text: str) -> tuple[str, bool]:
    changed = False

    def repl(match: re.Match[str]) -> str:
        nonlocal changed
        body, did_change = normalize_zone_body(match.group(1))
        changed = changed or did_change
        return LLM_OPEN + body + LLM_CLOSE

    return ZONE_RX.sub(repl, text), changed


def add_current_citations(
    text: str,
    baseline_text: str | None,
    path: str,
    source_id: str,
    section_label: str,
) -> tuple[str, bool]:
    """Append current provenance to changed callout paragraphs that omitted it."""
    current = parse_page(PageInput(path=path, text=text))
    baseline = (
        parse_page(PageInput(path=path, text=baseline_text)).paragraphs
        if baseline_text is not None
        else ()
    )
    changed = modified_paragraphs(current.paragraphs, baseline)
    missing = [
        paragraph for paragraph in changed
        if not has_exact_citation(paragraph.text, source_id, section_label)
    ]
    if not missing:
        return text, False
    lines = text.splitlines()
    citation = f" [{expected_citation(source_id, section_label)}]"
    for paragraph in missing:
        lines[paragraph.end_line - 1] = lines[paragraph.end_line - 1].rstrip() + citation
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), True


def _baseline_text(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "show", f"HEAD:{path.as_posix()}"],
        text=True,
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def _page_paths(args: argparse.Namespace) -> list[Path]:
    if args.all:
        roots = [VAULT_ROOT / "wiki" / "entities", VAULT_ROOT / "wiki" / "topics"]
        return sorted(p for root in roots if root.is_dir() for p in root.rglob("*.md"))
    return [Path(p) for p in args.pages]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="format all wiki entity/topic pages")
    ap.add_argument("--check", action="store_true", help="report pages that would change")
    ap.add_argument("--source-id", default="", help="append this run's missing citations")
    ap.add_argument("--section-label", default="")
    ap.add_argument("pages", nargs="*")
    args = ap.parse_args()
    if not args.all and not args.pages:
        ap.error("pass page paths or --all")

    changed: list[Path] = []
    for path in _page_paths(args):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        new_text, did_change = normalize_text(text)
        if args.source_id:
            new_text, cited = add_current_citations(
                new_text,
                _baseline_text(path),
                path.as_posix(),
                args.source_id,
                args.section_label,
            )
            did_change = did_change or cited
        if not did_change:
            continue
        changed.append(path)
        if not args.check:
            path.write_text(new_text, encoding="utf-8")

    if args.check:
        for path in changed:
            print(path)
        return 1 if changed else 0
    for path in changed:
        print(f"formatted {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
