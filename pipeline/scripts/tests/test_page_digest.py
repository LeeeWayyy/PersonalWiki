import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "page_digest", ROOT / "scripts" / "page-digest.py"
)
page_digest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(page_digest)


class PageDigestTests(unittest.TestCase):
    def test_legacy_page_marks_omitted_content(self):
        text = "\n".join(f"line {index}" for index in range(20)) + "\n"
        result = page_digest.digest(text, body_lines=2)
        self.assertIn("line 6", result)
        self.assertNotIn("line 7\n", result)
        self.assertIn("digest: 13 body line(s) elided", result)
        self.assertIn("Request full content via expand action", result)

    def test_short_legacy_page_has_no_false_elision_marker(self):
        text = "# Legacy\n\nComplete body.\n"
        self.assertEqual(page_digest.digest(text, body_lines=2), text)


if __name__ == "__main__":
    unittest.main()
