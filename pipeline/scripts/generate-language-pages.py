#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
#     "fugashi",
#     "unidic-lite",
# ]
# ///
"""
Generate ONE interactive HTML reading page per ingested Japanese source.

Part of the `--profile lang` ingest path (writes under `content/lang/`, fully
isolated from the wiki). Per source:

  1. fugashi (+ unidic-lite) tokenizes each chapter → content words, each with
     its lemma, lemma-reading (語彙素読み, normalized katakana→hiragana for
     furigana) and coarse POS. Dedup key = (lemma, lForm, pos1) — the stable
     lexeme identity. (unidic-lite does not context-disambiguate homographs, so
     this currently behaves like (lemma, pos1); the key future-proofs a swap to
     full UniDic. Documented limitation.)
  2. Text is split into sentences deterministically (。！？). The LLM writes a
     per-sentence English translation, an English gloss for each content word,
     and grammar points (each optionally anchored to a sentence). fugashi
     readings are authoritative and never overridden. Cached under
     `.wiki/lang-cache/` keyed by source sha + chapter + prompt version.

Then ONE deterministic render: `_reading/<slug>.html` — a self-contained page
(inline CSS+JS, no external deps) showing the full original text in order with
native <ruby> furigana, per-sentence English, click-a-word → meaning, click-a-
sentence → grammar, and first-occurrence ("new word") highlighting. Being date-
free it re-renders byte-identically from cache, so a no-change re-run is a no-op.

The log (`.wiki/log.md`) is appended AFTER rendering succeeds, one idempotent
line per chapter, so a render crash leaves no dirty log. The last stdout line
is `MANIFEST:<json>` — the relative paths ingest must stage.

Run (normally invoked by ingest.py --profile lang):
    VAULT_CONTENT_DIR=content/lang generate-language-pages.py --source-id <ULID>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import fugashi

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _util import default_vault_root  # noqa: E402
import derived_lib as dl  # noqa: E402

TOOLING_ROOT = Path(__file__).resolve().parent.parent
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
READING_DIR = VAULT_ROOT / "_reading"
SOURCES_DIR = VAULT_ROOT / "sources"
LOG_PATH = VAULT_ROOT / ".wiki" / "log.md"
CACHE_DIR = VAULT_ROOT / ".wiki" / "lang-cache"
EXTRACT = TOOLING_ROOT / "scripts" / "extract.py"

PROMPT_VERSION = "v4"  # v4: per-sentence translation target = PW_TRANSLATE_LANG
TARGET_LANG = os.environ.get("PW_TRANSLATE_LANG", "Simplified Chinese")
SOURCE_CHAR_LIMIT = 300_000
LLM_TIMEOUT_S = 900
WHOLE_LABEL = "whole"  # synthetic chapter for heading-less assets (transcripts)

# fugashi pos1 values to DROP (function words, symbols, whitespace). Everything
# else (名詞 nouns, 動詞 verbs, 形容詞/形状詞 adjectives, 副詞 adverbs,
# 連体詞, 感動詞, 接続詞, 代名詞 …) is a content word.
DROP_POS1 = {"助詞", "助動詞", "補助記号", "記号", "空白"}

CJK_RX = re.compile(r"[㐀-鿿豈-﫿\U00020000-\U0002ffff]")

_TAGGER: fugashi.Tagger | None = None


def tagger() -> fugashi.Tagger:
    global _TAGGER
    if _TAGGER is None:
        _TAGGER = fugashi.Tagger()  # auto-wires unidic-lite
    return _TAGGER


def kata2hira(s: str) -> str:
    """Katakana → hiragana for furigana display (ー and non-kana pass through)."""
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def has_kanji(s: str) -> bool:
    return bool(CJK_RX.search(s))


def chapter_key(raw_title: str) -> str:
    """Canonical chapter identity used EVERYWHERE — render slug, log `#chapter`
    token, citation anchor, and the idempotency dedup key — so a metachar/space
    heading can't render one page but log/dedupe another. Besides collapsing
    whitespace, it strips the ASCII chars that would break the structured
    contexts the key is embedded in: `[ ] #` (citation anchor `[src:#<key>]`),
    `|` (markdown table cell), and `:` (the `…#<key>  pages:` log delimiter, via
    `LOG_LINE_RX`). CJK and ordinary text are unaffected."""
    cleaned = re.sub(r"[\[\]#|:]", " ", raw_title)
    return re.sub(r"\s+", " ", cleaned).strip() or WHOLE_LABEL


def token_key(f, surface: str) -> str:
    """Composite lexeme key (lemma, lForm, pos1) joined on U+001F — matches the
    dedup key content_words builds, so an inline token can look up its gloss."""
    lemma = (f.lemma or surface or "").strip()
    lform = f.lForm or f.kana or ""
    return "\x1f".join((lemma, lform, f.pos1 or ""))


def content_words(text: str) -> list[dict]:
    """Tokenize a chapter → deduped content words in first-appearance order.
    Each: {key, lemma, reading, pos}. key = (lemma, lForm, pos1)."""
    seen: dict[str, dict] = {}
    for w in tagger()(text):
        f = w.feature
        pos1 = f.pos1 or ""
        if pos1 in DROP_POS1:
            continue
        lemma = (f.lemma or w.surface or "").strip()
        if not lemma:
            continue
        # Drop tokens with no letter — bare numerals (ASCII "1" OR fullwidth
        # "６"), punctuation, symbols — while keeping ASCII words (DNA/ATP) and
        # all kana/kanji (which are .isalpha()). This is the fullwidth-numeral fix.
        if not any(ch.isalpha() for ch in lemma):
            continue
        key = token_key(f, w.surface)
        if key in seen:
            continue
        seen[key] = {
            "key": key,
            "lemma": lemma,
            "reading": kata2hira(f.lForm or f.kana or ""),
            "pos": pos1,
        }
    return list(seen.values())


def join_wrapped(lines: list[str]) -> str:
    """Join hard-wrapped source lines into one paragraph WITHOUT inserting a
    space between CJK characters (Japanese has no inter-word spaces — collapsing
    newlines to spaces would inject spurious mid-sentence spaces). A space is
    added only between two ASCII word chars (a genuine English word boundary)."""
    out = ""
    for raw in lines:
        ln = raw.strip().lstrip("　")  # drop full-width paragraph indent
        if not ln:
            continue
        if not out:
            out = ln
            continue
        a, b = out[-1], ln[0]
        if a.isascii() and a.isalnum() and b.isascii() and b.isalnum():
            out += " " + ln
        else:
            out += ln
    return out


def paragraphs_of(text: str) -> list[str]:
    """Blank-line-separated paragraphs, each with its wrapped lines re-joined."""
    blocks = re.split(r"\n[ \t　]*\n", text.replace("\r\n", "\n"))
    paras = [join_wrapped(b.split("\n")).strip() for b in blocks]
    return [p for p in paras if p]


def split_sentences_in(para: str) -> list[str]:
    """Split ONE paragraph on 。！？ (keeping the ender + any trailing closing
    quote/bracket)."""
    parts = re.split(r"(?<=[。！？])(?![」』）\)】])", para)
    return [p.strip() for p in parts if p.strip()]


def split_paragraphs(text: str) -> list[list[str]]:
    """Format the raw extracted text into well-organized paragraphs, each a list
    of sentences. Deterministic → sentence numbering stays stable for the LLM
    prompt / cache. Replaces the old whitespace-collapsing splitter."""
    return [split_sentences_in(p) for p in paragraphs_of(text)]


def build_prompt(sentences: list[str], words: list[dict], title: str, chapter: str) -> str:
    sent_lines = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences, 1))
    # Number the words so the LLM keys meanings by INDEX, not lemma — two rows
    # can share a lemma (different POS/reading), so a lemma key would be
    # ambiguous; the index is 1:1 with the fugashi word list.
    lemma_lines = "\n".join(
        f"{i}. {w['lemma']}（{w['reading']}）[{w['pos']}]" for i, w in enumerate(words, 1)
    )
    return f"""You are helping a learner study Japanese. Below is a chapter of
