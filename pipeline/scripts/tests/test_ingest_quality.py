import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ingest_quality as quality  # noqa: E402


SOURCE_ID = "01KX582AX79FD9BQG2VNMG41NY"
SECTION = "Chapter 2"


def current_ref(source_id=SOURCE_ID, section_label=SECTION):
    return quality.expected_citation(source_id, section_label)


def current_citation(source_id=SOURCE_ID, section_label=SECTION):
    return f"[{current_ref(source_id, section_label)}]"


def intelligence(*candidates, entities=None, topics=None):
    return {
        "schema": "chapter-intelligence/1",
        "source_id": SOURCE_ID,
        "section_label": SECTION,
        "entities": entities or [],
        "topics": topics or [],
        "page_candidates": list(candidates),
    }


def production_intelligence(candidate_required=True):
    return {
        "schema": "chapter-intelligence/1",
        "source_id": SOURCE_ID,
        "source_sha256": "a" * 64,
        "text_sha256": "b" * 64,
        "section_label": SECTION,
        "prompt_version": "v1",
        "language": "en",
        "summary": "ATP is central to cellular energy coupling.",
        "central_question": "How is cellular work coupled to energy release?",
        "chapter_claim": "ATP couples energy-releasing reactions to work.",
        "builds_on": None,
        "claims": [
            {
                "id": "c1",
                "kind": "claim",
                "text": "ATP couples energy-releasing reactions to work.",
                "importance": 5,
                "source_spans": [{"start": 0, "end": 3, "quote": "ATP"}],
                "entities": ["ATP"],
            }
        ],
        "entities": [
            {
                "name": "ATP",
                "type": "molecule",
                "aliases": ["Adenosine triphosphate"],
                "importance": 5,
                "role": "Couples reactions to work.",
                "page_hint": "entity",
                "claim_ids": ["c1"],
            }
        ],
        "topics": [],
        "relations": [],
        "page_candidates": [
            {
                "page_type": "entity",
                "name": "ATP",
                "importance": 5,
                "required": candidate_required,
                "claim_ids": ["c1"],
                "reason": "A reusable central molecule.",
            }
        ],
        "claim_coverage": [
            {
                "claim_id": "c1",
                "page_candidates": [{"page_type": "entity", "name": "ATP"}],
                "skip_reason": None,
            }
        ],
        "open_questions": [],
    }


def page_text(
    title,
    *paragraphs,
    page_type="Entity",
    aliases=(),
    close_zone=True,
):
    quoted = "\n>\n".join(f"> {paragraph}" for paragraph in paragraphs)
    close = "\n<!-- /llm-zone -->" if close_zone else ""
    return f"""---
type: {page_type}
aliases: {json.dumps(list(aliases), ensure_ascii=False)}
tags: [concept, science]
---

# {title}

<!-- llm-zone -->
> [!AI] LLM Synthesis
>
{quoted}{close}
"""


def codes(receipt, field="errors"):
    return {issue["code"] for issue in receipt[field]}


class IntelligenceValidationTests(unittest.TestCase):
    def test_accepts_current_exact_analyzer_candidate_shape(self):
        candidates = quality.validate_intelligence(
            production_intelligence(), source_id=SOURCE_ID, section_label=SECTION
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].accepted_names, ("ATP", "Adenosine triphosphate"))

    def test_full_analyzer_artifact_fails_closed_on_invalid_required_flag(self):
        with self.assertRaisesRegex(
            quality.IntelligenceValidationError, "must be true for importance 4-5"
        ):
            quality.validate_intelligence(
                production_intelligence(candidate_required=False),
                source_id=SOURCE_ID,
                section_label=SECTION,
            )

    def test_required_flag_selects_and_marks_central(self):
        artifact = intelligence(
            {
                "name": "Cellular respiration",
                "page_type": "topic",
                "required": True,
            },
            {
                "name": "Incidental example",
                "page_type": "entity",
                "importance": 1,
            },
        )
        candidates = quality.validate_intelligence(
            artifact, source_id=SOURCE_ID, section_label=SECTION
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].page_type, "topic")
        self.assertTrue(candidates[0].central)

    def test_rejects_candidate_without_selection_metadata(self):
        artifact = intelligence({"name": "ATP", "page_type": "entity"})
        with self.assertRaisesRegex(
            quality.IntelligenceValidationError, "needs importance"
        ):
            quality.validate_intelligence(
                artifact, source_id=SOURCE_ID, section_label=SECTION
            )

    def test_rejects_source_or_section_mismatch(self):
        artifact = intelligence()
        with self.assertRaisesRegex(quality.IntelligenceValidationError, "source_id"):
            quality.validate_intelligence(
                artifact, source_id="different", section_label=SECTION
            )
        with self.assertRaisesRegex(quality.IntelligenceValidationError, "section_label"):
            quality.validate_intelligence(
                artifact, source_id=SOURCE_ID, section_label="Chapter 3"
            )


