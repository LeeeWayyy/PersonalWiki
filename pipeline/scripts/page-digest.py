#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Emit a compact digest of a wiki page for inclusion in LLM prompts.

The full body of a candidate page is often expensive context for the
LLM ingest call. This produces a much smaller representation that
preserves the high-signal parts (frontmatter, H1, headings, opening
prose lines) and truncates the rest.

If the LLM decides it actually needs the full content of a page to
make accurate edits, it emits a JSON expand action and the harness
re-runs with full content for those pages only (see ingest.py).

Usage:
    scripts/page-digest.py <path> [--body-lines N]

Output to stdout (no trailing newline beyond final \\n).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FM_RX = re.compile(r"^---\n(.*?)\n---(?:\n(.*))?$", re.DOTALL)
# Headings can be bare (`### Foo`) or wrapped in an Obsidian callout
# prefix (`> ### Synthesis`, `> > ### Nested`). The vault's two-tier
# llm-zone format puts `### Synthesis` / `### From src:<id>#<label>`
# inside a `> [!AI]` callout — so without the optional `>` prefix
# match, those structural headings would get elided as ordinary body
# lines, weakening the "digest shows headings" contract that the LLM
# relies on to decide whether to expand.
HEADING_RX = re.compile(r"^(?:>\s*)*#{1,6}\s+\S")
ZONE_RX = re.compile(r"^<!--\s*/?(?:human|llm)-zone\s*-->\s*$")


def digest(text: str, body_lines: int = 6) -> str:
    """Return compact digest preserving frontmatter + H1 + headings + first
    `body_lines` non-empty body lines after the H1.

    Pages without frontmatter return unchanged but capped at body_lines+5.
    """
    m = FM_RX.match(text)
    if not m:
        capped = text.splitlines()[: body_lines + 5]
        return "\n".join(capped) + "\n"

    fm = m.group(1)
    body = m.group(2) or ""  # frontmatter may end at EOF with no body
    out: list[str] = ["---", fm, "---", ""]

    kept_body = 0
    saw_h1 = False
    elided_count = 0

    for line in body.splitlines():
        # Existing kept/skipped decision. `emitted` tracks whether
        # we appended the line to `out`. After the decision tree,
        # `elided_count` is incremented ONLY when emitted is False
        # AND the line is real prose (not blank, YAML delimiter, or
        # HTML comment). This makes the marker count exactly inverse
        # to emitted real prose, so the marker only appears when
        # there is genuinely-elided content the LLM hasn't seen —
        # important for the prompt rule "MUST expand-on-modify if
        # the digest carries the elision marker."
        emitted = False
        if HEADING_RX.match(line) or ZONE_RX.match(line):
            # Always preserve structural anchors — they're cheap and
            # tell the LLM what's already organized on the page.
            out.append(line)
            emitted = True
            if line.startswith("# "):
                saw_h1 = True
        elif not line.strip():
            # Preserve blank lines only after we've started keeping content.
            if out and out[-1] != "" and saw_h1:
                out.append("")
                emitted = True
        elif saw_h1 and kept_body < body_lines:
            out.append(line)
            emitted = True
            kept_body += 1

        if emitted:
            continue
        # Skipped — does this line count toward elision?
        stripped = line.strip()
        if not stripped:
            continue                       # blank — never count
        if stripped == "---":
            continue                       # YAML delimiter
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue                       # zone marker / HTML comment
        elided_count += 1

    while out and out[-1] == "":
        out.pop()

    if elided_count > 0:
        out.append("")
        out.append(f"<!-- digest: {elided_count} body line(s) elided. "
                   f"Request full content via expand action if needed. -->")

    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Compact digest of a wiki page.")
    ap.add_argument("path")
    ap.add_argument("--body-lines", type=int, default=6,
                    help="Max non-empty body lines to keep (default: 6)")
    args = ap.parse_args()

    p = Path(args.path)
    if not p.is_file():
        print(f"page-digest: not a file: {p}", file=sys.stderr)
        return 2

    sys.stdout.write(digest(p.read_text(encoding="utf-8"), body_lines=args.body_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
