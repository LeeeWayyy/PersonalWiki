import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load_extract_module():
    """Load extract.py with a stub markdownify so only bs4 is required."""
    fake_markdownify = types.ModuleType("markdownify")

    def markdownify(html, heading_style="ATX", bullets="-"):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        return "\n".join(l.strip() for l in soup.get_text("\n").splitlines() if l.strip())

    fake_markdownify.markdownify = markdownify
    spec = importlib.util.spec_from_file_location("extract_caption_test_mod",
                                                  ROOT / "scripts" / "extract.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"markdownify": fake_markdownify}):
        sys.modules["extract_caption_test_mod"] = module
        spec.loader.exec_module(module)
    return module


try:
    extract = load_extract_module()
    _load_error = None
except ImportError as e:  # bs4 absent (bare CI python) → skip, don't error.
    extract = None
    _load_error = e


def _cap(html):
    from bs4 import BeautifulSoup
    return extract._embedded_caption(BeautifulSoup(html, "html.parser").find("img"))


@unittest.skipIf(extract is None, f"extract.py deps unavailable: {_load_error}")
class EmbeddedCaptionHtmlTests(unittest.TestCase):
    def test_figcaption_wins_over_junk_alt(self):
        html = '<figure><img alt="x.png"/><figcaption>图 12-2 线粒体呼吸链</figcaption></figure>'
        self.assertEqual(_cap(html), "图 12-2 线粒体呼吸链")

    def test_good_alt_used_when_no_figcaption(self):
        self.assertEqual(_cap('<img alt="A scatter plot of oxygen vs depth"/>'),
                         "A scatter plot of oxygen vs depth")

    def test_junk_filename_alt_rejected(self):
        self.assertIsNone(_cap('<img alt="image1.jpg"/>'))

    def test_too_short_alt_rejected(self):
        self.assertIsNone(_cap('<img alt="图"/>'))

    def test_placeholder_alt_rejected(self):
        self.assertIsNone(_cap('<img alt="image"/>'))

    def test_bare_label_alt_rejected(self):
        self.assertIsNone(_cap('<img alt="Figure 3"/>'))
        self.assertIsNone(_cap('<img alt="图 12-2"/>'))

    def test_labelled_alt_with_description_kept(self):
        self.assertEqual(_cap('<img alt="图 12-2 线粒体呼吸链"/>'), "图 12-2 线粒体呼吸链")

    def test_bare_label_alt_falls_through_to_adjacent(self):
        html = '<div><img alt="Figure 3"/><p class="caption">Figure 3. A cell.</p></div>'
        self.assertEqual(_cap(html), "Figure 3. A cell.")

    def test_adjacent_caption_class(self):
        self.assertEqual(_cap('<div><img/><p class="caption">Figure 3. A cell.</p></div>'),
                         "Figure 3. A cell.")

    def test_adjacent_figure_label_paragraph(self):
        self.assertEqual(_cap('<div><img/><p>图3-1 细胞结构</p></div>'), "图3-1 细胞结构")

    def test_body_prose_not_captured(self):
        self.assertIsNone(_cap('<div><img/><p>Some body prose here.</p></div>'))

    def test_no_caption_source_at_all(self):
        self.assertIsNone(_cap('<img/>'))


@unittest.skipIf(extract is None, f"extract.py deps unavailable: {_load_error}")
class PdfLabelCaptionTests(unittest.TestCase):
    # pdfplumber coords: top=0 at page top; "below" = larger top.
    LINES = [
        {"text": "图 12-2 线粒体呼吸链示意图", "x0": 100, "x1": 300, "top": 410, "bottom": 422},
        {"text": "显示电子传递过程", "x0": 100, "x1": 260, "top": 423, "bottom": 435},
        {"text": "下一段正文开始", "x0": 100, "x1": 260, "top": 470, "bottom": 482},
    ]

    def test_label_below_figure_gathers_continuation(self):
        # figure sits just above the label (bottom=400, label top=410 → gap 10).
        cap = extract._pdf_caption_for((100, 200, 320, 400), self.LINES)
        self.assertEqual(cap, "图 12-2 线粒体呼吸链示意图 显示电子传递过程")

    def test_no_label_line_returns_none(self):
        lines = [{"text": "just body", "x0": 100, "x1": 200, "top": 410, "bottom": 422}]
        self.assertIsNone(extract._pdf_caption_for((100, 200, 320, 400), lines))

    def test_label_too_far_returns_none(self):
        # figure bottom=300, label top=410 → gap 110 > PDF_CAPTION_MAX_GAP.
        self.assertIsNone(extract._pdf_caption_for((100, 200, 320, 300), self.LINES))

    def test_label_needs_horizontal_overlap(self):
        # label is off to the side (x 600-700), no overlap with figure x 100-320.
        off = [{"text": "图 9-9 无关图注", "x0": 600, "x1": 700, "top": 410, "bottom": 422}]
        self.assertIsNone(extract._pdf_caption_for((100, 200, 320, 400), off))

    def test_prefers_below_label_over_closer_above(self):
        # A previous figure's caption sits just ABOVE (gap 5); the correct
        # caption sits BELOW (gap 10). Below must win despite the larger gap.
        lines = [
            {"text": "图 5-1 上一个图注", "x0": 100, "x1": 300, "top": 150, "bottom": 195},
            {"text": "图 5-2 正确图注", "x0": 100, "x1": 300, "top": 410, "bottom": 422},
        ]
        self.assertEqual(extract._pdf_caption_for((100, 200, 320, 400), lines),
                         "图 5-2 正确图注")

    def test_above_label_used_when_no_below(self):
        lines = [{"text": "图 5-1 上方图注", "x0": 100, "x1": 300, "top": 150, "bottom": 195}]
        self.assertEqual(extract._pdf_caption_for((100, 200, 320, 400), lines),
                         "图 5-1 上方图注")


@unittest.skipIf(extract is None, f"extract.py deps unavailable: {_load_error}")
class ApplySourceCaptionTests(unittest.TestCase):
    def _entry(self, **kw):
        return extract.ImageEntry(file="a.png", sha256="x", bytes=1,
                                  dimensions=[10, 10], **kw)

    def test_none_caption_is_noop(self):
        e = self._entry()
        extract._apply_source_caption(e, None, "embedded")
        self.assertIsNone(e.caption)
        self.assertIsNone(e.caption_source)

    def test_clears_stale_vision_state_on_write(self):
        e = self._entry(decorative=True, caption_model="agy:x",
                        caption_at="2020-01-01", caption_error="boom",
                        caption_error_kind="terminal")
        extract._apply_source_caption(e, "图 1-1 真图注", "embedded")
        self.assertEqual(e.caption, "图 1-1 真图注")
        self.assertEqual(e.caption_source, "embedded")
        self.assertFalse(e.decorative)          # was True → cleared
        self.assertIsNone(e.caption_model)
        self.assertIsNone(e.caption_at)         # embedded stays dateless
        self.assertIsNone(e.caption_error)
        self.assertIsNone(e.caption_error_kind)

    def test_overwrites_stale_vision_caption(self):
        e = self._entry(caption="old vision", caption_source="vision")
        extract._apply_source_caption(e, "真图注", "embedded")
        self.assertEqual(e.caption, "真图注")
        self.assertEqual(e.caption_source, "embedded")

    def test_overwrites_legacy_caption_without_source(self):
        # Pre-caption_source manifests: caption set, source None → refreshable.
        e = self._entry(caption="legacy", caption_source=None)
        extract._apply_source_caption(e, "真图注", "pdf-label")
        self.assertEqual(e.caption, "真图注")

    def test_keeps_existing_embedded_caption(self):
        e = self._entry(caption="first figcaption", caption_source="embedded")
        extract._apply_source_caption(e, "second", "pdf-label")
        self.assertEqual(e.caption, "first figcaption")   # first-source-wins
        self.assertEqual(e.caption_source, "embedded")

    def test_keeps_existing_pdf_label_caption(self):
        e = self._entry(caption="first label", caption_source="pdf-label")
        extract._apply_source_caption(e, "second", "embedded")
        self.assertEqual(e.caption, "first label")


if __name__ == "__main__":
    unittest.main()