Japanese text, split into NUMBERED sentences, plus a NUMBERED list of the
CONTENT WORDS a tokenizer extracted (with reading and part of speech — do NOT
change these). Output ONE JSON object (no prose, no markdown fences) with
exactly this shape:

{{
  "sentences": [
    {{"s": <the integer sentence number>,
      "en": "natural {TARGET_LANG} translation of that sentence"}}
  ],
  "words": [
    {{"i": <the integer index from the WORDS list>,
      "meaning_en": "concise {TARGET_LANG} gloss (1-6 words)",
      "notes": "optional short usage/nuance note, or empty string"}}
  ],
  "grammar_points": [
    {{"pattern": "the grammar pattern as it appears (e.g. 〜なければならない)",
      "explanation": "what it means / when it's used, 1-2 sentences English",
      "example_jp": "a short example sentence from or fitting the chapter",
      "s": <the sentence number this pattern appears in, or 0 if general>}}
  ]
}}

Rules:
- Translate EVERY numbered sentence, echoing its number in "s".
- Give a meaning for EVERY numbered word, echoing its index in "i". Do not
  invent indices outside the lists.
- grammar_points: 3-10 notable patterns a learner would want, anchored to the
  sentence they appear in via "s" where possible. Skip if the text is too short.
- Output ONLY the JSON object.

