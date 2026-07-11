import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "pipeline" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ingest_quality as quality  # noqa: E402
import source_citations as citations  # noqa: E402

LINT_SPEC = importlib.util.spec_from_file_location("citation_lint", SCRIPTS / "lint.py")
lint = importlib.util.module_from_spec(LINT_SPEC)
LINT_SPEC.loader.exec_module(lint)
SYNC_SPEC = importlib.util.spec_from_file_location(
    "citation_sync_frontmatter", SCRIPTS / "sync-frontmatter.py"
)
sync_frontmatter = importlib.util.module_from_spec(SYNC_SPEC)
SYNC_SPEC.loader.exec_module(sync_frontmatter)

SOURCE_ID = "01KX582AX79FD9BQG2VNMG41NY"
OTHER_ID = "01K00000000000000000000000"


class SourceCitationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads(
            (ROOT / "ci-fixtures" / "source-citation-contract.json").read_text(
                encoding="utf-8"
            )
        )

    def test_shared_fixture_round_trips_with_canonical_encoding(self):
        for case in self.cases:
            with self.subTest(label=case["label"]):
                self.assertEqual(
                    citations.encode_section_anchor(case["label"]), case["anchor"]
                )
                self.assertEqual(
                    citations.decode_source_anchor(case["anchor"]), case["label"]
                )

    def test_reserved_label_remains_one_part_in_multi_source_group(self):
        label = self.cases[1]["label"]
        encoded = citations.source_citation_ref(SOURCE_ID, label)
        text = f"Claim [{encoded}, src:{OTHER_ID}#legacy section]."

        parsed = citations.iter_source_citations(text)
        self.assertEqual(
            [(item.source_id, item.anchor) for item in parsed],
            [(SOURCE_ID, label), (OTHER_ID, "legacy section")],
        )

    def test_parser_accepts_legacy_but_current_quality_requires_canonical(self):
        label = self.cases[1]["label"]
        encoded = citations.source_citation(SOURCE_ID, label)
        self.assertTrue(quality.has_exact_citation(encoded, SOURCE_ID, label))
        legacy = f"[src:{SOURCE_ID}#Legacy chapter]"
        parsed = citations.iter_source_citations(legacy)
        self.assertEqual(
            [(item.source_id, item.anchor) for item in parsed],
            [(SOURCE_ID, "Legacy chapter")],
        )
        self.assertFalse(
            quality.has_exact_citation(legacy, SOURCE_ID, "Legacy chapter")
        )
        noncanonical = encoded.replace("%2C", "%2c", 1)
        self.assertNotEqual(noncanonical, encoded)
        self.assertEqual(
            citations.iter_source_citations(noncanonical)[0].anchor,
            label,
        )
        self.assertFalse(
            quality.has_exact_citation(noncanonical, SOURCE_ID, label)
        )
        self.assertFalse(quality.has_exact_citation(encoded, SOURCE_ID, "other"))

    def test_lint_parser_keeps_encoded_anchor_and_finds_both_ids(self):
        anchor = self.cases[1]["anchor"]
        text = f"[{citations.source_citation_ref(SOURCE_ID, self.cases[1]['label'])},src:{OTHER_ID}]"
        self.assertEqual(lint.extract_citations(text), [SOURCE_ID, OTHER_ID])
        self.assertEqual(
            lint.extract_citation_keys(text), [f"{SOURCE_ID}#{anchor}", OTHER_ID]
        )

    def test_lint_retains_malformed_id_for_orphan_validation(self):
        malformed = "01ZZZZZZZZZZZZZZZZZZZZZZZZZ"
        self.assertEqual(lint.extract_citations(f"[src:{malformed}]"), [malformed])

    def test_frontmatter_sync_reads_encoded_group_without_phantom_ids(self):
        text = (
            f"[{citations.source_citation_ref(SOURCE_ID, self.cases[1]['label'])},"
            f"src:{OTHER_ID}#legacy]"
        )
        self.assertEqual(sync_frontmatter.citations_in(text), [SOURCE_ID, OTHER_ID])

    def test_parser_rejects_noncanonical_space_after_opening_bracket(self):
        self.assertEqual(
            citations.iter_source_citations(f"[ src:{SOURCE_ID}]"), []
        )

    def test_malformed_utf8_anchor_matches_javascript_fallback(self):
        self.assertEqual(citations.decode_source_anchor("sec=%FF"), "%FF")
        self.assertEqual(citations.decode_source_anchor("sec=%C3%28"), "%C3%28")


if __name__ == "__main__":
    unittest.main()
