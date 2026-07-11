import contextlib
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "generate_mindmap", ROOT / "scripts" / "generate-mindmap.py"
)
mindmap = importlib.util.module_from_spec(SPEC)
sys.modules["generate_mindmap"] = mindmap
SPEC.loader.exec_module(mindmap)
import source_citations  # noqa: E402


SOURCE_ID = "01K00000000000000000000000"
SOURCE_SHA = "a" * 64
MODEL_IDENTITY = {"provider": "fake", "model": "chapter-model"}
SCHEMA_SHA = "b" * 64


def artifact_for(label: str, *, chapter_claim: str | None = None) -> dict:
    entity_name = f"System concept for {label}"
    return {
        "schema": mindmap.ci.SCHEMA_VERSION,
        "source_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA,
        "text_sha256": mindmap.ci.sha256_text(f"Extracted text for {label}."),
        "section_label": label,
        "prompt_version": mindmap.ci.PROMPT_VERSION,
        "language": "en",
        "summary": f"Summary for {label}.",
        "central_question": f"What question does {label} answer?",
        "chapter_claim": chapter_claim or f"Principal claim from {label}.",
        "builds_on": None,
        "claims": [
            {
                "id": "c1",
                "kind": "question",
                "text": f"{label} asks a concrete question.",
                "importance": 3,
                "source_spans": [],
                "entities": [entity_name],
            },
            {
                "id": "c2",
                "kind": "claim",
                "text": f"{label} gives a concrete answer.",
                "importance": 3,
                "source_spans": [],
                "entities": [entity_name],
            },
        ],
        "entities": [
            {
                "name": entity_name,
                "type": "concept",
                "aliases": [],
                "importance": 3,
                "role": "Connects the chapter question and answer.",
                "page_hint": "entity",
                "claim_ids": ["c1", "c2"],
            }
        ],
        "topics": [
            {
                "name": f"Reasoning topic for {label}",
                "question": "How does the answer follow?",
                "synthesis_angle": "Connect the question to its answer.",
                "importance": 3,
                "claim_ids": ["c2"],
            }
        ],
        "relations": [{"from": "c2", "to": "c1", "rel": "answers"}],
        "page_candidates": [
            {
                "page_type": "entity",
                "name": entity_name,
                "importance": 3,
                "required": False,
                "claim_ids": ["c1", "c2"],
                "reason": "Reusable concept represented in the argument map fixture.",
            },
            {
                "page_type": "topic",
                "name": f"Reasoning topic for {label}",
                "importance": 3,
                "required": False,
                "claim_ids": ["c2"],
                "reason": "Reusable synthesis represented in the argument map fixture.",
            },
        ],
        "claim_coverage": [
            {
                "claim_id": "c1",
                "page_candidates": [
                    {"page_type": "entity", "name": entity_name}
                ],
                "skip_reason": None,
            },
            {
                "claim_id": "c2",
                "page_candidates": [
                    {"page_type": "entity", "name": entity_name},
                    {"page_type": "topic", "name": f"Reasoning topic for {label}"},
                ],
                "skip_reason": None,
            },
        ],
        "open_questions": [],
    }


def write_artifact(
    cache_dir: Path,
    artifact: dict,
    ordered_sections: list[str],
    *,
    model_identity: dict | None = None,
) -> Path:
    identity = model_identity or MODEL_IDENTITY
    template_sha = mindmap.ci.prompt_template_identity()
    key = mindmap.ci.cache_key(
        source_sha256=artifact["source_sha256"],
        text_sha256=artifact["text_sha256"],
        section_label=artifact["section_label"],
        prompt_version=mindmap.ci.PROMPT_VERSION,
        model_identity=identity,
        schema_ingest_sha256=SCHEMA_SHA,
        ordered_sections=ordered_sections,
        prior_chapters=[],
        prompt_template_sha256=template_sha,
    )
    path = mindmap.ci.cache_path(
        cache_dir,
        prompt_version=mindmap.ci.PROMPT_VERSION,
        source_id=SOURCE_ID,
        key=key,
    )
    inputs = mindmap.ci.cache_inputs(
        source_sha256=artifact["source_sha256"],
        text_sha256=artifact["text_sha256"],
        section_label=artifact["section_label"],
        prompt_version=mindmap.ci.PROMPT_VERSION,
        model_identity=identity,
        schema_ingest_sha256=SCHEMA_SHA,
        ordered_sections=ordered_sections,
        prior_chapters=[],
        prompt_template_sha256=template_sha,
    )
    mindmap.ci.write_cache_entry(path, artifact, inputs)
    return path


