"""Timed-transcript lang path: extract.py's .transcript.json extraction and
generate-language-pages.py's sentence↔timestamp alignment."""

import importlib.util
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name: str, filename: str, stubs: dict):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _stub(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


try:
    extract = _load("extract_transcript_test_mod", "extract.py",
                    {"markdownify": _stub("markdownify", markdownify=lambda h, **kw: h)})
    genlang = _load("genlang_transcript_test_mod", "generate-language-pages.py",
                    {"fugashi": _stub("fugashi", Tagger=None)})
    _load_error = None
except ImportError as e:  # bs4/yaml absent (bare CI python) → skip, don't error.
    extract = genlang = None
    _load_error = e


DOC = {
    "language": "ja",
    "meta": {"audio_ext": "m4a"},
    "segments": [
        {"text": "こんにちは。", "start": 0.0, "end": 1.2, "speaker": "S0",
         "words": [{"word": "こんにちは", "start": 0.1, "end": 1.0},
                   {"word": "。", "start": 1.0, "end": 1.2}]},
        {"text": "今日はいい天気です。", "start": 1.5, "end": 3.4, "speaker": "S0",
         "words": [{"word": "今日", "start": 1.5, "end": 1.9},
                   {"word": "は", "start": 1.9, "end": 2.0},
                   {"word": "いい", "start": 2.0, "end": 2.4},
                   {"word": "天気", "start": 2.4, "end": 2.9},
                   {"word": "です", "start": 2.9, "end": 3.3}]},
        # >2s silence gap → new paragraph; no word timings → segment fallback.
        {"text": "散歩に行きましょう。", "start": 6.0, "end": 8.0, "speaker": "S0"},
        # speaker change → new paragraph
        {"text": "そうですね。", "start": 8.2, "end": 9.0, "speaker": "S1"},
    ],
}


@unittest.skipIf(extract is None, f"deps unavailable: {_load_error}")
class TestExtractTranscriptJson(unittest.TestCase):
    def _extract(self, doc):
        with tempfile.NamedTemporaryFile("w", suffix=".transcript.json",
                                         delete=False) as f:
            json.dump(doc, f, ensure_ascii=False)
        return extract.extract_transcript_json(Path(f.name))

    def test_paragraph_breaks_on_gap_and_speaker(self):
        text = self._extract(DOC)
        paras = [p.replace("\n", "") for p in text.strip().split("\n\n")]
        self.assertEqual(paras, ["こんにちは。今日はいい天気です。",
                                 "散歩に行きましょう。",
                                 "そうですね。"])

    def test_dispatch_routes_transcript_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".transcript.json",
                                         delete=False) as f:
            json.dump(DOC, f, ensure_ascii=False)
        self.assertIn("こんにちは。", extract.dispatch(f.name))

    def test_empty_segments_skipped(self):
        text = self._extract({"segments": [{"text": "  ", "start": 0, "end": 1},
                                           {"text": "あ。", "start": 1, "end": 2}]})
        self.assertEqual(text, "あ。\n")


@unittest.skipIf(genlang is None, f"deps unavailable: {_load_error}")
class TestAlignSentenceTimes(unittest.TestCase):
    def _per_chapter(self):
        # The deterministic sentence split of the extracted text above.
        sents = ["こんにちは。", "今日はいい天気です。", "散歩に行きましょう。", "そうですね。"]
        return [("whole", {"sentences": [{"jp": s, "en": ""} for s in sents]})]

    def test_word_level_and_segment_fallback(self):
        per_chapter = self._per_chapter()
        timed = genlang.align_sentence_times(DOC, per_chapter)
        self.assertEqual(timed, 4)
        s = per_chapter[0][1]["sentences"]
        self.assertEqual((s[0]["t0"], s[0]["t1"]), (0.1, 1.2))   # word-level
        self.assertEqual((s[1]["t0"], s[1]["t1"]), (1.5, 3.3))
        self.assertEqual((s[2]["t0"], s[2]["t1"]), (6.0, 8.0))   # segment fallback
        self.assertEqual((s[3]["t0"], s[3]["t1"]), (8.2, 9.0))

    def test_unmatched_sentence_stays_untimed(self):
        per_chapter = [("whole", {"sentences": [{"jp": "全然違う文。", "en": ""}]})]
        genlang.align_sentence_times({"segments": DOC["segments"][:1]}, per_chapter)
        self.assertNotIn("t0", per_chapter[0][1]["sentences"][0])

    def test_audio_src_from_meta(self):
        asset = Path("/x/sources/2026-07-14-foo.transcript.json")
        self.assertEqual(genlang.audio_src_for(asset, DOC),
                         "../sources/.media/2026-07-14-foo.m4a")
        self.assertIsNone(genlang.audio_src_for(asset, {"meta": {}}))


