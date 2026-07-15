import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "delete-lang-source.py"
SOURCE_ID = "01DELETE000000000000000000"


class DeleteLangSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.reading = self.repo / "lang" / "_reading"
        self.sources = self.repo / "lang" / "sources"
        self.reading.mkdir(parents=True)
        self.sources.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "t@t.t"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "t"], check=True)
        with (self.repo / ".git" / "info" / "exclude").open("a", encoding="utf-8") as f:
            f.write("\n/lang/.wiki/\n")

        self.asset = self.sources / "book.txt"
        self.sidecar = self.sources / "book.txt.md"
        self.manifest = self.reading / "book.reading.json"
        self.html = self.reading / "book.html"
        self.asset.write_text("book\n", encoding="utf-8")
        digest = hashlib.sha256(self.asset.read_bytes()).hexdigest()
        self.sidecar.write_text(
            f"---\nsource_id: {SOURCE_ID}\ntype: source\nsha256: {digest}\n---\n",
            encoding="utf-8",
        )
        self.manifest.write_text(json.dumps({"source_id": SOURCE_ID}) + "\n", encoding="utf-8")
        self.html.write_text("<p>reader</p>\n", encoding="utf-8")
        self.commit("seed")

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, message):
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", message], check=True)

    def run_delete(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--source-id", SOURCE_ID],
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "PW_CONTENT_DIR": str(self.repo), "VAULT_CONTENT_DIR": str(self.repo)},
        )

    def run_delete_with_lint(self, lint):
        runner = """
import importlib.util
import sys
from pathlib import Path

script, lint, source_id = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("delete_lang_source", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.LINT = lint
sys.argv = [str(script), "--source-id", str(source_id)]
raise SystemExit(module.main())
"""
        return subprocess.run(
            [sys.executable, "-c", runner, str(SCRIPT), str(lint), SOURCE_ID],
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "PW_CONTENT_DIR": str(self.repo), "VAULT_CONTENT_DIR": str(self.repo)},
        )

    def test_deletes_and_commits_tracked_reader(self):
        result = self.run_delete()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(path.exists() for path in (
            self.asset, self.sidecar, self.manifest, self.html,
        )))
        message = subprocess.check_output(
            ["git", "-C", str(self.repo), "log", "-1", "--format=%s"], text=True,
        ).strip()
        self.assertEqual(message, f"lang: delete {SOURCE_ID}")
        self.assertEqual(self.git_status(), "")

    def test_ignores_source_id_in_sidecar_body(self):
        decoy_asset = self.sources / "decoy.txt"
        decoy_sidecar = self.sources / "decoy.txt.md"
        decoy_asset.write_text("decoy\n", encoding="utf-8")
        digest = hashlib.sha256(decoy_asset.read_bytes()).hexdigest()
        decoy_sidecar.write_text(
            "---\nsource_id: 01DECOY0000000000000000000\ntype: source\n"
            f"sha256: {digest}\n---\n\nsource_id: {SOURCE_ID}\n",
            encoding="utf-8",
        )
        self.commit("decoy")

        result = self.run_delete()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(decoy_asset.exists())
        self.assertTrue(decoy_sidecar.exists())
        self.assertEqual(self.git_status(), "")

    def test_refuses_source_used_by_merged_reader(self):
        merged = self.reading / "merged.reading.json"
        merged.write_text(json.dumps({
            "source_id": "merge-book-audio", "merged_from": [SOURCE_ID, "01AUDIO"],
        }) + "\n", encoding="utf-8")
        (self.reading / "merged.html").write_text("<p>merged</p>\n", encoding="utf-8")
        self.commit("merged")
        head = self.head()

        result = self.run_delete()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("delete them first", result.stderr)
        self.assertTrue(all(path.exists() for path in (
            self.asset, self.sidecar, self.manifest, self.html,
        )))
        self.assertEqual(self.head(), head)
        self.assertEqual(self.git_status(), "")

    def test_lint_failure_rolls_back_deletion(self):
        legacy = self.reading / "legacy.html"
        legacy.write_text(f"<p>[src:{SOURCE_ID}]</p>\n", encoding="utf-8")
        self.commit("legacy citation")
        head = self.head()

        result = self.run_delete()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("orphan citation", (result.stdout + result.stderr).lower())
        self.assertTrue(all(path.exists() for path in (
            self.asset, self.sidecar, self.manifest, self.html,
        )))
        self.assertEqual(self.head(), head)
        self.assertEqual(self.git_status(), "")

    def test_sigterm_during_transaction_rolls_back_deletion(self):
        killer = self.repo / ".git" / "kill-parent"
        killer.write_text("#!/bin/sh\nkill -TERM \"$PPID\"\n", encoding="utf-8")
        killer.chmod(0o755)
        head = self.head()

        result = self.run_delete_with_lint(killer)

        self.assertEqual(result.returncode, 128 + 15, result.stderr)
        self.assertTrue(all(path.exists() for path in (
            self.asset, self.sidecar, self.manifest, self.html,
        )))
        self.assertEqual(self.head(), head)
        self.assertEqual(self.git_status(), "")

    def test_sigterm_after_commit_leaves_committed_delete_clean(self):
        commit_then_kill = self.repo / ".git" / "commit-then-kill"
        commit_then_kill.write_text(
            "#!/bin/sh\n"
            "git -C \"$PW_CONTENT_DIR\" -c user.email=t@t.t -c user.name=t "
            "commit -q -m landed\n"
            "kill -TERM \"$PPID\"\n",
            encoding="utf-8",
        )
        commit_then_kill.chmod(0o755)
        head = self.head()

        result = self.run_delete_with_lint(commit_then_kill)

        self.assertEqual(result.returncode, 128 + 15, result.stderr)
        self.assertFalse(any(path.exists() for path in (
            self.asset, self.sidecar, self.manifest, self.html,
        )))
        self.assertNotEqual(self.head(), head)
        self.assertEqual(self.git_status(), "")

    def test_unrelated_head_advance_still_rolls_back_dirty_targets(self):
        other = self.repo / "other.txt"
        other.write_text("before\n", encoding="utf-8")
        self.commit("other")
        commit_other_then_kill = self.repo / ".git" / "commit-other-then-kill"
        commit_other_then_kill.write_text(
            "#!/bin/sh\n"
            "printf 'after\\n' > \"$PW_CONTENT_DIR/other.txt\"\n"
            "git -C \"$PW_CONTENT_DIR\" add other.txt\n"
            "git -C \"$PW_CONTENT_DIR\" -c user.email=t@t.t -c user.name=t "
            "commit -q -m unrelated --only other.txt\n"
            "kill -TERM \"$PPID\"\n",
            encoding="utf-8",
        )
        commit_other_then_kill.chmod(0o755)
        head = self.head()

        result = self.run_delete_with_lint(commit_other_then_kill)

        self.assertEqual(result.returncode, 128 + 15, result.stderr)
        self.assertTrue(all(path.exists() for path in (
            self.asset, self.sidecar, self.manifest, self.html,
        )))
        self.assertNotEqual(self.head(), head)
        self.assertEqual(other.read_text(encoding="utf-8"), "after\n")
        self.assertEqual(self.git_status(), "")

    def head(self):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True,
        ).strip()

    def git_status(self):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), "status", "--porcelain"], text=True,
        ).strip()


if __name__ == "__main__":
    unittest.main()
