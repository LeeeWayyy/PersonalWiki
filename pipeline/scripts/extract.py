#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "ebooklib>=0.18",
#     "beautifulsoup4>=4.12",
#     "markdownify>=0.11",
#     "trafilatura>=1.12",
#     "pypdf>=5.0",
#     "requests>=2.31",
#     # MOBI conversion is pinned for the same idempotency class as image
#     # rasterization: KindleUnpack changes can shift extracted bytes/paths
#     # and therefore source asset hashes.
#     "mobi==0.4.1",
#     # Image-rasterizing deps PINNED to exact versions per plan §3.3:
#     # different libjpeg/libpng quantization tables produce different
#     # output bytes for the same input, breaking idempotency. Bumping
#     # either of these is a vault-wide breaking change — every
#     # PDF-rasterized PNG re-hashes, every wiki embed pointing at a
#     # PDF figure breaks. Re-extract the affected sources after a
#     # bump and expect one churn commit.
#     "pdfplumber==0.11.9",
#     "pillow==12.2.0",
# ]
# ///
"""
Extract clean text (markdown) from a source: EPUB, MOBI/AZW, PDF, HTML file,
URL, or plain text.

Usage:
    scripts/extract.py <path-or-url> [--section REGEX] [--limit N] [--list-sections]
                                     [--write-assets]

Output: markdown-ish UTF-8 text on stdout. Section headings come through as
`## <title>` so downstream consumers can filter with --section.

When `--write-assets` is set on a local file source, also extracts
embedded images alongside the text:
  - EPUB and MOBI/AZW images dump to `<source>.assets/<sha12>.<ext>`.
  - PDF figures (embedded raster + vector clusters) are rasterized to
    PNG at PDF_RASTER_DPI (300) and dumped under the same asset dir
    (Phase 2).
  - HTML files with embedded `<img>` tags pointing at external URLs
    download those images to the asset dir (Phase 2). Use
    `--base-url` to resolve relative URLs for snapshot files.
  - A `_manifest.md` file in that dir records each image's sha256,
    size, dimensions, and origin_refs (provenance back into the EPUB
    / PDF page / web URL).
  - The text written to stdout is text-only — `<img>`/`<picture>`
    tags are stripped from HTML/EPUB before markdownify, and PDF text
    extraction was always image-free.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify as md_from_html

# Local helper module (sibling file).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from asset_manifest import ImageEntry, OriginRef, read_manifest, write_manifest  # noqa: E402

SHA_PREFIX_LEN = 12   # see plan/image-ingest-plan.md §3.2

PDF_RASTER_DPI = 300
# Outline flattening depth: top level + one nesting, so Part → Chapter books
# split by chapter without exploding subsection-heavy outlines into noise.
PDF_OUTLINE_MAX_DEPTH = 2
# Max vertical gap (PDF points) between a figure and its caption line.
PDF_CAPTION_MAX_GAP = 60.0
WEB_IMAGE_CAP = 20            # max image downloads per page
WEB_USER_AGENT = "PersonalWiki-ingest/1.0 (+local-vault)"
WEB_TIMEOUT = 30              # seconds
WEB_MAX_FILESIZE = 5 * 1024 * 1024


def _dump_asset(assets_dir: Path, data: bytes, ext: str) -> tuple[str, Path] | None:
    """Write asset bytes as `<sha12>.<ext>`, extending the sha prefix on collision.

    Idempotent on rerun (an existing file with matching bytes is reused).
    Returns (full_sha, target_path), or None on catastrophic collision.
    """
    full_sha = hashlib.sha256(data).hexdigest()
    assets_dir.mkdir(parents=True, exist_ok=True)
    target = assets_dir / f"{full_sha[:SHA_PREFIX_LEN]}.{ext}"
    if not target.exists():
        target.write_bytes(data)
        return full_sha, target
    if target.read_bytes() == data:
        return full_sha, target
    # Real sha-prefix collision (vanishingly rare at sha12). Extend the
    # prefix until we find either an empty slot OR an existing file with
    # matching bytes (idempotent on rerun).
    for n in range(SHA_PREFIX_LEN + 1, 65):
        alt = assets_dir / f"{full_sha[:n]}.{ext}"
        if not alt.exists():
            alt.write_bytes(data)
            return full_sha, alt
        if alt.read_bytes() == data:
            return full_sha, alt
    print(f"extract: catastrophic sha collision on {full_sha}", file=sys.stderr)
    return None


def _sniff_image_ext(data: bytes) -> str | None:
    """Return file extension from magic bytes, or None if unrecognized."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:512].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<svg"):
        return "svg"
    return None


def _image_dimensions(data: bytes, ext: str) -> list[int] | None:
    """Best-effort image dimensions via Pillow (already a pinned dep).

    For SVG we return None (dimensions live in viewBox/width/height
    attrs; Pillow can't open SVG).
    """
    if ext == "svg":
        return None
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            return list(img.size)
    except Exception:
        return None


def _read_source_id(source_path: Path) -> str | None:
    """Read `source_id` from the sidecar `<source>.md`, if present.

    Tolerant of: surrounding quotes (single or double), and any case
    (project convention is uppercase ULID, but `ulid-py` and other tools
    sometimes emit lowercase).
    """
    sidecar = source_path.parent / (source_path.name + ".md")
    if not sidecar.is_file():
        return None
    # `utf-8-sig` strips a leading BOM if the sidecar was hand-edited
    # in an editor that adds one (rare, but causes `^` anchor to miss).
    text = sidecar.read_text(encoding="utf-8-sig")
    # Allow leading whitespace before `source_id:` (valid YAML indent).
    m = re.search(r"^\s*source_id:\s*['\"]?([0-9A-Za-z]{26})['\"]?\s*$",
                  text, re.MULTILINE)
    if not m:
        return None
    # Project convention is uppercase; uppercase to keep manifest stable
    # across sidecars that may have either casing.
    return m.group(1).upper()