@unittest.skipIf(genlang is None, f"deps unavailable: {_load_error}")
class TestAnnotateBatching(unittest.TestCase):
    """Oversized chapters (heading-less transcripts) split into several LLM
    calls with GLOBAL sentence/word numbering; an all-empty merge must raise
    instead of poisoning the cache."""

    def test_batches_keep_global_numbering_and_merge(self):
        sents = [f"文{i}。" for i in range(1, 6)]
        words = [{"key": f"k{i}", "lemma": f"w{i}", "reading": "r", "pos": "名詞"}
                 for i in range(1, 6)]
        prompts = []

        def fake_llm(prompt, timeout):
            prompts.append(prompt)
            nums = [int(n) for n in re.findall(r"(?m)^(\d+)\. 文", prompt)]
            return json.dumps({"sentences": [{"s": n, "en": f"e{n}"} for n in nums]})

        with patch.object(genlang, "BATCH_SENTS", 2), \
             patch.object(genlang.dl, "call_llm", side_effect=fake_llm):
            obj = genlang.annotate(sents, words, "t", "whole", jobs=1)
        self.assertEqual(len(prompts), 3)                       # 2+2+1 sentences
        self.assertIn("3. 文3。", prompts[1])                   # numbering is global
        self.assertIn("5. w5（r）[名詞]", prompts[2])            # word numbering too
        self.assertEqual([s["s"] for s in obj["sentences"]], [1, 2, 3, 4, 5])

    def test_unparseable_batch_retries_once(self):
        calls = []

        def flaky(prompt, timeout):
            calls.append(prompt)
            if len(calls) == 1:
                return "sorry, I cannot do that"
            nums = [int(n) for n in re.findall(r"(?m)^(\d+)\. 文", prompt)]
            return json.dumps({"sentences": [{"s": n, "en": "e"} for n in nums]})

        with patch.object(genlang, "BATCH_SENTS", 2), \
             patch.object(genlang.dl, "call_llm", side_effect=flaky):
            obj = genlang.annotate([f"文{i}。" for i in range(1, 4)], [],
                                   "t", "whole", jobs=1)
        self.assertEqual(len(calls), 3)          # 2 batches + 1 retry
        self.assertEqual(len(obj["sentences"]), 3)

    def test_all_empty_annotations_raise_not_cache(self):
        with tempfile.TemporaryDirectory() as td:
            meta = {"sha256": "0" * 64, "source_id": "SID",
                    "asset": Path(td) / "a.transcript.json", "title": "t"}
            with patch.object(genlang, "CACHE_DIR", Path(td)), \
                 patch.object(genlang.dl, "extract_source_text", return_value="犬。猫。"), \
                 patch.object(genlang, "content_words", return_value=[]), \
                 patch.object(genlang.dl, "call_llm", return_value="{}"):
                with self.assertRaises(RuntimeError):
                    genlang.chapter_data(meta, 1, "whole", None,
                                         refresh=True, dry_run=False)
            self.assertEqual(list(Path(td).glob("SID.*")), [])  # nothing cached


if __name__ == "__main__":
    unittest.main()
