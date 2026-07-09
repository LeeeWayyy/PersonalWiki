import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("vendor_content", ROOT / "scripts" / "vendor_content.py")
vendor_content = importlib.util.module_from_spec(SPEC)
sys.modules["vendor_content"] = vendor_content
SPEC.loader.exec_module(vendor_content)


class VendorContentTests(unittest.TestCase):
    def test_reject_overlap_blocks_same_or_nested_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"

            for left, right in (
                (source, source),
                (source, source / "nested"),
                (source / "nested", source),
            ):
                with self.subTest(left=left, right=right):
                    with self.assertRaises(vendor_content.VendorContentError):
                        vendor_content.reject_overlap(left, right)

            vendor_content.reject_overlap(source, dest)

    def test_import_content_copies_files_and_skips_local_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "content"
            (source / "wiki").mkdir(parents=True)
            (source / "wiki" / ".git").mkdir()
            (source / "wiki" / "index.md").write_text("# Home\n", encoding="utf-8")
            (source / ".DS_Store").write_text("ignored\n", encoding="utf-8")
            (source / "wiki" / ".git" / "HEAD").write_text("ignored\n", encoding="utf-8")

            messages = vendor_content.import_content(source, dest, root=root)

            self.assertEqual((dest / "wiki" / "index.md").read_text(encoding="utf-8"), "# Home\n")
            self.assertFalse((dest / ".DS_Store").exists())
            self.assertFalse((dest / "wiki" / ".git").exists())
            self.assertTrue(any("content/ populated" in message for message in messages))

    def test_init_git_snapshot_seeds_gitignore_so_alias_index_is_ignored(self):
        # Regression: an ingested vault leaves wiki/.alias-index.json untracked;
        # without a seeded .gitignore the backend preflight (refuses any untracked
        # wiki/ path) blocks every later ingest. init must seed the ignore rules.
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / "wiki").mkdir()
            (dest / "wiki" / ".alias-index.json").write_text("{}", encoding="utf-8")
            (dest / "wiki" / "entities").mkdir()
            (dest / "wiki" / "entities" / "x.md").write_text("page\n", encoding="utf-8")

            ok, _ = vendor_content.init_git_snapshot(
                dest, user_email="t@t", user_name="t", commit_message="init", allow_empty=True)
            self.assertTrue(ok)
            self.assertIn("wiki/.alias-index.json",
                          (dest / ".gitignore").read_text(encoding="utf-8"))
            status = subprocess.run(
                ["git", "-C", str(dest), "status", "--porcelain"],
                capture_output=True, text=True).stdout
            self.assertNotIn(".alias-index.json", status)  # ignored → not untracked
            self.assertEqual(status.strip(), "")           # clean tree, nothing to block on

    def test_init_empty_content_creates_committed_git_vault_from_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "content"   # does not exist yet
            ok, msg = vendor_content.init_empty_content(dest)
            self.assertTrue(ok, msg)
            self.assertTrue((dest / ".git").is_dir())
            self.assertTrue((dest / ".gitignore").is_file())
            status = subprocess.run(
                ["git", "-C", str(dest), "status", "--porcelain"],
                capture_output=True, text=True).stdout
            self.assertEqual(status.strip(), "")  # clean → preflight-ready
            log = subprocess.run(
                ["git", "-C", str(dest), "log", "--oneline"],
                capture_output=True, text=True).stdout
            self.assertTrue(log.strip())          # has a baseline commit

    def test_init_empty_content_refuses_populated_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "content"
            dest.mkdir()
            (dest / "keep.md").write_text("mine\n", encoding="utf-8")
            ok, msg = vendor_content.init_empty_content(dest)
            self.assertFalse(ok)
            self.assertIn("not empty", msg)
            self.assertEqual((dest / "keep.md").read_text(encoding="utf-8"), "mine\n")

    def test_init_git_snapshot_keeps_existing_gitignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / ".gitignore").write_text("custom-rule\n", encoding="utf-8")
            ok, _ = vendor_content.init_git_snapshot(
                dest, user_email="t@t", user_name="t", commit_message="init", allow_empty=True)
            self.assertTrue(ok)
            self.assertEqual((dest / ".gitignore").read_text(encoding="utf-8"), "custom-rule\n")

    def test_import_content_refuses_populated_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "content"
            source.mkdir()
            dest.mkdir()
            (dest / "keep.md").write_text("keep\n", encoding="utf-8")

            messages = vendor_content.import_content(source, dest, root=root)

            self.assertEqual(
                messages,
                [f"content/ is already populated. Remove it first to re-vendor: rm -rf {dest.resolve()}"],
            )
            self.assertEqual((dest / "keep.md").read_text(encoding="utf-8"), "keep\n")


if __name__ == "__main__":
    unittest.main()