def _html_to_md(html: str) -> str:
    md = md_from_html(html, heading_style="ATX", bullets="-")
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def _demote_headings(md: str, levels: int = 2) -> str:
    """Shift all `# ...` headings down so the document has only one top-level heading.

    ebooklib gives us per-spine-item chapter titles; we own the `##` level.
    Any headings that come from body markdown (h1/h2/h3 inside the chapter)
    must be demoted so section filters based on `^## ` don't split a chapter.
    """
    def repl(m: re.Match) -> str:
        hashes = m.group(1)
        return "#" * min(len(hashes) + levels, 6) + " "
    return re.sub(r"^(#{1,6})\s+", repl, md, flags=re.MULTILINE)


def extract_epub(path: Path, *, write_assets: bool = False,
                 source_id: str | None = None,
                 assets_dir: Path | None = None) -> str:
    """Extract markdown text from an EPUB.

    When `write_assets=True`, also dumps embedded images to
    `<path>.assets/<sha12>.<ext>` and writes/merges `_manifest.md` there.

    Phase 1 invariant: stdout text is byte-identical regardless of
    `write_assets` — `<img>` tags are stripped from the HTML before
    markdownify in BOTH modes. (Phase 4 will add image-ref placeholders
    when write_assets is on.)
    """
    import ebooklib
    from ebooklib import epub

    book = epub.read_epub(str(path))

    title_map: dict[str, str] = {}
    try:
        toc_items = book.toc or []

        def walk(items):
            for it in items:
                if isinstance(it, tuple):
                    section, children = it[0], it[1]
                    if hasattr(section, "href") and hasattr(section, "title"):
                        title_map[section.href.split("#")[0]] = section.title
                    walk(children)
                elif hasattr(it, "href") and hasattr(it, "title"):
                    title_map[it.href.split("#")[0]] = it.title

        walk(toc_items)
    except Exception:
        pass

    # Asset extraction context (only populated when write_assets=True).
    manifest_map: dict[str, ImageEntry] = {}   # sha256 -> entry
    item_to_sha: dict[str, str] = {}          # normalized EPUB path -> sha256
    refreshed_shas: set[str] = set()          # entries whose origin_refs we cleared
    if write_assets:
        assets_dir = assets_dir or path.parent / (path.name + ".assets")
        # Read existing manifest (re-runs are merge-not-replace).
        _, existing_entries = read_manifest(assets_dir)
        for e in existing_entries:
            manifest_map[e.sha256] = e
        # Pass 1: dump bytes, create manifest entries with NO origin_refs.
        # origin_refs are populated in pass 2 (HTML walk) where we know
        # both `item` (manifest path) AND `chapter` (referencing chapter).
        # For ANY entry whose sha is in this run's EPUB, we clear its
        # existing origin_refs first (then pass 2 rebuilds them) — this
        # way a chapter rename / image relocation produces correct refs
        # rather than accumulating stale ones across runs. Entries whose
        # sha is no longer in the EPUB keep their old refs (orphans, but
        # provenance preserved).
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            data = item.get_content()
            if not data:
                continue
            ext = _sniff_image_ext(data)
            if ext is None:
                print(f"extract: unknown image type for {item.get_name()}, skipping",
                      file=sys.stderr)
                continue
            dumped = _dump_asset(assets_dir, data, ext)
            if dumped is None:
                continue
            full_sha, target = dumped
            item_to_sha[_normalize_manifest_key(item.get_name())] = full_sha
            if full_sha not in manifest_map:
                dims = _image_dimensions(data, ext) or [0, 0]
                manifest_map[full_sha] = ImageEntry(
                    file=target.name,
                    sha256=full_sha,
                    bytes=len(data),
                    dimensions=dims,
                    origin_refs=[],
                )
                refreshed_shas.add(full_sha)
            # Existing entry whose sha is in current EPUB: clear origin_refs
            # so pass 2 rebuilds them fresh. (Track which shas we cleared
            # so cross-source ingests on different EPUBs don't blow each
            # other away when run sequentially.)
            elif full_sha not in refreshed_shas:
                manifest_map[full_sha].origin_refs = []
                refreshed_shas.add(full_sha)

    parts: list[str] = []
    for item in book.spine:
        item_id = item[0]
        doc = book.get_item_with_id(item_id)
        if doc is None or doc.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        href = doc.get_name()
        raw = doc.get_content().decode("utf-8", errors="replace")

        soup = BeautifulSoup(raw, "html.parser")
        title = title_map.get(href)
        if not title:
            h = soup.find(["h1", "h2", "h3", "title"])
            if h:
                title = h.get_text(strip=True)
        if not title:
            title = href

        body = soup.body or soup

        # Pass 2: walk image-bearing elements (<img>, <image>, <source srcset>,
        # <picture> children). For each src that resolves to a manifest
        # item, add an origin_ref with both `item` and `chapter`. Then
        # strip the host tag so SOURCE_TEXT remains text-only.
        if write_assets and assets_dir is not None:
            chapter_norm = _normalize_manifest_key(href)
            seen_tags: set[int] = set()
            for tag, src in _iter_image_srcs(body):
                target_path = _resolve_epub_ref(href, src)
                if target_path and target_path in item_to_sha:
                    sha = item_to_sha[target_path]
                    entry = manifest_map[sha]
                    new_ref = OriginRef(kind="epub", item=target_path,
                                        chapter=chapter_norm)
                    seen = {r.canonical_json() for r in entry.origin_refs}
                    if new_ref.canonical_json() not in seen:
                        entry.origin_refs.append(new_ref)
                    # Prefer the book's own caption (free, deterministic) over
                    # a later vision call. First chapter that carries one wins;
                    # no caption_at so re-runs stay byte-identical.
                    _apply_source_caption(entry, _embedded_caption(tag), "embedded")
                # Strip the host tag (once — multiple srcs from one tag
                # like <img srcset>+src share the same node).
                if id(tag) not in seen_tags:
                    seen_tags.add(id(tag))
                    tag.decompose()

        md = _html_to_md(str(body))
        if not md:
            continue
        md = _demote_headings(md, levels=2)
        parts.append(f"\n\n## {title}\n\n{md}\n")

    if write_assets and assets_dir is not None:
        write_manifest(assets_dir, source_id, list(manifest_map.values()))

    return "\n".join(parts).strip() + "\n"


