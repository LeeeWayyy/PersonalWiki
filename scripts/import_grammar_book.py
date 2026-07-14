#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mobi==0.4.1", "fugashi", "unidic-lite"]
# ///
"""One-time import: 《日语语法新思维（修订版）》 .mobi -> grammar cards in study.db.

Parses the book's fixed entry template (title / ★解释 / ★意译 / ★接续 / 例句 / 直译 / 意译)
straight from the unpacked Kindle HTML, because the HTML markup disambiguates
furigana: inside 例句 lines, real-text kana is wrapped in <span bgcolor=...> while
bare text runs are strictly (kanji + its furigana) pairs. Bold (grammar-pattern)
segments lack the span marks; their furigana is stripped by enumerating possible
splits and keeping the one whose fugashi reading matches the segment's kana
projection, with a kanji->reading map harvested from the bare runs as fallback.

Parsing also writes the cards to pipeline/data/grammar-cards.ja.json — the
repo-committed grammar bank (study.db itself is gitignored personal state).
Pass that JSON as the source to re-seed a fresh study.db without the mobi.
Cards import as status='known' (state=1, no due date) so 400+ patterns don't
flood the review queue; flip individual ones to learning as you meet them.

Usage:
  scripts/import_grammar_book.py "/path/to/日语语法新思维(修订版).mobi" [--dry-run]
  scripts/import_grammar_book.py pipeline/data/grammar-cards.ja.json
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app import db  # noqa: E402

SOURCE_ID = "book:日语语法新思维（修订版）"
JSON_PATH = ROOT / "pipeline" / "data" / "grammar-cards.ja.json"
TITLE_RE = re.compile(r'<p[^>]*><font size="4"><font color="#5453a3">(.*?)</font></font></p>')
BLOCKQUOTE_RE = re.compile(r"<blockquote[^>]*>(.*?)</blockquote>", re.S)
FIELD_RE = re.compile(r"<b>([^<：]{1,4})：</b>(.*)", re.S)
PAIR_RE = re.compile(r"([一-鿿々]+)([ぁ-ゖ]+)")
ENTRY_PREFIXES = ("～", "—", "「")
MEANING_LABELS = {"意译", "意思", "意１", "意２", "意３", "意４", "意５", "意６", "意７"}
GLOSS_EXTRA_LABELS = {"解释", "区别", "注意", "语气"}

KANJI = lambda c: "一" <= c <= "鿿" or c == "々"
HIRA = lambda c: "ぁ" <= c <= "ゖ"


def kata2hira(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def strip_tags(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def split_example_pieces(fragment: str):
    """Yield (text, in_span, in_bold) for the text nodes of an 例句 fragment."""
    span = bold = 0
    for part in re.split(r"(<[^>]+>)", fragment):
        if not part:
            continue
        if part.startswith("<"):
            tag = part.lower()
            if tag.startswith("<span"):
                span += 1
            elif tag.startswith("</span"):
                span = max(0, span - 1)
            elif tag.startswith("<b>") or tag.startswith("<b "):
                bold += 1
            elif tag.startswith("</b"):
                bold = max(0, bold - 1)
            continue
        yield html.unescape(part), span > 0, bold > 0


def build_reading_map(example_fragments: list[str]) -> dict[str, set[str]]:
    """kanji-run -> observed furigana readings, from unambiguous bare runs."""
    rmap: dict[str, set[str]] = {}
    for frag in example_fragments:
        for text, in_span, in_bold in split_example_pieces(frag):
            if in_span or in_bold:
                continue
            for kanji_run, reading in PAIR_RE.findall(text):
                rmap.setdefault(kanji_run, set()).add(reading)
    return rmap


def sentence_reading(tagger, text: str) -> str:
    out = []
    for word in tagger(text):
        kana = word.feature.kana
        out.append(kata2hira(kana) if kana else kata2hira(word.surface))
    return "".join(out)


def _rmap_strip(text: str, rmap: dict[str, set[str]]) -> tuple[str, int]:
    """Greedy furigana strip via observed readings; count unresolved kanji."""
    out: list[str] = []
    unresolved = 0
    i = 0
    while i < len(text):
        if KANJI(text[i]):
            matched = False
            for run_len in (3, 2, 1):
                run = text[i : i + run_len]
                if len(run) == run_len and all(map(KANJI, run)) and run in rmap:
                    tail = text[i + run_len :]
                    for reading in sorted(rmap[run], key=len, reverse=True):
                        if tail.startswith(reading):
                            out.append(run)
                            i += run_len + len(reading)
                            matched = True
                            break
                if matched:
                    break
            if not matched:
                out.append(text[i])
                i += 1
                if i < len(text) and HIRA(text[i]):
                    unresolved += 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out), unresolved


def strip_bold_furigana(text: str, rmap: dict[str, set[str]], tagger, lemma: str) -> tuple[str, int]:
    """Drop interleaved furigana from a bold segment.

    Enumerate splits (each kanji absorbs 0-5 following hiragana as furigana) and
    keep candidates whose fugashi reading equals the segment's kana projection —
    the interleave guarantees "all kana in order" IS the full reading. When the
    book's reading disagrees with fugashi, fall back to the candidate that
    reproduces the entry's own pattern (the bold text is the pattern occurrence).
    """
    if not any(map(KANJI, text)):
        return text, 0
    projection = kata2hira("".join(c for c in text if not KANJI(c)))
    candidates: list[str] = []

    def rec(i: int, acc: list[str]) -> None:
        if len(candidates) >= 500:
            return
        if i == len(text):
            candidates.append("".join(acc))
            return
        acc.append(text[i])
        if KANJI(text[i]):
            j = i + 1
            k = j
            while True:
                rec(k, acc)
                if k - j >= 5 or k >= len(text) or not HIRA(text[k]):
                    break
                k += 1
        else:
            rec(i + 1, acc)
        acc.pop()

    rec(0, [])
    valid = sorted({c for c in candidates if sentence_reading(tagger, c) == projection})
    if valid:
        rmap_guess, misses = _rmap_strip(text, rmap)
        if misses == 0 and rmap_guess in valid:
            return rmap_guess, 0
        # Ambiguity (alternate okurigana spellings): minimal strip keeps the
        # standard spelling, e.g. に基づいて over に基いて.
        return max(valid, key=len), 0
    lemma_clean = lemma.strip("～—「」")
    in_lemma = [c for c in set(candidates) if c != text and c in lemma_clean]
    if in_lemma:
        return max(sorted(in_lemma), key=len), 0
    return _rmap_strip(text, rmap)


def clean_example(fragment: str, rmap: dict[str, set[str]], tagger, lemma: str) -> tuple[str, int]:
    """Interleaved-furigana HTML -> plain sentence, bold pattern wrapped in 【】."""
    out: list[str] = []
    unresolved = 0
    was_bold = False
    for text, in_span, in_bold in split_example_pieces(fragment):
        if in_bold != was_bold:
            out.append("【" if in_bold else "】")
            was_bold = in_bold
        if in_bold:
            cleaned, misses = strip_bold_furigana(text, rmap, tagger, lemma)
            out.append(cleaned)
            unresolved += misses
        elif in_span:
            out.append(text)
        else:
            out.append("".join(c for c in text if not HIRA(c)))
    if was_bold:
        out.append("】")
    return "".join(out).strip(), unresolved


def parse_entries(page: str):
    """Yield {lemma, fields: [(label, raw_html)...]} for each grammar-pattern entry."""
    matches = list(TITLE_RE.finditer(page))
    for idx, m in enumerate(matches):
        lemma = strip_tags(m.group(1))
        if not lemma.startswith(ENTRY_PREFIXES):
            continue
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(page)
        fields = []
        for block in BLOCKQUOTE_RE.findall(page[m.end() : end]):
            fm = FIELD_RE.search(block)
            if fm:
                fields.append((fm.group(1), fm.group(2)))
        yield {"lemma": lemma, "fields": fields}


def build_card(entry: dict, rmap: dict[str, set[str]], tagger) -> tuple[dict, int]:
    """Walk fields in order. 直译/意译 right after an 例句 are that example's
    translations; any other label switches back to entry-level (head) fields."""
    head: list[tuple[str, str]] = []
    examples: list[dict] = []
    current_example: dict | None = None
    unresolved = 0
    for label, raw in entry["fields"]:
        if label == "例句":
            sentence, misses = clean_example(raw, rmap, tagger, entry["lemma"])
            unresolved += misses
            current_example = {"jp": sentence}
            examples.append(current_example)
        elif current_example is not None and label in ("直译", "意译") and label not in current_example:
            current_example[label] = strip_tags(raw)
        else:
            current_example = None
            head.append((label, strip_tags(raw)))

    gloss_lines = []
    for label, text in head:
        if not text:
            continue
        if label in ("意译", "意思"):
            gloss_lines.append(text)
        elif label in MEANING_LABELS or label in GLOSS_EXTRA_LABELS:
            gloss_lines.append(f"{label}：{text}")

    example = None
    if examples:
        first = examples[0]
        translation = first.get("意译") or first.get("直译") or ""
        example = "\n".join(filter(None, [first["jp"], translation]))

    pos = next((text for label, text in head if label == "接续" and text), None)
    card = {"lemma": entry["lemma"], "gloss": "\n".join(gloss_lines), "pos": pos, "example": example}
    return card, unresolved


def merge_duplicates(cards: list[dict]) -> list[dict]:
    """Same pattern appears under several meaning categories; concatenate glosses."""
    merged: dict[str, dict] = {}
    for card in cards:
        key = db.normalize_key("grammar", card["lemma"])
        if key not in merged:
            merged[key] = dict(card)
        else:
            kept = merged[key]
            if card["gloss"]:
                kept["gloss"] = "\n".join(filter(None, [kept["gloss"], card["gloss"]]))
            kept["pos"] = kept["pos"] or card["pos"]
            kept["example"] = kept["example"] or card["example"]
    return list(merged.values())


def upsert(conn, card: dict) -> None:
    conn.execute(
        """INSERT INTO items(kind,norm_key,lemma,reading,pos,gloss,example,source_id,anchor,created,status,state,due)
           VALUES('grammar',?,?,NULL,?,?,?,?,NULL,?,'known',1,NULL)
           ON CONFLICT(kind,norm_key) DO UPDATE SET
             pos=COALESCE(NULLIF(excluded.pos,''),items.pos),
             gloss=COALESCE(NULLIF(excluded.gloss,''),items.gloss),
             example=COALESCE(NULLIF(excluded.example,''),items.example),
             source_id=COALESCE(NULLIF(excluded.source_id,''),items.source_id)""",
        (
            db.normalize_key("grammar", card["lemma"]),
            card["lemma"],
            card["pos"],
            card["gloss"],
            card["example"],
            SOURCE_ID,
            db.now_iso(),
        ),
    )


def parse_book(mobi_path: str, dry_run: bool) -> list[dict]:
    import fugashi
    import mobi

    tagger = fugashi.Tagger()
    _, extracted = mobi.extract(mobi_path)
    page = Path(extracted).read_text(encoding="utf-8", errors="ignore")

    entries = list(parse_entries(page))
    rmap = build_reading_map([raw for e in entries for label, raw in e["fields"] if label == "例句"])
    cards, total_unresolved = [], 0
    for entry in entries:
        card, unresolved = build_card(entry, rmap, tagger)
        cards.append(card)
        if unresolved:
            total_unresolved += unresolved
            print(f"  ! unresolved furigana in bold: {card['lemma']}", file=sys.stderr)

    merged = merge_duplicates(cards)
    empty_gloss = [c["lemma"] for c in merged if not c["gloss"]]
    print(f"parsed {len(cards)} entries -> {len(merged)} cards "
          f"({len(cards) - len(merged)} merged duplicates, {total_unresolved} unresolved bold furigana, "
          f"{len(empty_gloss)} empty glosses: {empty_gloss})")

    if not dry_run:
        JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        JSON_PATH.write_text(
            json.dumps({"source_id": SOURCE_ID, "cards": merged}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"wrote {JSON_PATH.relative_to(ROOT)}")
    return merged


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help=".mobi to parse, or the committed grammar-cards .json")
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    if args.source.endswith(".json"):
        merged = json.loads(Path(args.source).read_text(encoding="utf-8"))["cards"]
        print(f"loaded {len(merged)} cards from {args.source}")
    else:
        merged = parse_book(args.source, args.dry_run)

    # Sanity checks against the known book before touching the DB.
    assert 380 <= len(merged) <= 700, f"unexpected card count: {len(merged)}"
    probe = next(c for c in merged if c["lemma"] == "～はおろか")
    assert probe["example"].startswith("日本に来た最初のころは、漢字の読み方【はおろか】、仮名も読めなかった。"), probe["example"]
    assert "不用说～" in probe["gloss"], probe["gloss"]

    if args.dry_run:
        for lemma in ("～は", "～はおろか", "—次第"):
            card = next((c for c in merged if c["lemma"] == lemma), None)
            if card:
                print("\n---", card["lemma"], "| 接续:", card["pos"])
                print(card["gloss"])
                print(card["example"])
        return

    conn = db.connect()
    try:
        before = conn.execute("SELECT COUNT(*) FROM items WHERE kind='grammar'").fetchone()[0]
        for card in merged:
            upsert(conn, card)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM items WHERE kind='grammar'").fetchone()[0]
    finally:
        conn.close()
    print(f"study.db grammar cards: {before} -> {after} (+{after - before} new, "
          f"{len(merged) - (after - before)} merged into existing)")


if __name__ == "__main__":
    main()
