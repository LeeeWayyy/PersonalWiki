import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

INGEST_SPEC = importlib.util.spec_from_file_location("ingest_mod", ROOT / "ingest.py")
ingest = importlib.util.module_from_spec(INGEST_SPEC)
sys.modules["ingest_mod"] = ingest
INGEST_SPEC.loader.exec_module(ingest)


def _args(input_path):
    return types.SimpleNamespace(
        input=str(input_path), section="", images_only=False, kind="",
        rerender=False, profile="wiki", limit="100000", model="",
        section_label="", chapters=False)


def _seed_vault(vault: Path, sha: str, source_id: str, done_labels):
    (vault / "sources").mkdir(parents=True, exist_ok=True)
    (vault / ".wiki").mkdir(parents=True, exist_ok=True)
    (vault / "sources" / "2026-01-01-book.epub.md").write_text(
        "---\n"
        f"source_id: {source_id}\n"
        "type: source\n"
        f"sha256: {sha}\n"
        "title: book\n"
        "---\n\n# book\n", encoding="utf-8")
    if done_labels:
        lines = "".join(
            f"2026-01-01T00:00:00Z  {source_id}#{lbl}  pages: wiki/x.md\n"
            for lbl in done_labels)
        (vault / ".wiki" / "log.md").write_text(lines, encoding="utf-8")


class GroupChaptersTests(unittest.TestCase):
    def test_orders_and_anchors(self):
        chs = ingest._group_chapters(["第1章", "第2章"])
        self.assertEqual([c[0] for c in chs], ["第1章", "第2章"])
        self.assertEqual(chs[0][1], "^第1章$")

    def test_identical_titles_collapse(self):
        chs = ingest._group_chapters(["Intro", "Body", "Intro"])
        self.assertEqual([c[0] for c in chs], ["Intro", "Body"])   # deduped, order kept
        self.assertEqual(chs[0][1], "^Intro$")

    def test_regex_special_titles_escaped(self):
        chs = ingest._group_chapters(["Ch. 1 (a)"])
        self.assertEqual(chs[0][1], r"^Ch\.\ 1\ \(a\)$")


class AutoChapterTests(unittest.TestCase):
    def test_auto_chapter_only_local_ebooks_without_slicing_flags(self):
        with tempfile.TemporaryDirectory() as d:
            book = Path(d) / "book.epub"
            book.write_bytes(b"epub-bytes")

            args = _args(book)
            self.assertTrue(ingest._should_auto_chapter(args))

            args = _args("https://example.com/book.epub")
            self.assertFalse(ingest._should_auto_chapter(args))

            args = _args(book)
            args.section_label = "第1章"
            self.assertFalse(ingest._should_auto_chapter(args))

            pdf = Path(d) / "paper.pdf"
            pdf.write_bytes(b"pdf-bytes")
            self.assertFalse(ingest._should_auto_chapter(_args(pdf)))

    def test_child_marker_prevents_reentry(self):
        # A no-section child (whole-unit fallback) must NOT re-auto-chapter, or a
        # heading-less ebook fork-bombs. _run_one_chapter sets the marker; with it
        # set, _should_auto_chapter refuses even for a local ebook.
        with tempfile.TemporaryDirectory() as d:
            book = Path(d) / "book.epub"
            book.write_bytes(b"epub-bytes")
            captured = {}

            def fake_run(argv, env=None):
                captured["env"] = env or {}
                return types.SimpleNamespace(returncode=0)

            with patch.object(ingest.subprocess, "run", fake_run):
                ingest._run_one_chapter(_args(book), section=None, label=None)
            self.assertEqual(captured["env"].get("PW_INGEST_NO_AUTOCHAPTER"), "1")

            with patch.dict(os.environ, {"PW_INGEST_NO_AUTOCHAPTER": "1"}):
                self.assertFalse(ingest._should_auto_chapter(_args(book)))


class LogProgressTests(unittest.TestCase):
    def test_pages_text_inside_label_round_trips(self):
        source_id = "01BOOKAAAAAAAAAAAAAAAAAAAA"
        labels, whole_done = ingest._source_log_progress(
            [f"2026-01-01T00:00:00Z  {source_id}#Front pages: a history  pages: wiki/x.md"],
            source_id,
        )
        self.assertFalse(whole_done)
        self.assertEqual(labels, {"Front pages: a history"})