## SENTENCES ({title} — {chapter})
{sent_lines}

## WORDS
{lemma_lines}
"""


def validate(obj: dict, sentences: list[str], words: list[dict],
             paras: list[list[str]] | None = None) -> dict:
    """Merge LLM output onto the deterministic sentence split + fugashi word
    list (fugashi reading/pos authoritative). Sentence English merged by number,
    word meanings by index (honoring the (lemma,lForm,pos1) identity — a lemma
    key would merge distinct same-lemma lexemes). Missing entries keep empties
    rather than dropping the row. Each sentence carries its 0-based paragraph
    index `para` (for well-organized rendering)."""
    en_by_s: dict[int, str] = {}
    for s in obj.get("sentences") or []:
        if isinstance(s, dict) and isinstance(s.get("s"), int):
            en_by_s[s["s"]] = str(s.get("en") or "").strip()
    para_of: dict[int, int] = {}
    if paras:
        n = 1
        for pi, p in enumerate(paras):
            for _ in p:
                para_of[n] = pi
                n += 1
    sents = [{"jp": jp, "en": en_by_s.get(i, ""), "para": para_of.get(i, 0)}
             for i, jp in enumerate(sentences, 1)]

    by_idx: dict[int, dict] = {}
    for w in obj.get("words") or []:
        if isinstance(w, dict) and isinstance(w.get("i"), int):
            by_idx[w["i"]] = w
    merged = []
    for i, w in enumerate(words, 1):  # 1-based, matching the numbered prompt list
        llm = by_idx.get(i, {})
        merged.append({
            **w,
            "meaning_en": str(llm.get("meaning_en") or "").strip(),
            "notes": str(llm.get("notes") or "").strip(),
        })

    grammar = []
    seen_g: set[str] = set()
    for g in obj.get("grammar_points") or []:
        if not isinstance(g, dict):
            continue
        pat = str(g.get("pattern") or "").strip()
        if not pat or pat in seen_g:
            continue
        seen_g.add(pat)
        s_raw = g.get("s")
        s = s_raw if isinstance(s_raw, int) and 1 <= s_raw <= len(sentences) else 0
        grammar.append({
            "pattern": pat,
            "explanation": str(g.get("explanation") or "").strip(),
            "example_jp": str(g.get("example_jp") or "").strip(),
            "s": s,
        })
    return {"sentences": sents, "words": merged, "grammar_points": grammar}


def chapter_data(meta: dict, idx: int, chapter: str, section: str | None,
                 refresh: bool, dry_run: bool) -> dict:
    """fugashi + LLM (cached) for one chapter. Returns {sentences, words,
    grammar_points}. The cache suffix carries the chapter ORDINAL `idx` (stable:
    the source sha in the key already pins the chapter set/order), so two
    distinct chapters whose slugs collapse to the same fs-safe value never share
    a cache file."""
    sha = meta["sha256"]
    suffix = f"{idx:02d}-{dl.fs_safe_slug(chapter_key(chapter), fallback='chap')}"
    cached = None if refresh else dl.load_cache(CACHE_DIR, PROMPT_VERSION, meta["source_id"], sha, suffix)
    if cached is not None:
        return cached
    if dry_run:
        raise RuntimeError(f"chapter {chapter!r} needs LLM (no cache) — skipped under --dry-run")
    text = dl.extract_source_text(EXTRACT, meta["asset"], SOURCE_CHAR_LIMIT, section)
    if not text.strip():
        raise RuntimeError(f"chapter {chapter!r}: section matched no text (blank slice)")
    paras = split_paragraphs(text)
    sentences = [s for p in paras for s in p]  # flat for prompt/word numbering
    words = content_words(text)
    raw = dl.call_llm(build_prompt(sentences, words, meta["title"], chapter), LLM_TIMEOUT_S)
    data = validate(dl.extract_json(raw), sentences, words, paras)
    dl.save_cache(CACHE_DIR, PROMPT_VERSION, meta["source_id"], sha, data, suffix)
    return data


# ─── render ──────────────────────────────────────────────────────────────────


def cite(source_id: str, chapter: str) -> str:
    return f"[src:{source_id}#{chapter_key(chapter)}]"


def source_cite(source_id: str) -> str:
    return f"[src:{source_id}]"


def esc(s: str) -> str:
    """Escape text for HTML element content."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def att(s: str) -> str:
    """Escape for a SINGLE-quoted HTML attribute (JSON with double quotes is
    safe inside; only & < > ' need encoding)."""
    return esc(s).replace("'", "&#39;")


