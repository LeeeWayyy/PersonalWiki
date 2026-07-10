from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load_build_blocks(module_name: str, env: dict[str, str] | None = None):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "scripts" / "build-blocks.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    with patch.dict(os.environ, env or {}, clear=False):
        spec.loader.exec_module(module)
    return module


build_blocks = load_build_blocks("build_blocks_contract_test_mod")


class BlockIdContractTests(unittest.TestCase):
    def test_block_ids_match_shared_fixture(self):
        fixture = json.loads((ROOT / "ci-fixtures" / "block-id-contract.json").read_text(encoding="utf-8"))
        for block in fixture["blocks"]:
            with self.subTest(block=block):
                self.assertEqual(build_blocks.block_id(block["type"], block["text"]), block["expected_id"])
                emitted = build_blocks.to_blocks([(block["type"], block["text"], "Fixture")])[0]
                self.assertEqual(emitted["id"], block["expected_id"])

    def test_default_chapter_heading_detection_matches_ingest_contract(self):
        self.assertTrue(build_blocks.is_chapter_heading("\u7b2c1\u7ae0 \u5c0e\u5165"))
        self.assertTrue(build_blocks.is_chapter_heading("Chapter 1: Setup"))
        self.assertTrue(build_blocks.is_chapter_heading("Part II"))
        self.assertFalse(build_blocks.is_chapter_heading("Section 1: Detail"))

    def test_chapter_heading_detection_honors_env_override(self):
        mod = load_build_blocks(
            "build_blocks_contract_env_test_mod",
            {"PW_CHAPTER_HEADING_RX": r"^\s*book\s+\d+\b"},
        )

        self.assertTrue(mod.is_chapter_heading("Book 7"))
        self.assertFalse(mod.is_chapter_heading("Chapter 7"))


if __name__ == "__main__":
    unittest.main()
