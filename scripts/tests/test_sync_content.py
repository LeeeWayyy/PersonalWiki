import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("sync_content", ROOT / "scripts" / "sync_content.py")
sync_content = importlib.util.module_from_spec(SPEC)
sys.modules["sync_content"] = sync_content
SPEC.loader.exec_module(sync_content)


class SyncContentTests(unittest.TestCase):
    def test_refuses_to_delete_generated_vault_when_used_as_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "vault"
            source.mkdir()
            marker = source / "keep.md"
            marker.write_text("do not delete\n", encoding="utf-8")

            with self.assertRaises(sync_content.UnsafePathError):
                sync_content.sync_content(
                    root,
                    {"PW_CONTENT_DIR": str(source)},
                    run_post_build=False,
                    log=lambda _message: None,
                )

            self.assertEqual(marker.read_text(encoding="utf-8"), "do not delete\n")

    def test_worktree_sync_writes_vault_assets_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "content"
            (content / "wiki").mkdir(parents=True)
            (content / "sources" / "book.assets").mkdir(parents=True)
            (content / "lang" / "sources" / "lesson.assets").mkdir(parents=True)
            (content / "wiki" / "index.md").write_text("# Home\n", encoding="utf-8")
            (content / "sources" / "book.assets" / "fig.txt").write_text("figure\n", encoding="utf-8")
            (content / "lang" / "sources" / "lesson.assets" / "audio.txt").write_text(
                "audio\n",
                encoding="utf-8",
            )

            result = sync_content.sync_content(
                root,
                {"PW_CONTENT_DIR": str(content), "PW_SYNC_WORKTREE": "1"},
                run_post_build=False,
                log=lambda _message: None,
            )

            self.assertEqual((root / "vault" / "wiki" / "index.md").read_text(encoding="utf-8"), "# Home\n")
            self.assertEqual(
                (root / "public" / "vault-assets" / "book.assets" / "fig.txt").read_text(encoding="utf-8"),
                "figure\n",
            )
            self.assertEqual(
                (root / "public" / "vault-assets" / "lang" / "lesson.assets" / "audio.txt").read_text(
                    encoding="utf-8"
                ),
                "audio\n",
            )
            meta = json.loads((root / "vault" / ".sync-meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["commit"], "local")
            self.assertEqual(meta["source"], str(content.resolve()))
            self.assertEqual(result.asset_count, 2)


if __name__ == "__main__":
    unittest.main()
