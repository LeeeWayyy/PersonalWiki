#!/usr/bin/env python3
"""Verify chapter-intelligence artifacts against a deterministic book baseline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chapter_intelligence as ci  # noqa: E402

# Same NFKC + casefold + whitespace-collapse normalizer the analyzer uses.
_normalize = ci.normalized_name


class BaselineVerificationError(ValueError):
    """Raised when a baseline or artifact bundle misses a required expectation."""


def _object(value: object, path: str) -> dict:
    if not isinstance(value, dict):
        raise BaselineVerificationError(f"{path}: expected an object")
    return value


def _list(value: object, path: str) -> list:
    if not isinstance(value, list):
        raise BaselineVerificationError(f"{path}: expected a list")
    return value


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BaselineVerificationError(f"{path}: expected a non-empty string")
    return value


def _alias_group(value: object, path: str) -> frozenset[str]:
    aliases = _list(value, path)
    normalized = frozenset(
        _normalize(_string(alias, f"{path}[{index}]"))
        for index, alias in enumerate(aliases)
    )
    if not normalized:
        raise BaselineVerificationError(f"{path}: alias group must not be empty")
    return normalized


def _groups(chapter: dict, field: str, path: str) -> list[frozenset[str]]:
    values = _list(chapter.get(field), f"{path}.{field}")
    return [_alias_group(value, f"{path}.{field}[{index}]") for index, value in enumerate(values)]


def validate_baseline(baseline: object) -> dict:
    obj = _object(baseline, "$baseline")
    if obj.get("schema") != "chapter-intelligence-baseline/1":
        raise BaselineVerificationError("$baseline.schema: unsupported schema")
    _string(obj.get("title"), "$baseline.title")
    source_sha = _string(obj.get("source_sha256"), "$baseline.source_sha256")
    if not ci.SHA256_RX.fullmatch(source_sha):
        raise BaselineVerificationError("$baseline.source_sha256: expected a SHA-256 hex digest")

    chapters = _list(obj.get("chapters"), "$baseline.chapters")
    if not chapters:
        raise BaselineVerificationError("$baseline.chapters: must not be empty")

    labels: set[str] = set()
    for index, value in enumerate(chapters):
        path = f"$baseline.chapters[{index}]"
        chapter = _object(value, path)
        label = _string(chapter.get("label"), f"{path}.label")
        normalized_label = _normalize(label)
        if normalized_label in labels:
            raise BaselineVerificationError(f"{path}.label: duplicate chapter label {label!r}")
        labels.add(normalized_label)
        _list(chapter.get("member_sections"), f"{path}.member_sections")
        _groups(chapter, "required_entity_alias_groups", path)
        _groups(chapter, "required_topic_alias_groups", path)
        _list(chapter.get("analysis_only_concepts"), f"{path}.analysis_only_concepts")
        claim_groups = _list(chapter.get("required_claim_term_groups"), f"{path}.required_claim_term_groups")
        for group_index, group in enumerate(claim_groups):
            _alias_group(group, f"{path}.required_claim_term_groups[{group_index}]")
        chapter_relations = _list(
            chapter.get("required_relation_kinds"), f"{path}.required_relation_kinds"
        )
        for relation_index, relation in enumerate(chapter_relations):
            _string(relation, f"{path}.required_relation_kinds[{relation_index}]")

    forbidden = _list(obj.get("forbidden_page_candidates"), "$baseline.forbidden_page_candidates")
    for index, name in enumerate(forbidden):
        _string(name, f"$baseline.forbidden_page_candidates[{index}]")
    return obj


def _candidate_aliases(artifact: dict, page_type: str) -> list[set[str]]:
    declared: dict[str, set[str]] = {}
    if page_type == "entity":
        for value in _list(artifact.get("entities", []), "artifact.entities"):
            entity = _object(value, "artifact.entities[]")
            name = _string(entity.get("name"), "artifact.entities[].name")
            aliases = {_normalize(name)}
            for alias in _list(entity.get("aliases", []), "artifact.entities[].aliases"):
                aliases.add(_normalize(_string(alias, "artifact.entities[].aliases[]")))
            declared[_normalize(name)] = aliases
    else:
        for value in _list(artifact.get("topics", []), "artifact.topics"):
            topic = _object(value, "artifact.topics[]")
            name = _string(topic.get("name"), "artifact.topics[].name")
            declared[_normalize(name)] = {_normalize(name)}

    result = []
    for value in _list(artifact.get("page_candidates", []), "artifact.page_candidates"):
        candidate = _object(value, "artifact.page_candidates[]")
        if candidate.get("page_type") != page_type:
            continue
        name = _string(candidate.get("name"), "artifact.page_candidates[].name")
        result.append(declared.get(_normalize(name), {_normalize(name)}))
    return result


def verify_artifacts(baseline: object, artifacts: list[object]) -> None:
    expected = validate_baseline(baseline)
    artifact_by_label: dict[str, dict] = {}
    for index, value in enumerate(artifacts):
        artifact = _object(value, f"$artifacts[{index}]")
        label = _string(artifact.get("section_label"), f"$artifacts[{index}].section_label")
        key = _normalize(label)
        if key in artifact_by_label:
            raise BaselineVerificationError(f"$artifacts[{index}]: duplicate section {label!r}")
        artifact_by_label[key] = artifact

    errors: list[str] = []
    forbidden = {_normalize(name) for name in expected["forbidden_page_candidates"]}
    for chapter_index, chapter in enumerate(expected["chapters"]):
        label = chapter["label"]
        artifact = artifact_by_label.get(_normalize(label))
        if artifact is None:
            errors.append(f"{label}: missing artifact")
            continue

        for page_type, field in (
            ("entity", "required_entity_alias_groups"),
            ("topic", "required_topic_alias_groups"),
        ):
            actual_aliases = _candidate_aliases(artifact, page_type)
            for group_index, raw_group in enumerate(chapter[field]):
                group = _alias_group(
                    raw_group,
                    f"$baseline.chapters[{chapter_index}].{field}[{group_index}]",
                )
                if not any(group & aliases for aliases in actual_aliases):
                    errors.append(
                        f"{label}: missing required {page_type} alias group "
                        f"{sorted(raw_group)!r}"
                    )

        for candidate in _list(artifact.get("page_candidates", []), "artifact.page_candidates"):
            item = _object(candidate, "artifact.page_candidates[]")
            name = _string(item.get("name"), "artifact.page_candidates[].name")
            if _normalize(name) in forbidden:
                errors.append(f"{label}: forbidden page candidate {name!r}")

        claim_texts = [
            _normalize(_string(_object(value, "artifact.claims[]").get("text"), "artifact.claims[].text"))
            for value in _list(artifact.get("claims", []), "artifact.claims")
        ]
        for group_index, raw_group in enumerate(chapter["required_claim_term_groups"]):
            terms = _alias_group(
                raw_group,
                f"$baseline.chapters[{chapter_index}].required_claim_term_groups[{group_index}]",
            )
            if not any(all(term in text for term in terms) for text in claim_texts):
                errors.append(f"{label}: missing claim term group {sorted(raw_group)!r}")

        analysis_text = " ".join(claim_texts)
        for value in _list(artifact.get("entities", []), "artifact.entities"):
            entity = _object(value, "artifact.entities[]")
            analysis_text += " " + _normalize(
                _string(entity.get("name"), "artifact.entities[].name")
            )
            analysis_text += " " + " ".join(
                _normalize(_string(alias, "artifact.entities[].aliases[]"))
                for alias in _list(entity.get("aliases", []), "artifact.entities[].aliases")
            )
        candidate_names = {
            _normalize(
                _string(
                    _object(value, "artifact.page_candidates[]").get("name"),
                    "artifact.page_candidates[].name",
                )
            )
            for value in _list(artifact.get("page_candidates", []), "artifact.page_candidates")
        }
        for concept in chapter["analysis_only_concepts"]:
            normalized_concept = _normalize(concept)
            if normalized_concept not in analysis_text:
                errors.append(f"{label}: missing analysis-only concept {concept!r}")
            if normalized_concept in candidate_names:
                errors.append(f"{label}: analysis-only concept became a page candidate {concept!r}")

        chapter_relation_kinds: set[str] = set()
        for value in _list(artifact.get("relations", []), "artifact.relations"):
            relation = _object(value, "artifact.relations[]")
            chapter_relation_kinds.add(
                _string(relation.get("rel"), "artifact.relations[].rel")
            )
        missing_relations = set(chapter["required_relation_kinds"]) - chapter_relation_kinds
        if missing_relations:
            errors.append(f"{label}: missing relation kinds {sorted(missing_relations)!r}")
    if errors:
        raise BaselineVerificationError("\n".join(errors))


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("artifacts", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        verify_artifacts(_load_json(args.baseline), [_load_json(path) for path in args.artifacts])
    except (BaselineVerificationError, OSError, json.JSONDecodeError) as exc:
        print(f"chapter-intelligence baseline failed: {exc}", file=sys.stderr)
        return 1
    print(f"chapter-intelligence baseline passed: {len(args.artifacts)} chapter artifact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
