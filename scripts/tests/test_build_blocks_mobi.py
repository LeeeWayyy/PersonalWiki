import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("build_blocks_mobi_test_mod", ROOT / "scripts" / "build-blocks.py")
build_blocks = importlib.util.module_from_spec(SPEC)
sys.modules["build_blocks_mobi_test_mod"] = build_blocks
SPEC.loader.exec_module(build_blocks)


class BuildBlocksMobiTests(unittest.TestCase):
    def test_epub_skip_docs_match_path_components_not_substrings(self):
        self.assertTrue(build_blocks.should_skip_epub_doc("OPS/nav.xhtml"))
        self.assertTrue(build_blocks.should_skip_epub_doc("OPS/toc.xhtml"))
        self.assertTrue(build_blocks.should_skip_epub_doc("OPS/title-page.xhtml"))
        self.assertTrue(build_blocks.should_skip_epub_doc("OPS/title_page.xhtml"))
        self.assertTrue(build_blocks.should_skip_epub_doc("OPS/titlepage.xhtml"))

        self.assertFalse(build_blocks.should_skip_epub_doc("OPS/tocqueville.xhtml"))
        self.assertFalse(build_blocks.should_skip_epub_doc("OPS/navigating/chapter.xhtml"))
        self.assertFalse(build_blocks.should_skip_epub_doc("OPS/title_ix.xhtml"))

    def test_main_emits_blocks_for_mobi_converted_to_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            sources = vault / "sources"
            out = vault / ".blocks"
            sources.mkdir(parents=True)
            sid = "S1MOBITEST00000000000000"
            sidecar = sources / "fixture.mobi.md"
            sidecar.write_text(
                "---\n"
                f"source_id: {sid}\n"
                "title: Fixture MOBI\n"
                "origin_type: file\n"
                "---\n",
                encoding="utf-8",
            )
            sidecar.with_suffix("").write_bytes(b"fake mobi payload")
            converted_dir = root / "converted"
            converted_dir.mkdir()
            converted = converted_dir / "fixture.html"
            converted.write_text(
                "<html><body><h1>第一章</h1><p>Reader block from MOBI.</p></body></html>",
                encoding="utf-8",
            )
            fake_mobi = types.SimpleNamespace(
                extract=lambda _path: (str(converted_dir), str(converted))
            )

            with patch.dict(sys.modules, {"mobi": fake_mobi}), patch.object(
                build_blocks, "SOURCES", sources
            ), patch.object(build_blocks, "OUT", out), patch.object(build_blocks, "PUBLIC", False):
                self.assertEqual(build_blocks.main(), 0)

            doc = json.loads((out / f"{sid}.blocks.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["source_id"], sid)
            self.assertEqual(doc["title"], "Fixture MOBI")
            self.assertEqual(doc["lang"], "zh")
            self.assertGreater(len(doc["blocks"]), 0)
            self.assertEqual([block["type"] for block in doc["blocks"]], ["heading", "paragraph"])
            self.assertIn("Reader block from MOBI.", doc["blocks"][1]["text"])
            self.assertFalse(converted_dir.exists())

    def test_main_returns_nonzero_when_supported_extraction_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp) / "sources"
            sources.mkdir()
            sidecar = sources / "broken.pdf.md"
            sidecar.write_text("---\nsource_id: BROKEN\n---\n", encoding="utf-8")
            sidecar.with_suffix("").write_bytes(b"not a pdf")

            with patch.object(build_blocks, "SOURCES", sources), patch.object(
                build_blocks, "OUT", Path(tmp) / "blocks"
            ), patch.object(build_blocks, "PUBLIC", False), patch.object(
                build_blocks, "extract_pdf", side_effect=RuntimeError("broken PDF")
            ):
                self.assertEqual(build_blocks.main(), 1)

    def test_public_full_text_skip_remains_successful(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            build_blocks, "SOURCES", Path(tmp)
        ), patch.object(build_blocks, "PUBLIC", True), patch.object(
            build_blocks, "ALLOW_FULL", False
        ), patch.object(build_blocks, "extract_pdf") as extract_pdf:
            self.assertEqual(build_blocks.main(), 0)
            extract_pdf.assert_not_called()


if __name__ == "__main__":
    unittest.main()
