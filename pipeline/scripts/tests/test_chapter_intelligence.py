import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import chapter_intelligence as ci  # noqa: E402


CLI_SPEC = importlib.util.spec_from_file_location(
    "analyze_chapter_cli", SCRIPTS / "analyze-chapter.py"
)
cli = importlib.util.module_from_spec(CLI_SPEC)
sys.modules["analyze_chapter_cli"] = cli
CLI_SPEC.loader.exec_module(cli)


SOURCE_ID = "01K00000000000000000000000"
SOURCE_SHA = "a" * 64
MODEL_IDENTITY = {"provider": "fake", "model": "fake-analyzer"}
TEXT = "Alpha reliably powers beta. Gamma measures delta. Epsilon records change."
QUOTE = "Alpha reliably powers beta."


def artifact_for(
    text: str = TEXT,
    *,
    section_label: str = "Chapter 2",
    count: int = 2,
    quote_first: bool = False,
) -> dict:
    start = text.index(QUOTE)
    span = {"quote": QUOTE}
    if not quote_first:
        span = {"start": start, "end": start + len(QUOTE), "quote": QUOTE}

    claims = []
    entities = []
    candidates = []
    coverage = []
    for number in range(1, count + 1):
        claim_id = f"c{number}"
        name = f"Entity {number}"
        claims.append(
            {
                "id": claim_id,
                "kind": "claim" if number == 1 else "evidence",
                "text": f"Proposition {number} is grounded in the chapter.",
                "importance": 5 if number == 1 else 4,
                "source_spans": [copy.deepcopy(span)],
                "entities": [name],
            }
        )
        entities.append(
            {
                "name": name,
                "type": "concept",
                "aliases": [f"E{number}"],
                "importance": 5 if number == 1 else 4,
                "role": f"Carries explanatory role {number}.",
                "page_hint": "entity",
                "claim_ids": [claim_id],
            }
        )
        candidates.append(
            {
                "page_type": "entity",
                "name": name,
                "importance": 5 if number == 1 else 4,
                "required": True,
                "claim_ids": [claim_id],
                "reason": f"Reusable concept {number}.",
            }
        )
        coverage.append(
            {
                "claim_id": claim_id,
                "page_candidates": [{"page_type": "entity", "name": name}],
                "skip_reason": None,
            }
        )

    topic = {
        "name": "System behavior",
        "question": "How do the entities form a system?",
        "synthesis_angle": "Connects the mechanism and its evidence.",
        "importance": 4,
        "claim_ids": ["c1"],
    }
    candidates.append(
        {
            "page_type": "topic",
            "name": topic["name"],
            "importance": 4,
            "required": True,
            "claim_ids": ["c1"],
            "reason": "A reusable cross-entity synthesis question.",
        }
    )
    coverage[0]["page_candidates"].append(
        {"page_type": "topic", "name": topic["name"]}
    )

    return {
        "schema": ci.SCHEMA_VERSION,
        "source_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA,
        "text_sha256": ci.sha256_text(text),
        "section_label": section_label,
        "prompt_version": ci.PROMPT_VERSION,
        "language": "en",
        "summary": "A compact chapter summary.",
        "central_question": "What powers the system?",
        "chapter_claim": "Alpha supplies the system's power.",
        "builds_on": None,
        "claims": claims,
        "entities": entities,
        "topics": [topic],
        "relations": [
            {"from": f"c{number}", "to": f"c{number - 1}", "rel": "supports"}
            for number in range(2, count + 1)
        ],
        "page_candidates": candidates,
        "claim_coverage": coverage,
        "open_questions": ["Which observation would falsify the mechanism?"],
    }


def validate(artifact: dict, text: str = TEXT, section_label: str = "Chapter 2") -> dict:
    return ci.validate_artifact(
        artifact,
        text=text,
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA,
        section_label=section_label,
    )


