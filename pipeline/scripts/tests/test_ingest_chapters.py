import importlib.util
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from _util import default_vault_root  # noqa: E402

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


def _write_minimal_epub(path: Path) -> None:
    container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    content_opf = """<?xml version="1.0" encoding="UTF-8"?>
<package version="3.0" unique-identifier="bookid" xmlns="http://www.idpf.org/2007/opf">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">chaptered-smoke</dc:identifier>
    <dc:title>Chaptered Smoke</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chap1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="chap2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="chap1"/>
    <itemref idref="chap2"/>
  </spine>
</package>
"""
    nav = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Contents</title></head>
  <body><nav epub:type="toc"><ol>
    <li><a href="chapter1.xhtml">Chapter 1</a></li>
    <li><a href="chapter2.xhtml">Chapter 2</a></li>
  </ol></nav></body>
</html>
"""
    chapter = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>{title}</title></head>
  <body>
    <h1>{title}</h1>
    <p>Mitochondria and ATP appear in this chaptered smoke source.</p>
  </body>
</html>
"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in (
            ("META-INF/container.xml", container_xml),
            ("OEBPS/content.opf", content_opf),
            ("OEBPS/nav.xhtml", nav),
            ("OEBPS/chapter1.xhtml", chapter.format(title="Chapter 1")),
            ("OEBPS/chapter2.xhtml", chapter.format(title="Chapter 2")),
        ):
            zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)


class GroupChaptersTests(unittest.TestCase):
    def test_orders_and_anchors(self):
        chs = ingest._group_chapters(["第1章", "第2章"])
        self.assertEqual([c[0] for c in chs], ["第1章", "第2章"])
        self.assertEqual(chs[0][1], "^第1章$")

    def test_identical_titles_remain_distinct(self):
        chs = ingest._group_chapters(["Intro", "Body", "Intro"])
        self.assertEqual([c[0] for c in chs], ["Intro", "Body", "Intro"])
        self.assertEqual(chs[0][1], "^Intro$")

    def test_duplicate_titles_receive_stable_occurrence_identities(self):
        instances = ingest._stable_chapter_instances(
            ingest._group_chapters(["Interlude", "Body", "Interlude"])
        )
        self.assertEqual(
            [instance[0] for instance in instances],
            ["Interlude [occurrence 1/2]", "Body", "Interlude [occurrence 2/2]"],
        )
        self.assertEqual([instance[3] for instance in instances], [1, None, 2])

    def test_normalized_duplicate_labels_select_their_own_exact_heading(self):
        instances = ingest._stable_chapter_instances(
            ingest._group_chapters(["ATP", "ＡＴＰ"])
        )
        self.assertEqual(
            [instance[0] for instance in instances],
            ["ATP [occurrence 1/2]", "ＡＴＰ [occurrence 2/2]"],
        )
        self.assertEqual([instance[3] for instance in instances], [1, 1])

    def test_selects_repeated_heading_occurrence_without_merging(self):
        text = "## Interlude\nfirst\n## Interlude\nsecond\n"
        self.assertEqual(
            ingest._select_section_occurrence(text, "Interlude", 2),
            "## Interlude\nsecond\n",
        )

    def test_grouped_ranges_do_not_cross_contaminate_repeated_subheadings(self):
        sections = [
            ("Chapter 1", 500), ("Introduction", 5000),
            ("Chapter 2", 500), ("Introduction", 6000),
        ]
        groups = ingest._grouped_chapter_ranges(sections)
        self.assertEqual(
            [(label, start, end) for label, _members, start, end in groups],
            [("Chapter 1", 0, 2), ("Chapter 2", 2, 4)],
        )
        text = (
            "## Chapter 1\nfirst chapter\n## Introduction\nfirst intro\n"
            "## Chapter 2\nsecond chapter\n## Introduction\nsecond intro\n"
        )
        first = ingest._select_heading_range(text, 0, 2)
        second = ingest._select_heading_range(text, 2, 4)
        self.assertIn("first intro", first)
        self.assertNotIn("second intro", first)
        self.assertIn("second intro", second)
        self.assertNotIn("first intro", second)

    def test_regex_special_titles_escaped(self):
        chs = ingest._group_chapters(["Ch. 1 (a)"])
        self.assertEqual(chs[0][1], r"^Ch\.\ 1\ \(a\)$")


class CitationAnchorTests(unittest.TestCase):
    def test_citation_keys_use_shared_encoded_multi_source_contract(self):
        first = "01KX582AX79FD9BQG2VNMG41NY"
        second = "01KX582AX79FD9BQG2VNMG41NZ"
        keys = ingest._citation_keys(
            f"[src:{first}#sec=Supply%2C%20Demand,src:{second}#frame-2]"
        )
        self.assertEqual(
            keys,
            {f"{first}#sec=Supply%2C%20Demand", f"{second}#frame-2"},
        )

    def test_missing_prior_chapter_anchor_is_detected(self):
        sid = "01KX582AX79FD9BQG2VNMG41NY"
        old = (
            f"> first [src:{sid}#第一章]\n"
            f"> second [src:{sid}#第二章]\n"
        )
        new = f"> third [src:{sid}#第三章]\n"
        old_keys = ingest._citation_keys(old)
        new_keys = ingest._citation_keys(new)
        missing = sorted(
            key for key in old_keys
            if not ingest._citation_key_still_present(key, new_keys)
        )
        self.assertEqual(missing, [f"{sid}#第一章", f"{sid}#第二章"])

    def test_bare_source_may_be_replaced_by_anchored_source(self):
        sid = "01KX582AX79FD9BQG2VNMG41NY"
        self.assertTrue(
            ingest._citation_key_still_present(sid, {f"{sid}#第一章"})
        )
        self.assertFalse(
            ingest._citation_key_still_present(f"{sid}#第一章", {sid})
        )


class ScaffoldTests(unittest.TestCase):
    def test_existing_vault_gets_local_chapter_cache_ignore(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            (vault / ".gitignore").write_text("custom-rule\n", encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                os.chdir(vault)
                ingest.ensure_wiki_scaffold()
                ingest.ensure_wiki_scaffold()
            finally:
                os.chdir(old_cwd)

            exclude = (vault / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertEqual(
                exclude.count(ingest.CHAPTER_INTELLIGENCE_CACHE_IGNORE), 1
            )
            self.assertEqual(exclude.count(ingest.INGEST_LOCK_IGNORE), 1)
            self.assertEqual(
                (vault / ".gitignore").read_text(encoding="utf-8"),
                "custom-rule\n",
            )
            cache_file = vault / ".wiki" / "chapter-intelligence-cache" / "x.json"
            cache_file.parent.mkdir(parents=True)
            cache_file.write_text("{}", encoding="utf-8")
            status = subprocess.run(
                ["git", "-C", str(vault), "status", "--short", "--", str(cache_file)],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            self.assertEqual(status, "")


class AutoChapterTests(unittest.TestCase):
    def test_analyzer_reasoning_is_child_scoped_and_defaults_low(self):
        with patch.dict(os.environ, {
            "PW_CODEX_REASONING_EFFORT": "high",
        }, clear=False):
            os.environ.pop("PW_ANALYZE_REASONING_EFFORT", None)
            env = ingest.analyzer_env()
            self.assertEqual(env["PW_CODEX_REASONING_EFFORT"], "low")
            self.assertEqual(os.environ["PW_CODEX_REASONING_EFFORT"], "high")
        with patch.dict(os.environ, {"PW_ANALYZE_REASONING_EFFORT": "medium"}):
            self.assertEqual(
                ingest.analyzer_env()["PW_CODEX_REASONING_EFFORT"], "medium"
            )

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

    def test_partial_section_requires_a_completion_label(self):
        with patch.object(ingest, "die", side_effect=RuntimeError) as die:
            with self.assertRaises(RuntimeError):
                ingest._validate_section_contract("^Chapter 1$", "", "")
        self.assertIn("requires --section-label", die.call_args.args[0])

    def test_label_without_selector_is_rejected(self):
        with patch.object(ingest, "die", side_effect=RuntimeError) as die:
            with self.assertRaises(RuntimeError):
                ingest._validate_section_contract("", "Chapter 1", "")
        self.assertIn("requires --section", die.call_args.args[0])

    def test_internal_ordered_range_may_use_label_without_regex(self):
        ingest._validate_section_contract("", "Chapter 1", "0:2")

    def test_grouped_range_does_not_forward_duplicate_title_occurrence(self):
        captured = {}

        def fake_run(argv, env=None):
            captured["argv"] = argv
            captured["env"] = env or {}
            return types.SimpleNamespace(returncode=0)

        args = _args("book.epub")
        with patch.object(ingest.subprocess, "run", fake_run):
            ingest._run_one_chapter(
                args,
                section="^Chapter 1$",
                label="Chapter 1 [occurrence 2/2]",
                source_title="Chapter 1",
                section_occurrence=2,
                section_range=(4, 7),
            )
        self.assertNotIn("--section", captured["argv"])
        self.assertEqual(captured["env"]["PW_SECTION_RANGE"], "4:7")
        self.assertNotIn("PW_SECTION_OCCURRENCE", captured["env"])

    def test_internal_ordered_range_cannot_silently_log_whole_source(self):
        for section, label in (("", ""), ("^Chapter$", "Chapter")):
            with self.subTest(section=section, label=label), patch.object(
                ingest, "die", side_effect=RuntimeError
            ):
                with self.assertRaises(RuntimeError):
                    ingest._validate_section_contract(section, label, "0:2")

    def test_explicit_selector_must_match_one_heading(self):
        with patch.object(ingest, "die", side_effect=RuntimeError) as die:
            with self.assertRaises(RuntimeError):
                ingest._require_one_selected_heading(
                    "## Intro\nfirst\n## Intro\nsecond\n", "^Intro$"
                )
        self.assertIn("matched 2 section headings", die.call_args.args[0])


class LogProgressTests(unittest.TestCase):
    def test_labeled_images_only_line_does_not_complete_section(self):
        source_id = "01BOOKAAAAAAAAAAAAAAAAAAAA"
        line = (
            f"2026-01-01T00:00:00Z  {source_id}#Chapter 1  "
            "pages: (images-only)"
        )
        self.assertEqual(
            ingest._source_log_progress([line], source_id), (set(), False)
        )

    def test_pages_text_inside_label_round_trips(self):
        source_id = "01BOOKAAAAAAAAAAAAAAAAAAAA"
        labels, whole_done = ingest._source_log_progress(
            [f"2026-01-01T00:00:00Z  {source_id}#Front pages: a history  pages: wiki/x.md"],
            source_id,
        )
        self.assertFalse(whole_done)
        self.assertEqual(labels, {"Front pages: a history"})

    def test_identical_supersede_requires_committed_wiki_citation(self):
        old_cwd = os.getcwd()
        old_src = dict(ingest.SRC)
        try:
            with tempfile.TemporaryDirectory() as d:
                vault = Path(d)
                os.chdir(vault)
                subprocess.run(["git", "init", "-q"], check=True)
                subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
                subprocess.run(["git", "config", "user.name", "Test"], check=True)
                (vault / "sources").mkdir()
                (vault / "wiki" / "entities").mkdir(parents=True)
                (vault / "wiki" / "topics").mkdir(parents=True)
                (vault / "sources" / "old.md").write_text(
                    "---\nsource_id: OLD\nsha256: same\n---\n", encoding="utf-8"
                )
                page = vault / "wiki" / "entities" / "x.md"
                page.write_text("# X\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-qm", "base"], check=True)
                ingest.SRC.clear()
                ingest.SRC.update({"SHA256": "same"})
                self.assertFalse(ingest._supersede_coverage_proven("OLD"))
                page.write_text("# X\n\n> covered [src:OLD]\n", encoding="utf-8")
                subprocess.run(["git", "add", str(page)], check=True)
                subprocess.run(["git", "commit", "-qm", "synthesis"], check=True)
                self.assertTrue(ingest._supersede_coverage_proven("OLD"))
                # A mismatched text-artifact hash short-circuits before git.
                ingest.SRC["SHA256"] = "different"
                self.assertFalse(ingest._supersede_coverage_proven("OLD"))
        finally:
            ingest.SRC.clear()
            ingest.SRC.update(old_src)
            os.chdir(old_cwd)

    def test_no_changes_arms_rollback_before_supersede_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "raw.txt"
            raw.write_text("NO_CHANGES: identical transcript\n", encoding="utf-8")
            old_src = dict(ingest.SRC)
            old_rollback = ingest._ROLLBACK_ON_FAILURE
            ingest.SRC.clear()
            ingest.SRC.update({
                "SUPERSEDES": "OLD",
                "SOURCE_ID": "NEW",
                "EXISTING_SIDECAR": "",
            })

            def fail_during_rewrite(*_args, **_kwargs):
                self.assertTrue(ingest._ROLLBACK_ON_FAILURE)
                raise RuntimeError("rewrite failed")

            try:
                ingest._ROLLBACK_ON_FAILURE = False
                with patch.object(
                    ingest, "_supersede_coverage_proven", return_value=True
                ), patch.object(ingest, "run_stream", side_effect=fail_during_rewrite):
                    with self.assertRaisesRegex(RuntimeError, "rewrite failed"):
                        ingest.handle_no_changes_or_continue(
                            str(raw), "", str(raw), str(raw)
                        )
            finally:
                ingest.SRC.clear()
                ingest.SRC.update(old_src)
                ingest._ROLLBACK_ON_FAILURE = old_rollback

    def test_ordinary_no_changes_arms_rollback_before_log_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "raw.txt"
            raw.write_text("NO_CHANGES: already represented\n", encoding="utf-8")
            old_src = dict(ingest.SRC)
            old_rollback = ingest._ROLLBACK_ON_FAILURE
            ingest.SRC.clear()
            ingest.SRC.update({"SUPERSEDES": "", "SOURCE_ID": "NEW"})

            def fail_before_log(*_args, **_kwargs):
                self.assertTrue(ingest._ROLLBACK_ON_FAILURE)
                raise RuntimeError("stop before log")

            try:
                ingest._ROLLBACK_ON_FAILURE = False
                with patch.object(
                    ingest, "_run_quality_gate", return_value=(0, {"ok": True})
                ), patch.object(ingest.Path, "mkdir", side_effect=fail_before_log):
                    with self.assertRaisesRegex(RuntimeError, "stop before log"):
                        ingest.handle_no_changes_or_continue(
                            str(raw), "", str(raw), str(raw)
                        )
            finally:
                ingest.SRC.clear()
                ingest.SRC.update(old_src)
                ingest._ROLLBACK_ON_FAILURE = old_rollback


class RunChapteredTests(unittest.TestCase):
    def _run(self, vault, input_path, titles, run_returns):
        calls = []

        def fake_run_one(args, section, label, *, skip_assets=False, **_kwargs):
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

    def test_legacy_prefix_resume_requires_one_unique_current_match(self):
        self.assertEqual(
            ingest._resolved_done_labels(
                ["Chapter 1 Origins", "Chapter 1 Methods"], {"Chapter 1"}
            ),
            set(),
        )
        self.assertEqual(
            ingest._resolved_done_labels(
                ["Chapter 1 Origins", "Chapter 2 Methods"], {"Chapter 1"}
            ),
            {"Chapter 1 Origins"},
        )

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

    def test_duplicate_labels_run_and_resume_by_occurrence_identity(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            (vault / "sources").mkdir(parents=True)
            book = vault / "book.epub"
            book.write_bytes(b"epub-bytes")
            captured = []

            def fake_run(_args, section, label, **kwargs):
                captured.append((label, section, kwargs.get("section_occurrence")))
                return 0

            with patch.object(ingest, "VAULT_ROOT", vault), \
                 patch.object(ingest, "_enumerate_sections", return_value=[
                     ("Interlude", 9000), ("Body", 9000), ("Interlude", 9000),
                 ]), \
                 patch.object(ingest, "_run_one_chapter", side_effect=fake_run):
                self.assertEqual(ingest.run_chaptered(_args(book)), 0)

            self.assertEqual(
                captured,
                [
                    ("Interlude [occurrence 1/2]", "^Interlude$", 1),
                    ("Body", "^Body$", None),
                    ("Interlude [occurrence 2/2]", "^Interlude$", 2),
                ],
            )
            self.assertIn(
                "Interlude [occurrence 1/2]", {"Interlude [occurrence 1/2]"}
            )
            self.assertNotIn(
                "Interlude [occurrence 2/2]", {"Interlude [occurrence 1/2]"}
            )

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


@unittest.skipUnless(shutil.which("uv"), "uv is required for ingest helper scripts")
class ChapteredSmokeTests(unittest.TestCase):
    def test_two_chapter_ingest_runs_children_without_parent_lock_deadlock(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "content"
            for rel in ("wiki/entities", "wiki/topics", "wiki/_index", "sources", ".wiki"):
                (vault / rel).mkdir(parents=True, exist_ok=True)
            (vault / "wiki" / "_taxonomy.md").write_text(
                ingest.TAXONOMY_PLACEHOLDER, encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=vault, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=vault, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=vault, check=True)
            subprocess.run(["git", "add", "-A"], cwd=vault, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=vault, check=True)

            book = root / "book.epub"
            _write_minimal_epub(book)
            env = os.environ.copy()
            env.update({
                "PW_CONTENT_DIR": str(vault),
                "VAULT_CONTENT_DIR": str(vault),
                "LLM_CMD": str(ROOT / "scripts" / "tests" / "stub-llm.py"),
                "STUB_ENTITY_FROM_SECTION": "1",
                "STUB_ENTITY_PREFIX": "chaptered-",
            })
            env.pop("PW_INGEST_NO_AUTOCHAPTER", None)

            res = subprocess.run(
                [sys.executable, str(ROOT / "ingest.py"), "--chapters", str(book)],
                cwd=root, env=env, text=True, capture_output=True, timeout=180,
            )
            self.assertEqual(
                res.returncode, 0,
                f"stdout:\n{res.stdout[-4000:]}\nstderr:\n{res.stderr[-4000:]}",
            )
            self.assertIn("chaptered ingest complete: 2 new chapter(s)", res.stdout)
            self.assertTrue((vault / "wiki" / "entities" / "chaptered-chapter-1.md").is_file())
            self.assertTrue((vault / "wiki" / "entities" / "chaptered-chapter-2.md").is_file())
            log = (vault / ".wiki" / "log.md").read_text(encoding="utf-8")
            self.assertRegex(log, r"\s[0-9A-Z]{26}#Chapter 1\s+pages:")
            self.assertRegex(log, r"\s[0-9A-Z]{26}#Chapter 2\s+pages:")
            commits = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=vault, text=True, capture_output=True, check=True,
            ).stdout.strip()
            self.assertEqual(commits, "3")

            head_before = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=vault, text=True, capture_output=True, check=True,
            ).stdout.strip()
            rerun = subprocess.run(
                [
                    sys.executable, str(ROOT / "ingest.py"),
                    "--section", "^Chapter 1$",
                    "--section-label", "Chapter 1",
                    str(book),
                ],
                cwd=root, env=env, text=True, capture_output=True, timeout=180,
            )
            self.assertEqual(
                rerun.returncode, 0,
                f"stdout:\n{rerun.stdout[-4000:]}\nstderr:\n{rerun.stderr[-4000:]}",
            )
            self.assertIn("section ingest already logged", rerun.stdout)
            head_after = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=vault, text=True, capture_output=True, check=True,
            ).stdout.strip()
            self.assertEqual(head_after, head_before)

    def test_truncated_explicit_section_never_logs_or_commits_completion(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "content"
            for rel in ("wiki/entities", "wiki/topics", "wiki/_index", "sources", ".wiki"):
                (vault / rel).mkdir(parents=True, exist_ok=True)
            (vault / "wiki" / "_taxonomy.md").write_text(
                ingest.TAXONOMY_PLACEHOLDER, encoding="utf-8"
            )
            (vault / ".wiki" / ".keep").write_text("", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=vault, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=vault, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=vault, check=True)
            subprocess.run(["git", "add", "-A"], cwd=vault, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=vault, check=True)

            source = root / "large.md"
            source.write_text(
                "## Chapter 1\n" + ("complete evidence sentence. " * 20) + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update({
                "PW_CONTENT_DIR": str(vault),
                "VAULT_CONTENT_DIR": str(vault),
                "LLM_CMD": str(ROOT / "scripts" / "tests" / "stub-llm.py"),
                "PW_INGEST_SKIP_ASSETS": "1",
            })
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "ingest.py"),
                    "--section", "^Chapter 1$",
                    "--section-label", "Chapter 1",
                    "--limit", "80",
                    str(source),
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("truncated text is never logged as complete", result.stderr)
            self.assertFalse((vault / ".wiki" / "log.md").exists())
            self.assertEqual(
                subprocess.run(
                    ["git", "status", "--porcelain"], cwd=vault,
                    text=True, capture_output=True, check=True,
                ).stdout,
                "",
            )


class GroupByChapterTests(unittest.TestCase):
    @staticmethod
    def _groups(sections):
        return [
            (label, members)
            for label, members, _start, _end in ingest._grouped_chapter_ranges(sections)
        ]

    def test_sections_grouped_front_back_excluded(self):
        sections = [("cover.xhtml", 1), ("序言", 5000), ("导言 世界", 16000),
                    ("第一章 起源", 5000), ("第一节 鸿沟", 9000), ("第二节 祖先", 9000),
                    ("第二章 生命力", 4000), ("第三节 起源", 9000),
                    ("后记", 9000), ("词汇表", 5000)]
        groups = self._groups(sections)
        self.assertEqual([g[0] for g in groups], ["第一章 起源", "第二章 生命力"])
        self.assertEqual(groups[0][1], ["第一章 起源", "第一节 鸿沟", "第二节 祖先"])
        self.assertEqual(groups[1][1], ["第二章 生命力", "第三节 起源"])

    def test_no_chapter_markers_returns_empty(self):
        self.assertEqual(
            self._groups([("intro", 100), ("body", 9000)]), [])

    def test_section_before_first_chapter_is_dropped(self):
        # A stray 节 before any 章 is not inside a chapter → excluded.
        groups = self._groups([("第九节 orphan", 9000),
                               ("第一章 A", 5000), ("第一节 a", 9000)])
        self.assertEqual([g[0] for g in groups], ["第一章 A"])
        self.assertEqual(groups[0][1], ["第一章 A", "第一节 a"])

    def test_substantial_descriptive_heading_stays_in_current_chapter(self):
        groups = self._groups([
            ("Chapter 1 Origins", 4000),
            ("A descriptive mechanism", 9000),
            ("spine-label.xhtml", 3),
            ("Section 2 Evidence", 8000),
            ("Afterword", 9000),
            ("Appendix data", 9000),
        ])
        self.assertEqual(
            groups,
            [(
                "Chapter 1 Origins",
                ["Chapter 1 Origins", "A descriptive mechanism", "Section 2 Evidence"],
            )],
        )

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


class PipelineRecoveryTests(unittest.TestCase):
    def test_failed_reused_asset_mutation_is_fully_rolled_back(self):
        old_cwd = os.getcwd()
        old_src = dict(ingest.SRC)
        old_rollback = ingest._ROLLBACK_ON_FAILURE
        old_files = set(ingest._RUN_CREATED_FILES)
        old_snapshot = set(ingest._PREEXISTING_SOURCE_PATHS)
        try:
            with tempfile.TemporaryDirectory() as directory:
                vault = Path(directory)
                assets = vault / "sources" / "book.assets"
                assets.mkdir(parents=True)
                manifest = assets / "_manifest.md"
                manifest.write_text("original\n", encoding="utf-8")
                subprocess.run(["git", "init", "-q"], cwd=vault, check=True)
                subprocess.run(["git", "add", "."], cwd=vault, check=True)
                subprocess.run(
                    ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
                     "commit", "-qm", "baseline"],
                    cwd=vault,
                    check=True,
                )
                os.chdir(vault)
                ingest.SRC.clear()
                ingest.SRC.update({
                    "DEST": "sources/book",
                    "SIDECAR": "sources/book.md",
                })
                ingest._ROLLBACK_ON_FAILURE = True
                ingest._RUN_CREATED_FILES.clear()
                ingest._PREEXISTING_SOURCE_PATHS = ingest._source_path_snapshot()
                manifest.write_text("changed\n", encoding="utf-8")
                (assets / "new.png").write_bytes(b"new")

                with patch.object(ingest, "_cleanup"):
                    with self.assertRaises(SystemExit):
                        ingest.die("forced failure")

                self.assertEqual(manifest.read_text(encoding="utf-8"), "original\n")
                self.assertFalse((assets / "new.png").exists())
                self.assertFalse(subprocess.run(
                    ["git", "status", "--porcelain"], cwd=vault,
                    text=True, capture_output=True, check=True,
                ).stdout)
        finally:
            os.chdir(old_cwd)
            ingest.SRC.clear()
            ingest.SRC.update(old_src)
            ingest._ROLLBACK_ON_FAILURE = old_rollback
            ingest._RUN_CREATED_FILES.clear()
            ingest._RUN_CREATED_FILES.update(old_files)
            ingest._PREEXISTING_SOURCE_PATHS = old_snapshot

    def test_termination_stops_identity_publisher_before_cleanup(self):
        events = []

        class FakeProcess:
            def poll(self):
                return None

            def terminate(self):
                events.append("terminate")

            def wait(self, timeout=None):
                events.append(f"wait:{timeout}")
                return -signal.SIGTERM

        old_proc = ingest._ACTIVE_SOURCE_IDENTITY
        old_terminating = ingest._TERMINATING
        try:
            ingest._ACTIVE_SOURCE_IDENTITY = FakeProcess()
            ingest._TERMINATING = False

            def stopped_die(_message):
                events.append("cleanup")
                raise RuntimeError("stopped")

            with patch.object(ingest, "die", side_effect=stopped_die):
                with self.assertRaisesRegex(RuntimeError, "stopped"):
                    ingest._handle_termination(signal.SIGTERM, None)
            self.assertEqual(events, ["terminate", "wait:5", "cleanup"])
        finally:
            ingest._ACTIVE_SOURCE_IDENTITY = old_proc
            ingest._TERMINATING = old_terminating

    def test_source_identity_registers_paths_before_publication(self):
        old_cwd = os.getcwd()
        old_files = set(ingest._RUN_CREATED_FILES)
        old_dirs = set(ingest._RUN_CREATED_DIRS)
        old_snapshot = set(ingest._PREEXISTING_SOURCE_PATHS)
        try:
            with tempfile.TemporaryDirectory() as d:
                vault = Path(d) / "content"
                vault.mkdir()
                (vault / "sources").mkdir()
                subprocess.run(["git", "init", "-q"], cwd=vault, check=True)
                source = Path(d) / "book.txt"
                source.write_text("transactional identity\n", encoding="utf-8")
                os.chdir(vault)
                ingest._RUN_CREATED_FILES.clear()
                ingest._RUN_CREATED_DIRS.clear()
                ingest._PREEXISTING_SOURCE_PATHS = ingest._source_path_snapshot()

                with patch.dict(os.environ, {"VAULT_CONTENT_DIR": str(vault)}):
                    result = ingest.run_source_identity(str(source))

                values = ingest.parse_shell_assignments(result.stdout)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(values["IDENTITY_READY"], "new")
                self.assertIn(values["DEST"], ingest._RUN_CREATED_FILES)
                self.assertIn(values["SIDECAR"], ingest._RUN_CREATED_FILES)
                self.assertTrue(Path(values["DEST"]).is_file())
                self.assertTrue(Path(values["SIDECAR"]).is_file())
        finally:
            os.chdir(old_cwd)
            ingest._RUN_CREATED_FILES.clear()
            ingest._RUN_CREATED_FILES.update(old_files)
            ingest._RUN_CREATED_DIRS.clear()
            ingest._RUN_CREATED_DIRS.update(old_dirs)
            ingest._PREEXISTING_SOURCE_PATHS = old_snapshot

    def test_default_vault_root_prefers_pw_content_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pw = root / "pw-content"
            legacy = root / "legacy-content"
            pw.mkdir()
            legacy.mkdir()
            with patch.dict(os.environ, {
                "PW_CONTENT_DIR": str(pw),
                "VAULT_CONTENT_DIR": str(legacy),
            }):
                self.assertEqual(default_vault_root(root / "pipeline"), pw.resolve())

    def test_log_prefix_includes_run_id(self):
        with patch.dict(os.environ, {"PW_RUN_ID": "ab12cd"}):
            self.assertEqual(ingest._log_prefix(), "ingest[ab12cd]")
        with patch.dict(os.environ, {"PW_RUN_ID": ""}):
            self.assertEqual(ingest._log_prefix(), "ingest")

    def test_detects_extraction_truncation_marker(self):
        self.assertTrue(ingest._has_extraction_truncation_marker(
            "body\n\n[... truncated at 100000 chars ...]\n"))
        self.assertFalse(ingest._has_extraction_truncation_marker("body without marker"))

    def test_truncated_section_is_rejected_too(self):
        with patch.object(ingest, "die", side_effect=RuntimeError) as die:
            with self.assertRaises(RuntimeError):
                ingest._require_complete_extraction(
                    "## Chapter\nbody\n[... truncated at 100 chars ...]\n",
                    sliced=True,
                )
        self.assertIn("selected section", die.call_args.args[0])

    def test_sigterm_handler_uses_die_cleanup_path(self):
        old_terminating = ingest._TERMINATING
        ingest._TERMINATING = False

        def fake_die(msg):
            raise RuntimeError(msg)

        try:
            with patch.object(ingest, "die", fake_die):
                with self.assertRaisesRegex(RuntimeError, "terminated by SIGTERM"):
                    ingest._handle_termination(signal.SIGTERM, None)
            self.assertTrue(ingest._TERMINATING)
        finally:
            ingest._TERMINATING = old_terminating

    def test_sigint_handler_uses_same_cleanup_path(self):
        old_terminating = ingest._TERMINATING
        ingest._TERMINATING = False

        def fake_die(msg):
            raise RuntimeError(msg)

        try:
            with patch.object(ingest, "die", fake_die):
                with self.assertRaisesRegex(RuntimeError, "terminated by SIGINT"):
                    ingest._handle_termination(signal.SIGINT, None)
            self.assertTrue(ingest._TERMINATING)
        finally:
            ingest._TERMINATING = old_terminating

    def test_section_label_validation_rejects_log_breakers(self):
        self.assertEqual(ingest.validate_section_label("第1章"), "第1章")
        with self.assertRaises(SystemExit):
            ingest.validate_section_label("bad\nlabel")
        with self.assertRaises(SystemExit):
            ingest.validate_section_label("bad\tlabel")
        with self.assertRaises(SystemExit):
            ingest.validate_section_label("x" * (ingest.SECTION_LABEL_MAX_CHARS + 1))

    def test_whole_source_already_logged_only_matches_unsectioned_marker(self):
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as d:
                vault = Path(d)
                os.chdir(vault)
                (vault / ".wiki").mkdir()
                sid = "01BOOKAAAAAAAAAAAAAAAAAAAA"
                (vault / ".wiki" / "log.md").write_text(
                    f"2026-01-01T00:00:00Z  {sid}#第1章  pages: wiki/a.md\n"
                    f"2026-01-01T00:00:00Z  01OTHERAAAAAAAAAAAAAAAAAAA  pages: wiki/b.md\n",
                    encoding="utf-8",
                )
                self.assertFalse(ingest.whole_source_already_logged(sid))
                with open(vault / ".wiki" / "log.md", "a", encoding="utf-8") as f:
                    f.write(f"2026-01-01T00:00:00Z  {sid}  pages: wiki/c.md\n")
                self.assertTrue(ingest.whole_source_already_logged(sid))
        finally:
            os.chdir(old_cwd)

    def test_images_only_log_does_not_mark_whole_source_done(self):
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as d:
                vault = Path(d)
                os.chdir(vault)
                (vault / ".wiki").mkdir()
                sid = "01BOOKAAAAAAAAAAAAAAAAAAAA"
                lines = [
                    f"2026-01-01T00:00:00Z  {sid}  pages: (images-only)",
                ]
                (vault / ".wiki" / "log.md").write_text(lines[0] + "\n", encoding="utf-8")

                self.assertFalse(ingest.whole_source_already_logged(sid))
                self.assertEqual(ingest._source_log_progress(lines, sid), (set(), False))

                lines.append(f"2026-01-01T00:00:01Z  {sid}  pages: wiki/entities/x.md")
                with open(vault / ".wiki" / "log.md", "a", encoding="utf-8") as f:
                    f.write(lines[-1] + "\n")
                self.assertTrue(ingest.whole_source_already_logged(sid))
                self.assertEqual(ingest._source_log_progress(lines, sid), (set(), True))
        finally:
            os.chdir(old_cwd)

    def test_section_already_logged_short_circuits_logged_sections(self):
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as d:
                vault = Path(d)
                os.chdir(vault)
                (vault / ".wiki").mkdir()
                sid = "01BOOKAAAAAAAAAAAAAAAAAAAA"
                (vault / ".wiki" / "log.md").write_text(
                    f"2026-01-01T00:00:00Z  {sid}#Chapter 1  pages: wiki/a.md\n",
                    encoding="utf-8",
                )
                self.assertTrue(ingest.section_already_logged(sid, "Chapter 1"))
                self.assertFalse(ingest.section_already_logged(sid, "Chapter 10"))
                with open(vault / ".wiki" / "log.md", "a", encoding="utf-8") as f:
                    f.write(f"2026-01-01T00:00:01Z  {sid}  pages: wiki/all.md\n")
                self.assertTrue(ingest.section_already_logged(sid, "Chapter 10"))
        finally:
            os.chdir(old_cwd)

    def test_collect_candidates_skips_alias_index_for_empty_or_small_vault(self):
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as d:
                vault = Path(d)
                os.chdir(vault)
                (vault / "wiki" / "entities").mkdir(parents=True)
                (vault / "wiki" / "topics").mkdir(parents=True)
                (vault / "wiki" / "entities" / "A.md").write_text("# A\n", encoding="utf-8")
                out_file = vault / "candidates.txt"
                intelligence_file = vault / "chapter-intelligence.json"
                intelligence_file.write_text("{}\n", encoding="utf-8")
                with patch.object(ingest, "run_stream") as run_stream:
                    ingest.collect_candidates(str(intelligence_file), str(out_file), 20)
                run_stream.assert_not_called()
                self.assertEqual(out_file.read_text(encoding="utf-8"), "wiki/entities/A.md\n")

                (vault / "wiki" / "entities" / "A.md").unlink()
                ingest.collect_candidates(str(intelligence_file), str(out_file), 20)
                self.assertEqual(out_file.read_text(encoding="utf-8"), "")
        finally:
            os.chdir(old_cwd)

    def test_intelligence_search_terms_prioritize_required_page_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            artifact_path = Path(d) / "chapter-intelligence.json"
            artifact_path.write_text(json.dumps({
                "entities": [
                    {"name": "线粒体", "aliases": ["Mitochondria"], "importance": 5},
                    {"name": "偶然例子", "aliases": [], "importance": 2},
                    {"name": "显式要求", "aliases": [], "importance": 3},
                ],
                "topics": [
                    {"name": "真核细胞起源", "importance": 4},
                ],
                "page_candidates": [
                    {"page_type": "entity", "name": "线粒体", "importance": 5,
                     "required": True},
                    {"page_type": "topic", "name": "真核细胞起源", "importance": 4,
                     "required": True},
                    {"page_type": "entity", "name": "显式要求", "importance": 3,
                     "required": True},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            terms = ingest._intelligence_search_terms(str(artifact_path))
        by_term = {row["term"]: row for row in terms}
        self.assertTrue(by_term["线粒体"]["required"])
        self.assertTrue(by_term["Mitochondria"]["required"])
        self.assertTrue(by_term["真核细胞起源"]["required"])
        self.assertTrue(by_term["显式要求"]["required"])
        self.assertFalse(by_term["偶然例子"]["required"])

    def test_candidate_decisions_use_nfkc_casefold_whitespace_normalization(self):
        with tempfile.TemporaryDirectory() as d:
            artifact_path = Path(d) / "chapter-intelligence.json"
            artifact_path.write_text(json.dumps({
                "entities": [
                    {"name": "ATP", "aliases": ["Adenosine   Triphosphate"],
                     "importance": 5},
                ],
                "topics": [],
                "page_candidates": [
                    {"page_type": "entity", "name": "ＡＴＰ", "importance": 5,
                     "required": True},
                ],
            }), encoding="utf-8")
            terms = ingest._intelligence_search_terms(str(artifact_path))
        by_name = {ingest.normalize_name(row["term"]): row for row in terms}
        self.assertTrue(by_name["atp"]["required"])
        self.assertTrue(by_name["adenosine triphosphate"]["required"])

    def test_large_vault_pins_all_required_exact_aliases_past_cap(self):
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as directory:
                vault = Path(directory)
                entities = vault / "wiki" / "entities"
                (vault / "wiki" / "topics").mkdir(parents=True)
                entities.mkdir(parents=True)
                (vault / "wiki" / "_taxonomy.md").write_text(
                    ingest.TAXONOMY_PLACEHOLDER, encoding="utf-8"
                )
                for index in range(22):
                    (entities / f"P{index}.md").write_text(
                        "---\n"
                        "type: Entity\n"
                        f"page_id: '01{index:024d}'\n"
                        f"aliases: [Alias {index}]\n"
                        "tags: [concept, biology/cell]\n"
                        "---\n\n"
                        f"# P{index}\n\nAlias {index} background.\n",
                        encoding="utf-8",
                    )
                required = ["Alias 0", "Alias 1", "Alias 2"]
                intelligence_file = vault / "intelligence.json"
                intelligence_file.write_text(json.dumps({
                    "entities": [
                        {"name": name, "aliases": [], "importance": 5}
                        for name in required
                    ],
                    "topics": [],
                    "page_candidates": [
                        {
                            "page_type": "entity",
                            "name": name,
                            "importance": 5,
                            "required": True,
                        }
                        for name in required
                    ],
                }), encoding="utf-8")
                candidates = vault / "candidates.txt"
                os.chdir(vault)
                with patch.dict(os.environ, {
                    "PW_CONTENT_DIR": str(vault),
                    "VAULT_CONTENT_DIR": str(vault),
                }):
                    ingest.collect_candidates(
                        str(intelligence_file), str(candidates), cap=2
                    )
                self.assertEqual(
                    set(candidates.read_text(encoding="utf-8").splitlines()),
                    {f"wiki/entities/P{index}.md" for index in range(3)},
                )
        finally:
            os.chdir(old_cwd)

    def test_renderer_reconciles_candidate_to_existing_global_page_type(self):
        old_cwd = os.getcwd()
        old_temps = list(ingest._TEMPS)
        try:
            with tempfile.TemporaryDirectory() as d:
                vault = Path(d)
                os.chdir(vault)
                (vault / "wiki" / "topics").mkdir(parents=True)
                page = vault / "wiki" / "topics" / "ATP.md"
                page.write_text("# ATP\n", encoding="utf-8")
                (vault / "wiki" / ".alias-index.json").write_text(json.dumps({
                    "aliases": {"atp": ["P1"]},
                    "pages": {"P1": {
                        "path": "wiki/topics/ATP.md", "type": "Topic",
                    }},
                }), encoding="utf-8")
                artifact = vault / "intelligence.json"
                artifact.write_text(json.dumps({
                    "page_candidates": [{
                        "page_type": "entity", "name": "ＡＴＰ", "importance": 5,
                    }],
                }), encoding="utf-8")
                candidates = vault / "candidates.txt"
                candidates.write_text("wiki/topics/ATP.md\n", encoding="utf-8")
                with patch.object(
                    ingest.subprocess, "run",
                    return_value=types.SimpleNamespace(returncode=0, stderr=""),
                ):
                    projected = ingest._renderer_intelligence_with_existing_types(
                        str(artifact), str(candidates)
                    )
                self.assertNotEqual(projected, str(artifact))
                value = json.loads(Path(projected).read_text(encoding="utf-8"))
                self.assertEqual(value["page_candidates"][0]["page_type"], "topic")
                original = json.loads(artifact.read_text(encoding="utf-8"))
                self.assertEqual(original["page_candidates"][0]["page_type"], "entity")
        finally:
            for path in ingest._TEMPS[len(old_temps):]:
                Path(path).unlink(missing_ok=True)
            ingest._TEMPS[:] = old_temps
            os.chdir(old_cwd)

    def test_cleanup_removes_only_registered_new_untracked_artifacts(self):
        old_cwd = os.getcwd()
        old_files = set(ingest._RUN_CREATED_FILES)
        old_dirs = set(ingest._RUN_CREATED_DIRS)
        old_snapshot = set(ingest._PREEXISTING_SOURCE_PATHS)
        try:
            with tempfile.TemporaryDirectory() as d:
                vault = Path(d)
                os.chdir(vault)
                subprocess.run(["git", "init", "-q"], check=True)
                (vault / "sources").mkdir()
                (vault / "sources" / "preexisting.md").write_text("keep", encoding="utf-8")
                (vault / "sources" / "tracked.md").write_text("keep", encoding="utf-8")
                subprocess.run(["git", "add", "sources/tracked.md"], check=True)

                ingest._RUN_CREATED_FILES.clear()
                ingest._RUN_CREATED_DIRS.clear()
                ingest._PREEXISTING_SOURCE_PATHS = ingest._source_path_snapshot()

                (vault / "sources" / "new.txt").write_text("remove", encoding="utf-8")
                (vault / "sources" / "new.txt.md").write_text("remove", encoding="utf-8")
                assets = vault / "sources" / "new.txt.assets"
                assets.mkdir()
                (assets / "_manifest.md").write_text("remove", encoding="utf-8")

                ingest.SRC.clear()
                ingest.SRC.update({"SIDECAR": "sources/new.txt.md",
                                   "AUDIT_JSON": "sources/new.txt.transcript.json"})
                (vault / "sources" / "new.txt.transcript.json").write_text("remove", encoding="utf-8")
                ingest._register_new_source_artifacts("sources/new.txt")
                ingest._register_run_created_file("sources/preexisting.md")
                ingest._register_run_created_file("sources/tracked.md")
                ingest._cleanup_run_created_artifacts()

                self.assertFalse((vault / "sources" / "new.txt").exists())
                self.assertFalse((vault / "sources" / "new.txt.md").exists())
                self.assertFalse((vault / "sources" / "new.txt.transcript.json").exists())
                self.assertFalse(assets.exists())
                self.assertTrue((vault / "sources" / "preexisting.md").exists())
                self.assertTrue((vault / "sources" / "tracked.md").exists())
        finally:
            os.chdir(old_cwd)
            ingest._RUN_CREATED_FILES.clear()
            ingest._RUN_CREATED_FILES.update(old_files)
            ingest._RUN_CREATED_DIRS.clear()
            ingest._RUN_CREATED_DIRS.update(old_dirs)
            ingest._PREEXISTING_SOURCE_PATHS = old_snapshot

    def test_post_apply_failure_rollback_clears_staged_wiki_changes(self):
        old_cwd = os.getcwd()
        old_flag = ingest._ROLLBACK_ON_FAILURE
        old_src = dict(ingest.SRC)
        try:
            with tempfile.TemporaryDirectory() as d:
                vault = Path(d)
                os.chdir(vault)
                subprocess.run(["git", "init", "-q"], check=True)
                subprocess.run(["git", "config", "user.email", "t@t"], check=True)
                subprocess.run(["git", "config", "user.name", "t"], check=True)
                (vault / "wiki" / "entities").mkdir(parents=True)
                (vault / ".wiki").mkdir()
                (vault / "sources" / "book.epub.assets").mkdir(parents=True)
                page = vault / "wiki" / "entities" / "x.md"
                page.write_text("# X\n", encoding="utf-8")
                log = vault / ".wiki" / "log.md"
                log.write_text("", encoding="utf-8")
                dest = vault / "sources" / "book.epub"
                dest.write_text("asset", encoding="utf-8")
                sidecar = vault / "sources" / "book.epub.md"
                sidecar.write_text("---\nsource_id: 01OLD\n---\n\n# book\n", encoding="utf-8")
                manifest = vault / "sources" / "book.epub.assets" / "_manifest.md"
                manifest.write_text("---\nsource_id: 01OLD\nimages: []\n---\n", encoding="utf-8")
                audit = vault / "sources" / "book.transcript.json"
                audit.write_text('{"ok": true}\n', encoding="utf-8")
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-qm", "init"], check=True)

                page.write_text("# X\n\n[src:01NEWAAAAAAAAAAAAAAAAAAAA]\n", encoding="utf-8")
                log.write_text("2026-01-01T00:00:00Z  01NEWAAAAAAAAAAAAAAAAAAAA  pages: wiki/entities/x.md\n",
                               encoding="utf-8")
                sidecar.write_text("---\nsource_id: 01OLD\n---\n\n# changed\n", encoding="utf-8")
                manifest.write_text("---\nsource_id: 01OLD\nimages:\n  - changed\n---\n", encoding="utf-8")
                audit.write_text('{"ok": false}\n', encoding="utf-8")
                added_page = vault / "wiki" / "entities" / "new.md"
                added_page.write_text("# New\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], check=True)
                ingest.SRC.clear()
                ingest.SRC.update({
                    "DEST": "sources/book.epub",
                    "SIDECAR": "sources/book.epub.md",
                    "AUDIT_JSON": "sources/book.transcript.json",
                })
                ingest._ROLLBACK_ON_FAILURE = True
                ingest._rollback_after_apply_failure()
                status = subprocess.run(["git", "status", "--short"], text=True,
                                        capture_output=True, check=True).stdout
                self.assertEqual(status, "")
                self.assertFalse(added_page.exists())
        finally:
            ingest._ROLLBACK_ON_FAILURE = old_flag
            ingest.SRC.clear()
            ingest.SRC.update(old_src)
            os.chdir(old_cwd)

    def test_rollback_git_failure_preserves_provenance_for_staged_citations(self):
        old_cwd = os.getcwd()
        old_flag = ingest._ROLLBACK_ON_FAILURE
        old_src = dict(ingest.SRC)
        old_files = set(ingest._RUN_CREATED_FILES)
        try:
            with tempfile.TemporaryDirectory() as d:
                vault = Path(d)
                os.chdir(vault)
                subprocess.run(["git", "init", "-q"], check=True)
                subprocess.run(["git", "config", "user.email", "t@t"], check=True)
                subprocess.run(["git", "config", "user.name", "t"], check=True)
                (vault / "wiki" / "entities").mkdir(parents=True)
                (vault / "sources").mkdir()
                (vault / ".wiki").mkdir()
                (vault / ".wiki" / "log.md").write_text("", encoding="utf-8")
                subprocess.run(["git", "add", ".wiki/log.md"], check=True)
                subprocess.run(["git", "commit", "-qm", "init"], check=True)

                source = vault / "sources" / "new.epub"
                sidecar = vault / "sources" / "new.epub.md"
                source.write_text("evidence", encoding="utf-8")
                sidecar.write_text("---\nsource_id: NEW\n---\n", encoding="utf-8")
                page = vault / "wiki" / "entities" / "new.md"
                page.write_text("# New\n\n[src:NEW]\n", encoding="utf-8")
                subprocess.run(["git", "add", "wiki/entities/new.md"], check=True)

                ingest.SRC.clear()
                ingest.SRC.update({
                    "DEST": "sources/new.epub",
                    "SIDECAR": "sources/new.epub.md",
                })
                ingest._RUN_CREATED_FILES.clear()
                ingest._RUN_CREATED_FILES.update({
                    "sources/new.epub", "sources/new.epub.md",
                })
                ingest._ROLLBACK_ON_FAILURE = True
                index_lock = vault / ".git" / "index.lock"
                index_lock.write_text("locked", encoding="utf-8")
                with patch.object(ingest, "_cleanup"):
                    with self.assertRaises(SystemExit):
                        ingest.die("forced failure")
                index_lock.unlink()

                self.assertTrue(source.exists())
                self.assertTrue(sidecar.exists())
                staged = subprocess.run(
                    ["git", "diff", "--cached", "--name-only"],
                    text=True, capture_output=True, check=True,
                ).stdout.splitlines()
                self.assertEqual(staged, ["wiki/entities/new.md"])
        finally:
            ingest._ROLLBACK_ON_FAILURE = old_flag
            ingest.SRC.clear()
            ingest.SRC.update(old_src)
            ingest._RUN_CREATED_FILES.clear()
            ingest._RUN_CREATED_FILES.update(old_files)
            os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
