import importlib.util
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


class PipelineRecoveryTests(unittest.TestCase):
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
                keywords_file = vault / "keywords.txt"
                keywords_file.write_text("A\n", encoding="utf-8")
                with patch.object(ingest, "run_stream") as run_stream:
                    ingest.collect_candidates(str(keywords_file), str(out_file), 20)
                run_stream.assert_not_called()
                self.assertEqual(out_file.read_text(encoding="utf-8"), "wiki/entities/A.md\n")

                (vault / "wiki" / "entities" / "A.md").unlink()
                ingest.collect_candidates(str(keywords_file), str(out_file), 20)
                self.assertEqual(out_file.read_text(encoding="utf-8"), "")
        finally:
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
        finally:
            ingest._ROLLBACK_ON_FAILURE = old_flag
            ingest.SRC.clear()
            ingest.SRC.update(old_src)
            os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
