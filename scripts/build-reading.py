#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["fugashi", "unidic-lite"]
# ///
"""build-reading.py — FALLBACK generator for the Miraa-style reader.

The DailyNotes lang pipeline (generate-language-pages.py) emits a structured
`_reading/<slug>.reading.json` per source. Until you re-ingest with that updated
pipeline, this builds an equivalent JSON from the COMMITTED lang artifacts
(source text + _vocab table + _grammar), using the same fugashi tokenizer so
furigana/readings match. Per-sentence translation (`en` field) is only filled at
build time when PW_BUILD_TRANSLATE=1. When enabled, it uses the same shared LLM
client the backend uses: custom LLM_CMD override, local Codex provider, then the
explicitly enabled API fallback. Target language is PW_TRANSLATE_LANG (default
Simplified Chinese), cached under .reading-cache/ so re-syncs are free. Without
the opt-in, translations stay empty and the reader fetches them on demand from
the backend.

Skips any source that already has a pipeline-produced reading.json.
Run from the site root after `sync`; no-op if fugashi/uv is unavailable.
"""
from __future__ import annotations
import json, os, re, sys
from pathlib import Path

import hashlib

ROOT = Path(__file__).resolve().parent.parent
VAULT = Path(os.environ.get("PW_VAULT") or (ROOT / "vault"))
LANG = VAULT / "lang"
READING = LANG / "_reading"
CACHE_DIR = ROOT / ".reading-cache"   # persists translations across syncs (gitignored)


def _load_env(p: Path):
    """Read KEY=VALUE from a .env without executing it; don't override real env."""
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            os.environ.setdefault(m.group(1), m.group(2).strip().strip('"').strip("'"))


_load_env(ROOT / "backend" / ".env")   # share the backend's LLM config
os.environ.setdefault("PW_LLM_CMD_BASE_DIR", str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend"))
from app import llm as llm_client  # noqa: E402

TARGET_LANG = os.environ.get("PW_TRANSLATE_LANG", "Simplified Chinese")  # per-sentence translation target
BUILD_TRANSLATE = os.environ.get("PW_BUILD_TRANSLATE") == "1"
TRANSLATE = BUILD_TRANSLATE and llm_client.configured() and os.environ.get("PW_NO_TRANSLATE") != "1"


def _llm(prompt: str, timeout: int = 180) -> str:
    return llm_client.complete(prompt, timeout=timeout) or ""


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def translate_all(source_id: str, sentences: list) -> int:
    """Fill each sentence's 'en' via the LLM (batched, cached by sentence sha so
    re-syncs are free). Returns how many were newly translated. No-op if no LLM."""
    if not TRANSLATE or not sentences:
        return 0
    cache_path = CACHE_DIR / f"{source_id}.json"
    try:
        cache = json.loads(cache_path.read_text())
    except Exception:  # noqa
        cache = {}
    key = lambda jp: _sha(TARGET_LANG + "\x1f" + jp)   # re-translates if target lang changes
    todo = [s for s in sentences if s["jp"] and key(s["jp"]) not in cache]
    made = 0
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        numbered = "\n".join(f"{j + 1}. {s['jp']}" for j, s in enumerate(batch))
        prompt = (f"Translate each numbered Japanese sentence into natural {TARGET_LANG} for a learner. "
                  "Output ONLY a JSON array of strings, one per sentence, in order. No notes.\n\n" + numbered)
        try:
            m = re.search(r"\[.*\]", _llm(prompt), re.S)
            arr = json.loads(m.group(0)) if m else []
        except Exception as e:  # noqa
            print(f"build-reading: translation batch failed ({e}); leaving those blank")
            arr = []
        for j, s in enumerate(batch):
            if j < len(arr) and isinstance(arr[j], str):
                cache[key(s["jp"])] = arr[j].strip(); made += 1
    for s in sentences:
        if s["jp"]:
            s["en"] = cache.get(key(s["jp"]), s.get("en", ""))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return made


def translate_vocab(source_id: str, vocab: dict) -> int:
    """Translate word glosses + usage notes (from the committed English vocab
    table) into TARGET_LANG, in place. Same cache as sentences. No-op if no LLM."""
    if not TRANSLATE or not vocab:
        return 0
    cache_path = CACHE_DIR / f"{source_id}.json"
    try:
        cache = json.loads(cache_path.read_text())
    except Exception:  # noqa
        cache = {}
    key = lambda t: _sha(TARGET_LANG + "\x1f" + t)
    uniq, seen = [], set()
    for v in vocab.values():
        for fld in ("meaning", "notes"):
            t = (v.get(fld) or "").strip()
            if t and key(t) not in cache and t not in seen:
                seen.add(t); uniq.append(t)
    made = 0
    for i in range(0, len(uniq), 50):
        batch = uniq[i:i + 50]
        numbered = "\n".join(f"{j + 1}. {t}" for j, t in enumerate(batch))
        prompt = (f"Translate each numbered item into natural {TARGET_LANG}. Each item is a short "
                  "English word gloss or usage note for a Japanese learner. Keep it concise. "
                  "Output ONLY a JSON array of strings, one per item, in order. No notes.\n\n" + numbered)
        try:
            m = re.search(r"\[.*\]", _llm(prompt), re.S)
            arr = json.loads(m.group(0)) if m else []
        except Exception as e:  # noqa
            print(f"build-reading: gloss batch failed ({e}); leaving those in English")
            arr = []
        for j, t in enumerate(batch):
            if j < len(arr) and isinstance(arr[j], str):
                cache[key(t)] = arr[j].strip(); made += 1
    for v in vocab.values():
        for fld in ("meaning", "notes"):
            t = (v.get(fld) or "").strip()
            if t:
                v[fld] = cache.get(key(t), v.get(fld, ""))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return made


try:
    import fugashi
except Exception:  # noqa
    print("build-reading: fugashi unavailable — skipping (reader will use raw fallback)")
    sys.exit(0)

_TAG = None
def tagger():
    global _TAG
    if _TAG is None:
        _TAG = fugashi.Tagger()
    return _TAG

DROP_POS1 = {"助詞", "助動詞", "補助記号", "記号", "空白"}
CJK = re.compile(r"[㐀-鿿豈-﫿\U00020000-\U0002ffff]")

def kata2hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)

