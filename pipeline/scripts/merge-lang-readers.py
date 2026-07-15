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
import argparse, importlib.util, json, os, re, subprocess, sys
from difflib import SequenceMatcher
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
# glp reads PW_CONTENT_DIR at import to fix VAULT_ROOT (the lang vault, content/lang).
# The backend job passes the git repo root (content/); the lang vault is its lang/
# subdir. Descend so READING_DIR/SOURCES_DIR resolve — but leave a dir that already
# points at the lang vault (has sources/) untouched, so the prototype CLI still works.
_cd = Path(os.environ.get("PW_CONTENT_DIR") or os.environ.get("VAULT_CONTENT_DIR") or ".").resolve()
if (_cd / "lang" / "sources").is_dir():
    os.environ["PW_CONTENT_DIR"] = str(_cd / "lang")
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


_OBJ_RX = re.compile(r'\{\s*"s"\s*:\s*(\d+)\s*,\s*"jp"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}')


def _parse_calibrated(raw: str) -> dict:
    """Best-effort {global_num: jp} from an LLM reply. Strict JSON first, then
    salvage every well-formed {"s":N,"jp":"…"} object so ONE malformed entry
    (e.g. a dropped closing quote) can't lose its whole batch — jp values use
    「」, never ASCII quotes, so the object regex is unambiguous."""
    try:
        obj = dl.extract_json(raw)
        return {int(o["s"]): str(o.get("jp") or "").strip()
                for o in (obj.get("calibrated") or []) if isinstance(o, dict) and "s" in o}
    except ValueError:
        pass
    out = {}
    for m in _OBJ_RX.finditer(raw):
        try:                                            # decode \n \" etc. in the value
            jp = json.loads(f'"{m.group(2)}"')
        except ValueError:
            jp = m.group(2)
        out[int(m.group(1))] = jp.strip()
    return out


def _calibrate_call(sents, nums, asr) -> dict:
    """One LLM calibration call over sentences numbered by `nums` (parallel to
    `sents`). Tolerant-parsed; 2 attempts. Returns {num: jp} (may be partial)."""
    numbered = "\n".join(f"{n}. {s}" for n, s in zip(nums, sents))
    prompt = CAL_PROMPT.format(sents=numbered, asr=asr or "(none)")
    got = {}
    for attempt in (1, 2):
        got = _parse_calibrated(dl.call_llm(prompt, CAL_TIMEOUT))
        if got:
            break
        print(f"    calibration call {nums[0]}–{nums[-1]}: nothing parseable "
              f"(attempt {attempt}/2)", file=sys.stderr)
    return got


def _cache_to_map(cached: dict) -> dict:
    """{num: jp} from either the current {"map":…} or legacy {"calibrated":[…]}."""
    if "map" in cached:
        return {int(k): v for k, v in (cached["map"] or {}).items()}
    return {int(o["s"]): str(o.get("jp") or "").strip()
            for o in (cached.get("calibrated") or []) if isinstance(o, dict) and "s" in o}


def _calibrate_batch(sents, base, asr, suffix, sha, source_id, refresh) -> dict:
    """One cached batch. `base` is the global 1-based number of sents[0]."""
    cache_dir = glp.CACHE_DIR / "merge"
    cached = None if refresh else dl.load_cache(cache_dir, CAL_VERSION, source_id, sha, suffix)
    if cached is None:
        cached = {"map": {str(k): v for k, v in
                          _calibrate_call(sents, list(range(base, base + len(sents))), asr).items()}}
        dl.save_cache(cache_dir, CAL_VERSION, source_id, sha, cached, suffix)
    return _cache_to_map(cached)


def calibrate_chapter(idx, chap, sentences, asr, sha, source_id, refresh):
    """LLM kana→kanji for a chapter, batched at BATCH_SENTS. Any sentence no
    batch produced (malformed JSON) is recovered in a tiny retry call; anything
    still missing keeps the original kana AND is logged — never silent. Guard:
    keep the original whenever the calibrated reading drifts. Returns
    (list[str], kept_missing, drift)."""
    slug = dl.fs_safe_slug(chap, fallback="chap")
    N = len(sentences)
    by_s: dict[int, str] = {}
    for lo in range(0, N, glp.BATCH_SENTS):
        suffix = f"{idx:02d}-{slug}-b{lo:04d}"
        by_s.update(_calibrate_batch(sentences[lo:min(lo + glp.BATCH_SENTS, N)],
                                     lo + 1, asr, suffix, sha, source_id, refresh))

    # Recover sentences no batch returned (a malformed entry skipped by salvage),
    # re-asking in small chunks where a completion almost never malforms. Cached.
    missing = [i for i in range(1, N + 1) if i not in by_s]
    if missing:
        cache_dir = glp.CACHE_DIR / "merge"
        rsuffix = f"{idx:02d}-{slug}-retry"
        rec = None if refresh else dl.load_cache(cache_dir, CAL_VERSION, source_id, sha, rsuffix)
        if rec is None:
            got = {}
            for c in range(0, len(missing), 8):
                chunk = missing[c:c + 8]
                got.update(_calibrate_call([sentences[i - 1] for i in chunk], chunk, asr))
            rec = {"map": {str(k): v for k, v in got.items()}}
            dl.save_cache(cache_dir, CAL_VERSION, source_id, sha, rec, rsuffix)
        by_s.update({int(k): v for k, v in (rec.get("map") or {}).items()})
        still = [i for i in missing if i not in by_s]
        if still:
            print(f"    ⚠ ch{idx:02d} {chap!r}: kept ORIGINAL kana for {len(still)} "
                  f"un-calibratable sentence(s): {still}", file=sys.stderr)

    out, kept, drift = [], 0, 0
    for i, orig in enumerate(sentences, 1):
        cal = by_s.get(i, "")
        if not cal:
            out.append(orig); kept += 1; continue      # logged above
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