def _resolve_epub_ref(chapter_path: str, src: str) -> str | None:
    """Resolve an `<img src="...">` against `chapter_path` per plan §2.1.

    Returns the EPUB-root-relative manifest-style path (percent-decoded,
    normalized), or None for out-of-band refs (http, data) and for paths
    that escape the root.
    """
    import posixpath
    from urllib.parse import unquote

    if not src:
        return None
    src = src.strip()
    # Out-of-band — skip.
    if src.startswith("http://") or src.startswith("https://"):
        return None
    if src.startswith("data:"):
        return None
    # Strip fragment first, then percent-decode.
    src = src.split("#", 1)[0]
    if not src:
        # Fragment-only href like `#foo`; nothing to look up.
        return None
    src = unquote(src)
    if src.startswith("/"):
        # EPUB-root-relative (jail to the EPUB).
        target = src.lstrip("/")
    else:
        chapter_dir = posixpath.dirname(chapter_path)
        target = posixpath.normpath(posixpath.join(chapter_dir, src))
    if target in ("", "."):
        return None
    if target == ".." or target.startswith("../") or target.startswith("/"):
        return None
    return target


def _normalize_manifest_key(name: str) -> str:
    """Apply the same normalization as `_resolve_epub_ref` to a manifest key.

    EPUB manifest keys (from `item.get_name()`) may be percent-encoded
    (e.g. `OEBPS/Images/fig%201.png`). HTML `<img src>` paths are decoded
    in `_resolve_epub_ref`. To make lookups work, both sides must be
    normalized identically.
    """
    import posixpath
    from urllib.parse import unquote
    return posixpath.normpath(unquote(name.strip()))


def _iter_image_srcs(soup_body) -> list[tuple[Any, str]]:
    """Yield (tag, src) pairs for every image-bearing element under `soup_body`.

    Covers:
      - <img src="...">
      - <img srcset="a.png 1x, b.png 2x"> (split BEFORE percent-decode so
        encoded commas don't false-positive)
      - <picture><source srcset="..."> (only <source> tags directly under
        <picture> — NOT <audio>/<video> sources, which would silently
        get decomposed below if we scoped this any wider)
      - <image href="..."> / <image xlink:href="..."> (SVG)
    Per plan §2.1 step 3.
    """
    out: list[tuple[Any, str]] = []
    for tag in soup_body.find_all(["img", "image"]):
        for attr in ("src", "href", "xlink:href"):
            val = tag.get(attr)
            if val:
                out.append((tag, val))
                break
        srcset = tag.get("srcset")
        if srcset:
            for cand in _parse_srcset(srcset):
                out.append((tag, cand))
    # Only scope <source> tags to those directly under <picture> — not
    # arbitrary <source> nodes inside <audio> or <video> elements.
    for picture in soup_body.find_all("picture"):
        for source in picture.find_all("source"):
            srcset = source.get("srcset") or source.get("src")
            if srcset:
                for cand in _parse_srcset(srcset):
                    out.append((source, cand))
    return out


# A caption/figure label like "图 12-2", "Figure 3", "Table 4.1". Used both to
# validate an adjacent caption paragraph (HTML) and to anchor captions to
# figures in PDFs.
FIGURE_LABEL_RX = re.compile(
    r"^\s*(图|圖|表|Figure|Plot|Fig\.?|Table)\s*\d+([-.]\d+)?", re.IGNORECASE)


def _clean_caption(text: str | None) -> str | None:
    """Collapse whitespace; empty → None."""
    if not text:
        return None
    text = " ".join(text.split())
    return text or None


def _good_alt(alt: str | None) -> str | None:
    """Return alt text only if it's a real caption, not a placeholder.

    EPUBs routinely ship `alt="image1.jpg"` / `alt=""` / `alt="图"`, which are
    worse than no caption — reject those so vision handles the image instead."""
    alt = _clean_caption(alt)
    if not alt or len(alt) < 4:
        return None
    if re.search(r"\.\w{2,4}$", alt):               # filename-shaped
        return None
    if alt.lower() in ("image", "img", "figure", "photo", "picture"):
        return None
    # Bare label like "Figure 3" / "图 12-2" — carries no description, and would
    # pre-empt a richer adjacent caption node. Skip it; adjacent/vision do better.
    m = FIGURE_LABEL_RX.match(alt)
    if m and len(alt[m.end():].strip(" .:：、,，-—")) < 4:
        return None
    return alt


def _embedded_caption(tag) -> str | None:
    """Best-effort caption from an <img>'s own markup, in order of reliability:
    enclosing <figcaption> → a usable alt → an adjacent caption paragraph.
    None means the source gave us nothing usable; let vision caption it."""
    fig = tag.find_parent("figure")
    if fig is not None:
        fc = fig.find("figcaption")
        if fc is not None:
            cap = _clean_caption(fc.get_text(" ", strip=True))
            if cap:
                return cap
    alt = _good_alt(tag.get("alt"))
    if alt:
        return alt
    # Immediate element siblings only — enough for `<img><p class="caption">`
    # without risking grabbing body prose.
    for sib in (tag.find_next_sibling(), tag.find_previous_sibling()):
        if sib is None:                         # find_*_sibling() yields Tags only
            continue
        classes = " ".join(sib.get("class", []) or []).lower()
        text = _clean_caption(sib.get_text(" ", strip=True))
        if not text:
            continue
        if "caption" in classes or FIGURE_LABEL_RX.match(text):
            return text
    return None


