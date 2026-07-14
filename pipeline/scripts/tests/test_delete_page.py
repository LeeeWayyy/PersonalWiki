from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


SCRIPTS = Path(__file__).resolve().parents[1]
DELETE = SCRIPTS / "delete-page.py"
MOCS = SCRIPTS / "generate-mocs.py"


class DeletePageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.entities = self.root / "wiki" / "entities"
        self.entities.mkdir(parents=True)
        (self.root / "wiki" / "_maps").mkdir()
        (self.root / "wiki" / "_taxonomy.md").write_text(
            "# Taxonomy\n\n## Domain\n- `test/domain`\n\n"
            "## Form\n- `concept`\n\n## Reserved\n- `taxonomy-gap`\n",
            encoding="utf-8",
        )
        self.env = {**os.environ, "VAULT_CONTENT_DIR": str(self.root)}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def page(
        self,
        stem: str,
        page_id: str,
        aliases: list[str],
        sources: list[str] | None = None,
        body: str = "",
    ) -> Path:
        path = self.entities / f"{stem}.md"
        path.write_text(
            "---\n"
            "type: Entity\n"
            f"page_id: {page_id}\n"
            f"aliases: {json.dumps(aliases, ensure_ascii=False)}\n"
            "tags: [test/domain, concept]\n"
            f"sources: {json.dumps(sources or [])}\n"
            "---\n"
            f"# {stem}\n\n{body}\n",
            encoding="utf-8",
        )
        return path

    def run_delete(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(DELETE), *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=check,
        )

    def build_mocs(self) -> None:
        subprocess.run([str(MOCS)], env=self.env, check=True, capture_output=True)

    def test_true_delete_unlinks_every_alias_and_generated_page(self) -> None:
        doomed = self.page("X", "X" * 26, ["Old X"])
        keeper = self.page(
            "Keep",
            "K" * 26,
            ["Keep"],
            body="[[X]] [[X#part|shown]] [[Old X]] ![[X]] [[Keep]]",
        )
        map_path = self.root / "wiki" / "_maps" / "source.md"
        map_path.write_text("[[X|map label]]\n", encoding="utf-8")
        self.build_mocs()

        self.run_delete("--no-git", str(doomed))

        self.assertFalse(doomed.exists())
        self.assertIn("X shown Old X X [[Keep]]", keeper.read_text(encoding="utf-8"))
        self.assertEqual("map label\n", map_path.read_text(encoding="utf-8"))
        self.assertNotIn("[[X|", (self.root / "wiki" / "_index" / "concept.md").read_text())
        index = json.loads((self.root / "wiki" / ".alias-index.json").read_text())
        self.assertNotIn("X" * 26, index["pages"])
        self.assertNotIn("old x", index["aliases"])

    def test_merge_redirects_links_and_transfers_aliases(self) -> None:
        doomed = self.page("X", "X" * 26, ["Old X", "Legacy"], ["S1"])
        target = self.page("Y", "Y" * 26, ["Y"], ["S1"])
        keeper = self.page(
            "Keep",
            "K" * 26,
            ["Keep"],
            body="[[X]] [[X#part|shown]] [[Legacy]]",
        )
        map_path = self.root / "wiki" / "_maps" / "source.md"
        map_path.write_text("[[X]]\n", encoding="utf-8")
        self.build_mocs()

        self.run_delete("--no-git", "--merge-into", str(target), str(doomed))

        self.assertFalse(doomed.exists())
        self.assertIn("[[Y]] [[Y#part|shown]] [[Legacy]]", keeper.read_text())
        self.assertEqual("[[Y]]\n", map_path.read_text())
        target_text = target.read_text(encoding="utf-8")
        target_fm = yaml.safe_load(target_text.split("---\n", 2)[1])
        self.assertEqual(["Y", "X", "Old X", "Legacy"], target_fm["aliases"])
        index = json.loads((self.root / "wiki" / ".alias-index.json").read_text())
        self.assertEqual(["Y" * 26], index["aliases"]["legacy"])
        self.assertNotIn("[[X|", (self.root / "wiki" / "_index" / "concept.md").read_text())

    def test_merge_refuses_to_drop_source_metadata(self) -> None:
        doomed = self.page("X", "X" * 26, ["X"], ["S2"])
        target = self.page("Y", "Y" * 26, ["Y"], ["S1"])

        result = self.run_delete(
            "--no-git", "--merge-into", str(target), str(doomed), check=False
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("missing source(s): S2", result.stderr)
        self.assertTrue(doomed.exists())


if __name__ == "__main__":
    unittest.main()