def render_tokens(jp: str, meanings: dict[str, dict], seen: set[str]) -> str:
    """One sentence → inline HTML: every token in order, <ruby> furigana on
    kanji-bearing tokens, content words with a gloss wrapped in a clickable
    span (first occurrence gets the `new` class)."""
    out: list[str] = []
    for w in tagger()(jp):
        f = w.feature
        surf = w.surface
        pos1 = f.pos1 or ""
        rt = kata2hira(f.kana or "") if has_kanji(surf) else ""
        ruby = f"<ruby>{esc(surf)}<rt>{esc(rt)}</rt></ruby>" if rt else esc(surf)
        m = meanings.get(token_key(f, surf))
        if m and pos1 not in DROP_POS1 and (m["meaning_en"] or m["notes"]):
            cls = "w"
            if m["key"] not in seen:
                seen.add(m["key"])
                cls = "w new"
            out.append(
                f"<span class=\"{cls}\" data-w='{att(m['lemma'])}' "
                f"data-m='{att(m['meaning_en'] or '—')}' data-n='{att(m['notes'])}'>{ruby}</span>"
            )
        else:
            out.append(ruby)
    return "".join(out)


def render_reading_html(display: str, source_id: str,
                        per_chapter: list[tuple[str, dict]],
                        meanings: dict[str, dict]) -> str:
    seen: set[str] = set()
    chapters: list[str] = []
    for chap, data in per_chapter:
        by_s: dict[int, list[dict]] = {}
        for g in data["grammar_points"]:
            by_s.setdefault(int(g.get("s") or 0), []).append(g)
        sents: list[str] = []
        for si, sent in enumerate(data["sentences"], 1):
            toks = render_tokens(sent["jp"], meanings, seen)
            g_here = by_s.get(si, [])
            en = f'<div class="en">{esc(sent["en"])}</div>' if sent.get("en") else ""
            sents.append(
                f'<div class="sent" data-g=\'{att(json.dumps(g_here, ensure_ascii=False))}\'>'
                f'<div class="jp">{toks}</div>{en}</div>'
            )
        gitems = "".join(
            f'<div class="gp"><b>{esc(g["pattern"])}</b><div>{esc(g["explanation"])}</div>'
            + (f'<div class="ex">{esc(g["example_jp"])}</div>' if g["example_jp"] else "")
            + "</div>"
            for g in data["grammar_points"]
        )
        details = (f'<details class="gram"><summary>文法 · Grammar '
                   f'({len(data["grammar_points"])})</summary>{gitems}</details>'
                   if data["grammar_points"] else "")
        chapters.append(
            f'<section class="chap"><h2>{esc(chap)}</h2>\n'
            f'<!-- {cite(source_id, chap)} -->\n'
            + "".join(sents) + details + "</section>"
        )
    return (PAGE
            .replace("__TITLE__", esc(display))
            .replace("__SRC__", esc(source_cite(source_id)))
            .replace("__CHAPTERS__", "\n".join(chapters)))


