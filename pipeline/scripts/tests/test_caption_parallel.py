import importlib.util
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("caption", ROOT / "scripts" / "caption.py")
caption = importlib.util.module_from_spec(SPEC)
sys.modules["caption"] = caption
SPEC.loader.exec_module(caption)

from asset_manifest import ImageEntry, read_manifest, write_manifest  # noqa: E402


def _seed_assets(assets_dir: Path, n: int) -> None:
    """n non-decorative entries with real backing files (big enough dims/bytes
    to clear the decorative heuristic), so main() routes them all to dispatch."""
    entries = []
    for i in range(n):
        name = f"img{i:02d}.png"
        (assets_dir / name).write_bytes(b"\x89PNG" + b"0" * 30_000)
        entries.append(ImageEntry(file=name, sha256=f"sha{i}", bytes=30_000,
                                  dimensions=[400, 400]))
    write_manifest(assets_dir, "SRC1", entries)


class ApplyCaptionResultTests(unittest.TestCase):
    def _entry(self):
        return ImageEntry(file="a.png", sha256="x", bytes=1, dimensions=[10, 10])

    def test_ok_sets_vision_source(self):
        e = self._entry()
        s = caption._apply_caption_result(e, "A red box.", None, "agy", "flash", "2026-01-01")
        self.assertEqual(e.caption, "A red box.")
        self.assertEqual(e.caption_source, "vision")
        self.assertFalse(e.decorative)
        self.assertEqual(e.caption_model, "agy:flash")
        self.assertIn("ok", s)

    def test_decorative_token(self):
        e = self._entry()
        caption._apply_caption_result(e, "DECORATIVE", None, "agy", "flash", "2026-01-01")
        self.assertTrue(e.decorative)
        self.assertIsNone(e.caption)
        self.assertIsNone(e.caption_source)

    def test_empty_output_is_transient(self):
        e = self._entry()
        caption._apply_caption_result(e, "   ", None, "agy", "flash", "2026-01-01")
        self.assertIsNone(e.caption)
        self.assertEqual(e.caption_error_kind, "transient")

    def test_called_process_error_classified(self):
        e = self._entry()
        exc = caption.subprocess.CalledProcessError(1, "agy", stderr="rate limit hit")
        caption._apply_caption_result(e, None, exc, "agy", "flash", "2026-01-01")
        self.assertIsNone(e.caption)
        self.assertIsNone(e.caption_source)
        self.assertEqual(e.caption_error_kind, "transient")   # "rate limit" → transient

    def test_timeout_error_is_transient(self):
        e = self._entry()
        exc = caption.subprocess.TimeoutExpired("agy", 180)
        caption._apply_caption_result(e, None, exc, "agy", "flash", "2026-01-01")
        self.assertEqual(e.caption_error_kind, "transient")
        self.assertIsNone(e.caption_source)


class ParallelDispatchTests(unittest.TestCase):
    def _run(self, assets_dir, jobs, dispatch):
        argv = ["caption.py", str(assets_dir), "--backend", "gemini", "--jobs", str(jobs)]
        with patch.object(caption, "_dispatch", dispatch), \
             patch.object(caption.shutil, "which", return_value="/usr/bin/true"), \
             patch.object(sys, "argv", argv), \
             patch.dict(os.environ, {}, clear=True):
            return caption.main()

    def test_all_captioned_and_runs_concurrently(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            assets = Path(d)
            _seed_assets(assets, 6)
            state = {"cur": 0, "max": 0}
            clk = threading.Lock()

            def dispatch(backend, model, prompt, image):
                with clk:
                    state["cur"] += 1
                    state["max"] = max(state["max"], state["cur"])
                time.sleep(0.05)
                with clk:
                    state["cur"] -= 1
                return f"caption for {image.name}"

            rc = self._run(assets, jobs=4, dispatch=dispatch)
            self.assertEqual(rc, 0)
            _, entries = read_manifest(assets)
            self.assertEqual(len(entries), 6)
            self.assertTrue(all(e.caption and e.caption_source == "vision" for e in entries))
            self.assertGreater(state["max"], 1)   # actually ran in parallel

    def test_limit_caps_dispatch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            assets = Path(d)
            _seed_assets(assets, 5)
            calls = {"n": 0}
            lk = threading.Lock()

            def dispatch(backend, model, prompt, image):
                with lk:
                    calls["n"] += 1
                return "cap"

            argv = ["caption.py", str(assets), "--backend", "gemini",
                    "--jobs", "4", "--limit", "2"]
            with patch.object(caption, "_dispatch", dispatch), \
                 patch.object(caption.shutil, "which", return_value="/usr/bin/true"), \
                 patch.object(sys, "argv", argv), \
                 patch.dict(os.environ, {}, clear=True):
                rc = caption.main()
            self.assertEqual(rc, 0)
            self.assertEqual(calls["n"], 2)        # --limit honored


if __name__ == "__main__":
    unittest.main()