class ValidationTests(unittest.TestCase):
    def test_accepts_more_than_five_claims_and_entities(self):
        artifact = artifact_for(count=8)
        self.assertIs(validate(artifact), artifact)
        self.assertEqual(len(artifact["claims"]), 8)
        self.assertEqual(len(artifact["entities"]), 8)

    def test_empty_section_label_is_valid(self):
        artifact = artifact_for(section_label="")
        self.assertIs(validate(artifact, section_label=""), artifact)

    def test_contrasts_is_a_valid_typed_relation(self):
        artifact = artifact_for()
        artifact["relations"][0]["rel"] = "contrasts"
        self.assertIs(validate(artifact), artifact)

    def test_rejects_scalar_coercion_and_bad_importance(self):
        cases = []
        bad = artifact_for()
        bad["summary"] = 7
        cases.append((bad, "summary"))
        bad = artifact_for()
        bad["claims"][0]["importance"] = True
        cases.append((bad, "importance"))
        bad = artifact_for()
        bad["entities"][0]["importance"] = "5"
        cases.append((bad, "importance"))
        bad = artifact_for()
        bad["page_candidates"][0]["required"] = 1
        cases.append((bad, "required"))
        bad = artifact_for()
        bad["claims"][0]["source_spans"][0]["start"] = 0.0
        cases.append((bad, "start"))
        for artifact, field in cases:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ci.ArtifactValidationError, field):
                    validate(artifact)

    def test_rejects_bad_claim_ids_and_dangling_references(self):
        cases = []
        bad = artifact_for()
        bad["claims"][0]["id"] = "claim-1"
        cases.append((bad, "must match"))
        bad = artifact_for()
        bad["claims"][1]["id"] = "c1"
        cases.append((bad, "duplicates claim"))
        bad = artifact_for()
        bad["entities"][0]["claim_ids"] = ["c99"]
        cases.append((bad, "unknown claim"))
        bad = artifact_for()
        bad["topics"][0]["claim_ids"] = ["c99"]
        cases.append((bad, "unknown claim"))
        bad = artifact_for()
        bad["relations"][0]["to"] = "c99"
        cases.append((bad, "unknown claim"))
        bad = artifact_for()
        bad["claims"][0]["entities"] = ["Undeclared"]
        cases.append((bad, "undeclared entity"))
        for artifact, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ci.ArtifactValidationError, message):
                    validate(artifact)

    def test_rejects_stale_metadata_and_unknown_fields(self):
        cases = []
        for field, value in (
            ("schema", "chapter-intelligence/2"),
            ("source_id", "another-source"),
            ("source_sha256", "b" * 64),
            ("text_sha256", "c" * 64),
            ("section_label", "Chapter 9"),
            ("prompt_version", "v0"),
        ):
            bad = artifact_for()
            bad[field] = value
            cases.append((bad, field))
        bad = artifact_for()
        bad["unexpected"] = "field"
        cases.append((bad, "unexpected"))

        for artifact, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ci.ArtifactValidationError, message):
                    validate(artifact)

    def test_rejects_noncanonical_or_mismatched_final_spans(self):
        bad = artifact_for()
        bad["claims"][0]["source_spans"][0]["quote"] = (
            "Alpha reliably powers BETA."
        )
        with self.assertRaisesRegex(ci.ArtifactValidationError, "does not match"):
            validate(bad)

        bad = artifact_for()
        bad["claims"][0]["source_spans"][0]["end"] = len(TEXT) + 1
        with self.assertRaisesRegex(ci.ArtifactValidationError, "outside text"):
            validate(bad)

    def test_importance_four_and_five_claims_require_a_span(self):
        for importance in (4, 5):
            bad = artifact_for()
            bad["claims"][0]["importance"] = importance
            bad["claims"][0]["source_spans"] = []
            with self.subTest(importance=importance):
                with self.assertRaisesRegex(
                    ci.ArtifactValidationError, "require at least one validated"
                ):
                    validate(bad)

        low = artifact_for()
        low["claims"][0]["importance"] = 3
        low["claims"][0]["source_spans"] = []
        self.assertIs(validate(low), low)

    def test_high_importance_page_candidate_must_be_required(self):
        bad = artifact_for()
        bad["page_candidates"][0]["required"] = False
        with self.assertRaisesRegex(ci.ArtifactValidationError, "must be true"):
            validate(bad)

    def test_page_hint_and_topic_require_matching_page_candidates(self):
        missing_entity = artifact_for()
        missing_entity["page_candidates"] = [
            candidate for candidate in missing_entity["page_candidates"]
            if candidate["name"] != "Entity 2"
        ]
        missing_entity["claim_coverage"][1] = {
            "claim_id": "c2",
            "page_candidates": [],
            "skip_reason": "Kept only as supporting context.",
        }
        with self.assertRaisesRegex(ci.ArtifactValidationError, "page_hint=entity"):
            validate(missing_entity)

        skipped_entity = copy.deepcopy(missing_entity)
        skipped_entity["entities"][1]["page_hint"] = "none"
        self.assertIs(validate(skipped_entity), skipped_entity)

        missing_topic = artifact_for()
        missing_topic["page_candidates"] = [
            candidate for candidate in missing_topic["page_candidates"]
            if candidate["page_type"] != "topic"
        ]
        missing_topic["claim_coverage"][0]["page_candidates"] = [
            ref for ref in missing_topic["claim_coverage"][0]["page_candidates"]
            if ref["page_type"] != "topic"
        ]
        with self.assertRaisesRegex(ci.ArtifactValidationError, "topic .*no page candidate"):
            validate(missing_topic)

    def test_claim_coverage_is_exhaustive_and_has_skip_reasons(self):
        bad = artifact_for()
        bad["claim_coverage"].pop()
        with self.assertRaisesRegex(ci.ArtifactValidationError, "missing claims"):
            validate(bad)

        skipped = artifact_for()
        skipped["page_candidates"] = [
            candidate
            for candidate in skipped["page_candidates"]
            if not (candidate["page_type"] == "entity" and candidate["name"] == "Entity 2")
        ]
        skipped["claim_coverage"][1] = {
            "claim_id": "c2",
            "page_candidates": [],
            "skip_reason": "Supporting observation is not a reusable page subject.",
        }
        skipped["entities"][1]["page_hint"] = "none"
        self.assertIs(validate(skipped), skipped)
        skipped["claim_coverage"][1]["skip_reason"] = ""
        with self.assertRaisesRegex(ci.ArtifactValidationError, "skip_reason"):
            validate(skipped)

    def test_claim_coverage_materialization_repairs_reverse_bookkeeping(self):
        raw = artifact_for()
        raw["claim_coverage"][0]["page_candidates"] = []
        raw["claim_coverage"][0]["skip_reason"] = "incorrect stale value"
        canonical = ci.materialize_claim_coverage(raw)
        expected = [
            {"page_type": "entity", "name": "Entity 1"},
            {"page_type": "topic", "name": "System behavior"},
        ]
        self.assertEqual(canonical["claim_coverage"][0]["page_candidates"], expected)
        self.assertIsNone(canonical["claim_coverage"][0]["skip_reason"])
        self.assertIs(validate(canonical), canonical)

    def test_page_hint_materialization_follows_page_candidates(self):
        raw = artifact_for()
        raw["entities"][0]["page_hint"] = "none"
        raw["entities"][1]["page_hint"] = "entity"
        raw["page_candidates"] = [
            candidate for candidate in raw["page_candidates"]
            if candidate["name"] != "Entity 2"
        ]
        raw["claim_coverage"][1] = {
            "claim_id": "c2",
            "page_candidates": [],
            "skip_reason": "Supporting context only.",
        }
        canonical = ci.materialize_page_hints(raw)
        self.assertEqual(canonical["entities"][0]["page_hint"], "entity")
        self.assertEqual(canonical["entities"][1]["page_hint"], "none")
        self.assertIs(validate(canonical), canonical)

    def test_entity_reference_materialization_repairs_missing_inventory_row(self):
        raw = artifact_for()
        raw["entities"] = [raw["entities"][0]]
        canonical = ci.materialize_entity_references(raw)
        derived = canonical["entities"][1]
        self.assertEqual(derived["name"], "Entity 2")
        self.assertEqual(derived["type"], "concept")
        self.assertEqual(derived["importance"], 4)
        self.assertEqual(derived["claim_ids"], ["c2"])
        self.assertEqual(derived["role"], canonical["claims"][1]["text"])
        canonical = ci.materialize_page_hints(canonical)
        self.assertEqual(canonical["entities"][1]["page_hint"], "entity")
        self.assertIs(validate(canonical), canonical)

    def test_entity_reference_materialization_repairs_reverse_claim_ids(self):
        raw = artifact_for()
        raw["entities"][0]["claim_ids"] = []
        canonical = ci.materialize_entity_references(raw)
        self.assertEqual(canonical["entities"][0]["claim_ids"], ["c1"])
        self.assertIs(validate(canonical), canonical)

    def test_alias_materialization_reserves_canonical_names_and_one_alias_owner(self):
        raw = artifact_for()
        raw["entities"][0]["aliases"] = ["System behavior", "Shared alias"]
        raw["entities"][1]["aliases"] = ["Ｓｈａｒｅｄ　Ａｌｉａｓ"]

        with self.assertRaisesRegex(
            ci.ArtifactValidationError, "duplicates wiki identity"
        ):
            validate(raw)

        canonical = ci.materialize_unique_aliases(raw)
        self.assertEqual(canonical["entities"][0]["aliases"], ["Shared alias"])
        self.assertEqual(canonical["entities"][1]["aliases"], [])
        self.assertIs(validate(canonical), canonical)

    def test_alias_materialization_prefers_stronger_page_candidate(self):
        raw = artifact_for()
        raw["entities"][0]["aliases"] = ["Shared alias"]
        raw["entities"][1]["aliases"] = ["Shared alias"]
        raw["page_candidates"][1]["importance"] = 5
        raw["page_candidates"][1]["claim_ids"] = ["c1", "c2"]

        canonical = ci.materialize_unique_aliases(raw)
        self.assertEqual(canonical["entities"][0]["aliases"], [])
        self.assertEqual(canonical["entities"][1]["aliases"], ["Shared alias"])


