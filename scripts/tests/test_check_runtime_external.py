import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "check_runtime_external",
    ROOT / "scripts" / "check_runtime_external.py",
)
check_runtime_external = importlib.util.module_from_spec(SPEC)
sys.modules["check_runtime_external"] = check_runtime_external
SPEC.loader.exec_module(check_runtime_external)


class CheckRuntimeExternalTests(unittest.TestCase):
    def test_scanner_allows_localhost_and_youtube_placeholders_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "public" / "vault-assets").mkdir(parents=True)
            (root / "src" / "page.ts").write_text(
                "\n".join(
                    [
                        'const api = "http://localhost:8787/health";',
                        'const placeholder = "https://youtube.com/\u2026";',
                        'const external = "https://cdn.example.com/app.js";',
                    ]
                ),
                encoding="utf-8",
            )
            (root / "public" / "vault-assets" / "remote.md").write_text(
                "https://cdn.example.com/private-asset\n",
                encoding="utf-8",
            )

            unexpected = check_runtime_external.scan_runtime_external(root)

            self.assertEqual(len(unexpected), 1)
            self.assertIn("https://cdn.example.com/app.js", unexpected[0])


if __name__ == "__main__":
    unittest.main()