def has_kanji(s: str) -> bool:
    return bool(CJK.search(s))

def join_wrapped(lines: list[str]) -> str:
    """Join hard-wrapped source lines into one paragraph WITHOUT inserting a
    space between CJK characters (Japanese has no inter-word spaces). A space is
    added only between two ASCII word chars (real English word boundary)."""
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


def split_sentences(para: str) -> list[str]:
    """Split one paragraph on 。！？ keeping trailing closing quotes/brackets."""
    parts = re.split(r"(?<=[。！？])(?![」』）\)】])", para)
    return [p.strip() for p in parts if p.strip()]

def parse_frontmatter(raw: str):
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", raw, re.S)
    if not m:
        return {}, raw
    data = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"^(\w+):\s*(.*)$", line)
        if mm:
            data[mm.group(1)] = mm.group(2).strip().strip("'\"")
    return data, m.group(2)

def parse_vocab(body: str) -> dict:
    """word(lemma) -> {reading,pos,meaning,notes}"""
    out = {}
    for line in body.splitlines():
        m = re.match(r"^\|\s*(.+?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|", line)
        if not m:
            continue
        word, reading, pos, meaning, notes = (g.strip() for g in m.groups())
        if word in ("Word",) or set(word) <= {"-"}:
            continue
        out[word] = {"reading": reading, "pos": pos, "meaning": meaning, "notes": notes}
    return out

def parse_grammar(body: str) -> list:
    out = []
    for chunk in re.split(r"^###\s+", body, flags=re.M)[1:]:
        lines = chunk.splitlines()
        pattern = re.sub(r"\s*\[src:[^\]]*\]\s*$", "", lines[0]).strip()
        expl = next((l.strip() for l in lines[1:] if l.strip() and not l.startswith(">")), "")
        ex = next((l.lstrip("> ").strip() for l in lines if l.strip().startswith(">")), "")
        if pattern:
            out.append({"pattern": pattern, "explanation": expl, "example_jp": ex})
    return out

def existing_pipeline_ids() -> set:
    """source_ids that already have a PIPELINE-produced reading.json (authoritative).
    Our own fallback output (prompt_version == 'fallback') is NOT counted, so we
    always regenerate/overwrite it."""
    ids = set()
    if READING.exists():
        for p in READING.glob("*.reading.json"):
            try:
                d = json.loads(p.read_text())
                if d.get("prompt_version") != "fallback":
                    ids.add(d.get("source_id"))
            except Exception:  # noqa
                pass
    return ids