def _apply_source_caption(entry: ImageEntry, caption: str | None, source: str) -> None:
    """Write a caption taken from the source itself (figcaption/alt/PDF label).

    Owns the overwrite policy: no-op when there's nothing to write, or when the
    entry already holds a source caption — deterministic source text is
    preferred, so re-extract refreshes stale *vision* captions but keeps the
    first source caption (first-source-wins → byte-identical re-runs).

    On write, clears stale vision-era state so downstream (render-images-block,
    lint) treats the entry as freshly, validly captioned: a previously-decorative
    or errored entry must lose those, or the new caption is silently filtered
    out. No caption_at — keeps re-runs byte-identical."""
    if not caption or entry.caption_source in ("embedded", "pdf-label"):
        return
    entry.caption = caption
    entry.caption_source = source
    entry.decorative = False
    entry.caption_model = None
    entry.caption_at = None
    entry.caption_error = None
    entry.caption_error_kind = None


def _parse_srcset(srcset: str) -> list[str]:
    """Split `srcset` into URL candidates.

    Important: split on `,` BEFORE percent-decoding so `%2C` (encoded
    comma) doesn't false-positive as a separator. Each candidate has its
    descriptor (`1x`, `100w`, etc.) trimmed.
    """
    out: list[str] = []
    for piece in srcset.split(","):
        piece = piece.strip()
        if not piece:
            continue
        # Descriptor is space-separated: "url 1x" or "url 100w".
        url = piece.split()[0]
        if url.startswith("data:"):
            continue
        out.append(url)
    return out


def extract_pdf(path: Path, *, write_assets: bool = False,
                source_id: str | None = None) -> str:
    """Extract markdown text from a PDF.

    Text is extracted via pypdf. When the PDF has an outline (bookmarks),
    sections are `## <bookmark title>` spanning that entry's page range —
    content-based splitting, same shape as EPUB chapters. Without an
    outline, sections fall back to `## Page N`. When `write_assets`
    is True, also enumerates figure bboxes per page (via pdfplumber) and
    rasterizes each bbox to PNG at 300 DPI; writes/merges _manifest.md.

    Per plan §2.2: always rasterize, never extract encoded bytes (the
    embedded image XObject byte streams from pdfplumber are typically
    decoded raw pixel data + colorspace/mask metadata, not standalone
    PNG/JPEG, so sniffing them as images is unreliable).
    """
    from pypdf import PdfReader

    # Asset extraction (Phase 2). Failure must not block text
    # extraction — pypdf can often read encrypted/decrypted text via
    # `reader.decrypt()` even when pdfplumber fails to open the PDF
    # for image extraction. Soft-fail: warn + continue.
    if write_assets:
        try:
            _extract_pdf_assets_pdfplumber(path, source_id)
        except Exception as e:
            print(f"extract: PDF asset extraction failed for {path}: {e}; "
                  "continuing with text-only output", file=sys.stderr)

    reader = PdfReader(str(path))
    page_texts = [(page.extract_text() or "").strip() for page in reader.pages]

    # Content-based split: when the PDF carries an outline (bookmarks), emit
    # one `## <bookmark title>` per entry spanning its page range — the same
    # shape as EPUB output, so chaptered ingest groups PDF chapters for free.
    sections = _pdf_sections_from_outline(page_texts,
                                          _pdf_outline_sections(reader))
    if sections:
        return "\n".join(sections).strip() + "\n"

    # No usable outline → per-page sections (existing behavior).
    pages = [f"\n\n## Page {i}\n\n{txt}\n"
             for i, txt in enumerate(page_texts, start=1) if txt]
    return "\n".join(pages).strip() + "\n"


def _pdf_outline_sections(reader) -> list[tuple[str, int]]:
    """Flatten the PDF outline/bookmarks to [(title, 0-based start page)].

    Depth is capped at PDF_OUTLINE_MAX_DEPTH. Entries whose destination can't
    be resolved are skipped; entries that jump backwards are dropped so the
    [start, next_start) page ranges downstream stay monotonic.
    """
    flat: list[tuple[str, int]] = []

    def walk(items, depth: int) -> None:
        for it in items:
            if isinstance(it, list):
                if depth < PDF_OUTLINE_MAX_DEPTH:
                    walk(it, depth + 1)
                continue
            try:
                page_idx = reader.get_destination_page_number(it)
            except Exception:
                continue
            title = " ".join((getattr(it, "title", None) or "").split())
            if title and page_idx is not None:
                flat.append((title, page_idx))

    try:
        walk(reader.outline or [], 1)
    except Exception:
        return []
    out: list[tuple[str, int]] = []
    for title, idx in flat:
        if out and idx < out[-1][1]:
            continue
        out.append((title, idx))
    return out


def _pdf_sections_from_outline(page_texts: list[str],
                               outline: list[tuple[str, int]]) -> list[str]:
    """Render `## <title>` sections from outline entries and per-page texts.

    Each entry spans pages [its start, next entry's start); the last runs to
    the end. Pages before the first entry become `## Front matter`. Empty
    ranges (e.g. two bookmarks on one page) are skipped — their text lands in
    the neighboring section. Returns [] when the outline yields nothing.
    """
    if not outline:
        return []
    sections: list[str] = []
    if outline[0][1] > 0:
        head = "\n\n".join(t for t in page_texts[:outline[0][1]] if t)
        if head:
            sections.append(f"\n\n## Front matter\n\n{head}\n")
    for n, (title, start) in enumerate(outline):
        end = outline[n + 1][1] if n + 1 < len(outline) else len(page_texts)
        body = "\n\n".join(t for t in page_texts[start:end] if t)
        if body:
            sections.append(f"\n\n## {title}\n\n{body}\n")
    return sections


