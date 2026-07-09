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
        with patch.dict(os.environ, {"CAPTION_BACKEND": "claude", "PW_LLM_PROVIDER": "codex"}, clear=True):
            self.assertEqual(caption._default_backend(), "claude")

    def test_matches_codex_llm_provider(self):
        with patch.dict(os.environ, {"PW_LLM_PROVIDER": "codex"}, clear=True):
            self.assertEqual(caption._default_backend(), "codex")

    def test_matches_legacy_llm_codex_bridge(self):
        with patch.dict(os.environ, {"LLM_CMD": "../pipeline/scripts/llm-codex.sh"}, clear=True):
            self.assertEqual(caption._default_backend(), "codex")

    def test_falls_back_to_gemini_without_codex(self):
        # No CAPTION_BACKEND, no codex provider → previous default.
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(caption._default_backend(), "gemini")

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
        self.assertEqual(seen["cmd"][-3:], ["-i", "/tmp/x.png", "-"])  # image + stdin prompt

    def test_agy_dispatch_points_at_path_with_model(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            class R:  # noqa
                stdout = "a caption"
            return R()

        with patch.object(caption.subprocess, "run", fake_run):
            out = caption._dispatch_agy("Gemini 3.5 Flash (Low)", "describe it",
                                        Path("/tmp/x.png"))
        self.assertEqual(out, "a caption")
        self.assertEqual(seen["cmd"][0], "agy")
        self.assertIn("--dangerously-skip-permissions", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("--model") + 1], "Gemini 3.5 Flash (Low)")
        prompt_arg = seen["cmd"][seen["cmd"].index("-p") + 1]
        self.assertIn("/tmp/x.png", prompt_arg)  # agy reads the file it's pointed at

    def test_parse_claude_result_prefers_result_event(self):
        stream = "\n".join([
            '{"type":"system","subtype":"init"}',
            '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"hmm"}]}}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"A red box."}]}}',
            'not-json',
            '{"type":"result","subtype":"success","result":"A red box over a blue box."}',
        ])
        self.assertEqual(caption._parse_claude_result(stream), "A red box over a blue box.")

    def test_parse_claude_result_falls_back_to_assistant_text(self):
        stream = '{"type":"assistant","message":{"content":[{"type":"text","text":"Fallback caption."}]}}'
        self.assertEqual(caption._parse_claude_result(stream), "Fallback caption.")


if __name__ == "__main__":
    unittest.main()
