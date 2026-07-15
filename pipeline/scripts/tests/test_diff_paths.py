import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("diff_paths", ROOT / "scripts" / "diff-paths.py")
diff_paths = importlib.util.module_from_spec(SPEC)
sys.modules["diff_paths"] = diff_paths
SPEC.loader.exec_module(diff_paths)


class DiffModeTests(unittest.TestCase):
    def test_scope_rejects_symlink_and_non_regular_git_modes(self):
        for mode_line in ("new file mode 120000", "index abc123..def456 160000"):
            with self.subTest(mode_line=mode_line), tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8"
            ) as patch:
                patch.write(
                    "diff --git a/wiki/entities/X.md b/wiki/entities/X.md\n"
                    f"{mode_line}\n"
                    "--- /dev/null\n"
                    "+++ b/wiki/entities/X.md\n"
                    "@@ -0,0 +1 @@\n"
                    "+unsafe\n"
                )
                patch.flush()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = diff_paths.cmd_scope(Path(patch.name))

                self.assertEqual(result, 1)
                self.assertIn("symlink or non-regular Git mode", output.getvalue())


if __name__ == "__main__":
    unittest.main()