def _bbox_iou(a: tuple[float, float, float, float],
              b: tuple[float, float, float, float]) -> float:
    """Intersection-over-union for axis-aligned bboxes (x0,y0,x1,y1)."""
    ix0 = max(a[0], b[0]); iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2]); iy1 = min(a[3], b[3])
    iw = max(0.0, ix1 - ix0); ih = max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter == 0:
        return 0.0
    aarea = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    barea = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    return inter / (aarea + barea - inter)


def _dedupe_bboxes(bboxes: list[tuple[float, float, float, float]],
                   iou_threshold: float = 0.6
                   ) -> list[tuple[float, float, float, float]]:
    """Drop bboxes that overlap heavily with one already kept (one bbox per figure)."""
    out: list[tuple[float, float, float, float]] = []
    # Largest first: prefer bigger bboxes (more likely the figure region).
    for b in sorted(bboxes, key=lambda x: -((x[2]-x[0]) * (x[3]-x[1]))):
        if any(_bbox_iou(b, k) >= iou_threshold for k in out):
            continue
        out.append(b)
    return out


def _pdf_caption_for(fig_bbox: tuple[float, float, float, float],
                     lines: list[dict]) -> str | None:
    """Caption for a figure from a page's text lines, or None.

    Anchors on a figure-label line ("图 12-2", "Figure 3") near the figure:
    the label is a deterministic "this text IS a caption" marker, so we pair a
    confirmed caption to its figure rather than guessing which prose is one. No
    match → None, and vision captions the figure instead. Assumes single-column
    reading order (pdfplumber's default top-to-bottom); good enough for books.
    """
    fx0, ftop, fx1, fbottom = fig_bbox
    # Track below and above separately: captions sit below the figure by
    # convention, so a below-match always wins over a closer above-match — which
    # would otherwise steal the PREVIOUS figure's caption sitting between them.
    best_below: tuple[float, int] | None = None
    best_above: tuple[float, int] | None = None
    for i, ln in enumerate(lines):
        text = _clean_caption(ln.get("text"))
        if not text or not FIGURE_LABEL_RX.match(text):
            continue
        if min(fx1, ln.get("x1", 0)) - max(fx0, ln.get("x0", 0)) <= 0:
            continue                            # no horizontal overlap
        ltop, lbottom = ln.get("top", 0), ln.get("bottom", 0)
        if ltop >= fbottom:                     # below the figure (typical)
            gap = ltop - fbottom
            if gap <= PDF_CAPTION_MAX_GAP and (best_below is None or gap < best_below[0]):
                best_below = (gap, i)
        elif lbottom <= ftop:                   # above the figure
            gap = ftop - lbottom
            if gap <= PDF_CAPTION_MAX_GAP and (best_above is None or gap < best_above[0]):
                best_above = (gap, i)
        # vertically overlapping labels are ambiguous → ignore.
    best = best_below or best_above
    return _gather_caption(lines, best[1]) if best else None


def _gather_caption(lines: list[dict], i: int) -> str | None:
    """Join the anchor line with following continuation lines (a caption often
    wraps). Stop at a paragraph gap or the next figure label."""
    parts = [_clean_caption(lines[i].get("text"))]
    prev_bottom = lines[i].get("bottom", 0)
    line_h = (lines[i].get("bottom", 0) - lines[i].get("top", 0)) or 12
    for ln in lines[i + 1:]:
        text = _clean_caption(ln.get("text"))
        if not text or FIGURE_LABEL_RX.match(text):
            break
        if ln.get("top", 0) - prev_bottom > line_h * 1.2:
            break                               # paragraph gap → caption ended
        parts.append(text)
        prev_bottom = ln.get("bottom", 0)
    joined = " ".join(p for p in parts if p)
    return joined or None


def _extract_pdf_assets_pdfplumber(path: Path, source_id: str | None) -> None:
    """Enumerate figure bboxes per PDF page, rasterize to PNG, write manifest."""
    import io
    import pdfplumber

    assets_dir = path.parent / (path.name + ".assets")
    _, existing_entries = read_manifest(assets_dir)
    manifest_map: dict[str, ImageEntry] = {e.sha256: e for e in existing_entries}
    refreshed_shas: set[str] = set()

    with pdfplumber.open(str(path)) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            bboxes = _figure_bboxes_pdfplumber(page)
            if not bboxes:
                continue
            try:
                text_lines = page.extract_text_lines()
            except Exception:
                text_lines = []                 # best-effort captions only
            page_w, page_h = page.width, page.height
            for bbox in bboxes:
                # Clip the bbox to the page (pdfplumber rejects out-of-page crops).
                x0 = max(0.0, bbox[0]); y0 = max(0.0, bbox[1])
                x1 = min(page_w, bbox[2]); y1 = min(page_h, bbox[3])
                if x1 - x0 < 20 or y1 - y0 < 20:
                    continue   # too small — likely noise
                clipped = (x0, y0, x1, y1)
                try:
                    img = page.crop(clipped).to_image(resolution=PDF_RASTER_DPI)
                except Exception as e:
                    print(f"extract: pdfplumber crop failed on page {page_idx} "
                          f"bbox={clipped}: {e}", file=sys.stderr)
                    continue
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                data = buf.getvalue()
                dumped = _dump_asset(assets_dir, data, "png")
                if dumped is None:
                    continue
                full_sha, target = dumped
                # Manifest entry: clear existing refs for this sha
                # exactly once per run (per-run rebuild). When creating
                # a new entry we ALSO mark refreshed so subsequent same-
                # sha occurrences don't clear the refs we're about to
                # add (codex round-3 BUG: first-run origin_refs loss on
                # duplicate bytes).
                if full_sha not in manifest_map:
                    manifest_map[full_sha] = ImageEntry(
                        file=target.name,
                        sha256=full_sha,
                        bytes=len(data),
                        dimensions=[img.original.width, img.original.height],
                        origin_refs=[],
                    )
                    refreshed_shas.add(full_sha)
                elif full_sha not in refreshed_shas:
                    manifest_map[full_sha].origin_refs = []
                    refreshed_shas.add(full_sha)
                # Round bbox to 2 decimals for stable serialization.
                rounded = [round(v, 2) for v in clipped]
                new_ref = OriginRef(kind="pdf", page=page_idx, bbox=rounded)
                seen = {r.canonical_json() for r in manifest_map[full_sha].origin_refs}
                if new_ref.canonical_json() not in seen:
                    manifest_map[full_sha].origin_refs.append(new_ref)
                # Attach the book's own figure caption if one sits by the figure.
                entry = manifest_map[full_sha]
                _apply_source_caption(entry, _pdf_caption_for(clipped, text_lines),
                                      "pdf-label")

    write_manifest(assets_dir, source_id, list(manifest_map.values()))