def map_completion(chapters: list[str]) -> str:
    nodes = [
        {
            "id": f"n{index}",
            "kind": "claim",
            "label": f"Map claim for {label}.",
            "chapter": label,
        }
        for index, label in enumerate(chapters, start=1)
    ]
    return json.dumps(
        {
            "central_question": "What is the book's central question?",
            "thesis": "The book gives a central answer.",
            "chapters": [
                {
                    "label": label,
                    "question": f"Question for {label}?",
                    "claim": f"Claim for {label}.",
                    "builds_on": None,
                }
                for label in chapters
            ],
            "nodes": nodes,
            "edges": [
                {
                    "from": nodes[index - 1]["id"],
                    "to": nodes[index]["id"],
                    "rel": "supports",
                }
                for index in range(1, len(nodes))
            ],
        }
    )


@contextlib.contextmanager
def generation_environment(root: Path, chapters: list[str]):
    wiki_dir = root / "wiki"
    with (
        patch.multiple(
            mindmap,
            VAULT_ROOT=root,
            WIKI_DIR=wiki_dir,
            MAPS_DIR=wiki_dir / "_maps",
            CACHE_DIR=root / ".wiki" / "mindmap-cache",
            CHAPTER_INTELLIGENCE_CACHE_DIR=(
                root / ".wiki" / "chapter-intelligence-cache"
            ),
            _LINK_ENTRIES=[],
        ),
        patch.object(mindmap, "chapter_order", return_value=chapters),
    ):
        yield