class PageQualityTests(unittest.TestCase):
    def test_whole_source_accepts_media_anchor_on_current_source(self):
        text = f"Evidence appears on a card [src:{SOURCE_ID}#card-2]."
        self.assertTrue(quality.has_exact_citation(text, SOURCE_ID, ""))
        self.assertFalse(quality.has_exact_citation(text, "01K00000000000000000000000", ""))

    def test_chaptered_source_requires_exact_section_anchor(self):
        text = f"Evidence belongs elsewhere [src:{SOURCE_ID}#Chapter 20]."
        self.assertFalse(quality.has_exact_citation(text, SOURCE_ID, SECTION))

    def test_unchanged_page_can_be_explicitly_already_covered(self):
        artifact = production_intelligence()
        text = page_text(
            "ATP",
            "ATP couples energy-releasing reactions to work.",
        )
        receipt = quality.evaluate_quality(
            artifact,
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput(
                "wiki/entities/ATP.md", text, text, disposition="existing"
            )],
        )
        self.assertTrue(receipt["ok"], receipt["errors"])
        self.assertEqual(receipt["summary"]["already_covered_candidates"], 1)
        self.assertEqual(receipt["candidates"][0]["disposition"], "already-covered")
        self.assertIn("coverage.already_covered", codes(receipt, "warnings"))

    def test_ambiguous_existing_alias_ownership_fails_closed(self):
        artifact = production_intelligence()
        text_a = page_text(
            "ATP carrier",
            "ATP couples energy-releasing reactions to work.",
            aliases=("ATP",),
        )
        text_b = page_text(
            "ATP molecule",
            "ATP couples energy-releasing reactions to work.",
            aliases=("ATP",),
        )
        receipt = quality.evaluate_quality(
            artifact,
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[
                quality.PageInput(
                    "wiki/entities/ATP-carrier.md", text_a, text_a,
                    disposition="existing",
                ),
                quality.PageInput(
                    "wiki/entities/ATP-molecule.md", text_b, text_b,
                    disposition="existing",
                ),
            ],
        )
        self.assertFalse(receipt["ok"])
        self.assertIn("coverage.candidate_ambiguous", codes(receipt))

    def test_existing_identity_without_claim_text_does_not_waive_required_coverage(self):
        artifact = production_intelligence()
        text = page_text("ATP", "ATP is a molecule with several cellular roles.")
        receipt = quality.evaluate_quality(
            artifact,
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput(
                "wiki/entities/ATP.md", text, text, disposition="existing"
            )],
        )
        self.assertIn("coverage.required_candidate_missing", codes(receipt))
        self.assertEqual(receipt["candidates"][0]["disposition"], "missing")

    def test_frontmatter_only_edit_does_not_count_as_substantive_coverage(self):
        artifact = production_intelligence()
        old = page_text("ATP", "ATP is a molecule with several cellular roles.")
        current = page_text(
            "ATP",
            "ATP is a molecule with several cellular roles.",
            aliases=("Adenosine triphosphate",),
        )
        receipt = quality.evaluate_quality(
            artifact,
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput(
                "wiki/entities/ATP.md", current, old, disposition="modified"
            )],
        )
        self.assertIn("coverage.required_candidate_missing", codes(receipt))
        self.assertEqual(receipt["candidates"][0]["disposition"], "missing")

    def test_frontmatter_only_edit_can_reuse_proven_historical_coverage(self):
        artifact = production_intelligence()
        prose = "ATP couples energy-releasing reactions to work."
        old = page_text("ATP", prose)
        current = page_text("ATP", prose, aliases=("Adenosine triphosphate",))
        receipt = quality.evaluate_quality(
            artifact,
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput(
                "wiki/entities/ATP.md", current, old, disposition="modified"
            )],
        )
        self.assertTrue(receipt["ok"], receipt["errors"])
        self.assertEqual(receipt["candidates"][0]["disposition"], "already-covered")

    def test_existing_global_identity_reconciles_historical_page_type(self):
        artifact = production_intelligence()
        text = page_text(
            "ATP",
            "ATP couples energy-releasing reactions to work.",
            page_type="Topic",
        )
        receipt = quality.evaluate_quality(
            artifact,
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput(
                "wiki/topics/ATP.md", text, text, disposition="existing"
            )],
        )
        self.assertTrue(receipt["ok"], receipt["errors"])
        row = receipt["candidates"][0]
        self.assertEqual(row["page_type"], "entity")
        self.assertEqual(row["resolved_page_type"], "topic")
        self.assertTrue(row["type_reconciled"])

    def test_passes_alias_coverage_and_developed_central_page(self):
        artifact = intelligence(
            {
                "name": "Adenosine triphosphate",
                "page_type": "entity",
                "importance": 5,
            },
            entities=[
                {
                    "name": "Adenosine triphosphate",
                    "aliases": ["ATP"],
                }
            ],
        )
        text = page_text(
            "ATP",
            "ATP couples energy-releasing reactions to cellular work. "
            f"Hydrolysis changes the free-energy balance {current_citation()}.",
            "Its regeneration links catabolism to biosynthesis, transport, and movement. "
            f"That coupling makes it a reusable metabolic node {current_citation()}.",
            aliases=("Adenosine triphosphate",),
        )
        receipt = quality.evaluate_quality(
            artifact,
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput("wiki/entities/ATP.md", text)],
        )
        self.assertTrue(receipt["ok"], receipt["errors"])
        self.assertEqual(receipt["summary"]["represented_candidates"], 1)

    def test_missing_or_wrong_type_page_does_not_cover_candidate(self):
        artifact = intelligence(
            {"name": "Energy metabolism", "page_type": "topic", "importance": 5}
        )
        text = page_text(
            "Energy metabolism",
            f"Energy metabolism couples pathways {current_citation()}.",
            page_type="Entity",
        )
        receipt = quality.evaluate_quality(
            artifact,
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput("wiki/entities/Energy metabolism.md", text)],
        )
        self.assertIn("coverage.required_candidate_missing", codes(receipt))

    def test_omitted_candidate_may_consolidate_when_all_claims_are_covered(self):
        artifact = intelligence(
            {
                "name": "ATP",
                "page_type": "entity",
                "importance": 5,
                "claim_ids": ["c1"],
            },
            {
                "name": "Mitochondria",
                "page_type": "entity",
                "importance": 4,
                "claim_ids": ["c1"],
            },
        )
        text = page_text(
            "ATP",
            f"ATP couples energy conversion to work {current_citation()}.",
            f"Its regeneration links several pathways {current_citation()}.",
        )
        receipt = quality.evaluate_quality(
            artifact,
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput("wiki/entities/ATP.md", text)],
        )
        self.assertTrue(receipt["ok"], receipt["errors"])
        self.assertIn("coverage.candidate_consolidated", codes(receipt, "warnings"))
        self.assertEqual(receipt["summary"]["represented_candidates"], 1)

    def test_omitted_candidate_with_unique_claim_still_fails(self):
        artifact = intelligence(
            {
                "name": "ATP",
                "page_type": "entity",
                "importance": 5,
                "claim_ids": ["c1"],
            },
            {
                "name": "Mitochondria",
                "page_type": "entity",
                "importance": 5,
                "claim_ids": ["c2"],
            },
        )
        text = page_text(
            "ATP",
            f"ATP couples energy conversion to work {current_citation()}.",
            f"Its regeneration links several pathways {current_citation()}.",
        )
        receipt = quality.evaluate_quality(
            artifact,
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput("wiki/entities/ATP.md", text)],
        )
        self.assertIn("coverage.required_candidate_missing", codes(receipt))

    def test_omitted_importance_four_unique_claim_is_visible_warning(self):
        artifact = intelligence(
            {
                "name": "ATP",
                "page_type": "entity",
                "importance": 5,
                "claim_ids": ["c1"],
            },
            {
                "name": "Natural selection",
                "page_type": "entity",
                "importance": 4,
                "claim_ids": ["c2"],
            },
        )
        text = page_text(
            "ATP",
            f"ATP couples energy conversion to work {current_citation()}.",
            f"Its regeneration links several pathways {current_citation()}.",
        )
        receipt = quality.evaluate_quality(
            artifact,
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput("wiki/entities/ATP.md", text)],
        )
        self.assertTrue(receipt["ok"], receipt["errors"])
        self.assertIn(
            "coverage.recommended_candidate_missing", codes(receipt, "warnings")
        )

    def test_only_semantically_modified_paragraphs_need_current_citation(self):
        old = page_text(
            "ATP",
            "Older evidence remains useful [src:OLD00000000000000000000000#Chapter 1].",
        )
        current = old.replace(
            "<!-- /llm-zone -->",
            f">\n> New mechanism is developed here {current_citation()}.\n"
            "<!-- /llm-zone -->",
        )
        receipt = quality.evaluate_quality(
            intelligence(),
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput("wiki/entities/ATP.md", current, old)],
        )
        self.assertTrue(receipt["ok"], receipt["errors"])
        self.assertEqual(receipt["summary"]["modified_substantive_paragraphs"], 1)

    def test_unchanged_legacy_section_citation_remains_compatible(self):
        historical = page_text(
            "ATP",
            f"Historical evidence remains readable [src:{SOURCE_ID}#{SECTION}].",
        )
        receipt = quality.evaluate_quality(
            intelligence(),
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput("wiki/entities/ATP.md", historical, historical)],
        )
        self.assertTrue(receipt["ok"], receipt["errors"])
        self.assertEqual(receipt["summary"]["modified_substantive_paragraphs"], 0)

    def test_wrong_or_bare_anchor_is_not_exact(self):
        for citation in (
            f"[src:{SOURCE_ID}]",
            f"[src:{SOURCE_ID}#Chapter 20]",
            f"[src:{SOURCE_ID}#{SECTION}]",
            f"[src:OTHER000000000000000000000#{SECTION}]",
        ):
            with self.subTest(citation=citation):
                text = page_text("ATP", f"A substantive claim {citation}.")
                receipt = quality.evaluate_quality(
                    intelligence(),
                    source_id=SOURCE_ID,
                    section_label=SECTION,
                    pages=[quality.PageInput("wiki/entities/ATP.md", text)],
                )
                self.assertIn("citation.current_missing", codes(receipt))

    def test_exact_citation_can_appear_in_multi_source_list(self):
        paragraph = (
            "A substantive comparison "
            f"[src:OLD00000000000000000000000#Chapter 1, {current_ref()}]."
        )
        receipt = quality.evaluate_quality(
            intelligence(),
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[
                quality.PageInput(
                    "wiki/topics/Comparison.md",
                    page_text("Comparison", paragraph, page_type="Topic"),
                )
            ],
        )
        self.assertTrue(receipt["ok"], receipt["errors"])

    def test_entity_forbidden_phrases_cover_english_and_chinese(self):
        for phrase in ("According to the source", "作者指出", "第十二章"):
            with self.subTest(phrase=phrase):
                text = page_text(
                    "ATP",
                    f"{phrase} ATP stores energy {current_citation()}.",
                )
                receipt = quality.evaluate_quality(
                    intelligence(),
                    source_id=SOURCE_ID,
                    section_label=SECTION,
                    pages=[quality.PageInput("wiki/entities/ATP.md", text)],
                )
                self.assertIn("entity.forbidden_attribution", codes(receipt))

    def test_chapter_anchor_in_citation_is_not_forbidden_entity_voice(self):
        text = page_text(
            "ATP",
            f"ATP stores energy {current_citation(section_label='第一章')}.",
        )
        artifact = intelligence()
        artifact["section_label"] = "第一章"
        receipt = quality.evaluate_quality(
            artifact,
            source_id=SOURCE_ID,
            section_label="第一章",
            pages=[quality.PageInput("wiki/entities/ATP.md", text)],
        )
        self.assertTrue(receipt["ok"], receipt["errors"])

    def test_unchanged_forbidden_entity_voice_does_not_fail_future_ingest(self):
        old = page_text(
            "ATP",
            "According to the source, ATP stores energy "
            "[src:OLD00000000000000000000000#Chapter 1].",
        )
        current = old.replace(
            "<!-- /llm-zone -->",
            f">\n> ATP also couples transport to metabolism {current_citation()}.\n"
            "<!-- /llm-zone -->",
        )
        receipt = quality.evaluate_quality(
            intelligence(),
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput("wiki/entities/ATP.md", current, old)],
        )
        self.assertTrue(receipt["ok"], receipt["errors"])

    def test_forbidden_entity_voice_does_not_apply_to_topic_pages(self):
        text = page_text(
            "Interpretations",
            f"According to Lane, the hypothesis is contested {current_citation()}.",
            page_type="Topic",
        )
        receipt = quality.evaluate_quality(
            intelligence(),
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput("wiki/topics/Interpretations.md", text)],
        )
        self.assertTrue(receipt["ok"], receipt["errors"])

    def test_unbalanced_zone_fails(self):
        text = page_text(
            "ATP",
            f"ATP stores energy {current_citation()}.",
            close_zone=False,
        )
        receipt = quality.evaluate_quality(
            intelligence(),
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput("wiki/entities/ATP.md", text)],
        )
        self.assertIn("zones.unclosed", codes(receipt))
        self.assertIn("zones.llm_missing", codes(receipt))

    def test_obvious_central_fact_card_fails_but_importance_four_does_not(self):
        text = page_text(
            "ATP",
            f"ATP is an energy carrier {current_citation()}.",
        )
        central = quality.evaluate_quality(
            intelligence({"name": "ATP", "page_type": "entity", "importance": 5}),
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput("wiki/entities/ATP.md", text)],
        )
        self.assertIn("central_page.fact_card", codes(central))

        noncentral = quality.evaluate_quality(
            intelligence({"name": "ATP", "page_type": "entity", "importance": 4}),
            source_id=SOURCE_ID,
            section_label=SECTION,
            pages=[quality.PageInput("wiki/entities/ATP.md", text)],
        )
        self.assertTrue(noncentral["ok"], noncentral["errors"])