def _figure_bboxes_pdfplumber(page) -> list[tuple[float, float, float, float]]:
    """Return de-duplicated figure bboxes for one page.

    Sources (per plan §2.2):
      - `page.images`: embedded raster figures.
      - Clusters of `page.curves`/`page.lines`/`page.rects` not
        overlapping any embedded raster: vector figures.

    De-duplicates overlapping detections (one bbox per figure).
    Filters tiny / page-margin-sized noise.
    """
    bboxes: list[tuple[float, float, float, float]] = []
    page_w, page_h = page.width, page.height
    page_area = page_w * page_h

    # Embedded rasters: pdfplumber bbox = (x0, top, x1, bottom). Note
    # pdfplumber's coord system has top=0 at top of page.
    for im in page.images:
        x0 = im.get("x0", 0); y0 = im.get("top", 0)
        x1 = im.get("x1", 0); y1 = im.get("bottom", 0)
        if x1 - x0 < 20 or y1 - y0 < 20:
            continue
        bboxes.append((x0, y0, x1, y1))

    # Vector cluster detection: collect each shape's individual bbox
    # (filtering noise), then group overlapping shapes into clusters
    # via a connected-components walk. Each cluster's union bbox is
    # one figure candidate. The previous single-union heuristic mashed
    # a multi-figure page into one giant crop (codex / gemini /
    # subagent all flagged this in round 1).
    shape_bboxes: list[tuple[float, float, float, float]] = []
    for shape_attr in ("curves", "lines", "rects"):
        for shape in getattr(page, shape_attr, []):
            x0 = shape.get("x0"); y0 = shape.get("top")
            x1 = shape.get("x1"); y1 = shape.get("bottom")
            if None in (x0, y0, x1, y1):
                continue
            w = x1 - x0; h = y1 - y0
            # Skip very thin rules (header/footer separators, table rules).
            if w < 30 and h < 30:
                continue
            if w > page_w * 0.95 and h < 5:
                continue   # full-width hairline → page rule
            shape_bboxes.append((x0, y0, x1, y1))

    for cluster_bbox in _cluster_overlapping_bboxes(shape_bboxes,
                                                    expand=10.0):
        cw = cluster_bbox[2] - cluster_bbox[0]
        ch = cluster_bbox[3] - cluster_bbox[1]
        if cw < 30 or ch < 30:
            continue
        c_area = cw * ch
        # Reject clusters that span essentially the whole page.
        if c_area / max(page_area, 1) >= 0.85:
            continue
        # Reject clusters that overlap an embedded raster heavily —
        # the raster IS the figure; don't double-count.
        if any(_bbox_iou(cluster_bbox, b) > 0.5 for b in bboxes):
            continue
        bboxes.append(cluster_bbox)

    return _dedupe_bboxes(bboxes)


def _cluster_overlapping_bboxes(
        items: list[tuple[float, float, float, float]],
        expand: float = 0.0,
        ) -> list[tuple[float, float, float, float]]:
    """Group overlapping bboxes into connected components.

    Two bboxes are connected if their (optionally `expand`-padded)
    rects intersect. `expand` lets nearby-but-not-quite-touching
    shapes (e.g. axis label vs tick mark) cluster together.

    Returns one union bbox per connected component.
    """
    if not items:
        return []
    n = len(items)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    def overlap_or_touch(a, b) -> bool:
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        return not (
            ax1 + expand < bx0 - expand
            or bx1 + expand < ax0 - expand
            or ay1 + expand < by0 - expand
            or by1 + expand < ay0 - expand
        )

    # O(n²) — fine for typical page shape counts (a few hundred max).
    for i in range(n):
        for j in range(i + 1, n):
            if overlap_or_touch(items[i], items[j]):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    out: list[tuple[float, float, float, float]] = []
    for idxs in groups.values():
        xs0 = min(items[k][0] for k in idxs)
        ys0 = min(items[k][1] for k in idxs)
        xs1 = max(items[k][2] for k in idxs)
        ys1 = max(items[k][3] for k in idxs)
        out.append((xs0, ys0, xs1, ys1))
    return out


