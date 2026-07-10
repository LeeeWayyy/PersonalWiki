import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "format_llm_zone", ROOT / "scripts" / "format-llm-zone.py"
)
fmt = importlib.util.module_from_spec(SPEC)
sys.modules["format_llm_zone"] = fmt
SPEC.loader.exec_module(fmt)


class FormatLlmZoneTests(unittest.TestCase):
    def test_wraps_plain_two_tier_zone_in_ai_callout(self):
        text = """# X

<!-- llm-zone -->
### Synthesis

Line one [src:01KX582AX79FD9BQG2VNMG41NY#第一章].

### From src:01KX582AX79FD9BQG2VNMG41NY#第一章

Evidence line [src:01KX582AX79FD9BQG2VNMG41NY#第一章].
<!-- /llm-zone -->
"""
        out, changed = fmt.normalize_text(text)
        self.assertTrue(changed)
        self.assertIn("> [!AI] LLM Synthesis", out)
        self.assertIn("> ### Synthesis", out)
        self.assertNotIn("From src:01KX582AX79FD9BQG2VNMG41NY", out)
        self.assertNotIn("\n### Synthesis", out)

    def test_strips_source_metadata_heading_from_valid_callout(self):
        text = """# X

<!-- llm-zone -->
> [!AI] LLM Synthesis
>
> ### From src:01KX582AX79FD9BQG2VNMG41NY#第一章 Noisy chapter label
>
> Evidence [src:01KX582AX79FD9BQG2VNMG41NY#第一章].
<!-- /llm-zone -->
"""
        out, changed = fmt.normalize_text(text)
        self.assertTrue(changed)
        self.assertNotIn("From src:", out)
        self.assertIn("> Evidence [src:01KX582AX79FD9BQG2VNMG41NY#第一章].", out)

    def test_valid_callout_is_unchanged(self):
        text = """# X

<!-- llm-zone -->
> [!AI] LLM Synthesis
>
> ### Synthesis
>
> Line [src:01KX582AX79FD9BQG2VNMG41NY#第一章].
<!-- /llm-zone -->
"""
        out, changed = fmt.normalize_text(text)
        self.assertFalse(changed)
        self.assertEqual(out, text)


if __name__ == "__main__":
    unittest.main()