class CliTests(unittest.TestCase):
    def run_cli(self, intelligence_path, *modified):
        command = [
            sys.executable,
            str(SCRIPTS / "verify-ingest-quality.py"),
            "--intelligence",
            str(intelligence_path),
            "--source-id",
            SOURCE_ID,
            "--section-label",
            SECTION,
        ]
        for path in modified:
            command.extend(["--modified", str(path)])
        return subprocess.run(command, text=True, capture_output=True)

    def test_cli_emits_json_receipt_and_accepts_repeated_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entity = root / "wiki" / "entities" / "ATP.md"
            topic = root / "wiki" / "topics" / "Energy.md"
            entity.parent.mkdir(parents=True)
            topic.parent.mkdir(parents=True)
            entity.write_text(
                page_text(
                    "ATP",
                    f"ATP supports cellular work {current_citation()}.",
                ),
                encoding="utf-8",
            )
            topic.write_text(
                page_text(
                    "Energy",
                    f"Energy links cellular processes {current_citation()}.",
                    page_type="Topic",
                ),
                encoding="utf-8",
            )
            artifact = root / "intelligence.json"
            artifact.write_text(json.dumps(intelligence()), encoding="utf-8")

            result = self.run_cli(artifact, entity, topic)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            receipt = json.loads(result.stdout)
            self.assertTrue(receipt["ok"])
            self.assertEqual(receipt["summary"]["modified_pages"], 2)
            self.assertIn("intelligence", receipt)

    def test_cli_fails_closed_on_malformed_intelligence_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entity = root / "wiki" / "entities" / "ATP.md"
            entity.parent.mkdir(parents=True)
            entity.write_text(
                page_text(
                    "ATP",
                    f"ATP supports cellular work {current_citation()}.",
                ),
                encoding="utf-8",
            )
            artifact = root / "intelligence.json"
            artifact.write_text("{not-json", encoding="utf-8")

            result = self.run_cli(artifact, entity)
            self.assertNotEqual(result.returncode, 0)
            receipt = json.loads(result.stdout)
            self.assertFalse(receipt["ok"])
            self.assertIn("intelligence.unreadable", codes(receipt))
            self.assertIn("intelligence.malformed", codes(receipt))

    def test_cli_accepts_no_changes_when_existing_page_proves_claim_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entity = root / "wiki" / "entities" / "ATP.md"
            entity.parent.mkdir(parents=True)
            text = page_text(
                "ATP", "ATP couples energy-releasing reactions to work."
            )
            entity.write_text(text, encoding="utf-8")
            artifact = root / "intelligence.json"
            artifact.write_text(
                json.dumps(production_intelligence()), encoding="utf-8"
            )
            command = [
                sys.executable,
                str(SCRIPTS / "verify-ingest-quality.py"),
                "--intelligence", str(artifact),
                "--source-id", SOURCE_ID,
                "--section-label", SECTION,
                "--existing", str(entity),
            ]
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["summary"]["modified_pages"], 0)
            self.assertEqual(receipt["summary"]["already_covered_candidates"], 1)


if __name__ == "__main__":
    unittest.main()