class GenerateMindmapTests(unittest.TestCase):

    def test_render_uses_shared_delimiter_safe_section_citation(self):
        cases = json.loads(
            (ROOT.parent / "ci-fixtures" / "source-citation-contract.json").read_text(
                encoding="utf-8"
            )
        )
        label = cases[1]["label"]
        data = json.loads(map_completion([label]))
        expected = source_citations.source_citation(SOURCE_ID, label)

        with patch.object(mindmap, "_LINK_ENTRIES", []):
            rendered = mindmap.render_map(
                SOURCE_ID,
                "Book",
                [label],
                data,
                "content-hash",
                "2026-07-11",
                None,
            )

        self.assertGreaterEqual(rendered.count(expected), 2)
        self.assertNotIn(f"[src:{SOURCE_ID}#{label}]", rendered)

    def test_main_returns_nonzero_when_any_source_fails(self):
        sources = {
            SOURCE_ID: {"sha256": SOURCE_SHA, "asset": "book.epub", "title": "Book"},
            "01K11111111111111111111111": {
                "sha256": "b" * 64,
                "asset": "other.epub",
                "title": "Other",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wiki = root / "wiki"
            wiki.mkdir()
            log = root / "log.md"
            log.write_text("", encoding="utf-8")
            with patch.object(mindmap, "WIKI_DIR", wiki), patch.object(
                mindmap, "LOG_PATH", log
            ), patch.object(mindmap.dl, "find_sources", return_value=sources), patch.object(
                mindmap, "generate_one", side_effect=RuntimeError("map failed")
            ), patch.object(sys, "argv", ["generate-mindmap.py", "--source-id", SOURCE_ID]):
                self.assertEqual(mindmap.main(), 1)
    def test_source_prompt_cap_is_around_120k(self):
        self.assertEqual(mindmap.SOURCE_CHAR_LIMIT, 120_000)

    def test_complete_intelligence_avoids_source_extraction(self):
        chapters = ["Chapter 1", "Chapter 2"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            intelligence_cache = root / ".wiki" / "chapter-intelligence-cache"
            for label in chapters:
                write_artifact(intelligence_cache, artifact_for(label), chapters)

            with (
                generation_environment(root, chapters),
                patch.object(mindmap.dl, "extract_source_text") as extract,
                patch.object(
                    mindmap.dl,
                    "call_llm",
                    return_value=map_completion(chapters),
                ) as complete,
            ):
                wrote, message = mindmap.generate_one(
                    SOURCE_ID,
                    {
                        "sha256": SOURCE_SHA,
                        "asset": root / "book.epub",
                        "title": "Book",
                    },
                    refresh=False,
                    dry_run=False,
                )

            self.assertTrue(wrote)
            extract.assert_not_called()
            prompt = complete.call_args.args[0]
            self.assertIn("VALIDATED CHAPTER INTELLIGENCE", prompt)
            self.assertIn("Principal claim from Chapter 1", prompt)
            self.assertIn('"relations"', prompt)
            self.assertIn('"entities"', prompt)
            self.assertIn('"topics"', prompt)
            self.assertNotIn('"source_spans"', prompt)
            self.assertIn("complete chapter intelligence", message)

    def test_incomplete_intelligence_falls_back_to_source_extraction(self):
        chapters = ["Chapter 1", "Chapter 2"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            intelligence_cache = root / ".wiki" / "chapter-intelligence-cache"
            write_artifact(
                intelligence_cache, artifact_for("Chapter 1"), chapters
            )

            with (
                generation_environment(root, chapters),
                patch.object(
                    mindmap.dl,
                    "extract_source_text",
                    return_value="RAW SOURCE SENTINEL",
                ) as extract,
                patch.object(
                    mindmap.dl,
                    "call_llm",
                    return_value=map_completion(chapters),
                ) as complete,
            ):
                wrote, message = mindmap.generate_one(
                    SOURCE_ID,
                    {
                        "sha256": SOURCE_SHA,
                        "asset": root / "book.epub",
                        "title": "Book",
                    },
                    refresh=False,
                    dry_run=False,
                )

            self.assertTrue(wrote)
            extract.assert_called_once_with(
                mindmap.EXTRACT, root / "book.epub", mindmap.SOURCE_CHAR_LIMIT
            )
            prompt = complete.call_args.args[0]
            self.assertIn("SOURCE TEXT", prompt)
            self.assertIn("RAW SOURCE SENTINEL", prompt)
            self.assertIn("raw-source fallback", message)
            self.assertIn("Chapter 2", message)

    def test_artifact_change_invalidates_map_cache(self):
        chapters = ["Chapter 1"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            intelligence_cache = root / ".wiki" / "chapter-intelligence-cache"
            artifact = artifact_for("Chapter 1")
            write_artifact(intelligence_cache, artifact, chapters)

            with (
                generation_environment(root, chapters),
                patch.object(mindmap.dl, "extract_source_text") as extract,
                patch.object(
                    mindmap.dl,
                    "call_llm",
                    return_value=map_completion(chapters),
                ) as complete,
            ):
                meta = {
                    "sha256": SOURCE_SHA,
                    "asset": root / "book.epub",
                    "title": "Book",
                }
                mindmap.generate_one(SOURCE_ID, meta, refresh=False, dry_run=False)
                mindmap.generate_one(SOURCE_ID, meta, refresh=False, dry_run=False)

                artifact["chapter_claim"] = "A revised principal claim."
                write_artifact(intelligence_cache, artifact, chapters)
                mindmap.generate_one(SOURCE_ID, meta, refresh=False, dry_run=False)

            extract.assert_not_called()
            self.assertEqual(complete.call_count, 2)
            map_cache = root / ".wiki" / "mindmap-cache"
            self.assertEqual(len(list(map_cache.glob("*.json"))), 2)

    def test_one_off_analyzer_model_is_discovered_after_model_config_changes(self):
        chapters = ["Chapter 1", "Chapter 2"]
        one_off = {"provider": "fake", "model": "one-off-analyzer"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            intelligence_cache = root / ".wiki" / "chapter-intelligence-cache"
            for label in chapters:
                write_artifact(
                    intelligence_cache,
                    artifact_for(label),
                    chapters,
                    model_identity=one_off,
                )
            # A later run under another configured model may coexist with the
            # one-off entry. Discovery still selects a valid manifested result
            # instead of treating the model variants as an ambiguity.
            write_artifact(
                intelligence_cache,
                artifact_for("Chapter 1"),
                chapters,
                model_identity=MODEL_IDENTITY,
            )

            with (
                generation_environment(root, chapters),
                patch.object(
                    mindmap.ci,
                    "resolve_model_identity",
                    side_effect=AssertionError("consumer must not resolve current model"),
                ),
                patch.object(mindmap.dl, "extract_source_text") as extract,
                patch.object(
                    mindmap.dl,
                    "call_llm",
                    return_value=map_completion(chapters),
                ),
            ):
                wrote, message = mindmap.generate_one(
                    SOURCE_ID,
                    {
                        "sha256": SOURCE_SHA,
                        "asset": root / "book.epub",
                        "title": "Book",
                    },
                    refresh=False,
                    dry_run=False,
                )

            self.assertTrue(wrote)
            extract.assert_not_called()
            self.assertIn("complete chapter intelligence", message)

    def test_map_cache_identity_is_exact_compact_prompt_projection(self):
        bundle = {
            "schema": mindmap.ci.SCHEMA_VERSION,
            "artifacts": [artifact_for("Chapter 1")],
        }
        compact = mindmap.compact_intelligence_input(bundle)
        canonical = json.dumps(
            compact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(
            mindmap.intelligence_cache_identity(compact),
            "i" + hashlib.sha256(canonical).hexdigest(),
        )

        bundle["artifacts"][0]["summary"] = "Not consumed by the map prompt."
        self.assertEqual(mindmap.compact_intelligence_input(bundle), compact)
        self.assertEqual(
            mindmap.intelligence_cache_identity(
                mindmap.compact_intelligence_input(bundle)
            ),
            mindmap.intelligence_cache_identity(compact),
        )


if __name__ == "__main__":
    unittest.main()
