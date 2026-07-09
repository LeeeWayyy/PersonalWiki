#!/usr/bin/env python3
"""build-blocks.py — extract readable source documents into the Source-Reader
block contract (docs/SOURCE-READER-DESIGN.md).

For each committed source in vault/sources/ that is an epub, mobi/azw, pdf,
markdown, or plain text file, emit
vault/.blocks/<source_id>.blocks.json:

  { source_id, title, lang, blocks: [
    { id, type, section_id, section, order, text, page?, prev, next } ] }

- Block id hashes ONLY type + normalized text (position-independent).
- section_id is non-positional (hash of the chapter heading text).
- Chapter membership = the most recent `第N章`-style heading while walking the
  epub spine; sub-headings (第N节, …) are kept as `type:heading` blocks under it.

Full-text guard: on a PUBLIC build (PW_PUBLIC_BUILD=1) this refuses to emit book
text unless PW_ALLOW_FULL_TEXT=1. Local builds are unaffected.

Run after `sync` (blocks live under the regenerated vault/, keyed by source_id).
No third-party deps for epub/markdown; mobi/azw and pdf use mobi/pypdf if
available.
"""
from __future__ import annotations
import os, re, sys, json, html, hashlib, zipfile, posixpath, shutil
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
VAULT = Path(os.environ.get("PW_VAULT") or (ROOT / "vault"))
SOURCES = VAULT / "sources"
OUT = VAULT / ".blocks"
# Figure images extracted from documents are served statically from public/. They
# are book content, so they're only written on a local (non-public) build — the
# PUBLIC guard in main() returns before any extraction runs. public/vault-assets
# is gitignored, so figures never enter git history.
PUBLIC_EPUB = ROOT / "public" / "vault-assets" / "_epub"
MIN_FIG_W = 200  # px; smaller <img> are inline footnote/decoration markers, not figures

PUBLIC = os.environ.get("PW_PUBLIC_BUILD") == "1"
ALLOW_FULL = os.environ.get("PW_ALLOW_FULL_TEXT") == "1"

CHAP_RE = re.compile(r"^第[〇零一二三四五六七八九十百千两\d]+章")
HEADING_TAGS = {"h1", "h2", "h3", "h4"}
PARA_TAGS = {"p", "li", "blockquote", "dd"}
SKIP_DOC = re.compile(r"(cover|toc|nav|copyright|title)", re.I)

_sha8 = lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]
_norm = lambda t: re.sub(r"\s+", "", t or "").strip()


def parse_frontmatter(raw: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", raw, re.S)
    if not m:
        return {}
    d = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"^(\w+):\s*(.*)$", line)
        if mm:
            d[mm.group(1)] = mm.group(2).strip().strip("'\"")
    return d


def strip_frontmatter(raw: str) -> str:
    return re.sub(r"^---\n.*?\n---\n?", "", raw, count=1, flags=re.S)


def clean_markdown_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"^\s{0,3}>\s?", "", text)
    text = re.sub(r"^\s{0,3}(?:[-+*]|\d+[.)])\s+", "", text)
    if re.match(r"^\s*\|", text) or re.search(r"\s\|\s", text):
        text = text.strip().strip("|").replace("|", " · ")
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[*_~]{1,3}", "", text)
    return re.sub(r"\s+", " ", text).strip()


