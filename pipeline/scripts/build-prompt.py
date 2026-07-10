#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""
Assemble the main ingest prompt. This keeps the deterministic
`build_prompt` + `build_candidate_blob` contract in one place, with
byte-level coverage from scripts/tests/test_build_prompt.sh.

Writes the full prompt to stdout. ingest.py redirects it to a file.

Inputs (all from ingest.py variables / files):
    --source-id --sha256 --added --origin-type --origin-ref --basename
    --section-label   (may be empty → a <none …> default that embeds the id)
    --all-source-ids  (newline-separated, may be empty)
    --source-terms-file salient source terms, one per line (may be empty)
    --text-file       extracted SOURCE_TEXT
    --candidates-file one candidate path per line (may be empty/missing)
    --expand-file     paths to inline FULL; others are digests (may be empty)
    --dest            the source asset path (for <dest>.assets/_manifest.md)
    --operation       digest | expand | retry

Reads from the content repo ($VAULT_CONTENT_DIR): wiki/_taxonomy.md. Reads from
the tooling repo (TOOLING_ROOT): prompts/ingest.md and
prompts/schema-ingest.md. Selects schema rule blocks for the current operation.
Shells out to scripts/page-digest.py and scripts/render-images-block.py (also
tooling).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from _util import default_vault_root

TOOLING_ROOT = Path(__file__).resolve().parent.parent  # tooling repo (scripts/, prompts/)
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
SCRIPTS = TOOLING_ROOT / "scripts"