def merged_id(book_id: str, audio_id: str) -> str:
    """Deterministic, unique, fs/URL-safe id for the book+audio merge."""
    return f"merge-{book_id[-8:]}-{audio_id[-8:]}".lower()


def write_and_commit(per_chapter, meanings, audio_src, meta, book_id, audio_id) -> None:
    """Write the merged reading page+json into the real lang vault (_reading/) and
    commit ONLY those two files under the content git repo. Byte-stable + a
    no-op commit when nothing changed, so a re-run is idempotent."""
    mid = merged_id(book_id, audio_id)
    display = dl.clean_title(meta["title"]) + " (merged)"
    reading = glp.build_reading_json(display, mid, per_chapter, meanings, audio_src)
    reading["merged"] = True                     # flag so the UI never re-merges it
    reading["merged_from"] = [book_id, audio_id]
    html = glp.render_reading_html(display, mid, per_chapter, meanings, audio_src)

    glp.READING_DIR.mkdir(parents=True, exist_ok=True)
    hp = glp.READING_DIR / f"{mid}.html"
    jp = glp.READING_DIR / f"{mid}.reading.json"
    dl.atomic_write(hp, html, False)
    dl.atomic_write(jp, json.dumps(reading, ensure_ascii=False) + "\n", False)

    repo = subprocess.check_output(
        ["git", "-C", str(glp.READING_DIR), "rev-parse", "--show-toplevel"], text=True).strip()
    subprocess.run(["git", "-C", repo, "add", "--", str(hp), str(jp)], check=True)
    if subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet",
                       "--", str(hp), str(jp)]).returncode == 0:
        print(f"nothing to commit (merged reader {mid} already up to date)")
        return
    subprocess.run(
        ["git", "-C", repo, "-c", "user.email=merge@personal-wiki.local",
         "-c", "user.name=lang-merge", "commit", "-m",
         f"lang: merge {book_id} + {audio_id} → {mid}", "--", str(hp), str(jp)], check=True)
    print(f"committed merged reader {mid}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-id", required=True)
    ap.add_argument("--audio-id", required=True)
    ap.add_argument("--out-dir", help="prototype: write demo page here (no vault write)")
    ap.add_argument("--commit", action="store_true",
                    help="write into the real lang vault (_reading/) and git-commit")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-calibrate", action="store_true",
                    help="text is already natural kanji (a hand-calibrated transcript): "
                         "align to the audio timeline only, skip the kana→kanji LLM pass")
    ap.add_argument("--limit", type=int, default=0, help="calibrate only first N chapters (debug)")
    args = ap.parse_args()
    if not args.commit and not args.out_dir:
        ap.error("provide --out-dir (prototype) or --commit (write to vault)")
    if args.commit and args.limit:
        ap.error("--limit is prototype-only; drop it for --commit")

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
    # --no-calibrate: the text is already a hand-calibrated transcript (natural
    # kanji). Alignment above already stamped the timeline; there's nothing for the
    # kana→kanji LLM to add, so keep the sentences verbatim and skip the ~30-min pass.
    if args.no_calibrate:
        print("calibration skipped (--no-calibrate): text kept verbatim, timing from audio")
    else:
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
                s["jp"] = new                           # <-- swap in calibrated kanji
            total_kept += kept; total_drift += drift
            print(f"  ch{idx:02d} {chap[:24]:24} sents={len(sents):3} kept={kept} drift-rejected={drift}")

    # If --limit, drop uncalibrated chapters so the demo page is coherent
    if args.limit:
        per_chapter = per_chapter[:args.limit]

    audio_src = glp.audio_src_for(audio_asset, doc)
    if args.commit:
        write_and_commit(per_chapter, meanings, audio_src, meta, args.book_id, args.audio_id)
    else:
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
    if args.commit:
        print(f"wrote           : {glp.READING_DIR / (merged_id(args.book_id, args.audio_id) + '.html')}")
    else:
        print(f"wrote           : {out_dir / (slug + '.html')}")
    print(f"\nsample upgrades (book → merged):")
    for o, n in samples:
        print(f"  - {o[:38]}\n    {n[:38]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