def _extract_web_assets_from_dom(body, assets_dir: Path,
                                 source_id: str | None,
                                 base_url: str | None) -> None:
    """Walk the DOM for image-bearing elements and download each external URL.

    Per plan §2.3:
      - Walks <img>/<image>/<picture><source srcset>/SVG <image href>
        via _iter_image_srcs.
      - Resolves relative URLs against `base_url` (skipped if base_url
        is None and the URL is relative).
      - HTTP fetch with --fail / --max-filesize / --user-agent /
        --referer behavior, retry once on transient 5xx/timeout.
      - Sniffs bytes to detect actual mime (PNG/JPEG/GIF/WebP/SVG),
        rejects others (HTML error pages disguised as 200).
      - Per-page download cap of WEB_IMAGE_CAP (20).
      - Skips data URIs, 1×1 tracking pixels, ≤2px dimensions,
        fully-transparent images, and divider-aspect images.

    On any download failure, log + skip; never raise. Manifest entries
    are added only for successful downloads.
    """
    import requests
    from urllib.parse import urljoin, urlparse

    _, existing_entries = read_manifest(assets_dir)
    manifest_map: dict[str, ImageEntry] = {e.sha256: e for e in existing_entries}
    refreshed_shas: set[str] = set()

    session = requests.Session()
    session.headers.update({"User-Agent": WEB_USER_AGENT})
    if base_url:
        session.headers["Referer"] = base_url

    seen_urls: set[str] = set()
    downloaded = 0

    for tag, raw_src in _iter_image_srcs(body):
        if downloaded >= WEB_IMAGE_CAP:
            print(f"extract: web image cap reached ({WEB_IMAGE_CAP}); "
                  "stopping further downloads", file=sys.stderr)
            break
        src = raw_src.strip()
        if not src or src.startswith("data:"):
            continue
        # Protocol-relative URL like `//cdn.example.com/img.png` —
        # urlparse(src).netloc is set but scheme is missing. urljoin
        # against the base_url (or default to https) supplies the
        # scheme. Without this branch, the next "starts with http" check
        # rejects them.
        if src.startswith("//"):
            if base_url:
                src = urljoin(base_url, src)
            else:
                src = "https:" + src
        # Resolve against base_url if still relative.
        elif base_url and not urlparse(src).netloc:
            src = urljoin(base_url, src)
        if not src.startswith(("http://", "https://")):
            print(f"extract: relative URL with no base_url; skipping {raw_src!r}",
                  file=sys.stderr)
            continue
        if src in seen_urls:
            continue
        seen_urls.add(src)

        data = _download_image(session, src, timeout=WEB_TIMEOUT,
                               max_filesize=WEB_MAX_FILESIZE)
        if data is None:
            continue
        ext = _sniff_image_ext(data)
        if ext is None:
            print(f"extract: web {src} returned non-image bytes; skipping",
                  file=sys.stderr)
            continue
        # Reject obvious tracking pixels / spacers.
        dims = _image_dimensions(data, ext) or [0, 0]
        if dims[0] and dims[1]:
            if dims[0] <= 2 or dims[1] <= 2:
                continue   # 1×1 / 2×2 trackers
            ratio = max(dims) / max(min(dims), 1)
            if ratio > 10 and len(data) < 5_000:
                continue   # likely a divider/spacer

        dumped = _dump_asset(assets_dir, data, ext)
        if dumped is None:
            continue
        full_sha, target = dumped
        if full_sha not in manifest_map:
            manifest_map[full_sha] = ImageEntry(
                file=target.name,
                sha256=full_sha,
                bytes=len(data),
                dimensions=dims,
                origin_refs=[],
            )
            refreshed_shas.add(full_sha)
        elif full_sha not in refreshed_shas:
            manifest_map[full_sha].origin_refs = []
            refreshed_shas.add(full_sha)
        new_ref = OriginRef(kind="web", url=src)
        seen = {r.canonical_json() for r in manifest_map[full_sha].origin_refs}
        if new_ref.canonical_json() not in seen:
            manifest_map[full_sha].origin_refs.append(new_ref)
        _apply_source_caption(manifest_map[full_sha], _embedded_caption(tag), "embedded")
        downloaded += 1

    write_manifest(assets_dir, source_id, list(manifest_map.values()))


def _download_image(session, url: str, *, timeout: int,
                    max_filesize: int) -> bytes | None:
    """Download an image URL, with retry on transient errors. Returns bytes or None."""
    import requests
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            with session.get(url, timeout=timeout, stream=True) as resp:
                if resp.status_code >= 400:
                    if 500 <= resp.status_code < 600 and attempt == 1:
                        continue   # retry once on 5xx
                    print(f"extract: HTTP {resp.status_code} for {url}; skip",
                          file=sys.stderr)
                    return None
                # Stream-read with a max-bytes guard.
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_filesize:
                        print(f"extract: {url} exceeded max_filesize={max_filesize}; "
                              "skipping", file=sys.stderr)
                        return None
                return b"".join(chunks)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            if attempt == 1:
                continue
        except requests.RequestException as e:
            last_err = e
            break
    if last_err is not None:
        print(f"extract: download failed for {url}: {last_err}", file=sys.stderr)
    return None


def extract_html_file(path: Path, *, write_assets: bool = False,
                      source_id: str | None = None,
                      base_url: str | None = None,
                      assets_dir: Path | None = None) -> str:
    """Extract markdown text from an HTML file.

    When `write_assets=True`, also walks the DOM for `<img>` /
    `<picture>` / `<source srcset>` / SVG `<image>` tags, downloads
    each external image (per plan §2.3), and builds the manifest.
    `base_url` is needed for resolving relative URLs (e.g. `/img/foo.png`);
    when unset, relative URLs are logged + skipped.
    """
    html = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else path.stem)
    body = soup.body or soup

    if write_assets:
        assets_dir = assets_dir or path.parent / (path.name + ".assets")
        _extract_web_assets_from_dom(body, assets_dir, source_id, base_url)
        # Strip image-bearing tags before markdownify so SOURCE_TEXT
        # stays text-only (Phase 1-3 invariant).
        for tag in body.find_all(["img", "image", "picture"]):
            tag.decompose()

    md = _demote_headings(_html_to_md(str(body)), levels=2)
    return f"## {title}\n\n{md}\n"


