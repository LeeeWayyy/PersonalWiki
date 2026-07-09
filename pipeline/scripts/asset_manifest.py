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

# YAML pinned-style serializer using stdlib only — we don't have a yaml
# dep. Format is constrained enough that we hand-roll it.

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
    fm = m.group(1)
    # Hand-rolled parse — enough for the constrained shape we write.
    source_id: str | None = None
    entries: list[ImageEntry] = []
    cur: dict[str, Any] | None = None
    cur_refs: list[dict[str, Any]] | None = None
    in_images = False
    for line in fm.splitlines():
        if line.startswith("source_id:"):
            source_id = line.split(":", 1)[1].strip()
            if source_id == "null":
                source_id = None
        elif line.strip() == "images:":
            in_images = True
        elif in_images and line.startswith("  - file:"):
            if cur:
                entries.append(_finalize_entry(cur, cur_refs or []))
            cur = {"file": _parse_yaml_str(line.split(":", 1)[1].strip())}
            cur_refs = []
        elif cur is not None and line.startswith("    "):
            stripped = line.strip()
            if stripped == "origin_refs:":
                cur_refs = []
            elif stripped.startswith("- {") and cur_refs is not None:
                # flow mapping
                cur_refs.append(_parse_flow_mapping(stripped[2:].strip()))
            elif ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip()
                cur[key] = _parse_scalar(val)
        # else: ignore
    if cur:
        entries.append(_finalize_entry(cur, cur_refs or []))
    return source_id, entries


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


def _parse_yaml_str(s: str) -> str:
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        return json.loads(s)
    return s


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if s == "null":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if s.startswith("[") and s.endswith("]"):
        # flow list — we only emit list-of-numbers and list-of-nothing
        inner = s[1:-1].strip()
        if not inner:
            return []
        parts = [p.strip() for p in inner.split(",")]
        try:
            return [int(p) for p in parts]
        except ValueError:
            try:
                return [float(p) for p in parts]
            except ValueError:
                return parts
    if s.startswith('"') and s.endswith('"'):
        return json.loads(s)
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _parse_flow_mapping(s: str) -> dict[str, Any]:
    """Parse `{ k: v, k2: "v2", k3: [1, 2, 3] }`."""
    s = s.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return {}
    inner = s[1:-1].strip()
    out: dict[str, Any] = {}
    # Split on top-level commas (not inside brackets / quotes).
    parts = _split_top_level(inner, ",")
    for p in parts:
        if not p.strip():
            continue
        if ":" not in p:
            continue
        key, _, val = p.partition(":")
        out[key.strip()] = _parse_scalar(val)
    return out


def _split_top_level(s: str, sep: str) -> list[str]:
    """Split `s` on `sep`, ignoring `sep` inside `"..."` strings or `[...]`/`{...}`
    nesting. Tracks JSON-style backslash escapes so `\"` does NOT close a
    string. Without this, any string value containing a literal `"` would
    cause the splitter to silently misalign and corrupt the parse — which
    matters once Phase 2 web origin_refs land with arbitrary URLs.
    """
    out: list[str] = []
    depth = 0
    in_str = False
    prev_backslash = False
    cur = []
    for ch in s:
        cur.append(ch)
        if ch == '"' and not prev_backslash:
            in_str = not in_str
            prev_backslash = False
            continue
        if not in_str:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif depth == 0 and ch == sep:
                # remove the just-appended sep and emit the segment
                cur.pop()
                out.append("".join(cur))
                cur = []
                prev_backslash = False
                continue
        # backslash tracking — only for in-string state, but cheap to track always
        prev_backslash = (ch == "\\") and not prev_backslash
    if cur:
        out.append("".join(cur))
    return out


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