class QuoteMaterializationTests(unittest.TestCase):
    def test_relation_verbs_in_claim_kind_are_canonicalized(self):
        for raw_kind, expected in ci.CLAIM_KIND_ALIASES.items():
            with self.subTest(kind=raw_kind):
                raw = artifact_for(count=1, quote_first=True)
                raw["claims"][0]["kind"] = raw_kind
                artifact = ci.materialize_response(json.dumps(raw), TEXT)
                self.assertEqual(artifact["claims"][0]["kind"], expected)
                validate(artifact)

    def test_unique_quote_materializes_canonical_offsets(self):
        raw = artifact_for(quote_first=True)
        raw["claims"][0]["source_spans"][0].update({"start": 999, "end": 1000})
        result = ci.materialize_source_spans(raw, TEXT)
        expected = {"start": 0, "end": len(QUOTE), "quote": QUOTE}
        self.assertEqual(result["claims"][0]["source_spans"], [expected])
        self.assertNotEqual(raw["claims"][0]["source_spans"], [expected])
        validate(result)

    def test_repeated_quote_defaults_to_first_or_uses_exact_bounds(self):
        quote = "repeat evidence phrase"
        text = f"{quote}; {quote}"
        second = len(quote) + 2
        raw = artifact_for(text=TEXT, count=1, quote_first=True)
        raw["claims"][0]["source_spans"] = [{"quote": quote}]
        first = ci.materialize_source_spans(raw, text)
        self.assertEqual(
            first["claims"][0]["source_spans"][0],
            {"start": 0, "end": len(quote), "quote": quote},
        )

        raw["claims"][0]["source_spans"] = [
            {"quote": quote, "start": second, "end": second + len(quote)}
        ]
        result = ci.materialize_source_spans(raw, text)
        self.assertEqual(
            result["claims"][0]["source_spans"][0],
            {"start": second, "end": second + len(quote), "quote": quote},
        )

    def test_quote_length_contract_is_enforced(self):
        raw = artifact_for(count=1, quote_first=True)
        raw["claims"][0]["source_spans"] = [{"quote": "too short"}]
        with self.assertRaisesRegex(ci.ArtifactValidationError, "at least 20"):
            ci.materialize_source_spans(raw, "too short")

        long_quote = "x" * 241
        raw["claims"][0]["source_spans"] = [{"quote": long_quote}]
        with self.assertRaisesRegex(ci.ArtifactValidationError, "at most 240"):
            ci.materialize_source_spans(raw, long_quote)

    def test_short_exact_quote_is_expanded_with_adjacent_source_text(self):
        text = "Prefix context before ATP and useful context after it."
        raw = artifact_for(count=1, quote_first=True)
        raw["claims"][0]["source_spans"] = [{"quote": "ATP"}]
        span = ci.materialize_source_spans(raw, text)["claims"][0]["source_spans"][0]
        self.assertEqual(len(span["quote"]), ci.SOURCE_QUOTE_MIN_CHARS)
        self.assertEqual(span["quote"], text[span["start"]:span["end"]])
        self.assertIn("ATP", span["quote"])

    def test_unmatched_fails_and_approximate_repeated_bounds_use_first(self):
        raw = artifact_for(count=1, quote_first=True)
        raw["claims"][0]["source_spans"] = [{"quote": "missing evidence phrase"}]
        with self.assertRaisesRegex(ci.ArtifactValidationError, "does not occur"):
            ci.materialize_source_spans(raw, TEXT)

        quote = "repeat evidence phrase"
        text = f"{quote}; {quote}"
        raw["claims"][0]["source_spans"] = [
            {"quote": quote, "start": len(quote) + 1, "end": len(quote) * 2 + 1}
        ]
        result = ci.materialize_source_spans(raw, text)
        self.assertEqual(
            result["claims"][0]["source_spans"][0],
            {"start": 0, "end": len(quote), "quote": quote},
        )

    def test_formatting_equivalent_quote_is_canonicalized_to_source_slice(self):
        text = "Alpha\u00a0powers\n\u201cbeta\u201d\u2026"
        raw = artifact_for(count=1, quote_first=True)
        raw["claims"][0]["source_spans"] = [
            {"quote": 'Alpha powers "beta"...'}
        ]
        result = ci.materialize_source_spans(raw, text)
        self.assertEqual(
            result["claims"][0]["source_spans"],
            [{"start": 0, "end": len(text), "quote": text}],
        )

    def test_formatting_match_does_not_accept_changed_words(self):
        text = "Alpha powers beta."
        raw = artifact_for(count=1, quote_first=True)
        raw["claims"][0]["source_spans"] = [{"quote": "Alpha measures beta."}]
        with self.assertRaisesRegex(
            ci.ArtifactValidationError, "formatting-equivalent"
        ):
            ci.materialize_source_spans(raw, text)

    def test_markdown_emphasis_is_canonicalized_to_exact_source_slice(self):
        text = "Mitochondria **can****not** be omitted."
        raw = artifact_for(count=1, quote_first=True)
        raw["claims"][0]["source_spans"] = [
            {"quote": "Mitochondria cannot be omitted."}
        ]
        result = ci.materialize_source_spans(raw, text)
        self.assertEqual(
            result["claims"][0]["source_spans"],
            [{"start": 0, "end": len(text), "quote": text}],
        )