def extract_mobi(path: Path, *, write_assets: bool = False,
                 source_id: str | None = None) -> str:
    """Convert MOBI/AZW via KindleUnpack, then reuse EPUB/HTML extraction.

    The `mobi` package returns a temporary converted file. Assets must be
    written beside the original source path, not beside that temporary file,
    because the temporary directory is removed before the ingest step finishes.
    """
    try:
        import mobi
    except Exception as e:
        print(f"extract: mobi support requires the `mobi` package: {e}",
              file=sys.stderr)
        sys.exit(3)

    tempdir: str | None = None
    try:
        tempdir, filepath = mobi.extract(str(path))
        converted = Path(filepath)
        ext = converted.suffix.lower()
        assets_dir = path.parent / (path.name + ".assets")
        if ext == ".epub":
            return extract_epub(converted, write_assets=write_assets,
                                source_id=source_id, assets_dir=assets_dir)
        if ext in (".html", ".htm", ".xhtml"):
            return extract_html_file(converted, write_assets=write_assets,
                                     source_id=source_id, assets_dir=assets_dir)
        if ext == ".pdf":
            print("extract: MOBI Print Replica converted to PDF; "
                  "PDF delegation is not supported for MOBI in v1",
                  file=sys.stderr)
            sys.exit(3)
        print(f"extract: MOBI converted to unsupported file type {ext or '(none)'}",
              file=sys.stderr)
        sys.exit(3)
    finally:
        if tempdir:
            shutil.rmtree(tempdir, ignore_errors=True)


def extract_url(url: str, *, write_assets: bool = False,
                source_id: str | None = None,
                assets_dir: Path | None = None) -> str:
    """Extract markdown text from a live URL via trafilatura.

    When `write_assets=True`, also walks the fetched HTML's DOM for
    image tags, downloads each, and writes the manifest under
    `assets_dir`. Caller must supply `assets_dir` because there's no
    obvious local path for a URL source.
    """
    import trafilatura

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        print(f"extract: failed to fetch {url}", file=sys.stderr)
        sys.exit(2)

    if write_assets and assets_dir is not None:
        soup_for_assets = BeautifulSoup(downloaded, "html.parser")
        body_for_assets = soup_for_assets.body or soup_for_assets
        _extract_web_assets_from_dom(body_for_assets, assets_dir,
                                     source_id, base_url=url)

    extracted = trafilatura.extract(
        downloaded,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    if not extracted:
        soup = BeautifulSoup(downloaded, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer"]):
            tag.decompose()
        extracted = _html_to_md(str(soup.body or soup))

    title = ""
    try:
        meta = trafilatura.extract_metadata(downloaded)
        if meta and meta.title:
            title = meta.title
    except Exception:
        pass
    if not title:
        title = urlparse(url).netloc + urlparse(url).path

    return f"## {title}\n\n{extracted}\n"


def extract_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def dispatch(source: str, *, write_assets: bool = False,
             source_id: str | None = None,
             base_url: str | None = None) -> str:
    if re.match(r"^https?://", source):
        # For live URLs, the assets dir would have to be supplied
        # externally; default behavior keeps URL ingestion simple.
        if write_assets:
            print("extract: --write-assets for live URL sources requires "
                  "the caller to materialize a local snapshot first; "
                  "use ingest.py which does this. Skipping image extraction.",
                  file=sys.stderr)
        return extract_url(source)

    path = Path(source).expanduser().resolve()
    if not path.is_file():
        print(f"extract: not a file: {path}", file=sys.stderr)
        sys.exit(2)

    if write_assets and source_id is None:
        # Try to read source_id from the sidecar.
        source_id = _read_source_id(path)

    ext = path.suffix.lower()
    if ext == ".epub":
        return extract_epub(path, write_assets=write_assets, source_id=source_id)
    if ext in (".mobi", ".azw", ".azw3"):
        return extract_mobi(path, write_assets=write_assets, source_id=source_id)
    if ext == ".pdf":
        return extract_pdf(path, write_assets=write_assets, source_id=source_id)
    if ext in (".html", ".htm", ".xhtml"):
        return extract_html_file(path, write_assets=write_assets,
                                 source_id=source_id, base_url=base_url)
    if ext in (".txt", ".md", ".markdown", ".rst", ""):
        return extract_text_file(path)

    try:
        return extract_text_file(path)
    except UnicodeDecodeError:
        print(f"extract: don't know how to handle {ext}", file=sys.stderr)
        sys.exit(3)


def filter_sections(text: str, pattern: str) -> str:
    rx = re.compile(pattern)
    lines = text.split("\n")
    out: list[str] = []
    keep = False
    for line in lines:
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            keep = bool(rx.search(m.group(1)))
            if keep:
                out.append(line)
        else:
            if keep:
                out.append(line)
    return "\n".join(out).strip() + "\n"


def list_sections(text: str) -> str:
    titles: list[str] = []
    for line in text.split("\n"):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            titles.append(m.group(1))
    return "\n".join(titles) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract text from a source.")
    ap.add_argument("source", help="Path to file, or http(s) URL")
    ap.add_argument("--section", help="Keep only sections whose ## heading matches this regex")
    ap.add_argument("--limit", type=int, default=0, help="Trim to at most N UTF-8 characters")
    ap.add_argument("--list-sections", action="store_true", help="Print section titles and exit")
    ap.add_argument("--write-assets", action="store_true",
                    help="Also dump embedded images to <source>.assets/ and write _manifest.md "
                         "(EPUB/MOBI/PDF files, plus web images from HTML snapshots).")
    ap.add_argument("--base-url", default=None,
                    help="Base URL for resolving relative <img src> in HTML "
                         "files (e.g. when the source is a saved snapshot of "
                         "a web page). Required for sites that use root-relative "
                         "paths like /img/foo.png.")
    args = ap.parse_args()

    text = dispatch(args.source, write_assets=args.write_assets,
                    base_url=args.base_url)

    if args.list_sections:
        sys.stdout.write(list_sections(text))
        return 0

    if args.section:
        text = filter_sections(text, args.section)

    if args.limit and len(text) > args.limit:
        text = text[: args.limit] + "\n\n[... truncated at %d chars ...]\n" % args.limit

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