CORE_SCHEMA_SECTIONS = [
    "Page Selection And Coverage",
    "Page Types",
    "Frontmatter",
    "Tags",
    "Zones",
    "Citations",
    "Voice And Attribution",
    "Language And Naming",
    "Prose Shape",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _nonempty_lines(path: Path) -> list[str]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _split_schema_blocks(text: str) -> tuple[str, dict[str, str]]:
    preamble: list[str] = []
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            blocks[current] = [line]
            continue
        if current is None:
            preamble.append(line)
        else:
            blocks[current].append(line)
    return "\n".join(preamble).rstrip() + "\n", {
        key: "\n".join(lines).rstrip() + "\n" for key, lines in blocks.items()
    }


def _render_images_block(dest: str) -> str:
    manifest = Path(f"{dest}.assets") / "_manifest.md"
    if not manifest.is_file():
        return "(no images extracted from this source)\n"
    res = subprocess.run(
        [str(SCRIPTS / "render-images-block.py"), str(manifest), dest],
        capture_output=True, text=True,
    )
    if res.returncode == 0:
        return res.stdout
    return "(images-block render failed; LLM proceeds without image table)\n"


def _image_block_has_rows(block: str) -> bool:
    text = block.strip()
    return bool(text) and not text.startswith("(")


def _selected_schema(operation: str, candidates_file: Path, expand_file: Path,
                     image_block: str) -> str:
    preamble, blocks = _split_schema_blocks(_read(TOOLING_ROOT / "prompts" / "schema-ingest.md"))
    candidates = _nonempty_lines(candidates_file)
    expanded = _nonempty_lines(expand_file)
    sections = list(CORE_SCHEMA_SECTIONS)

    if candidates:
        sections.append("Candidate Pages")
        if expanded:
            sections.append("Expanded Candidate Editing")
        else:
            sections.append("Candidate Digests And Expansion")
        sections.extend(["Multi-Source Synthesis", "Candidate Updates And Conflicts"])

    if _image_block_has_rows(image_block):
        sections.append("Images")

    if operation == "retry":
        sections.append("Patch Retry")

    missing = [name for name in sections if name not in blocks]
    if missing:
        raise SystemExit(f"schema-ingest.md missing section(s): {', '.join(missing)}")
    return preamble + "\n" + "\n".join(blocks[name] for name in sections)


def build_candidate_blob(candidates_file: Path, expand_file: Path, out) -> None:
    """Mirror of bash build_candidate_blob: for each candidate path, emit a
    fenced markdown block — full content if the path is listed in the
    expand file, else its page-digest (falling back to full content if the
    digest script fails). Empty/absent candidates file → nothing."""
    if not candidates_file.is_file() or candidates_file.stat().st_size == 0:
        return
    expand_paths: set[str] = set()
    if expand_file.is_file() and expand_file.stat().st_size > 0:
        expand_paths = {
            ln for ln in expand_file.read_text(encoding="utf-8").splitlines() if ln
        }
    for p in candidates_file.read_text(encoding="utf-8").splitlines():
        if not p:
            continue
        out.write(f"\n### {p}\n```markdown\n")
        if p in expand_paths:
            out.write(_read(Path(p)))
        else:
            res = subprocess.run(
                [str(SCRIPTS / "page-digest.py"), p],
                capture_output=True, text=True,
            )
            out.write(res.stdout if res.returncode == 0 else _read(Path(p)))
        out.write("\n```\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--source-id", required=True)
    ap.add_argument("--sha256", required=True)
    ap.add_argument("--added", required=True)
    ap.add_argument("--origin-type", required=True)
    ap.add_argument("--origin-ref", required=True)
    ap.add_argument("--basename", required=True)
    ap.add_argument("--section-label", default="")
    ap.add_argument("--all-source-ids", default="")
    ap.add_argument("--source-terms-file", required=True)
    ap.add_argument("--text-file", required=True)
    ap.add_argument("--candidates-file", required=True)
    ap.add_argument("--expand-file", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--operation", choices=["digest", "expand", "retry"], default="digest")
    args = ap.parse_args()

    expand_file = Path(args.expand_file)
    expand_nonempty = expand_file.is_file() and expand_file.stat().st_size > 0
    candidates_file = Path(args.candidates_file)
    image_block = _render_images_block(args.dest)

    out = sys.stdout
    # Keep stable high-reuse instructions first; source/candidate run data
    # follows so provider prefix caches can reuse the invariant prompt prefix.
    # 1. ingest prompt
    out.write(_read(TOOLING_ROOT / "prompts" / "ingest.md"))
    # 2. schema excerpt
    out.write("\n\n---\n\n## SCHEMA\n")
    out.write(_selected_schema(args.operation, candidates_file, expand_file, image_block))
    # 3. ALL_SOURCE_IDS
    out.write(f"\n\n---\n\n## ALL_SOURCE_IDS\n{args.all_source_ids}\n")
    # 4. TAXONOMY
    out.write("\n---\n\n## TAXONOMY\n")
    out.write(_read(VAULT_ROOT / "wiki" / "_taxonomy.md"))
    # 5. SOURCE_META
    out.write("\n\n---\n\n## SOURCE_META\n")
    out.write(
        f"source_id: {args.source_id}\n"
        f"sha256: {args.sha256}\n"
        f"added: {args.added}\n"
        f"origin_type: {args.origin_type}\n"
        f"origin_ref: {args.origin_ref}\n"
        f"basename: {args.basename}\n"
    )
    # 6. SOURCE_KEY_TERMS
    out.write("\n## SOURCE_KEY_TERMS\n")
    terms = _read(Path(args.source_terms_file)).strip()
    out.write(terms if terms else "(not available)")
    out.write("\n")
    # 7. SECTION_LABEL — bash default embeds the source_id.
    section = args.section_label or f"<none — cite as bare [src:{args.source_id}]>"
    out.write(f"\n## SECTION_LABEL\n{section}\n")
    # 8. SOURCE_TEXT
    out.write("\n## SOURCE_TEXT\n")
    out.write(_read(Path(args.text_file)))
    # 9. CANDIDATE_PAGES header
    out.write("\n\n---\n\n## CANDIDATE_PAGES")
    if expand_nonempty:
        # bash uses `wc -l` (counts trailing newlines); match exactly.
        n = expand_file.read_text(encoding="utf-8").count("\n")
        out.write(f" (expanded: {n} file(s) shown in full; rest are digests)")
    else:
        out.write(" (digests only — emit expand action if you need full content)")
    out.write("\n")
    build_candidate_blob(candidates_file, expand_file, out)
    # 10. IMAGES
    out.write("\n---\n\n## IMAGES\n")
    out.write(image_block)
    # 11. trailer
    if expand_nonempty:
        out.write("\n---\n\nNow emit the unified diff. Reminder: only modify\n")
        out.write("candidates shown in full or candidates whose digest has\n")
        out.write("no elision marker; leave still-truncated candidates unchanged.\n")
    else:
        out.write("\n---\n\nNow emit the unified diff, OR a single JSON line\n")
        out.write("requesting expansion if you need full content for any candidate:\n")
        out.write('  {"action":"expand","files":["wiki/entities/X.md", ...]}\n')
        out.write("Expansion is allowed at most once per ingest.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
