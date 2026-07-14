import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load_extract_module():
    """Load extract.py with a stub markdownify so only bs4 is required."""
    fake_markdownify = types.ModuleType("markdownify")
    fake_markdownify.markdownify = lambda html, **kw: html
    spec = importlib.util.spec_from_file_location("extract_pdf_outline_test_mod",
                                                  ROOT / "scripts" / "extract.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"markdownify": fake_markdownify}):
        sys.modules["extract_pdf_outline_test_mod"] = module
        spec.loader.exec_module(module)
    return module


try:
    import pypdf
    extract = load_extract_module()
    _load_error = None
except ImportError as e:  # bs4/pypdf absent (bare CI python) → skip, don't error.
    extract = None
    _load_error = e


@unittest.skipIf(extract is None, f"deps unavailable: {_load_error}")
class TestPdfOutlineSections(unittest.TestCase):
    def _reader_with_outline(self):
        """Blank 6-page PDF with a Part → Chapter outline plus a deep entry."""
        w = pypdf.PdfWriter()
        for _ in range(6):
            w.add_blank_page(width=200, height=200)
        part = w.add_outline_item("Part I", 1)
        ch1 = w.add_outline_item("Chapter 1", 1, parent=part)
        w.add_outline_item("1.1 Deep subsection", 2, parent=ch1)  # depth 3 → dropped
        w.add_outline_item("Chapter 2", 3, parent=part)
        w.add_outline_item("Backwards jump", 0)                   # non-monotonic → dropped
        w.add_outline_item("Appendix", 5)
        buf = io.BytesIO()
        w.write(buf)
        buf.seek(0)
        return pypdf.PdfReader(buf)

    def test_flatten_depth_and_monotonic(self):
        got = extract._pdf_outline_sections(self._reader_with_outline())
        self.assertEqual(got, [("Part I", 1), ("Chapter 1", 1),
                               ("Chapter 2", 3), ("Appendix", 5)])

    def test_no_outline_returns_empty(self):
        w = pypdf.PdfWriter()
        w.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        w.write(buf)
        buf.seek(0)
        self.assertEqual(extract._pdf_outline_sections(pypdf.PdfReader(buf)), [])

    def test_sections_from_outline(self):
        page_texts = ["front.", "one-a.", "one-b.", "two.", "", "appx."]
        outline = [("Part I", 1), ("Chapter 1", 1), ("Chapter 2", 3),
                   ("Appendix", 5)]
        got = extract._pdf_sections_from_outline(page_texts, outline)
        self.assertEqual(got, [
            "\n\n## Front matter\n\nfront.\n",
            # "Part I" and "Chapter 1" share a start page → Part I's range is
            # empty and skipped; its text lands under Chapter 1.
            "\n\n## Chapter 1\n\none-a.\n\none-b.\n",
            "\n\n## Chapter 2\n\ntwo.\n",
            "\n\n## Appendix\n\nappx.\n",
        ])

    def test_join_pages_mid_sentence_continues_paragraph(self):
        # Complete sentence → paragraph break; mid-sentence → same paragraph.
        self.assertEqual(extract._join_pages(["終わった。", "つぎ"]), "終わった。\n\nつぎ")
        self.assertEqual(extract._join_pages(["とちゅうで", "つづき。"]), "とちゅうで\nつづき。")

    def test_trailing_fragment_carries_into_next_section(self):
        # Chapter 1's page ends mid-sentence ("…でも") — the fragment must move
        # to the head of Chapter 2 so the sentence stays whole.
        page_texts = ["はじめ。ここで終わらない。でも", "かえってくるのは。おわり。"]
        outline = [("1", 0), ("2", 1)]
        got = extract._pdf_sections_from_outline(page_texts, outline)
        self.assertEqual(got, [
            "\n\n## 1\n\nはじめ。ここで終わらない。\n",
            "\n\n## 2\n\nでも\nかえってくるのは。おわり。\n",
        ])

    def test_printed_title_line_is_dropped(self):
        # The chapter title printed as the page's first line duplicates the
        # `##` heading and would glue into the next sentence — drop it.
        got = extract._pdf_sections_from_outline(
            ["レオン・ウェルトに\n　子どものみなさん、ゆるしてください。"],
            [("レオン・ウェルトに", 0)])
        self.assertEqual(got, ["\n\n## レオン・ウェルトに\n\n　子どものみなさん、ゆるしてください。\n"])

    def test_closing_bracket_ends_a_sentence(self):
        # 〉 closes the frontispiece caption — it must NOT be carried into the
        # next section as a mid-sentence fragment.
        got = extract._pdf_sections_from_outline(
            ["〈キャプション。\n〉", "つぎのしょう。"], [("1", 0), ("2", 1)])
        self.assertEqual(got, [
            "\n\n## 1\n\n〈キャプション。\n〉\n",
            "\n\n## 2\n\nつぎのしょう。\n",
        ])

    def test_own_line_tail_is_not_carried(self):
        # An unpunctuated line of its own at section end (dedication/sign-off)
        # is deliberate typesetting, not a severed sentence — keep it.
        got = extract._pdf_sections_from_outline(
            ["ことばをこう書きなおします。\n（かわいい少年だったころの）\nレオン・ウェルトに",
             "ぼくが６さいのとき。"],
            [("レオン・ウェルトに", 0), ("１", 1)])
        self.assertEqual(got, [
            "\n\n## レオン・ウェルトに\n\nことばをこう書きなおします。\n（かわいい少年だったころの）\nレオン・ウェルトに\n",
            "\n\n## １\n\nぼくが６さいのとき。\n",
        ])

    def test_no_terminator_at_all_keeps_section_intact(self):
        got = extract._pdf_sections_from_outline(["no ender here", "next."],
                                                 [("1", 0), ("2", 1)])
        self.assertEqual(got, [
            "\n\n## 1\n\nno ender here\n",
            "\n\n## 2\n\nnext.\n",
        ])

    def test_empty_outline_means_page_fallback(self):
        self.assertEqual(extract._pdf_sections_from_outline(["a"], []), [])


if __name__ == "__main__":
    unittest.main()