class CacheAndPromptTests(unittest.TestCase):
    def test_analyzer_schema_selection_keeps_planning_and_naming_only(self):
        full_rules = ci.DEFAULT_SCHEMA_INGEST_PATH.read_text(encoding="utf-8")
        selected = ci.select_analyzer_schema_rules(full_rules)
        for section in ci.ANALYZER_SCHEMA_SECTIONS:
            self.assertIn(f"## {section}", selected)
        for excluded in (
            "Frontmatter",
            "Tags",
            "Zones",
            "Citations",
            "Candidate Digests And Expansion",
            "Images",
            "Patch Retry",
        ):
            self.assertNotIn(f"## {excluded}", selected)
        self.assertIn("There is no per-chapter entity cap", selected)
        self.assertIn("chapter titles", selected)
        self.assertIn("Source language wins", selected)
        self.assertIn("established English and", selected)
        self.assertIn("Chinese name", selected)
        self.assertEqual(
            ci.select_analyzer_schema_rules(
                full_rules.replace(
                    "## Patch Retry", "## Patch Retry\n\nUnrelated renderer-only change."
                )
            ),
            selected,
        )

    def test_cache_key_changes_for_every_required_input(self):
        base = {
            "source_sha256": "a" * 64,
            "text_sha256": "b" * 64,
            "section_label": "Chapter 1",
            "prompt_version": "v1",
            "model_identity": {"provider": "fake", "model": "m1"},
            "schema_ingest_sha256": "c" * 64,
            "ordered_sections": ["Chapter 1", "Chapter 2"],
            "prior_chapters": [
                {
                    "section_label": "Chapter 1",
                    "central_question": "Earlier question?",
                    "chapter_claim": "Earlier claim.",
                }
            ],
            "prompt_template_sha256": "1" * 64,
        }
        original = ci.cache_key(**base)
        variations = [
            {"source_sha256": "d" * 64},
            {"text_sha256": "e" * 64},
            {"section_label": "Chapter 2"},
            {"prompt_version": "v2"},
            {"model_identity": {"provider": "other", "model": "m1"}},
            {"model_identity": {"provider": "fake", "model": "m2"}},
            {
                "model_identity": {
                    "provider": "fake",
                    "model": "m1",
                    "reasoning": "high",
                }
            },
            {
                "model_identity": {
                    "provider": "fake",
                    "model": "m1",
                    "verbosity": "medium",
                }
            },
            {
                "model_identity": {
                    "provider": "fake",
                    "model": "m1",
                    "api_base_url": "https://api.example.test/v1",
                }
            },
            {
                "model_identity": {
                    "provider": "fake",
                    "model": "m1",
                    "command_fingerprint": "3" * 64,
                }
            },
            {
                "model_identity": {
                    "provider": "fake",
                    "model": "m1",
                    "codex_binary_fingerprint": "4" * 64,
                }
            },
            {
                "model_identity": {
                    "provider": "fake",
                    "model": "m1",
                    "codex_config_fingerprint": "5" * 64,
                }
            },
            {"schema_ingest_sha256": "f" * 64},
            {"ordered_sections": ["Chapter 2", "Chapter 1"]},
            {
                "prior_chapters": [
                    {
                        "section_label": "Chapter 1",
                        "central_question": "Earlier question?",
                        "chapter_claim": "Revised earlier claim.",
                    }
                ]
            },
            {"prompt_template_sha256": "2" * 64},
        ]
        for variation in variations:
            args = {**base, **variation}
            with self.subTest(field=next(iter(variation))):
                self.assertNotEqual(ci.cache_key(**args), original)

    def test_prompt_has_structured_coverage_and_quote_first_contract(self):
        prompt = ci.build_prompt(
            TEXT,
            source_id=SOURCE_ID,
            source_sha256=SOURCE_SHA,
            section_label="Chapter 2",
            schema_ingest_rules="PAGE SELECTION RULE",
            ordered_sections=["Chapter 1", "Chapter 2"],
            prior_chapters=[
                {
                    "section_label": "Chapter 1",
                    "central_question": "Earlier question?",
                    "chapter_claim": "Earlier claim.",
                }
            ],
        )
        for field in (
            '"claims"',
            '"entities"',
            '"topics"',
            '"page_candidates"',
            '"claim_coverage"',
            '"skip_reason"',
            '"required"',
        ):
            self.assertIn(field, prompt)
        self.assertIn("There is NO fixed entity", prompt)
        self.assertIn("coverage audit over the source in order", prompt)
        self.assertIn("selectivity rule, not a numeric page cap", prompt)
        self.assertIn("durable question or phenomenon", prompt)
        self.assertIn('"contrasts"', prompt)
        self.assertIn('"leads-to"', prompt)
        self.assertIn("Do not try to calculate offsets", prompt)
        self.assertIn("distinctive 20-240 character", prompt)
        self.assertIn("Never join separated phrases", prompt)
        self.assertIn("Importance 4-5 claims require", prompt)
        self.assertIn("never emit an object or array", prompt)
        self.assertIn("Earlier claim.", prompt)
        self.assertIn("PAGE SELECTION RULE", prompt)
        self.assertLess(
            prompt.index("Validation and coverage rules:"),
            prompt.index(f'"source_id": "{SOURCE_ID}"'),
        )
        self.assertLess(
            prompt.index("PAGE SELECTION RULE"),
            prompt.index(f'"source_id": "{SOURCE_ID}"'),
        )

    def test_analyze_atomically_caches_and_reuses_validated_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.md"
            schema.write_text("schema version one", encoding="utf-8")
            cache = root / "cache"
            calls = []

            def fake_complete(prompt, *, timeout, model):
                calls.append((prompt, timeout, model))
                return "```json\n" + json.dumps(
                    artifact_for(quote_first=True)
                ) + "\n```"

            kwargs = {
                "source_id": SOURCE_ID,
                "source_sha256": SOURCE_SHA,
                "section_label": "Chapter 2",
                "model_identity": MODEL_IDENTITY,
                "schema_ingest_path": schema,
                "cache_dir": cache,
                "complete": fake_complete,
            }
            first = ci.analyze_chapter(TEXT, **kwargs)
            second = ci.analyze_chapter(TEXT, **kwargs)
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)
            cache_files = list(cache.rglob("*.json"))
            self.assertEqual(len(cache_files), 1)
            self.assertEqual(json.loads(cache_files[0].read_text()), first)
            manifests = list(cache.rglob("*.manifest"))
            self.assertEqual(len(manifests), 1)
            entry = ci.read_cache_entry(cache_files[0])
            self.assertEqual(entry["artifact"], first)
            self.assertEqual(
                entry["manifest"]["cache_inputs"]["analysis_context"],
                {
                    "ordered_sections": [],
                    "previous_chapter_spine": [],
                },
            )
            self.assertEqual(list(cache.rglob("*.tmp")), [])

            schema.write_text("schema version two", encoding="utf-8")
            third = ci.analyze_chapter(TEXT, **kwargs)
            self.assertEqual(third, first)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(list(cache.rglob("*.json"))), 2)

    def test_changed_prior_spine_invalidates_cached_builds_on(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.md"
            schema.write_text("rules", encoding="utf-8")
            calls: list[str] = []

            def fake_complete(prompt, *, timeout, model):
                calls.append(prompt)
                raw = artifact_for(quote_first=True)
                raw["builds_on"] = (
                    "Revised dependency."
                    if "Revised earlier claim." in prompt
                    else "Original dependency."
                )
                return json.dumps(raw)

            base = {
                "source_id": SOURCE_ID,
                "source_sha256": SOURCE_SHA,
                "section_label": "Chapter 2",
                "ordered_sections": ["Chapter 1", "Chapter 2"],
                "model_identity": MODEL_IDENTITY,
                "schema_ingest_path": schema,
                "cache_dir": root / "cache",
                "complete": fake_complete,
            }
            original_spine = [
                {
                    "section_label": "Chapter 1",
                    "central_question": "Earlier question?",
                    "chapter_claim": "Original earlier claim.",
                }
            ]
            revised_spine = [
                {
                    **original_spine[0],
                    "chapter_claim": "Revised earlier claim.",
                }
            ]

            first = ci.analyze_chapter(TEXT, prior_chapters=original_spine, **base)
            cached = ci.analyze_chapter(TEXT, prior_chapters=original_spine, **base)
            revised = ci.analyze_chapter(TEXT, prior_chapters=revised_spine, **base)

            self.assertEqual(first["builds_on"], "Original dependency.")
            self.assertEqual(cached, first)
            self.assertEqual(revised["builds_on"], "Revised dependency.")
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(list((root / "cache").rglob("*.json"))), 2)

    def test_invalid_completion_is_never_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.md"
            schema.write_text("rules", encoding="utf-8")
            raw = artifact_for(quote_first=True)
            raw["claims"][0]["source_spans"] = [
                {"quote": "invented evidence quote"}
            ]
            with self.assertRaisesRegex(ci.ArtifactValidationError, "does not occur"):
                ci.analyze_chapter(
                    TEXT,
                    source_id=SOURCE_ID,
                    source_sha256=SOURCE_SHA,
                    section_label="Chapter 2",
                    model_identity=MODEL_IDENTITY,
                    schema_ingest_path=schema,
                    cache_dir=root / "cache",
                    complete=lambda *args, **kwargs: json.dumps(raw),
                )
            self.assertEqual(
                list((root / "cache" / ci.PROMPT_VERSION).rglob("*.json")),
                [],
            )
            diagnostics = list((root / "cache" / "_invalid").rglob("*.json"))
            self.assertEqual(len(diagnostics), 1)
            diagnostic = json.loads(diagnostics[0].read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["schema"], "chapter-intelligence-invalid/1")
            self.assertIn("does not occur", diagnostic["error"])
            self.assertIn("invented evidence quote", diagnostic["raw_response"])

    def test_invalid_completion_gets_one_repair_attempt(self):
        invalid = artifact_for(quote_first=True)
        invalid["claims"][0]["source_spans"] = [{"quote": "invented evidence quote"}]
        responses = [json.dumps(invalid), json.dumps(artifact_for(quote_first=True))]
        prompts = []

        def complete(prompt, **_kwargs):
            prompts.append(prompt)
            return responses.pop(0)

        result = ci.analyze_chapter(
            TEXT,
            source_id=SOURCE_ID,
            source_sha256=SOURCE_SHA,
            section_label="Chapter 2",
            model_identity=MODEL_IDENTITY,
            complete=complete,
        )
        self.assertEqual(result["schema"], ci.SCHEMA_VERSION)
        self.assertEqual(len(prompts), 2)
        self.assertIn("previous response was rejected", prompts[1])

    def test_cached_alias_collision_is_repaired_without_another_llm_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.md"
            schema.write_text("rules", encoding="utf-8")
            cache = root / "cache"
            artifact = artifact_for()
            artifact["entities"][0]["aliases"] = ["System behavior", "Shared alias"]
            artifact["entities"][1]["aliases"] = ["Shared alias"]
            key = ci.cache_key(
                source_sha256=SOURCE_SHA,
                text_sha256=ci.sha256_text(TEXT),
                section_label="Chapter 2",
                prompt_version=ci.PROMPT_VERSION,
                model_identity=MODEL_IDENTITY,
                schema_ingest_sha256=ci.selected_schema_digest(schema),
                ordered_sections=[],
                prior_chapters=[],
                prompt_template_sha256=ci.prompt_template_identity(),
            )
            path = ci.cache_path(
                cache,
                prompt_version=ci.PROMPT_VERSION,
                source_id=SOURCE_ID,
                key=key,
            )
            ci.atomic_write_json(path, artifact)

            repaired = ci.analyze_chapter(
                TEXT,
                source_id=SOURCE_ID,
                source_sha256=SOURCE_SHA,
                section_label="Chapter 2",
                model_identity=MODEL_IDENTITY,
                schema_ingest_path=schema,
                cache_dir=cache,
                complete=lambda *args, **kwargs: self.fail("cache repair called LLM"),
            )
            self.assertEqual(repaired["entities"][0]["aliases"], ["Shared alias"])
            self.assertEqual(repaired["entities"][1]["aliases"], [])
            self.assertEqual(json.loads(path.read_text()), repaired)

    def test_prior_spines_are_discovered_in_outline_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.md"
            schema.write_text("rules", encoding="utf-8")
            cache = root / "cache"
            outline = ["Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4"]
            prior: list[dict] = []
            for number in (1, 2, 4):
                section = f"Chapter {number}"
                raw = artifact_for(section_label=section, quote_first=True)
                raw["central_question"] = f"Question {number}?"
                raw["chapter_claim"] = f"Claim {number}."
                ci.analyze_chapter(
                    TEXT,
                    source_id=SOURCE_ID,
                    source_sha256=SOURCE_SHA,
                    section_label=section,
                    ordered_sections=outline,
                    prior_chapters=prior,
                    model_identity=MODEL_IDENTITY,
                    schema_ingest_path=schema,
                    cache_dir=cache,
                    complete=lambda *args, payload=raw, **kwargs: json.dumps(payload),
                )
                prior.append(
                    {
                        "section_label": section,
                        "central_question": f"Question {number}?",
                        "chapter_claim": f"Claim {number}.",
                    }
                )

            spines = ci.discover_prior_spines(
                cache,
                chapter_outline=outline,
                current_section_label="Chapter 3",
                source_id=SOURCE_ID,
                source_sha256=SOURCE_SHA,
                prompt_version=ci.PROMPT_VERSION,
                model_identity=MODEL_IDENTITY,
                schema_ingest_sha256=ci.selected_schema_digest(schema),
                prompt_template_sha256=ci.prompt_template_identity(),
            )
            self.assertEqual(
                spines,
                [
                    {
                        "section_label": "Chapter 1",
                        "central_question": "Question 1?",
                        "chapter_claim": "Claim 1.",
                    },
                    {
                        "section_label": "Chapter 2",
                        "central_question": "Question 2?",
                        "chapter_claim": "Claim 2.",
                    },
                ],
            )

    def test_prior_spine_discovery_selects_coherent_refreshed_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.md"
            schema.write_text("rules", encoding="utf-8")
            cache = root / "cache"
            outline = ["Chapter 1", "Chapter 2", "Chapter 3"]

            def analyze(label, claim, *, prior=(), refresh=False):
                raw = artifact_for(section_label=label, quote_first=True)
                raw["central_question"] = f"Question for {claim}?"
                raw["chapter_claim"] = claim
                return ci.analyze_chapter(
                    TEXT,
                    source_id=SOURCE_ID,
                    source_sha256=SOURCE_SHA,
                    section_label=label,
                    ordered_sections=outline,
                    prior_chapters=prior,
                    model_identity=MODEL_IDENTITY,
                    schema_ingest_path=schema,
                    cache_dir=cache,
                    refresh=refresh,
                    complete=lambda *args, **kwargs: json.dumps(raw),
                )

            chapter_1_old = analyze("Chapter 1", "Original chapter-one claim.")
            analyze(
                "Chapter 2",
                "Chapter two built on the original.",
                prior=[
                    {
                        key: chapter_1_old[key]
                        for key in (
                            "section_label",
                            "central_question",
                            "chapter_claim",
                        )
                    }
                ],
            )
            chapter_1_new = analyze(
                "Chapter 1", "Revised chapter-one claim.", refresh=True
            )
            chapter_2_new = analyze(
                "Chapter 2",
                "Chapter two built on the revision.",
                prior=[
                    {
                        key: chapter_1_new[key]
                        for key in (
                            "section_label",
                            "central_question",
                            "chapter_claim",
                        )
                    }
                ],
            )

            spines = ci.discover_prior_spines(
                cache,
                chapter_outline=outline,
                current_section_label="Chapter 3",
                source_id=SOURCE_ID,
                source_sha256=SOURCE_SHA,
                prompt_version=ci.PROMPT_VERSION,
                model_identity=MODEL_IDENTITY,
                schema_ingest_sha256=ci.selected_schema_digest(schema),
                prompt_template_sha256=ci.prompt_template_identity(),
            )
            self.assertEqual(
                [spine["chapter_claim"] for spine in spines],
                [chapter_1_new["chapter_claim"], chapter_2_new["chapter_claim"]],
            )


