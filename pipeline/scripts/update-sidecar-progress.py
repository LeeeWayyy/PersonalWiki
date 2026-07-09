#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
"""
Refresh the per-source ingest progress checklist in a sidecar.

Reads the asset's section list (via scripts/extract.py --list-sections) and
.wiki/log.md, then rewrites the sidecar body with a "Progress" section where
each section is a `- [ ]` / `- [x]` item depending on whether a matching
`<source_id>#<label>` line exists in the log.

The frontmatter is preserved verbatim. Body content below the frontmatter is
fully regenerated — the sidecar is auto-managed; do not hand-edit it.

Usage:
    scripts/update-sidecar-progress.py <sidecar.md>
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

from _util import chapter_order_from_lines, default_vault_root, split_frontmatter

TOOLING_ROOT = Path(__file__).resolve().parent.parent  # tooling repo (scripts/, schema.md)
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
LOG_PATH = VAULT_ROOT / ".wiki" / "log.md"
EXTRACT = Path(__file__).resolve().parent / "extract.py"


def list_sections(asset: Path) -> list[str]:
    res = subprocess.run(
        [str(EXTRACT), str(asset), "--list-sections"],
        capture_output=True, text=True, check=True,
    )
    titles = [line.rstrip() for line in res.stdout.splitlines() if line.strip()]
    # Drop fallback titles that came from href (extract.py uses the href when
    # no <h1>/<h2>/<title> tag is found in the spine item). These look like
    # paths, e.g. "xhtml/chapter1.xhtml" — useless for progress tracking.
    return [t for t in titles if not re.match(r"^[A-Za-z0-9._/-]+\.x?html?$", t)]


def completed_labels(source_id: str) -> set[str]:
    """Read .wiki/log.md and return labels that have been ingested for
    `source_id`. The log line format (written by ingest.py) is:
        <iso>  <source_id>[#<label>]  pages: <files…>
    The label may contain spaces and text such as `pages:`."""
    if not LOG_PATH.exists():
        return set()
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return set(chapter_order_from_lines(lines, source_id))


def is_done(title: str, labels: set[str]) -> bool:
    """A label matches a section title when:
    - the title equals the label exactly, OR
    - the title starts with the label and the next character is non-
      alphanumeric (whitespace, punctuation, EOL).
    The non-alphanumeric tail check is what prevents `Chapter 1` from
    spuriously matching `Chapter 10`. CJK characters are not alphanumeric
    in Python's `str.isalnum()`-by-codepoint sense for this purpose, but
    they're not digits either, so labels like `第一章` correctly match
    `第一章 有希望的怪物：…` and do not match `第十章 …` (different chars
    altogether)."""
    for lbl in labels:
        if title == lbl:
            return True
        if title.startswith(lbl):
            tail = title[len(lbl):]
            if not tail or not (tail[0].isalnum() and tail[0].isascii()):
                return True
    return False


def render_body(title: str, sections: list[str], labels: set[str]) -> str:
    lines = [
        f"# {title}",
        "",
        "Auto-generated sidecar. Do not hand-edit.",
        "",
        "## Progress",
        "",
    ]
    if not sections:
        lines.append("_No sections detected._")
        lines.append("")
    else:
        for s in sections:
            mark = "x" if is_done(s, labels) else " "
            lines.append(f"- [{mark}] {s}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: update-sidecar-progress.py <sidecar.md>", file=sys.stderr)
        return 2

    sidecar = Path(sys.argv[1]).resolve()
    if not sidecar.is_file():
        print(f"not a file: {sidecar}", file=sys.stderr)
        return 2

    asset = sidecar.with_suffix("")  # "<name>.epub.md" → "<name>.epub"
    if not asset.exists():
        print(f"asset not found alongside sidecar: {asset}", file=sys.stderr)
        return 2

    text = sidecar.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    if not split:
        if text.startswith("---\n"):
            sys.exit("sidecar frontmatter not terminated")
        sys.exit("sidecar has no frontmatter")
    _, fm_text, _ = split
    fm = yaml.safe_load(fm_text) or {}
    source_id = fm.get("source_id")
    title = fm.get("title") or sidecar.stem
    if not source_id:
        print(f"no source_id in {sidecar}", file=sys.stderr)
        return 2

    sections = list_sections(asset)
    labels = completed_labels(source_id)
    body = render_body(title, sections, labels)

    sidecar.write_text(f"---\n{fm_text}\n---\n\n{body}", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
