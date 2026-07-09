#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Render the IMAGES prompt block from a `_manifest.md`.

Used by ingest.py during prompt assembly. Output is a markdown table
of every non-decorative image with a caption — the inputs the LLM
needs to decide whether to embed a figure in its diff.

Usage:
    scripts/render-images-block.py <manifest-path> <source-asset-path>

Outputs to stdout. Stderr-quiet on missing/empty manifest (returns
"(no captioned images for this source)" placeholder so the prompt
remains well-formed).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from asset_manifest import read_manifest  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: render-images-block.py <manifest-path> <source-asset-path>",
              file=sys.stderr)
        return 2
    manifest_path = Path(sys.argv[1]).resolve()
    source_asset = Path(sys.argv[2]).resolve()
    if not manifest_path.is_file():
        print("(no manifest)")
        return 0
    assets_dir = manifest_path.parent
    _, entries = read_manifest(assets_dir)

    # Filter to non-decorative entries with captions.
    rows = [
        e for e in entries
        if not e.decorative and e.caption is not None
    ]
    if not rows:
        print("(no captioned non-decorative images for this source)")
        return 0

    # Vault-relative path. assets_dir = sources/<asset>.assets — we need
    # the full vault-relative form. Walk up to find the vault root: the
    # directory containing both `wiki/` and `sources/`.
    vault_root = source_asset.parent
    while vault_root.parent != vault_root:
        if (vault_root / "wiki").is_dir() and (vault_root / "sources").is_dir():
            break
        vault_root = vault_root.parent

    # Print a markdown table.
    print("| path | caption | dimensions |")
    print("|---|---|---|")
    for e in rows:
        # path: relative to vault root, posix style.
        full = (assets_dir / e.file)
        try:
            rel = full.resolve().relative_to(vault_root)
        except ValueError:
            rel = full
        path = str(rel).replace("\\", "/")
        caption = (e.caption or "").replace("|", r"\|").replace("\n", " ")
        if e.dimensions and len(e.dimensions) >= 2:
            dims = f"{e.dimensions[0]}×{e.dimensions[1]}"
        else:
            dims = "?"
        print(f"| {path} | {caption} | {dims} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
