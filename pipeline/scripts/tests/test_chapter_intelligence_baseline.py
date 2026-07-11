import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURE = (
    SCRIPTS
    / "tests"
    / "fixtures"
    / "chapter-intelligence"
    / "energy-sex-suicide.first-3.baseline.json"
)
SPEC = importlib.util.spec_from_file_location(
    "verify_chapter_intelligence_baseline",
    SCRIPTS / "verify-chapter-intelligence-baseline.py",
)
verifier = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verifier
SPEC.loader.exec_module(verifier)


def _baseline() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _matching_artifacts(baseline: dict) -> list[dict]:
    artifacts = []
    for chapter in baseline["chapters"]:
        claims = [
            {"id": f"c{index}", "text": "；".join(terms)}
            for index, terms in enumerate(chapter["required_claim_term_groups"], 1)
        ]
        required_entities = [
            {"name": group[0], "aliases": group[1:]}
            for group in chapter["required_entity_alias_groups"]
        ]
        entities = list(required_entities)
        entities.extend(
            {"name": concept, "aliases": []}
            for concept in chapter["analysis_only_concepts"]
        )
        topics = [{"name": group[0]} for group in chapter["required_topic_alias_groups"]]
        candidates = [
            {"page_type": "entity", "name": entity["name"]}
            for entity in required_entities
        ] + [{"page_type": "topic", "name": topic["name"]} for topic in topics]
        relations = [
            {"from": "c1", "to": "c2", "rel": relation}
            for relation in chapter["required_relation_kinds"]
        ]
        artifacts.append(
            {
                "section_label": chapter["label"],
                "claims": claims,
                "entities": entities,
                "topics": topics,
                "relations": relations,
                "page_candidates": candidates,
            }
        )
    return artifacts


class BaselineVerifierTests(unittest.TestCase):
    def setUp(self):
        self.baseline = _baseline()
        self.artifacts = _matching_artifacts(self.baseline)

    def test_real_book_fixture_is_active_and_portable(self):
        verifier.validate_baseline(self.baseline)
        serialized = FIXTURE.read_text(encoding="utf-8")
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("DailyNotes", serialized)
        verifier.verify_artifacts(self.baseline, self.artifacts)

    def test_missing_required_entity_alias_group_fails(self):
        artifacts = copy.deepcopy(self.artifacts)
        missing = self.baseline["chapters"][0]["required_entity_alias_groups"][0][0]
        artifacts[0]["page_candidates"] = [
            item for item in artifacts[0]["page_candidates"] if item["name"] != missing
        ]
        with self.assertRaisesRegex(verifier.BaselineVerificationError, "missing required entity"):
            verifier.verify_artifacts(self.baseline, artifacts)

    def test_missing_required_topic_alias_group_fails(self):
        artifacts = copy.deepcopy(self.artifacts)
        missing = self.baseline["chapters"][1]["required_topic_alias_groups"][0][0]
        artifacts[1]["page_candidates"] = [
            item for item in artifacts[1]["page_candidates"] if item["name"] != missing
        ]
        with self.assertRaisesRegex(verifier.BaselineVerificationError, "missing required topic"):
            verifier.verify_artifacts(self.baseline, artifacts)

    def test_forbidden_page_candidate_fails(self):
        artifacts = copy.deepcopy(self.artifacts)
        artifacts[0]["page_candidates"].append(
            {"page_type": "topic", "name": self.baseline["forbidden_page_candidates"][0]}
        )
        with self.assertRaisesRegex(verifier.BaselineVerificationError, "forbidden page candidate"):
            verifier.verify_artifacts(self.baseline, artifacts)

    def test_missing_claim_term_group_fails(self):
        artifacts = copy.deepcopy(self.artifacts)
        artifacts[2]["claims"][0]["text"] = "unrelated claim"
        with self.assertRaisesRegex(verifier.BaselineVerificationError, "missing claim term group"):
            verifier.verify_artifacts(self.baseline, artifacts)

    def test_missing_relation_kind_fails(self):
        artifacts = copy.deepcopy(self.artifacts)
        missing = self.baseline["chapters"][0]["required_relation_kinds"][0]
        artifacts[0]["relations"] = [
            relation for relation in artifacts[0]["relations"] if relation["rel"] != missing
        ]
        with self.assertRaisesRegex(verifier.BaselineVerificationError, "missing relation kinds"):
            verifier.verify_artifacts(self.baseline, artifacts)

    def test_analysis_only_concept_cannot_become_page_candidate(self):
        artifacts = copy.deepcopy(self.artifacts)
        concept = self.baseline["chapters"][1]["analysis_only_concepts"][0]
        artifacts[1]["page_candidates"].append({"page_type": "entity", "name": concept})
        with self.assertRaisesRegex(verifier.BaselineVerificationError, "analysis-only concept"):
            verifier.verify_artifacts(self.baseline, artifacts)


if __name__ == "__main__":
    unittest.main()
