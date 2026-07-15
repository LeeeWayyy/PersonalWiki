#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0", "fugashi", "unidic-lite"]
# ///
"""PROTOTYPE — merge a BOOK lang reader with an AUDIO lang reader into ONE.

Book gives clean structure + grammar/vocab/translations (but kid-friendly kana);
audio gives natural kanji orthography + a real timeline. We:

  1. Align the book's per-sentence READINGS to the audio timeline (difflib forced
     alignment on hiragana → per-sentence [t0,t1]).
  2. Per chapter, LLM-calibrate the book's kana sentences into natural kanji,
     using the aligned audio ASR span as a spelling reference (cached). A reading-
     similarity guard rejects any sentence whose pronunciation drifted.
  3. Emit ONE reading page: calibrated kanji text (furigana now renders) + the
     book's grammar/vocab/translations (lemma-keyed, so they still resolve) +
     audio timestamps inherited 1:1 from the book alignment.

Prototype: reads the real content repo (PW_CONTENT_DIR) but writes the merged
page to --out-dir (scratchpad) so nothing in the vault is touched.
"""
import argparse, importlib.util, json, os, re, sys
from difflib import SequenceMatcher
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("glp", SCRIPTS / "generate-language-pages.py")
glp = importlib.util.module_from_spec(spec); spec.loader.exec_module(glp)
import derived_lib as dl

CAL_VERSION = "merge-v1"
CAL_TIMEOUT = 900
READING_GUARD = 0.80  # min reading similarity to accept a calibrated sentence


def reading_of(text: str) -> str:
    return "".join(glp.kata2hira(w.feature.kana or w.surface) for w in glp.tagger()(text))


def load_book(source_id: str):
    """Book per_chapter from cache (dry-run) + its meanings map."""
    sources = dl.find_sources(glp.SOURCES_DIR)
    if source_id not in sources:
        sys.exit(f"no book sidecar {source_id} in {glp.SOURCES_DIR}")
    meta = {**sources[source_id], "source_id": source_id}
    groups: dict[str, list[str]] = {}
    for t in dl.list_sections(glp.EXTRACT, meta["asset"]):
        groups.setdefault(glp.chapter_key(t), []).append(t)
    chapters = [(raws[0], "^(?:" + "|".join(re.escape(r) for r in raws) + ")$")
                for raws in groups.values()]
    per_chapter = []
    for idx, (label, section) in enumerate(chapters, 1):
        data = glp.chapter_data(meta, idx, label, section, refresh=False, dry_run=True)
        per_chapter.append((glp.chapter_key(label), data))
    meanings: dict[str, dict] = {}
    for _c, d in per_chapter:
        for w in d["words"]:
            meanings.setdefault(w["key"], w)
    return meta, per_chapter, meanings


def audio_streams(asset: Path):
    """(reading stream [(hira,t0,t1)], segment list [(text,start,end)])."""
    doc = json.loads(asset.read_text(encoding="utf-8"))
    stream, segs = [], []
    for seg in doc.get("segments") or []:
        text = (seg.get("text") or "").strip()
        segs.append((text, float(seg.get("start") or 0.0), float(seg.get("end") or 0.0)))
        ct = glp._segment_char_times(seg)
        surf = "".join(c for c, _, _ in ct)
        pos = 0
        for w in glp.tagger()(text):
            s = w.surface
            j = surf.find(s, pos)
            if j < 0:
                t0 = t1 = (ct[pos - 1][2] if 0 < pos <= len(ct) else 0.0)
            else:
                span = ct[j:j + len(s)]; pos = j + len(s)
                t0, t1 = span[0][1], span[-1][2]
            for ch in glp.kata2hira(w.feature.kana or s):
                if not ch.isspace():
                    stream.append((ch, t0, t1))
    return stream, segs, doc


