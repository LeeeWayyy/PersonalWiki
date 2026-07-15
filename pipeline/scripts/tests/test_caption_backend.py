import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("caption", ROOT / "scripts" / "caption.py")
caption = importlib.util.module_from_spec(SPEC)
sys.modules["caption"] = caption
SPEC.loader.exec_module(caption)


class CaptionBackendTests(unittest.TestCase):
    def test_explicit_caption_backend_wins(self):
        with patch.dict(os.environ, {"CAPTION_BACKEND": "gemini", "PW_LLM_PROVIDER": "codex"}, clear=True):
            self.assertEqual(caption._default_backend(), "agy")

    def test_matches_codex_llm_provider(self):
        with patch.dict(os.environ, {"PW_LLM_PROVIDER": "codex"}, clear=True):
            self.assertEqual(caption._default_backend(), "codex")

    def test_matches_legacy_llm_codex_bridge(self):
        with patch.dict(os.environ, {"LLM_CMD": "../pipeline/scripts/llm-codex.sh"}, clear=True):
            self.assertEqual(caption._default_backend(), "codex")

    def test_falls_back_to_agy_without_codex(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(caption._default_backend(), "agy")

    def test_codex_default_is_mini_model(self):
        self.assertEqual(caption.DEFAULT_MODELS["codex"], "gpt-5-mini")

    def test_agy_binary_prefers_new_name_and_accepts_legacy_fallback(self):
        with patch.dict(os.environ, {"PW_AGY_BIN": "new-agy", "GEMINI_BIN": "old-agy"}, clear=True):
            self.assertEqual(caption._backend_bin("agy"), "new-agy")
        with patch.dict(os.environ, {"GEMINI_BIN": "old-agy"}, clear=True):
            self.assertEqual(caption._backend_bin("gemini"), "old-agy")

    def test_agy_dispatch_uses_long_flags_stdin_and_scrubbed_env(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen.update(cmd=cmd, kwargs=kwargs)
            class R:  # noqa
                stdout = "a caption"
            return R()

        env = {
            "PW_AGY_BIN": "agy-test",
            "PW_AUTH_TOKEN": "app-secret",
            "GEMINI_API_KEY": "agy-secret",
            "PATH": os.environ.get("PATH", ""),
        }
        with patch.dict(os.environ, env, clear=True), patch.object(caption.subprocess, "run", fake_run):
            out = caption._dispatch("gemini", "flash", "prompt", Path("/tmp/image with space.png"))
        self.assertEqual(out, "a caption")
        self.assertEqual(seen["cmd"], [
            "agy-test", "--print", "--mode", "plan", "--sandbox",
            "--model", "flash",
        ])
        self.assertEqual(seen["kwargs"]["input"], "prompt @/tmp/image with space.png")
        self.assertNotIn("PW_AUTH_TOKEN", seen["kwargs"]["env"])
        self.assertEqual(seen["kwargs"]["env"]["GEMINI_API_KEY"], "agy-secret")

    def test_codex_dispatch_omits_model_when_none(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            class R:  # noqa
                stdout = "a caption"
            return R()

        with patch.object(caption.subprocess, "run", fake_run):
            out = caption._dispatch_codex(None, "prompt", Path("/tmp/x.png"))
        self.assertEqual(out, "a caption")
        self.assertNotIn("-m", seen["cmd"])                       # no forced model
        self.assertIn("--sandbox", seen["cmd"])                   # mirrors llm_client flags
        self.assertIn("shell_tool", seen["cmd"])
        self.assertEqual(seen["cmd"][-3:], ["-i", "/tmp/x.png", "-"])  # image + stdin prompt

if __name__ == "__main__":
    unittest.main()