class CliTests(unittest.TestCase):
    def test_cli_reads_text_accepts_empty_label_and_writes_status_only_stdout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text_path = root / "chapter.txt"
            output_path = root / "result.json"
            text_path.write_text(TEXT, encoding="utf-8")
            raw = artifact_for(section_label="", quote_first=True)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(
                cli.ci.llm_client,
                "identity",
                return_value={"provider": "fake", "model": "default"},
            ), patch.object(
                cli.ci.llm_client,
                "complete",
                return_value=json.dumps(raw),
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = cli.main(
                    [
                        "--text-file",
                        str(text_path),
                        "--source-id",
                        SOURCE_ID,
                        "--source-sha256",
                        SOURCE_SHA,
                        "--section-label",
                        "",
                        "--model",
                        "fake-analyzer",
                        "--output",
                        str(output_path),
                        "--chapter-outline-json",
                        "[]",
                    ]
                )

            self.assertEqual(result, 0, stderr.getvalue())
            self.assertEqual(json.loads(output_path.read_text()), validate(
                ci.materialize_source_spans(raw, TEXT), TEXT, ""
            ))
            status_lines = stdout.getvalue().splitlines()
            self.assertEqual(len(status_lines), 1)
            self.assertIn("analyze-chapter: wrote", status_lines[0])
            self.assertNotIn('"schema"', stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
