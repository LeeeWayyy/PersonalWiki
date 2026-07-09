import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load_extract_module():
    fake_markdownify = types.ModuleType("markdownify")

    def markdownify(html, heading_style="ATX", bullets="-"):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())

    fake_markdownify.markdownify = markdownify
    spec = importlib.util.spec_from_file_location("extract_mobi_test_mod", ROOT / "scripts" / "extract.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"markdownify": fake_markdownify}):
        sys.modules["extract_mobi_test_mod"] = module
        spec.loader.exec_module(module)
    return module


try:
    extract = load_extract_module()
    _load_error = None
except ImportError as e:  # bs4/markdownify absent (e.g. bare CI python) → skip, don't error.
    extract = None
    _load_error = e


@unittest.skipIf(extract is None, f"extract.py deps unavailable: {_load_error}")
class MobiSupportTests(unittest.TestCase):
    def test_dispatches_mobi_through_converted_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "fixture.mobi"
            source.write_bytes(b"fake mobi payload")
            converted_dir = root / "converted"
            converted_dir.mkdir()
            converted = converted_dir / "fixture.html"
            converted.write_text(
                "<html><head><title>Fixture MOBI</title></head>"
                "<body><h1>第一章</h1><p>Known MOBI paragraph.</p></body></html>",
                encoding="utf-8",
            )
            fake_mobi = types.SimpleNamespace(
                extract=lambda _path: (str(converted_dir), str(converted))
            )

            with patch.dict(sys.modules, {"mobi": fake_mobi}):
                text = extract.dispatch(str(source))

            self.assertIn("## Fixture MOBI", text)
            self.assertIn("Known MOBI paragraph.", text)
            self.assertFalse(converted_dir.exists())

    def test_mobi_assets_are_routed_to_original_source_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "fixture.azw3"
            source.write_bytes(b"fake mobi payload")
            converted_dir = root / "converted"
            converted_dir.mkdir()
            converted = converted_dir / "fixture.html"
            converted.write_text(
                "<html><head><title>Fixture AZW3</title></head>"
                "<body><p>Text.</p><img src=\"https://example.test/fig.png\"></body></html>",
                encoding="utf-8",
            )
            fake_mobi = types.SimpleNamespace(
                extract=lambda _path: (str(converted_dir), str(converted))
            )
            seen = {}

            def fake_extract_web_assets(_body, assets_dir, source_id, base_url):
                seen["assets_dir"] = assets_dir
                seen["source_id"] = source_id
                seen["base_url"] = base_url

            with patch.dict(sys.modules, {"mobi": fake_mobi}), patch.object(
                extract, "_extract_web_assets_from_dom", fake_extract_web_assets
            ):
                extract.extract_mobi(source, write_assets=True, source_id="S1MOBI")

            self.assertEqual(seen["assets_dir"], root / "fixture.azw3.assets")
            self.assertEqual(seen["source_id"], "S1MOBI")
            self.assertIsNone(seen["base_url"])
            self.assertFalse(converted_dir.exists())


if __name__ == "__main__":
    unittest.main()
