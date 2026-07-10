import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("app_config", ROOT / "scripts" / "app_config.py")
app_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app_config)


class AppConfigTests(unittest.TestCase):
    def test_parse_env_value_preserves_literal_hash_and_strips_comments(self):
        self.assertEqual(app_config.parse_env_value("abc#literal"), "abc#literal")
        self.assertEqual(app_config.parse_env_value("abc # comment"), "abc")
        self.assertEqual(app_config.parse_env_value('"quoted # literal" # comment'), "quoted # literal")

    def test_local_env_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend").mkdir()
            (root / ".env").write_text(
                "PW_CONTENT_DIR=root-content\nPW_AUTH_TOKEN=root-token\nROOT_ONLY=1\n",
                encoding="utf-8",
            )
            (root / "backend" / ".env").write_text(
                "PW_CONTENT_DIR=backend-content\nPW_AUTH_TOKEN=backend-token\n",
                encoding="utf-8",
            )

            updates = app_config.local_env_updates(root, {"PW_AUTH_TOKEN": "explicit-token"})

            self.assertEqual(updates["PW_CONTENT_DIR"], "backend-content")
            self.assertEqual(updates["ROOT_ONLY"], "1")
            self.assertNotIn("PW_AUTH_TOKEN", updates)

    def test_ensure_backend_env_generates_stable_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend").mkdir()
            (root / "backend" / ".env.example").write_text("PW_AUTH_TOKEN=\n", encoding="utf-8")

            app_config.ensure_backend_env(root, {})
            first = app_config.backend_env_token(root / "backend" / ".env")
            app_config.ensure_backend_env(root, {})
            second = app_config.backend_env_token(root / "backend" / ".env")

            self.assertTrue(first)
            self.assertEqual(first, second)

    def test_content_dir_resolves_relative_to_root(self):
        root = Path("/tmp/personal-wiki-root")
        self.assertEqual(
            app_config.content_dir(root, {"PW_CONTENT_DIR": "wiki"}),
            root / "wiki",
        )

    def test_parse_port_validates_range(self):
        self.assertEqual(app_config.parse_port("PW_PORT", "8787"), 8787)
        with self.assertRaisesRegex(app_config.AppConfigError, "must be a number"):
            app_config.parse_port("PW_PORT", "nope")
        with self.assertRaisesRegex(app_config.AppConfigError, "between 1 and 65535"):
            app_config.parse_port("PW_PORT", "70000")

    def test_validate_content_dir_rejects_empty_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(app_config.AppConfigError, "wiki folder is empty"):
                app_config.validate_content_dir(Path(tmp))

    def test_bootstrap_local_env_skips_file_bootstrap_when_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "wiki"
            content.mkdir()
            (content / "index.md").write_text("# Home\n", encoding="utf-8")
            env = {
                app_config.BOOTSTRAPPED_ENV_KEY: "1",
                "PW_CONTENT_DIR": str(content),
                "PW_AUTH_TOKEN": "token",
            }

            resolved, messages = app_config.bootstrap_local_env(root, env)

            self.assertEqual(resolved, content.resolve())
            self.assertEqual(messages, [])
            self.assertFalse((root / "backend" / ".env").exists())

    def test_bootstrap_local_env_creates_default_content_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend").mkdir()
            (root / "backend" / ".env.example").write_text("PW_AUTH_TOKEN=\n", encoding="utf-8")
            env: dict[str, str] = {}

            resolved, messages = app_config.bootstrap_local_env(root, env)

            self.assertEqual(resolved, (root / "content").resolve())
            self.assertEqual(env["PW_CONTENT_DIR"], str((root / "content").resolve()))
            self.assertTrue((root / "content" / ".git").is_dir())
            self.assertTrue(any("created an empty wiki vault" in message for message in messages))
            status = subprocess.run(
                ["git", "-C", str(root / "content"), "status", "--porcelain"],
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(status.strip(), "")


if __name__ == "__main__":
    unittest.main()