def align_book_to_audio(per_chapter, stream) -> int:
    """difflib forced-alignment: mutate each book sentence with t0/t1. Returns
    timed count. B = concatenated sentence readings; A = audio reading string."""
    A = "".join(c for c, _, _ in stream)
    spans, parts, b = [], [], 0
    for _c, d in per_chapter:
        for s in d["sentences"]:
            rr = "".join(ch for ch in reading_of(s["jp"]) if not ch.isspace())
            parts.append(rr); spans.append((s, b, b + len(rr))); b += len(rr)
    B = "".join(parts)
    b2a = {}
    for i, j, n in SequenceMatcher(None, B, A, autojunk=False).get_matching_blocks():
        for k in range(n):
            b2a[i + k] = j + k
    timed = 0
    for s, bs, be in spans:
        hits = [b2a[k] for k in range(bs, be) if k in b2a]
        if hits and len(hits) * 2 >= (be - bs):
            s["t0"] = round(stream[min(hits)][1], 2)
            s["t1"] = round(stream[max(hits)][2], 2)
            timed += 1
    return timed


def asr_reference(segs, t0, t1) -> str:
    """Segment texts overlapping [t0,t1] — the spelling reference for a chapter."""
    return "".join(text for text, s0, s1 in segs if s1 >= t0 and s0 <= t1)


CAL_PROMPT = """You refine a children's-book Japanese passage (written mostly in
kana) into natural adult orthography, using an audio transcript of the SAME
passage as a spelling reference. For each numbered sentence output the SAME
sentence with natural kanji where a fluent adult would write it (さむい→寒い,
おなか→お腹, へらす→減らす …).

STRICT RULES:
- Preserve wording, particles, conjugation and meaning EXACTLY. Change ONLY
  kana↔kanji orthography. The pronunciation (reading) must stay identical.
- Never add, delete, merge, split, or reorder sentences or words.
- Prefer the kanji the AUDIO REFERENCE uses when it matches the sentence, but the
  reference has transcription errors — ignore words it clearly got wrong.
- Output ONLY JSON: {{"calibrated":[{{"s":<num>,"jp":"<sentence>"}}]}}

## ORIGINAL SENTENCES
{sents}

## AUDIO REFERENCE (spelling hint only; may contain errors)
{asr}
"""


def _calibrate_batch(sents, base, asr, suffix, sha, source_id, refresh) -> dict:
    """One LLM call for a sentence batch (cached, 2 attempts). `base` is the
    global 1-based number of sents[0]. Returns {global_num: calibrated_jp};
    empty dict on persistent failure (caller keeps originals)."""
    cache_dir = glp.CACHE_DIR / "merge"
    cached = None if refresh else dl.load_cache(cache_dir, CAL_VERSION, source_id, sha, suffix)
    if cached is None:
        numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sents, base))
        prompt = CAL_PROMPT.format(sents=numbered, asr=asr or "(none)")
        for attempt in (1, 2):
            try:
                cached = dl.extract_json(dl.call_llm(prompt, CAL_TIMEOUT))
                break
            except ValueError:
                print(f"    batch {suffix}: unparseable (attempt {attempt}/2)", file=sys.stderr)
        if cached is None:
            return {}                                  # give up → keep originals
        dl.save_cache(cache_dir, CAL_VERSION, source_id, sha, cached, suffix)
    return {int(o["s"]): str(o.get("jp") or "").strip()
            for o in (cached.get("calibrated") or []) if isinstance(o, dict) and "s" in o}


def calibrate_chapter(idx, chap, sentences, asr, sha, source_id, refresh):
    """LLM kana→kanji for a chapter, batched at BATCH_SENTS (big completions
    truncate). Guard: keep the original whenever the calibrated reading drifts.
    Returns (list[str], kept, drift)."""
    slug = dl.fs_safe_slug(chap, fallback="chap")
    by_s: dict[int, str] = {}
    for lo in range(0, len(sentences), glp.BATCH_SENTS):
        hi = min(lo + glp.BATCH_SENTS, len(sentences))
        suffix = f"{idx:02d}-{slug}-b{lo:04d}"
        by_s.update(_calibrate_batch(sentences[lo:hi], lo + 1, asr, suffix,
                                     sha, source_id, refresh))
    out, kept, drift = [], 0, 0
    for i, orig in enumerate(sentences, 1):
        cal = by_s.get(i, "")
        if not cal:
            out.append(orig); kept += 1; continue
        sim = SequenceMatcher(None, reading_of(orig), reading_of(cal)).ratio()
        if sim < READING_GUARD:
            out.append(orig); drift += 1               # reject: pronunciation moved
        else:
            out.append(cal)
    return out, kept, drift