def tokenize(jp, vocab, seen):
    toks = []
    for w in tagger()(jp):
        f = w.feature
        surf, pos1 = w.surface, (f.pos1 or "")
        rt = kata2hira(f.kana or "") if has_kanji(surf) else ""
        tok = {"t": surf}
        if rt:
            tok["rt"] = rt
        lemma = (f.lemma or surf or "").strip()
        v = vocab.get(lemma) or vocab.get(surf)
        if v and pos1 not in DROP_POS1 and v.get("meaning"):
            is_new = lemma not in seen
            if is_new:
                seen.add(lemma)
            tok.update({"w": lemma, "m": v["meaning"] or "—", "n": v.get("notes", ""),
                        "pos": v.get("pos", pos1), "key": lemma, "new": is_new})
        toks.append(tok)
    return toks


def build_one(source_id, title, chapters_text, vocab, grammar) -> dict:
    gmade = translate_vocab(source_id, vocab)   # glosses/notes → TARGET_LANG (before tokenizing)
    if gmade:
        print(f"build-reading: translated {gmade} gloss/note(s) → {TARGET_LANG}")
    seen = set()
    chapters = []
    for heading, text in chapters_text:
        paragraphs = []
        for para in paragraphs_of(text):
            sents = []
            for jp in split_sentences(para):
                g_here = [g for g in grammar if g["example_jp"] and g["example_jp"][:6] in jp]
                sents.append({"jp": jp, "en": "", "tokens": tokenize(jp, vocab, seen), "grammar": g_here})
            if sents:
                paragraphs.append({"sentences": sents})
        chapters.append({"chapter": heading or "", "paragraphs": paragraphs, "grammar": grammar})
    # Translate every sentence at build time (baked into `en`, cached) only when
    # explicitly enabled, so normal site builds do not invoke an LLM.
    allsents = [s for ch in chapters for p in ch["paragraphs"] for s in p["sentences"]]
    made = translate_all(source_id, allsents)
    if made:
        print(f"build-reading: translated {made} sentence(s) → {TARGET_LANG}")
    return {"schema": "reading/2", "source_id": source_id, "title": title,
            "lang": "ja", "target_lang": TARGET_LANG, "prompt_version": "fallback", "chapters": chapters}

def main() -> int:
    if not (LANG / "_vocab").exists():
        print("build-reading: no lang/_vocab — nothing to do")
        return 0
    skip = existing_pipeline_ids()
    READING.mkdir(parents=True, exist_ok=True)
    built = 0
    for vf in (LANG / "_vocab").glob("*.md"):
        data, body = parse_frontmatter(vf.read_text(encoding="utf-8"))
        sid = data.get("source_id")
        if not sid or sid in skip:
            continue
        vocab = parse_vocab(body)
        grammar = []
        for gf in (LANG / "_grammar").glob("*.md"):
            gd, gb = parse_frontmatter(gf.read_text(encoding="utf-8"))
            if gd.get("source_id") == sid:
                grammar = parse_grammar(gb); break
        title, chapters_text = vf.stem, []
        for sf in (LANG / "sources").glob("*.md.md"):
            sd, _ = parse_frontmatter(sf.read_text(encoding="utf-8"))
            if sd.get("source_id") != sid:
                continue
            textfile = sf.with_suffix("")  # drop one .md
            if textfile.exists():
                text = textfile.read_text(encoding="utf-8")
                h1 = re.search(r"^#\s+(.+)$", text, re.M)
                title = h1.group(1).strip() if h1 else vf.stem
                secs = re.split(r"^##\s+", text, flags=re.M)
                if len(secs) > 1:
                    for s in secs[1:]:
                        l = s.splitlines()
                        chapters_text.append((l[0].strip(), "\n".join(l[1:]).strip()))
                else:
                    chapters_text.append(("", re.sub(r"^#.*$", "", text, count=1, flags=re.M).strip()))
            break
        if not chapters_text:
            continue
        out = build_one(sid, title, chapters_text, vocab, grammar)
        (READING / f"{sid}.reading.json").write_text(
            json.dumps(out, ensure_ascii=False) + "\n", encoding="utf-8")
        built += 1
        print(f"build-reading: wrote _reading/{sid}.reading.json ({len(chapters_text)} chapter(s))")
    if not built:
        print("build-reading: nothing to build (all sources have pipeline JSON or no text)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
