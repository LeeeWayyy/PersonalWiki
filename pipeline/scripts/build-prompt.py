#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""
Assemble the main ingest prompt. Extracted verbatim from ingest.sh's
`build_prompt` + `build_candidate_blob` (§14 refactor) — this is a
BEHAVIOR-PRESERVING move: the emitted bytes must match the old bash
heredoc exactly (verified by scripts/tests/test_build_prompt.sh).

Writes the full prompt to stdout. ingest.py redirects it to a file.

Inputs (all from ingest.py variables / files):
    --source-id --sha256 --added --origin-type --origin-ref --basename
    --section-label   (may be empty → a <none …> default that embeds the id)
    --all-source-ids  (newline-separated, may be empty)
    --text-file       extracted SOURCE_TEXT
    --candidates-file one candidate path per line (may be empty/missing)
    --expand-file     paths to inline FULL; others are digests (may be empty)
    --dest            the source asset path (for <dest>.assets/_manifest.md)

Reads from the content repo ($VAULT_CONTENT_DIR): wiki/_taxonomy.md. Reads from
the tooling repo (TOOLING_ROOT): prompts/ingest.md. Shells out to
scripts/page-digest.py and scripts/render-images-block.py (also tooling)
exactly as the original bash implementation did.
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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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
    ap.add_argument("--text-file", required=True)
    ap.add_argument("--candidates-file", required=True)
    ap.add_argument("--expand-file", required=True)
    ap.add_argument("--dest", required=True)
    args = ap.parse_args()

    expand_file = Path(args.expand_file)
    expand_nonempty = expand_file.is_file() and expand_file.stat().st_size > 0

    out = sys.stdout
    # Keep stable high-reuse instructions first; source/candidate run data
    # follows so provider prefix caches can reuse the invariant prompt prefix.
    # 1. ingest prompt
    out.write(_read(TOOLING_ROOT / "prompts" / "ingest.md"))
    # 2. ALL_SOURCE_IDS
    out.write(f"\n\n---\n\n## ALL_SOURCE_IDS\n{args.all_source_ids}\n")
    # 3. TAXONOMY
    out.write("\n---\n\n## TAXONOMY\n")
    out.write(_read(VAULT_ROOT / "wiki" / "_taxonomy.md"))
    # 4. SOURCE_META
    out.write("\n\n---\n\n## SOURCE_META\n")
    out.write(
        f"source_id: {args.source_id}\n"
        f"sha256: {args.sha256}\n"
        f"added: {args.added}\n"
        f"origin_type: {args.origin_type}\n"
        f"origin_ref: {args.origin_ref}\n"
        f"basename: {args.basename}\n"
    )
    # 5. SECTION_LABEL — bash default embeds the source_id.
    section = args.section_label or f"<none — cite as bare [src:{args.source_id}]>"
    out.write(f"\n## SECTION_LABEL\n{section}\n")
    # 6. SOURCE_TEXT
    out.write("\n## SOURCE_TEXT\n")
    out.write(_read(Path(args.text_file)))
    # 7. CANDIDATE_PAGES header
    out.write("\n\n---\n\n## CANDIDATE_PAGES")
    if expand_nonempty:
        # bash uses `wc -l` (counts trailing newlines); match exactly.
        n = expand_file.read_text(encoding="utf-8").count("\n")
        out.write(f" (expanded: {n} file(s) shown in full; rest are digests)")
    else:
        out.write(" (digests only — emit expand action if you need full content)")
    out.write("\n")
    build_candidate_blob(Path(args.candidates_file), expand_file, out)
    # 8. IMAGES
    out.write("\n---\n\n## IMAGES\n")
    manifest = Path(f"{args.dest}.assets") / "_manifest.md"
    if manifest.is_file():
        res = subprocess.run(
            [str(SCRIPTS / "render-images-block.py"), str(manifest), args.dest],
            capture_output=True, text=True,
        )
        if res.returncode == 0:
            out.write(res.stdout)
        else:
            out.write("(images-block render failed; LLM proceeds without image table)\n")
    else:
        out.write("(no images extracted from this source)\n")
    # 9. trailer
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