def kanji_upgrades(orig: str, cal: str) -> int:
    """Count content tokens that gained kanji (kana surface → kanji surface)."""
    o_reads = {}
    for w in glp.tagger()(orig):
        o_reads.setdefault(glp.kata2hira(w.feature.kana or w.surface), w.surface)
    n = 0
    for w in glp.tagger()(cal):
        r = glp.kata2hira(w.feature.kana or w.surface)
        if glp.has_kanji(w.surface) and r in o_reads and not glp.has_kanji(o_reads[r]):
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-id", required=True)
    ap.add_argument("--audio-id", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="calibrate only first N chapters (debug)")
    args = ap.parse_args()

    meta, per_chapter, meanings = load_book(args.book_id)
    nsent = sum(len(d["sentences"]) for _c, d in per_chapter)
    print(f"book {args.book_id}: {len(per_chapter)} chapters, {nsent} sentences")

    sources = dl.find_sources(glp.SOURCES_DIR)
    audio_asset = Path(sources[args.audio_id]["asset"])
    stream, segs, doc = audio_streams(audio_asset)
    print(f"audio {args.audio_id}: {len(segs)} segments, {len(stream)} reading chars")

    timed = align_book_to_audio(per_chapter, stream)
    print(f"aligned: {timed}/{nsent} sentences timed ({timed/nsent*100:.1f}%)")

    sha = meta["sha256"]
    total_up = total_kept = total_drift = 0
    samples = []
    for idx, (chap, d) in enumerate(per_chapter, 1):
        if args.limit and idx > args.limit:
            break
        sents = [s["jp"] for s in d["sentences"]]
        times = [(s.get("t0"), s.get("t1")) for s in d["sentences"]]
        t0s = [t for t, _ in times if t is not None]
        t1s = [t for _, t in times if t is not None]
        asr = asr_reference(segs, min(t0s), max(t1s)) if t0s else ""
        cal, kept, drift = calibrate_chapter(idx, chap, sents, asr, sha, args.book_id, args.refresh)
        for s, new in zip(d["sentences"], cal):
            up = kanji_upgrades(s["jp"], new)
            total_up += up
            if up and len(samples) < 10 and s["jp"] != new:
                samples.append((s["jp"], new))
            s["jp"] = new                               # <-- swap in calibrated kanji
        total_kept += kept; total_drift += drift
        print(f"  ch{idx:02d} {chap[:24]:24} sents={len(sents):3} kept={kept} drift-rejected={drift}")

    # If --limit, drop uncalibrated chapters so the demo page is coherent
    if args.limit:
        per_chapter = per_chapter[:args.limit]

    audio_src = glp.audio_src_for(audio_asset, doc)
    display = dl.clean_title(meta["title"]) + " (merged)"
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    slug = "merged-" + args.book_id[:8]
    html = glp.render_reading_html(display, args.book_id, per_chapter, meanings, audio_src)
    (out_dir / f"{slug}.html").write_text(html, encoding="utf-8")
    reading = glp.build_reading_json(display, args.book_id, per_chapter, meanings, audio_src)
    (out_dir / f"{slug}.reading.json").write_text(json.dumps(reading, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\n=== RESULT ===")
    print(f"timed sentences : {timed}/{nsent} ({timed/nsent*100:.1f}%)")
    print(f"kanji upgrades  : {total_up} tokens gained kanji")
    print(f"guard rejects   : {total_drift} sentences kept original (reading drift)")
    print(f"wrote           : {out_dir / (slug + '.html')}")
    print(f"\nsample upgrades (book → merged):")
    for o, n in samples:
        print(f"  - {o[:38]}\n    {n[:38]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
