import importlib.util
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load_merge():
    fugashi = types.ModuleType("fugashi")
    fugashi.Tagger = None
    spec = importlib.util.spec_from_file_location("merge_lang_test_mod",
                                                  SCRIPTS / "merge-lang-readers.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"fugashi": fugashi}):
        spec.loader.exec_module(module)
    return module


try:
    merge = _load_merge()
    _load_error = None
except ImportError as exc:
    merge = None
    _load_error = exc


@unittest.skipIf(merge is None, f"deps unavailable: {_load_error}")
class MergeLangReadersTests(unittest.TestCase):
    def test_loads_headingless_committed_reader_without_language_cache(self):
        doc = {
            "source_id": "BOOK",
            "chapters": [{
                "chapter": "whole",
                "paragraphs": [{"sentences": [{
                    "jp": "さむい。", "en": "It is cold.",
                    "tokens": [{"t": "さむい", "w": "寒い", "m": "cold",
                                "n": "", "pos": "形容詞", "key": "old-key"}],
                }]}],
                "grammar": [{"pattern": "〜い", "s": 1}],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            reading_dir = Path(tmp)
            (reading_dir / "book.reading.json").write_text(json.dumps(doc), encoding="utf-8")
            with (patch.object(merge.glp, "READING_DIR", reading_dir),
                  patch.object(merge.glp, "SOURCES_DIR", reading_dir / "sources"),
                  patch.object(merge.glp, "WHOLE_LABEL", "whole"),
                  patch.object(merge.glp, "chapter_key", side_effect=lambda value: value),
                  patch.object(merge.glp, "chapter_data", side_effect=AssertionError("cache used")),
                  patch.object(merge.dl, "find_sources", return_value={
                      "BOOK": {"title": "Book", "asset": Path("book.txt"), "sha256": "a" * 64}
                  })):
                _, chapters, meanings = merge.load_book("BOOK")

        self.assertEqual(chapters[0][0], "whole")
        self.assertEqual(chapters[0][1]["sentences"][0]["en"], "It is cold.")
        self.assertEqual(chapters[0][1]["grammar_points"][0]["pattern"], "〜い")
        self.assertEqual(meanings["old-key"]["meaning_en"], "cold")

    def test_alignment_guard_allows_known_pair_and_rejects_noise(self):
        self.assertAlmostEqual(merge.require_meaningful_alignment(843, 1000), .843)
        with self.assertRaises(SystemExit):
            merge.require_meaningful_alignment(799, 1000)
        with self.assertRaises(SystemExit):
            merge.require_meaningful_alignment(0, 0)

    def test_calibration_cache_identity_includes_audio_asr(self):
        base = merge.calibration_sha("book", "AUDIO-A", "asr-a")
        self.assertNotEqual(base, merge.calibration_sha("book", "AUDIO-B", "asr-a"))
        self.assertNotEqual(base, merge.calibration_sha("book", "AUDIO-A", "asr-b"))

    def test_pronunciation_guard_requires_the_same_reading(self):
        readings = {"orig": "abcdefghij", "changed": "abcdefghiX", "same": "abcdefghij"}
        with (patch.object(merge, "reading_of", side_effect=lambda text: readings[text]),
              patch.object(merge, "_calibrate_batch", return_value={1: "changed"}),
              patch.object(merge.glp, "BATCH_SENTS", 40)):
            calibrated, _, drift = merge.calibrate_chapter(
                1, "chapter", ["orig"], "asr", "sha", "book", False)
        self.assertEqual(calibrated, ["orig"])
        self.assertEqual(drift, 1)

        with (patch.object(merge, "reading_of", side_effect=lambda text: readings[text]),
              patch.object(merge, "_calibrate_batch", return_value={1: "same"}),
              patch.object(merge.glp, "BATCH_SENTS", 40)):
            calibrated, _, drift = merge.calibrate_chapter(
                1, "chapter", ["orig"], "asr", "sha", "book", False)
        self.assertEqual(calibrated, ["same"])
        self.assertEqual(drift, 0)

    def test_gloss_metadata_follows_calibrated_token_by_reading_span(self):
        feature = types.SimpleNamespace(kana="サムイ")
        original = types.SimpleNamespace(surface="さむい", feature=feature)
        calibrated = types.SimpleNamespace(surface="寒い", feature=feature)
        meaning = {"key": "old", "lemma": "寒い", "meaning_en": "cold", "notes": ""}
        meanings = {"old": meaning}
        with (patch.object(merge.glp, "tagger",
                           return_value=lambda text: [original if text == "さむい" else calibrated]),
              patch.object(merge.glp, "token_key", return_value="new")):
            merge.preserve_meanings(
                "さむい", [{"t": "さむい", "key": "old"}], "寒い", meanings)
        self.assertEqual(meanings["new"]["meaning_en"], "cold")
        self.assertEqual(meanings["new"]["key"], "new")

    def test_structured_reader_rejects_empty_token_surfaces(self):
        def reading(surface):
            return {"chapters": [{"paragraphs": [{"sentences": [
                {"tokens": [{"t": surface}]}
            ]}]}]}

        merge.require_visible_tokens(reading("寒い"))
        with self.assertRaisesRegex(RuntimeError, "empty token surface"):
            merge.require_visible_tokens(reading(""))

    def test_compound_alias_uses_the_greatest_reading_overlap(self):
        def word(surface, kana):
            return types.SimpleNamespace(surface=surface,
                                         feature=types.SimpleNamespace(kana=kana))

        analyzed = {
            "小むぎ": [word("小", "コ"), word("むぎ", "ムギ")],
            "小麦": [word("小麦", "コムギ")],
        }
        meanings = {
            "small": {"key": "small", "lemma": "小", "meaning_en": "small"},
            "wheat": {"key": "wheat", "lemma": "麦", "meaning_en": "wheat"},
        }
        with (patch.object(merge.glp, "tagger",
                           return_value=lambda text: analyzed[text]),
            patch.object(merge.glp, "token_key", return_value="compound")):
            merge.preserve_meanings(
                "小むぎ", [{"t": "小", "key": "small"},
                            {"t": "むぎ", "key": "wheat"}],
                "小麦", meanings)
        self.assertEqual(meanings["compound"]["lemma"], "麦")

    def test_gloss_spans_use_exact_sentence_with_spaces_and_fullwidth_equals(self):
        def word(surface, reading=None):
            return types.SimpleNamespace(
                surface=surface, feature=types.SimpleNamespace(kana=reading))

        original = "LE PETIT PRINCE アントワーヌ＝Saint-Exupery ほしからでる。"
        calibrated = "LE PETIT PRINCE アントワーヌ＝Saint-Exupery 星から出る。"
        old_specs = [("LE", None), ("PETIT", None), ("PRINCE", None),
                     ("アントワーヌ", "アントワーヌ"), ("＝", None),
                     ("Saint", None), ("-", None), ("Exupery", None),
                     ("ほし", "ホシ"), ("から", "カラ"), ("でる", "デル"), ("。", None)]
        new_specs = [*old_specs[:8], ("星", "ホシ"), ("から", "カラ"),
                     ("出る", "デル"), ("。", None)]
        analyzed = {
            original: [word(*spec) for spec in old_specs],
            calibrated: [word(*spec) for spec in new_specs],
        }
        tokens = [{"t": surface, "key": f"old-{surface}"}
                  for surface, _reading in old_specs]
        meanings = {token["key"]: {"key": token["key"], "lemma": token["t"],
                                    "meaning_en": token["t"]}
                    for token in tokens if token["t"] not in {"＝", "-", "から", "。"}}

        with (patch.object(merge.glp, "tagger", return_value=lambda text: analyzed[text]),
              patch.object(merge.glp, "token_key",
                           side_effect=lambda _feature, surface: f"new-{surface}")):
            merge.preserve_meanings(original, tokens, calibrated, meanings)

        for old, new in (("PRINCE", "PRINCE"), ("アントワーヌ", "アントワーヌ"),
                         ("Saint", "Saint"), ("Exupery", "Exupery"),
                         ("ほし", "星"), ("でる", "出る")):
            self.assertEqual(meanings[f"new-{new}"]["lemma"], old)

    def test_no_calibrate_still_rekeys_durable_glosses(self):
        sentence = {"jp": "さむい。", "_tokens": [{"t": "さむい", "key": "old"}]}
        chapters = [("whole", {"sentences": [sentence], "grammar_points": []})]
        args = types.SimpleNamespace(book_id="BOOK", audio_id="AUDIO", no_calibrate=True,
                                     limit=0, refresh=False, commit=False)
        with tempfile.TemporaryDirectory() as tmp:
            args.out_dir = tmp
            with (patch.object(merge, "load_book",
                               return_value=({"title": "Book", "sha256": "book-sha"},
                                             chapters, {"old": {"key": "old"}})),
                  patch.object(merge.dl, "find_sources", return_value={
                      "AUDIO": {"asset": Path("audio.json"), "sha256": "audio-sha"}}),
                  patch.object(merge, "audio_streams", return_value=([], [], {})),
                  patch.object(merge, "align_book_to_audio", return_value=1),
                  patch.object(merge, "require_meaningful_alignment", return_value=1.0),
                  patch.object(merge, "preserve_meanings") as preserve,
                  patch.object(merge.glp, "audio_src_for", return_value=None),
                  patch.object(merge.glp, "build_reading_json",
                               return_value={"chapters": []}),
                  patch.object(merge, "render_merged_html", return_value="<html></html>"),
                  patch.object(merge.dl, "clean_title", return_value="Book")):
                merge.run(args)

        preserve.assert_called_once_with("さむい。", sentence["_tokens"],
                                         "さむい。", {"old": {"key": "old"}})

    def test_commit_cites_book_and_rolls_back_when_lint_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reading_dir = root / "lang" / "_reading"
            calls = []
            signal_calls = []
            previous_handler = object()

            def render(_display, source_id, *_args):
                calls.append(source_id)
                return f"<head></head><!-- [src:{source_id}] -->"

            def fake_run(argv, **_kwargs):
                if any(str(arg).endswith("lint.py") for arg in argv):
                    return subprocess.CompletedProcess(argv, 1)
                if "status" in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout="?? merged-reader")
                return subprocess.CompletedProcess(argv, 0)

            with (patch.object(merge.glp, "READING_DIR", reading_dir),
                  patch.object(merge.glp, "build_reading_json",
                               side_effect=lambda _d, sid, *_a: {"source_id": sid}),
                  patch.object(merge.glp, "render_reading_html", side_effect=render),
                  patch.object(merge.dl, "clean_title", side_effect=lambda title: title),
                  patch.object(merge.subprocess, "check_output", return_value=str(root)),
                  patch.object(merge.subprocess, "run", side_effect=fake_run),
                  patch.object(merge.signal, "getsignal", return_value=previous_handler),
                  patch.object(merge.signal, "signal",
                               side_effect=lambda _sig, handler: signal_calls.append(handler))):
                html = merge.render_merged_html("Book", [], {}, None, "BOOK", "AUDIO")
                self.assertIn("[src:BOOK]", html)
                self.assertIn("[src:AUDIO]", html)
                self.assertNotIn("src:merge-", html)
                calls.clear()
                with self.assertRaisesRegex(RuntimeError, "lang lint rejected"):
                    merge.write_and_commit([], {}, None, {"title": "Book"}, "BOOK", "AUDIO")

            self.assertEqual(calls, ["BOOK"])
            self.assertFalse(any(reading_dir.glob("merge-*")))
            with self.assertRaises(SystemExit):
                signal_calls[0](merge.signal.SIGTERM, None)
            self.assertIs(signal_calls[-1], previous_handler)

    def test_commit_lock_uses_lang_ingest_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []
            with (patch.object(merge.glp, "VAULT_ROOT", root),
                  patch.object(merge.fcntl, "flock", side_effect=lambda _fd, op: calls.append(op))):
                with merge.content_ingest_lock():
                    self.assertTrue((root / ".wiki" / "ingest.lock").exists())
            self.assertEqual(calls, [merge.fcntl.LOCK_EX | merge.fcntl.LOCK_NB,
                                     merge.fcntl.LOCK_UN])

    def test_sigterm_after_commit_does_not_restore_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reading_dir = root / "lang" / "_reading"
            handlers = []

            def fake_run(argv, **_kwargs):
                if any(str(arg).endswith("lint.py") for arg in argv):
                    return subprocess.CompletedProcess(argv, 0)
                if "status" in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout="")
                if "diff" in argv:
                    return subprocess.CompletedProcess(argv, 1)
                if "commit" in argv:
                    handlers[-1](merge.signal.SIGTERM, None)
                return subprocess.CompletedProcess(argv, 0)

            with (patch.object(merge.glp, "READING_DIR", reading_dir),
                  patch.object(merge.glp, "build_reading_json", return_value={"chapters": []}),
                  patch.object(merge.glp, "render_reading_html", return_value="<head></head>"),
                  patch.object(merge.dl, "clean_title", side_effect=lambda title: title),
                  patch.object(merge.subprocess, "check_output", return_value=str(root)),
                  patch.object(merge.subprocess, "run", side_effect=fake_run),
                  patch.object(merge.signal, "getsignal", return_value=object()),
                  patch.object(merge.signal, "signal",
                               side_effect=lambda _sig, handler: handlers.append(handler))):
                with self.assertRaises(SystemExit):
                    merge.write_and_commit([], {}, None, {"title": "Book"}, "BOOK", "AUDIO")

            mid = merge.merged_id("BOOK", "AUDIO")
            self.assertTrue((reading_dir / f"{mid}.html").exists())
            self.assertTrue((reading_dir / f"{mid}.reading.json").exists())

    def test_interrupted_atomic_write_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reading_dir = root / "lang" / "_reading"

            def interrupted_write(path, content, _dry_run):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.with_suffix(path.suffix + ".tmp").write_text(content, encoding="utf-8")
                raise SystemExit(143)

            def fake_run(argv, **_kwargs):
                if "status" in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout="")
                return subprocess.CompletedProcess(argv, 0)

            with (patch.object(merge.glp, "READING_DIR", reading_dir),
                  patch.object(merge.glp, "build_reading_json", return_value={"chapters": []}),
                  patch.object(merge.glp, "render_reading_html", return_value="<head></head>"),
                  patch.object(merge.dl, "clean_title", side_effect=lambda title: title),
                  patch.object(merge.dl, "atomic_write", side_effect=interrupted_write),
                  patch.object(merge.subprocess, "check_output", return_value=str(root)),
                  patch.object(merge.subprocess, "run", side_effect=fake_run),
                  patch.object(merge.signal, "getsignal", return_value=object()),
                  patch.object(merge.signal, "signal")):
                with self.assertRaises(SystemExit):
                    merge.write_and_commit([], {}, None, {"title": "Book"}, "BOOK", "AUDIO")

            self.assertFalse(list(reading_dir.glob("*.tmp")))
            self.assertFalse(list(reading_dir.glob("merge-*")))


if __name__ == "__main__":
    unittest.main()
