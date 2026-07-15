import importlib.util
import io
import json
import os
import sys
import tarfile
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
    def test_rejects_non_regular_archive_entries(self):
        for entry_type in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            with self.subTest(entry_type=entry_type), tempfile.TemporaryDirectory() as tmp:
                archive_file = io.BytesIO()
                with tarfile.open(fileobj=archive_file, mode="w") as archive:
                    entry = tarfile.TarInfo("wiki/unsafe.md")
                    entry.type = entry_type
                    entry.linkname = "../../outside" if entry_type != tarfile.FIFOTYPE else ""
                    archive.addfile(entry)
                archive_file.seek(0)

                with self.assertRaisesRegex(sync_content.SyncError, "unsafe archive entry"):
                    sync_content._safe_extract_tar(archive_file, Path(tmp))

    def test_worktree_sync_rejects_symlinks_hardlinks_and_special_files(self):
        makers = {
            "symlink": lambda source, outside: (source / "unsafe").symlink_to(outside),
            "hardlink": lambda source, outside: os.link(outside, source / "unsafe"),
            "fifo": lambda source, _outside: os.mkfifo(source / "unsafe"),
        }
        for name, make_entry in makers.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "content"
                source.mkdir()
                outside = root / "outside"
                outside.write_text("secret\n", encoding="utf-8")
                make_entry(source, outside)

                with self.assertRaisesRegex(sync_content.SyncError, "unsafe filesystem entry"):
                    sync_content._copy_worktree_snapshot(source, root / "vault")

    def test_asset_sync_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "sources" / "book.assets"
            assets.mkdir(parents=True)
            outside = root / "outside"
            outside.write_text("secret\n", encoding="utf-8")
            (assets / "unsafe").symlink_to(outside)

            with self.assertRaisesRegex(sync_content.SyncError, "unsafe filesystem entry"):
                sync_content._copy_asset_dirs(root / "sources", root / "public", Path())

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