# Self-contained reading page. __TITLE__/__SRC__/__CHAPTERS__ are substituted;
# CSS/JS use literal braces so this stays a plain string (no .format).
PAGE = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<!-- __SRC__ -->
<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font-family: "Hiragino Mincho ProN", "Yu Mincho", serif;
  line-height: 2.1; color: #1a1a1a; background: #fbfaf7; }
main { max-width: 46rem; margin: 0 auto; padding: 2rem 1.2rem 8rem; }
h1 { font-size: 1.5rem; line-height: 1.4; margin: 0 0 .3rem; }
.hint { font-family: system-ui, sans-serif; font-size: .8rem; color: #8a8578;
  margin-bottom: 2rem; }
.chap { margin: 2.5rem 0; }
.chap h2 { font-size: 1.15rem; border-bottom: 1px solid #e6e1d5; padding-bottom: .3rem; }
.sent { padding: .5rem .6rem; margin: .1rem -.6rem; border-radius: .4rem; cursor: pointer; }
.sent:hover { background: #f2efe6; }
.jp { font-size: 1.35rem; }
ruby rt { font-size: .55em; color: #9a8f78; font-family: system-ui, sans-serif; font-weight: 400; }
.w { cursor: pointer; border-bottom: 1px dotted #c3b9a3; }
.w:hover { background: #efe7d2; }
.w.new { background: #fff2b8; border-bottom-color: #d9c25a; }
.en { font-family: system-ui, sans-serif; font-size: .9rem; color: #6b6455;
  line-height: 1.5; margin-top: .15rem; }
.gram { font-family: system-ui, sans-serif; font-size: .9rem; margin-top: 1.2rem;
  background: #f4f1e8; border-radius: .5rem; padding: .3rem .9rem; }
.gram summary { cursor: pointer; color: #6b6455; }
.gp { padding: .5rem 0; border-top: 1px solid #e6e1d5; }
.gp .ex { color: #6b6455; margin-top: .2rem; }
#p { position: fixed; left: 0; right: 0; bottom: 0; background: #2b2823; color: #f4f1e8;
  font-family: system-ui, sans-serif; font-size: 1rem; line-height: 1.5;
  padding: 1rem 1.2rem; transform: translateY(110%); transition: transform .18s;
  box-shadow: 0 -4px 20px rgba(0,0,0,.25); max-height: 45vh; overflow: auto; }
#p.on { transform: none; }
#p b { font-size: 1.2rem; }
#p .nt, #p .ex { color: #c9c2b2; font-size: .9rem; margin-top: .3rem; }
#p hr { border: none; border-top: 1px solid #4a463d; margin: .6rem 0; }
@media (prefers-color-scheme: dark) {
  body { color: #e8e4da; background: #16150f; }
  .chap h2 { border-color: #35322a; }
  .sent:hover { background: #23211a; }
  ruby rt { color: #8f866f; }
  .w { border-bottom-color: #55503f; }
  .w:hover { background: #2c2920; }
  .w.new { background: #4a4218; border-bottom-color: #8a7a2e; color: #fdf6d8; }
  .en, .gram summary, .gp .ex { color: #a49c88; }
  .gram { background: #201e17; }
  .gp { border-color: #35322a; }
}
</style></head>
<body><main>
<h1>__TITLE__</h1>
<div class="hint">Tap a <b>word</b> for its meaning · tap a <b>sentence</b> for grammar · <span style="background:#fff2b8;color:#000">highlighted</span> = first time it appears</div>
__CHAPTERS__
</main>
<div id="p"><div id="pb"></div></div>
<script>
var panel = document.getElementById('p'), pb = document.getElementById('pb');
function E(s){ return (s+'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function show(h){ pb.innerHTML = h; panel.classList.add('on'); }
document.addEventListener('click', function(e){
  var w = e.target.closest('.w');
  if (w) {
    show('<b>' + E(w.dataset.w) + '</b> — ' + E(w.dataset.m) +
         (w.dataset.n ? '<div class="nt">' + E(w.dataset.n) + '</div>' : ''));
    e.stopPropagation(); return;
  }
  var s = e.target.closest('.sent');
  if (s) {
    var g = []; try { g = JSON.parse(s.dataset.g || '[]'); } catch (_) {}
    show(g.length
      ? g.map(function(x){ return '<b>' + E(x.pattern) + '</b><div>' + E(x.explanation) + '</div>' +
          (x.example_jp ? '<div class="ex">' + E(x.example_jp) + '</div>' : ''); }).join('<hr>')
      : '<i>No grammar note for this sentence.</i>');
    return;
  }
  panel.classList.remove('on');
});
</script>
</body></html>
"""


# ─── structured reading JSON (Miraa-style consumer data) ─────────────────────
# Emitted ALONGSIDE the self-contained HTML so an external reader (the personal
# website) can render the same content in its own design and wire it to a word
# bank / SRS. Same data the HTML render uses; a separate first-occurrence pass so
# the HTML path stays byte-for-byte unchanged.


def tokenize_json(jp: str, meanings: dict[str, dict], seen: set[str]) -> list[dict]:
    """One sentence → ordered token dicts. Content words carrying a gloss get the
    lexeme fields + a first-occurrence `new` flag; other tokens are just surface
    (+ furigana reading on kanji-bearing tokens)."""
    toks: list[dict] = []
    for w in tagger()(jp):
        f = w.feature
        surf = w.surface
        pos1 = f.pos1 or ""
        rt = kata2hira(f.kana or "") if has_kanji(surf) else ""
        tok: dict = {"t": surf}
        if rt:
            tok["rt"] = rt
        m = meanings.get(token_key(f, surf))
        if m and pos1 not in DROP_POS1 and (m["meaning_en"] or m["notes"]):
            is_new = m["key"] not in seen
            if is_new:
                seen.add(m["key"])
            tok.update({"w": m["lemma"], "m": m["meaning_en"] or "—",
                        "n": m["notes"], "pos": m["pos"], "key": m["key"], "new": is_new})
        toks.append(tok)
    return toks


def build_reading_json(display: str, source_id: str,
                       per_chapter: list[tuple[str, dict]],
                       meanings: dict[str, dict]) -> dict:
    seen: set[str] = set()
    chapters = []
    for chap, data in per_chapter:
        by_s: dict[int, list[dict]] = {}
        for g in data["grammar_points"]:
            by_s.setdefault(int(g.get("s") or 0), []).append(g)
        # Group sentences into paragraphs (by each sentence's `para` index),
        # walking in reading order so first-occurrence `new` flags stay correct.
        para_map: dict[int, list[dict]] = {}
        order: list[int] = []
        for si, sent in enumerate(data["sentences"], 1):
            pi = int(sent.get("para", 0))
            if pi not in para_map:
                para_map[pi] = []
                order.append(pi)
            para_map[pi].append({
                "jp": sent["jp"],
                "en": sent.get("en", ""),
                "tokens": tokenize_json(sent["jp"], meanings, seen),
                "grammar": by_s.get(si, []),
            })
        chapters.append({
            "chapter": chap,
            "paragraphs": [{"sentences": para_map[pi]} for pi in order],
            "grammar": data["grammar_points"],
        })
    return {"schema": "reading/2", "source_id": source_id, "title": display,
            "lang": "ja", "prompt_version": PROMPT_VERSION, "chapters": chapters}


def generate(source_id: str, refresh: bool, dry_run: bool) -> list[Path]:
    sources = dl.find_sources(SOURCES_DIR)
    if source_id not in sources:
        raise SystemExit(f"generate-language-pages: no sidecar for {source_id} in {SOURCES_DIR}")
    meta = {**sources[source_id], "source_id": source_id}

    # GROUP headings by chapter_key — the identity used for slug/log/anchor.
    # Headings that canonicalize the same MERGE into one chapter. extract.py
    # --section is a heading regex with no Nth-occurrence selector, so the merged
    # chapter's section regex must match EVERY raw heading in the group
    # (`^(?:raw1|raw2|…)$`) — else the non-first raw forms wouldn't match and
    # their text would be silently DROPPED. Groups preserve first-appearance order.
    groups: dict[str, list[str]] = {}
    for t in dl.list_sections(EXTRACT, meta["asset"]):
        groups.setdefault(chapter_key(t), []).append(t)
    chapters: list[tuple[str, str | None]] = []
    for k, raws in groups.items():
        if len(raws) > 1:
            print(f"  ⚠ headings {raws} share chapter key {k!r} — merged into one "
                  f"chapter (extract can't split same-key sections)", file=sys.stderr)
        section = "^(?:" + "|".join(re.escape(r) for r in raws) + ")$"
        chapters.append((raws[0], section))  # raws[0] → label; section matches all
    if not chapters:
        chapters = [(WHOLE_LABEL, None)]  # transcript / heading-less → one unit

    # Per-chapter fugashi + LLM (cached), in document order.
    per_chapter: list[tuple[str, dict]] = []
    for idx, (label, section) in enumerate(chapters, start=1):
        data = chapter_data(meta, idx, label, section, refresh, dry_run)
        per_chapter.append((chapter_key(label), data))

    # Global gloss map: key → word (first meaning wins). Inline tokens look up
    # their gloss here; the render's `seen` set drives first-occurrence highlight.
    meanings: dict[str, dict] = {}
    for _chap, data in per_chapter:
        for w in data["words"]:
            meanings.setdefault(w["key"], w)

    # Per-source slug carries a short source_id so two DISTINCT sources sharing a
    # title (e.g. a re-downloaded edition) can't overwrite each other's page.
    slug = f"{dl.fs_safe_slug(dl.source_slug(meta['title'], source_id), fallback=source_id)}-{source_id[:8]}"
    display = dl.clean_title(meta["title"])
    path = READING_DIR / f"{slug}.html"

    html = render_reading_html(display, source_id, per_chapter, meanings)
    wrote = dl.atomic_write(path, html, dry_run)
    rendered = [path]

    # Structured reading data for external consumers (the personal website's
    # Miraa-style reader + word bank). Byte-stable like the HTML, so a no-change
    # re-run stays a no-op.
    json_path = READING_DIR / f"{slug}.reading.json"
    reading = build_reading_json(display, source_id, per_chapter, meanings)
    dl.atomic_write(json_path, json.dumps(reading, ensure_ascii=False) + "\n", dry_run)
    rendered.append(json_path)

    if not dry_run:
        append_log(source_id, [c for c, _ in per_chapter], rendered)

    print(f"  {'wrote' if wrote else 'unchanged'} {path.relative_to(VAULT_ROOT)}")
    return rendered


def append_log(source_id: str, chapters: list[str], rendered: list[Path]) -> None:
    """Append one `<added>  <sid>#<chapter>  pages: …` line per chapter, but
    only for (source_id, chapter) keys not already present — a no-change re-run
    produces zero log delta (idempotency). Date read lazily to keep imports light."""
    from datetime import date
    existing_lines = (LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
                      if LOG_PATH.is_file() else [])
    have = set(dl.chapter_order_from_lines(existing_lines, source_id))
    new_for: list[str] = []
    for c in chapters:  # skip already-logged AND dedup within this batch
        if c not in have:
            have.add(c)
            new_for.append(c)
    if not new_for:
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    added = date.today().isoformat()
    pages = " ".join(sorted(str(p.relative_to(VAULT_ROOT)) for p in rendered))
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        for chap in new_for:
            f.write(f"{added}  {source_id}#{chap}  pages: {pages}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--source-id", required=True, help="the source_id to generate for")
    ap.add_argument("--refresh", action="store_true", help="re-call the LLM, ignore cache")
    ap.add_argument("--dry-run", action="store_true", help="render from cache only; never call LLM or write")
    args = ap.parse_args()

    for d in (READING_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)

    rendered = generate(args.source_id, args.refresh, args.dry_run)
    manifest = sorted(str(p.relative_to(VAULT_ROOT)) for p in rendered)
    if LOG_PATH.is_file():
        manifest.append(str(LOG_PATH.relative_to(VAULT_ROOT)))
    print("MANIFEST:" + json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
