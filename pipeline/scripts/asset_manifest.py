#!/usr/bin/env python3
"""
Read/write/merge `_manifest.md` for an `<asset>.assets/` directory.

Format (frontmatter-only YAML, no body):

```
---
schema_version: 1
source_id: 01KQN…
images:
  - file: a1b2c3d4e5f6.png
    sha256: a1b2c3d4e5f6...
    bytes: 245678
    dimensions: [1200, 800]
    origin_refs:
      - { kind: epub, item: "OEBPS/Images/fig.png", chapter: "OEBPS/Text/ch3.xhtml" }
    decorative: false
    caption: null
    caption_source: null      # embedded | pdf-label | vision
    caption_model: null
    caption_at: null
    caption_error: null
---
```

Used by extract.py (writer of initial entries), caption.py (writer of
captions), and lint.py (reader to enforce rules). Atomic writes via
`.tmp` + `os.replace`.

Determinism contract: re-runs produce byte-identical output. Image
entries sorted by sha256; origin_refs sorted by canonical-JSON form.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Keep the pinned-style serializer for byte determinism; use PyYAML to parse.

SCHEMA_VERSION = 1

FM_RX = re.compile(r"^---\n(.*?)\n---\s*$", re.DOTALL)


@dataclass
class OriginRef:
    """One provenance record: where this image bytes appeared."""
    kind: str                    # "epub" | "pdf" | "web"
    # Per-kind fields (only those for `kind` should be set):
    item: str | None = None      # epub: manifest path inside the EPUB
    chapter: str | None = None   # epub: chapter HTML path inside the EPUB
    page: int | None = None      # pdf: page number (1-indexed)
    xref: int | None = None      # pdf: image xref (None for vector regions)
    bbox: list[float] | None = None   # pdf: [x0, y0, x1, y1] (vector regions)
    url: str | None = None       # web: original URL

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind}
        for k in ("item", "chapter", "page", "xref", "bbox", "url"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OriginRef":
        # `kind` is required; default to empty string rather than passing
        # None into the str field (corrupt manifests would otherwise raise
        # a TypeError later when ref.kind.startswith() is called).
        return cls(kind=d.get("kind") or "",
                   **{k: d.get(k) for k in
                      ("item", "chapter", "page", "xref", "bbox", "url")})

    def canonical_json(self) -> str:
        """Stable serialization for dedup."""
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)


@dataclass
class ImageEntry:
    """One image in the manifest."""
    file: str                          # filename inside the .assets/ dir
    sha256: str                        # full sha256 of bytes on disk
    bytes: int
    dimensions: list[int]              # [width, height]
    origin_refs: list[OriginRef] = field(default_factory=list)
    decorative: bool = False
    caption: str | None = None
    caption_source: str | None = None      # "embedded" | "pdf-label" | "vision"
    caption_model: str | None = None
    caption_at: str | None = None
    caption_error: str | None = None
    caption_error_kind: str | None = None  # "transient" | "terminal" | None
    original_sha256: str | None = None     # pre-resize sha256, if resized

    def to_yaml_block(self) -> str:
        """Emit one YAML list item for this entry."""
        lines = [f"  - file: {_yaml_str(self.file)}"]
        lines.append(f"    sha256: {self.sha256}")
        if self.original_sha256:
            lines.append(f"    original_sha256: {self.original_sha256}")
        lines.append(f"    bytes: {self.bytes}")
        lines.append(f"    dimensions: [{self.dimensions[0]}, {self.dimensions[1]}]")
        if self.origin_refs:
            lines.append("    origin_refs:")
            for ref in self.origin_refs:
                lines.append(f"      - {_yaml_inline(ref.to_dict())}")
        else:
            lines.append("    origin_refs: []")
        lines.append(f"    decorative: {str(self.decorative).lower()}")
        lines.append(f"    caption: {_yaml_optional_str(self.caption)}")
        lines.append(f"    caption_source: {_yaml_optional_str(self.caption_source)}")
        lines.append(f"    caption_model: {_yaml_optional_str(self.caption_model)}")
        lines.append(f"    caption_at: {_yaml_optional_str(self.caption_at)}")
        lines.append(f"    caption_error: {_yaml_optional_str(self.caption_error)}")
        lines.append(f"    caption_error_kind: {_yaml_optional_str(self.caption_error_kind)}")
        return "\n".join(lines)


def _yaml_str(s: str) -> str:
    """YAML scalar — quote if contains special characters."""
    if re.match(r"^[A-Za-z0-9._/\-]+$", s):
        return s
    return json.dumps(s, ensure_ascii=False)


def _yaml_optional_str(s: str | None) -> str:
    if s is None:
        return "null"
    return _yaml_str(s)


def _yaml_inline(d: dict[str, Any]) -> str:
    """Flow-style mapping: `{ kind: epub, item: "foo", ... }`."""
    parts = []
    for k in sorted(d):
        v = d[k]
        if isinstance(v, str):
            parts.append(f"{k}: {_yaml_str(v)}")
        elif isinstance(v, bool):
            parts.append(f"{k}: {str(v).lower()}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}: {v}")
        elif isinstance(v, list):
            parts.append(f"{k}: [{', '.join(map(str, v))}]")
        elif v is None:
            parts.append(f"{k}: null")
        else:
            parts.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    return "{ " + ", ".join(parts) + " }"


def manifest_path(assets_dir: Path) -> Path:
    return assets_dir / "_manifest.md"


def read_manifest(assets_dir: Path) -> tuple[str | None, list[ImageEntry]]:
    """Return (source_id, entries). Empty if missing."""
    p = manifest_path(assets_dir)
    if not p.is_file():
        return None, []
    text = p.read_text(encoding="utf-8")
    m = FM_RX.match(text)
    if not m:
        return None, []
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None, []
    if not isinstance(data, dict):
        return None, []
    images = data.get("images") or []
    if not isinstance(images, list):
        return data.get("source_id"), []
    return data.get("source_id"), [
        _finalize_entry(item, item.get("origin_refs") or [])
        for item in images if isinstance(item, dict)
    ]


def _finalize_entry(d: dict[str, Any], refs: list[dict[str, Any]]) -> ImageEntry:
    return ImageEntry(
        file=d.get("file", ""),
        sha256=d.get("sha256", ""),
        bytes=int(d.get("bytes", 0)),
        dimensions=d.get("dimensions", [0, 0]),
        origin_refs=[OriginRef.from_dict(r) for r in refs],
        decorative=bool(d.get("decorative", False)),
        caption=d.get("caption"),
        caption_source=d.get("caption_source"),
        caption_model=d.get("caption_model"),
        caption_at=d.get("caption_at"),
        caption_error=d.get("caption_error"),
        caption_error_kind=d.get("caption_error_kind"),
        original_sha256=d.get("original_sha256"),
    )


def write_manifest(assets_dir: Path, source_id: str | None,
                   entries: list[ImageEntry]) -> None:
    """Write atomically. Entries sorted by sha256; refs sorted by canonical JSON."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    # Sort entries by sha256 for determinism.
    entries_sorted = sorted(entries, key=lambda e: e.sha256)
    # Sort each entry's origin_refs deterministically.
    for e in entries_sorted:
        e.origin_refs = sorted(e.origin_refs, key=lambda r: r.canonical_json())

    lines = ["---", f"schema_version: {SCHEMA_VERSION}",
             f"source_id: {source_id or 'null'}", "images:"]
    if not entries_sorted:
        # Trailing colon followed by empty list — keep YAML legal.
        lines[-1] = "images: []"
    else:
        for e in entries_sorted:
            lines.append(e.to_yaml_block())
    lines.append("---")
    lines.append("")  # trailing newline

    # Sweep stale temp files left over from prior crashed runs. The atomic
    # write below uses tempfile.mkstemp which leaves a `.manifest.XXXXXX`
    # if the process is SIGKILLed between fdopen and replace; without this
    # cleanup, ingest.py's `git add <assets-dir>` would commit the leftover.
    for stale in assets_dir.glob(".manifest.*"):
        try:
            stale.unlink()
        except OSError:
            pass

    target = manifest_path(assets_dir)
    fd, tmp_path = tempfile.mkstemp(prefix=".manifest.", dir=str(assets_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