class RunChapteredTests(unittest.TestCase):
    def _run(self, vault, input_path, titles, run_returns):
        calls = []

        def fake_run_one(args, section, label, *, skip_assets=False):
            calls.append((label, skip_assets))
            return run_returns.get(label, 0)

        # Titles may be plain strings (all substantial) or (title, size) pairs.
        sections = [t if isinstance(t, tuple) else (t, 10_000) for t in titles]
        with patch.object(ingest, "VAULT_ROOT", vault), \
             patch.object(ingest, "_enumerate_sections", return_value=sections), \
             patch.object(ingest, "_run_one_chapter", side_effect=fake_run_one):
            rc = ingest.run_chaptered(_args(input_path))
        return rc, calls

    def test_fresh_run_ingests_all(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            (vault / "sources").mkdir(parents=True)
            book = vault / "book.epub"
            book.write_bytes(b"epub-bytes")
            rc, calls = self._run(vault, book, ["A", "B", "C"], {})
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [("A", False), ("B", True), ("C", True)])

    def test_resume_skips_done_chapters(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            book = vault / "book.epub"
            book.write_bytes(b"epub-bytes")
            sha = ingest.sha256_of(book)
            _seed_vault(vault, sha, "01BOOKAAAAAAAAAAAAAAAAAAAA", ["A", "B"])
            rc, calls = self._run(vault, book, ["A", "B", "C", "D"], {})
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [("C", False), ("D", True)])  # A, B skipped from log

    def test_resume_uses_prefix_done_semantics(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            book = vault / "book.epub"
            book.write_bytes(b"epub-bytes")
            sha = ingest.sha256_of(book)
            _seed_vault(vault, sha, "01BOOKAAAAAAAAAAAAAAAAAAAA", ["第1章"])
            rc, calls = self._run(vault, book, ["第1章 Full Title", "第2章"], {})
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [("第2章", False)])

    def test_whole_doc_prior_log_skips_all_chapters(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            book = vault / "book.epub"
            book.write_bytes(b"epub-bytes")
            sha = ingest.sha256_of(book)
            _seed_vault(vault, sha, "01BOOKAAAAAAAAAAAAAAAAAAAA", [])
            (vault / ".wiki" / "log.md").write_text(
                "2026-01-01T00:00:00Z  01BOOKAAAAAAAAAAAAAAAAAAAA  pages: wiki/x.md\n",
                encoding="utf-8",
            )
            rc, calls = self._run(vault, book, ["A", "B"], {})
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [])

    def test_failure_stops_and_preserves_remaining(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            (vault / "sources").mkdir(parents=True)
            book = vault / "book.epub"
            book.write_bytes(b"epub-bytes")
            rc, calls = self._run(vault, book, ["A", "B", "C"], {"B": 1})
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [("A", False), ("B", True)])  # stopped at B, C not attempted

    def test_no_chapters_falls_back_to_single_unit(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            (vault / "sources").mkdir(parents=True)
            book = vault / "book.epub"
            book.write_bytes(b"epub-bytes")
            rc, calls = self._run(vault, book, [], {})     # extract found no headings
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [(None, False)])       # one whole-source run

    def test_thin_sections_skipped_in_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            (vault / "sources").mkdir(parents=True)
            book = vault / "book.epub"
            book.write_bytes(b"epub-bytes")
            # No chapter markers → fallback per-section: cover (1) and a title-only
            # page (5) are structural → skipped; the two real sections ingest.
            titles = [("cover.xhtml", 1), ("preface", 5),
                      ("body-a", 9000), ("body-b", 9000)]
            rc, calls = self._run(vault, book, titles, {})
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [("body-a", False), ("body-b", True)])

    def test_groups_sections_under_chapters(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            (vault / "sources").mkdir(parents=True)
            book = vault / "book.epub"
            book.write_bytes(b"epub-bytes")
            titles = [("序言", 5000), ("导言 x", 16000),
                      ("第一章 A", 5000), ("第一节 a", 9000), ("第二节 b", 9000),
                      ("第二章 B", 4000), ("第三节 c", 9000),
                      ("后记", 9000), ("词汇表", 5000)]
            rc, calls = self._run(vault, book, titles, {})
            self.assertEqual(rc, 0)
            # Only the 2 chapters ingest; 序言/导言/后记/词汇表 excluded.
            self.assertEqual([c[0] for c in calls], ["第一章 A", "第二章 B"])


class GroupByChapterTests(unittest.TestCase):
    def test_sections_grouped_front_back_excluded(self):
        sections = [("cover.xhtml", 1), ("序言", 5000), ("导言 世界", 16000),
                    ("第一章 起源", 5000), ("第一节 鸿沟", 9000), ("第二节 祖先", 9000),
                    ("第二章 生命力", 4000), ("第三节 起源", 9000),
                    ("后记", 9000), ("词汇表", 5000)]
        groups = ingest._group_by_chapter(sections)
        self.assertEqual([g[0] for g in groups], ["第一章 起源", "第二章 生命力"])
        self.assertEqual(groups[0][1], ["第一章 起源", "第一节 鸿沟", "第二节 祖先"])
        self.assertEqual(groups[1][1], ["第二章 生命力", "第三节 起源"])

    def test_no_chapter_markers_returns_empty(self):
        self.assertEqual(
            ingest._group_by_chapter([("intro", 100), ("body", 9000)]), [])

    def test_section_before_first_chapter_is_dropped(self):
        # A stray 节 before any 章 is not inside a chapter → excluded.
        groups = ingest._group_by_chapter([("第九节 orphan", 9000),
                                           ("第一章 A", 5000), ("第一节 a", 9000)])
        self.assertEqual([g[0] for g in groups], ["第一章 A"])
        self.assertEqual(groups[0][1], ["第一章 A", "第一节 a"])

    def test_anchored_regex_matches_all_members(self):
        rx = ingest._anchored_regex(["第一章 A", "第一节 a"])
        self.assertEqual(rx, r"^(?:第一章\ A|第一节\ a)$")


class SectionSizesTests(unittest.TestCase):
    def test_counts_body_chars_per_heading(self):
        text = (
            "## Cover\n"
            "## 第一节\n"
            "line one of real content\n"
            "line two here\n"
            "## Empty\n"
        )
        sizes = dict(ingest._section_sizes(text))
        self.assertEqual(sizes["Cover"], 0)
        self.assertEqual(sizes["Empty"], 0)
        self.assertGreater(sizes["第一节"], 20)


if __name__ == "__main__":
    unittest.main()
