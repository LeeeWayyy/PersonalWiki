import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("app_start", ROOT / "scripts" / "app_start.py")
app_start = importlib.util.module_from_spec(SPEC)
sys.modules["app_start"] = app_start
SPEC.loader.exec_module(app_start)


def write_backend(root: Path, env_text: str = "") -> None:
    backend = root / "backend"
    backend.mkdir()
    (backend / ".env.example").write_text("PW_AUTH_TOKEN=\nPW_CONTENT_DIR=content\n", encoding="utf-8")
    if env_text:
        (backend / ".env").write_text(env_text, encoding="utf-8")


def write_content(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "index.md").write_text("# Home\n", encoding="utf-8")


class AppStartTests(unittest.TestCase):
    def test_node_version_check(self):
        self.assertEqual(app_start.parse_node_version("v24.10.0"), (24, 10))
        self.assertTrue(app_start.node_version_ok((22, 12)))
        self.assertTrue(app_start.node_version_ok((23, 0)))
        self.assertFalse(app_start.node_version_ok((22, 11)))

    def test_select_python_skips_incompatible_default(self):
        versions = {"/python3": (3, 9), "/python3.11": (3, 11)}
        with patch.object(app_start.sys, "executable", "/python3"), patch.object(
            app_start.shutil,
            "which",
            side_effect=lambda name: name if name in versions else f"/{name}" if f"/{name}" in versions else None,
        ), patch.object(app_start, "python_version", side_effect=lambda name, _env: versions.get(name)):
            self.assertEqual(app_start.select_python({}), "/python3.11")

    def test_build_config_loads_env_and_resolves_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_backend(
                root,
                "PW_AUTH_TOKEN=backend-token\nPW_CONTENT_DIR=wiki\nPW_PORT=9888\nSITE_PORT=4555\n",
            )
            write_content(root / "wiki")

            config = app_start.build_config([], root, {})

            self.assertEqual(config.mode, "production")
            self.assertEqual(config.backend_port, 9888)
            self.assertEqual(config.site_port, 4555)
            self.assertEqual(config.env["PW_AUTH_TOKEN"], "backend-token")
            self.assertEqual(config.env["PW_CONTENT_DIR"], str((root / "wiki").resolve()))
            self.assertFalse(config.kill_ports)

    def test_build_config_preserves_explicit_env_and_dev_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_backend(root, "PW_AUTH_TOKEN=backend-token\nPW_CONTENT_DIR=backend-content\n")
            explicit = root / "explicit"
            write_content(explicit)

            config = app_start.build_config(
                ["--dev", "--open", "--kill-ports"],
                root,
                {
                    "PW_AUTH_TOKEN": "explicit-token",
                    "PW_CONTENT_DIR": str(explicit),
                    "PW_PORT": "9999",
                    "SITE_PORT": "4444",
                },
            )

            self.assertEqual(config.mode, "dev")
            self.assertTrue(config.open_ui)
            self.assertTrue(config.kill_ports)
            self.assertEqual(config.env["PW_AUTH_TOKEN"], "explicit-token")
            self.assertEqual(config.content_dir, explicit.resolve())

    def test_build_config_creates_default_content_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_backend(root)

            config = app_start.build_config([], root, {})

            self.assertEqual(config.content_dir, (root / "content").resolve())
            self.assertTrue((root / "content" / ".git").is_dir())
            self.assertTrue(any("created an empty wiki vault" in msg for msg in config.messages))

    def test_sigterm_handler_raises_keyboard_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            app_start._terminate_on_sigterm()

    def test_stop_processes_terminates_live_child(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            app_start.stop_processes([proc], log=lambda _msg: None)
            self.assertIsNotNone(proc.poll(), "child should be dead after stop_processes")
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_build_config_rejects_missing_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_backend(root, "PW_AUTH_TOKEN=token\nPW_CONTENT_DIR=missing\n")

            with self.assertRaisesRegex(app_start.AppStartError, "wiki folder not found"):
                app_start.build_config([], root, {})


if __name__ == "__main__":
    unittest.main()
