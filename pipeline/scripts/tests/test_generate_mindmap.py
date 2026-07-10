import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("generate_mindmap", ROOT / "scripts" / "generate-mindmap.py")
mindmap = importlib.util.module_from_spec(SPEC)
sys.modules["generate_mindmap"] = mindmap
SPEC.loader.exec_module(mindmap)


class GenerateMindmapTests(unittest.TestCase):
    def test_source_prompt_cap_is_around_120k(self):
        self.assertEqual(mindmap.SOURCE_CHAR_LIMIT, 120_000)


if __name__ == "__main__":
    unittest.main()
