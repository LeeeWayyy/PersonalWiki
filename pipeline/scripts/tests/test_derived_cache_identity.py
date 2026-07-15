import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import derived_lib  # noqa: E402


class DerivedCacheIdentityTests(unittest.TestCase):
    def test_provider_switch_misses_existing_cache(self):
        identities = {
            "codex": {"provider": "codex", "model": "a"},
            "claude": {"provider": "claude", "model": "b"},
        }
        selected = "codex"
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            derived_lib.llm_client,
            "execution_identity",
            side_effect=lambda: identities[selected],
        ):
            cache = Path(tmp)
            derived_lib.save_cache(cache, "v1", "S", "a" * 64, {"ok": True}, "01")
            self.assertEqual(
                derived_lib.load_cache(cache, "v1", "S", "a" * 64, "01"),
                {"ok": True},
            )
            selected = "claude"
            self.assertIsNone(
                derived_lib.load_cache(cache, "v1", "S", "a" * 64, "01")
            )


if __name__ == "__main__":
    unittest.main()