class Blocks(HTMLParser):
    """Collect entries in reading order: ('heading'|'paragraph', text) for text
    blocks and ('image', src, alt) for figure-sized images."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buf = None; self.tag = None; self.depth = 0; self.out = []
    def _img(self, attrs):
        d = dict(attrs)
        w = d.get("width")
        try:
            w = int(re.sub(r"\D", "", w)) if w else None
        except ValueError:
            w = None
        if w is not None and w < MIN_FIG_W:
            return  # inline footnote marker / decoration, not a figure
        src = d.get("src")
        if src:
            self.out.append(("image", src, (d.get("alt") or "").strip()))
    def handle_startendtag(self, t, a):
        if t == "img":
            self._img(a)
    def handle_starttag(self, t, a):
        if t == "img":
            self._img(a); return
        if self.buf is not None:
            if t == self.tag:
                self.depth += 1
            return
        if t in HEADING_TAGS or t in PARA_TAGS:
            self.buf = []; self.tag = t; self.depth = 1
    def handle_data(self, d):
        if self.buf is not None:
            self.buf.append(d)
    def handle_endtag(self, t):
        if self.buf is None or t != self.tag:
            return
        self.depth -= 1
        if self.depth <= 0:
            txt = re.sub(r"[ \t　]+", " ", "".join(self.buf)).strip()
            if txt:
                self.out.append(("heading" if self.tag in HEADING_TAGS else "paragraph", txt))
            self.buf = None; self.tag = None


def epub_docs(zf: zipfile.ZipFile):
    names = zf.namelist()
    opf = next((n for n in names if n.endswith(".opf")), None)
    if not opf:
        return []
    txt = zf.read(opf).decode("utf-8", "replace")
    ids = {}
    for m in re.finditer(r"<item\b[^>]*>", txt):   # attribute order-independent
        tag = m.group(0)
        i = re.search(r'\bid="([^"]+)"', tag)
        h = re.search(r'\bhref="([^"]+)"', tag)
        if i and h:
            ids[i.group(1)] = h.group(1)
    spine = re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', txt)
    base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
    return [base + unquote(ids[i]) for i in spine if i in ids]


def _write_image(zf: zipfile.ZipFile, doc: str, href: str, sid: str) -> str | None:
    """Extract one image from the epub zip into public/ and return its served URL."""
    ref = unquote(href.split("#")[0].split("?")[0])
    zpath = posixpath.normpath(posixpath.join(posixpath.dirname(doc), ref))
    try:
        data = zf.read(zpath)
    except KeyError:
        cand = next((n for n in zf.namelist() if n.endswith("/" + posixpath.basename(zpath))
                     or n == posixpath.basename(zpath)), None)
        if not cand:
            return None
        data = zf.read(cand); zpath = cand
    ext = posixpath.splitext(zpath)[1].lower() or ".img"
    h = _sha8("IMG\x1f" + zpath)
    outdir = PUBLIC_EPUB / sid
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"{h}{ext}").write_bytes(data)
    return f"/vault-assets/_epub/{sid}/{h}{ext}"


def extract_epub(path: Path, sid: str) -> list[dict]:
    zf = zipfile.ZipFile(path)
    raw_blocks = []            # (kind, text, section[, page, src])
    current = None
    for doc in epub_docs(zf):
        if SKIP_DOC.search(doc):
            continue
        try:
            html_txt = zf.read(doc).decode("utf-8", "replace")
        except KeyError:
            continue
        p = Blocks(); p.feed(html_txt)
        for entry in p.out:
            if entry[0] == "image":
                _, src, alt = entry
                url = _write_image(zf, doc, src, sid)
                if url:
                    raw_blocks.append(("image", alt, current or "", None, url))
                continue
            kind, txt = entry
            if kind == "heading":
                if CHAP_RE.match(txt):
                    current = txt
                elif current is None:
                    current = txt          # front matter heading
                raw_blocks.append((kind, txt, current or txt))
            else:
                raw_blocks.append((kind, txt, current or ""))
    return raw_blocks


def extract_pdf(path: Path) -> list[dict]:
    try:
        import pypdf
    except Exception:  # noqa
        return []
    r = pypdf.PdfReader(str(path))
    raw = []
    for i, page in enumerate(r.pages, 1):
        txt = page.extract_text() or ""
        for para in re.split(r"\n\s*\n", txt):
            para = re.sub(r"\s+", " ", para).strip()
            if len(para) > 1:
                raw.append(("paragraph", para, f"p{i}", i))
    return raw


def extract_html(path: Path) -> list[dict]:
    p = Blocks()
    p.feed(path.read_text(encoding="utf-8", errors="replace"))
    raw = []
    current = None
    for entry in p.out:
        if entry[0] == "image":
            continue  # MOBI6 HTML reader path is text-only in v1.
        kind, txt = entry
        if kind == "heading":
            if CHAP_RE.match(txt):
                current = txt
            elif current is None:
                current = txt
            raw.append((kind, txt, current or txt))
        else:
            raw.append((kind, txt, current or ""))
    return raw


def extract_mobi(asset: Path, sid: str) -> list[dict]:
    try:
        import mobi
    except Exception:  # noqa
        return []

    tempdir: str | None = None
    try:
        tempdir, filepath = mobi.extract(str(asset))
        converted = Path(filepath)
        ext = converted.suffix.lower()
        if ext == ".epub":
            return extract_epub(converted, sid)
        if ext in {".html", ".htm", ".xhtml"}:
            return extract_html(converted)
        if ext == ".pdf":
            print(f"build-blocks: {asset.name}: MOBI Print Replica converted to PDF; skipping")
            return []
        print(f"build-blocks: {asset.name}: MOBI converted to unsupported file type {ext or '(none)'}")
        return []
    finally:
        if tempdir:
            shutil.rmtree(tempdir, ignore_errors=True)


def extract_markdown(path: Path) -> list[dict]:
    raw_text = strip_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    raw = []
    current = ""
    para: list[str] = []
    in_fence = False

    def flush_para():
        nonlocal para
        if not para:
            return
        text = clean_markdown_text(" ".join(para))
        if text:
            raw.append(("paragraph", text, current))
        para = []

    for line in raw_text.splitlines():
        stripped = line.strip()
        if re.match(r"^(```|~~~)", stripped):
            flush_para()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not stripped:
            flush_para()
            continue
        if re.match(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", stripped):
            continue
        hm = re.match(r"^\s{0,3}(#{1,4})\s+(.+?)\s*#*\s*$", line)
        if hm:
            flush_para()
            text = clean_markdown_text(hm.group(2))
            if text:
                current = text
                raw.append(("heading", text, current))
            continue
        para.append(stripped)
    flush_para()
    return raw


def to_blocks(raw) -> list[dict]:
    blocks = []
    order = 0
    for item in raw:
        kind, text, section = item[0], item[1], item[2]
        page = item[3] if len(item) > 3 else None
        src = item[4] if len(item) > 4 else None
        section_id = "s-" + _sha8("SEC\x1f" + _norm(section))
        if kind == "image":
            b = {"id": "i-" + _sha8("image\x1f" + (src or text)), "type": "image",
                 "section_id": section_id, "section": section, "order": order,
                 "text": text, "src": src}
        else:
            pfx = "h-" if kind == "heading" else "p-"
            b = {"id": pfx + _sha8(kind + "\x1f" + _norm(text)), "type": kind,
                 "section_id": section_id, "section": section, "order": order, "text": text}
        if page is not None:
            b["page"] = page
        blocks.append(b); order += 1
    for i, b in enumerate(blocks):
        b["prev"] = blocks[i - 1]["id"] if i > 0 else ""
        b["next"] = blocks[i + 1]["id"] if i < len(blocks) - 1 else ""
    return blocks


def main() -> int:
    if not SOURCES.exists():
        print("build-blocks: no vault/sources — nothing to do"); return 0
    if PUBLIC and not ALLOW_FULL:
        print("build-blocks: PUBLIC build without PW_ALLOW_FULL_TEXT — skipping full-text extraction"); return 0
    OUT.mkdir(parents=True, exist_ok=True)
    made = 0
    for sidecar in sorted(SOURCES.glob("*.md")):
        data = parse_frontmatter(sidecar.read_text(encoding="utf-8", errors="replace"))
        sid = data.get("source_id")
        asset = sidecar.with_suffix("")  # <file>.md → <file>
        if not sid or not asset.exists():
            continue
        ext = asset.suffix.lower()
        try:
            if ext == ".epub":
                raw = extract_epub(asset, sid)
            elif ext in {".mobi", ".azw3", ".azw"}:
                raw = extract_mobi(asset, sid)
            elif ext == ".pdf":
                raw = extract_pdf(asset)
            elif ext in {".md", ".markdown", ".txt"}:
                raw = extract_markdown(asset)
            else:
                raw = []
        except Exception as e:  # noqa
            print(f"build-blocks: {asset.name}: extract failed ({e})"); continue
        blocks = to_blocks(raw)
        if not blocks:
            continue
        doc = {"source_id": sid, "title": data.get("title", sidecar.stem),
               "lang": data.get("lang", "zh" if ext in {".epub", ".mobi", ".azw3", ".azw"} else ""), "blocks": blocks}
        (OUT / f"{sid}.blocks.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        made += 1
        print(f"build-blocks: {asset.name} → {len(blocks)} blocks ({sid})")
    if not made:
        print("build-blocks: no readable source files to extract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
