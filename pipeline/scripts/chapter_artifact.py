"""Validation and deterministic materialization for chapter-intelligence artifacts."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from typing import Sequence


SCHEMA_VERSION = "chapter-intelligence/1"

PROMPT_VERSION = "v3"

SOURCE_QUOTE_MIN_CHARS = 20
SOURCE_QUOTE_MAX_CHARS = 240

CLAIM_KINDS = {
    "question",
    "hypothesis",
    "claim",
    "evidence",
    "mechanism",
    "definition",
    "contrast",
    "consequence",
}
RELATION_KINDS = {
    "answers",
    "supports",
    "explains",
    "causes",
    "leads-to",
    "competes-with",
    "contrasts",
    "refines",
}
CLAIM_KIND_ALIASES = {
    "answers": "claim",
    "supports": "evidence",
    "explains": "mechanism",
    "causes": "mechanism",
    "leads-to": "consequence",
    "competes-with": "contrast",
    "contrasts": "contrast",
    "refines": "claim",
}
PAGE_TYPES = {"entity", "topic"}
ENTITY_PAGE_HINTS = {"entity", "none"}

CLAIM_ID_RX = re.compile(r"^c[1-9][0-9]*$")
SHA256_RX = re.compile(r"^[0-9a-fA-F]{64}$")

_QUOTE_PUNCTUATION = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
    "\u2026": "...",
})

TOP_LEVEL_KEYS = {
    "schema",
    "source_id",
    "source_sha256",
    "text_sha256",
    "section_label",
    "prompt_version",
    "language",
    "summary",
    "central_question",
    "chapter_claim",
    "builds_on",
    "claims",
    "entities",
    "topics",
    "relations",
    "page_candidates",
    "claim_coverage",
    "open_questions",
}


class ArtifactValidationError(ValueError):
    """Raised when an analyzer response violates the artifact contract."""


def sha256_text(text: str) -> str:
    if type(text) is not str:
        raise TypeError("text must be a string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Projection field tuples shared with the renderer (build-prompt) and the
# argument-map builder (generate-mindmap). This module owns the artifact schema,
# so both consumers import these instead of respelling them.
CLAIM_PROJECTION_FIELDS = ("id", "kind", "text", "importance", "entities")
ENTITY_PROJECTION_FIELDS = (
    "name",
    "type",
    "aliases",
    "importance",
    "role",
    "claim_ids",
)

def _fail(path: str, message: str) -> None:
    raise ArtifactValidationError(f"{path}: {message}")


def _object(value: object, path: str, keys: set[str]) -> dict:
    if type(value) is not dict:
        _fail(path, "must be an object")
    obj = value
    actual = set(obj)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        details = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unexpected {extra}")
        _fail(path, "; ".join(details))
    return obj


def _list(value: object, path: str) -> list:
    if type(value) is not list:
        _fail(path, "must be an array")
    return value


def _string(
    value: object,
    path: str,
    *,
    max_length: int,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        _fail(path, "must be a string")
    if not allow_empty and not value.strip():
        _fail(path, "must not be empty")
    if len(value) > max_length:
        _fail(path, f"must be at most {max_length} characters")
    return value


def _integer(value: object, path: str) -> int:
    if type(value) is not int:
        _fail(path, "must be an integer")
    return value


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _importance(value: object, path: str) -> int:
    number = _integer(value, path)
    if not 1 <= number <= 5:
        _fail(path, "must be between 1 and 5")
    return number


def _string_list(
    value: object,
    path: str,
    *,
    max_items: int | None = None,
    max_length: int = 512,
    allow_empty: bool = True,
) -> list[str]:
    items = _list(value, path)
    if not allow_empty and not items:
        _fail(path, "must not be empty")
    if max_items is not None and len(items) > max_items:
        _fail(path, f"must contain at most {max_items} items")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        string = _string(item, f"{path}[{index}]", max_length=max_length)
        if string in seen:
            _fail(f"{path}[{index}]", f"duplicates {string!r}")
        seen.add(string)
        result.append(string)
    return result


def _hash(value: object, path: str) -> str:
    string = _string(value, path, max_length=64)
    if not SHA256_RX.fullmatch(string):
        _fail(path, "must be a 64-character SHA-256 hex digest")
    return string


def normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _quote_match_form(value: str) -> tuple[str, list[int]]:
    """Normalize formatting-only quote differences and retain source offsets."""
    characters: list[str] = []
    offsets: list[int] = []
    source_index = 0
    while source_index < len(value):
        source_character = value[source_index]
        if source_character in {"*", "_"}:
            run_end = source_index + 1
            while run_end < len(value) and value[run_end] == source_character:
                run_end += 1
            # EPUB-to-Markdown conversion can split emphasized words into
            # adjacent bold runs (`**只****可****能**`). Ignore only multi-char
            # emphasis delimiters; a literal single `*`/`_` remains evidence.
            if run_end - source_index >= 2:
                source_index = run_end
                continue
        expanded = unicodedata.normalize("NFKC", source_character).translate(
            _QUOTE_PUNCTUATION
        )
        for character in expanded:
            if character.isspace():
                if characters and characters[-1] != " ":
                    characters.append(" ")
                    offsets.append(source_index)
                continue
            characters.append(character)
            offsets.append(source_index)
        source_index += 1
    if characters and characters[-1] == " ":
        characters.pop()
        offsets.pop()
    return "".join(characters), offsets


def _format_equivalent_quote_occurrences(text: str, quote: str) -> list[tuple[int, int]]:
    normalized_text, offsets = _quote_match_form(text)
    normalized_quote, _ = _quote_match_form(quote)
    if not normalized_quote:
        return []
    occurrences: list[tuple[int, int]] = []
    cursor = 0
    while True:
        position = normalized_text.find(normalized_quote, cursor)
        if position < 0:
            break
        end_position = position + len(normalized_quote) - 1
        occurrences.append((offsets[position], offsets[end_position] + 1))
        cursor = position + 1
    return occurrences


def _claim_references(
    value: object,
    path: str,
    claim_ids: set[str],
) -> list[str]:
    references = _string_list(
        value, path, max_length=64, allow_empty=False
    )
    for index, claim_id in enumerate(references):
        if claim_id not in claim_ids:
            _fail(f"{path}[{index}]", f"references unknown claim {claim_id!r}")
    return references


def materialize_source_spans(artifact: object, text: str) -> dict:
    """Resolve quote-first model spans to canonical exact character offsets.

    Raw spans may contain only ``quote`` or may also carry approximate integer
    ``start``/``end`` values. A unique exact quote occurrence wins regardless of
    approximate bounds. Valid exact bounds can select a repeated occurrence;
    otherwise the first identical occurrence is chosen deterministically.
    Exact short quotes are expanded with adjacent source text to the schema
    minimum instead of spending another model call on a clerical length error.
    """
    _string(text, "text", max_length=max(len(text), 1), allow_empty=True)
    if type(artifact) is not dict:
        _fail("$", "must be an object")
    result = copy.deepcopy(artifact)
    claims = _list(result.get("claims"), "$.claims")
    for claim_index, claim in enumerate(claims):
        claim_path = f"$.claims[{claim_index}]"
        if type(claim) is not dict:
            _fail(claim_path, "must be an object")
        spans = _list(claim.get("source_spans"), f"{claim_path}.source_spans")
        canonical: list[dict] = []
        seen_offsets: set[tuple[int, int]] = set()
        for span_index, span in enumerate(spans):
            span_path = f"{claim_path}.source_spans[{span_index}]"
            if type(span) is not dict:
                _fail(span_path, "must be an object")
            keys = set(span)
            if keys not in ({"quote"}, {"quote", "start", "end"}):
                _fail(
                    span_path,
                    "must contain quote, with optional paired start and end",
                )
            quote = _string(
                span["quote"],
                f"{span_path}.quote",
                max_length=SOURCE_QUOTE_MAX_CHARS,
            )
            supplied_bounds = "start" in span
            if supplied_bounds:
                supplied_start = _integer(span["start"], f"{span_path}.start")
                supplied_end = _integer(span["end"], f"{span_path}.end")
            else:
                supplied_start = supplied_end = -1

            occurrences: list[tuple[int, int]] = []
            cursor = 0
            while True:
                position = text.find(quote, cursor)
                if position < 0:
                    break
                occurrences.append((position, position + len(quote)))
                cursor = position + 1
            if not occurrences:
                occurrences = _format_equivalent_quote_occurrences(text, quote)
            if not occurrences:
                _fail(
                    f"{span_path}.quote",
                    "does not occur in extracted text (including formatting-equivalent matching)",
                )
            if (
                len(occurrences) > 1
                and supplied_bounds
                and (supplied_start, supplied_end) in occurrences
            ):
                start, end = supplied_start, supplied_end
            else:
                start, end = occurrences[0]
            if end - start < SOURCE_QUOTE_MIN_CHARS:
                missing = SOURCE_QUOTE_MIN_CHARS - (end - start)
                start = max(0, start - missing // 2)
                end = min(len(text), start + SOURCE_QUOTE_MIN_CHARS)
                start = max(0, end - SOURCE_QUOTE_MIN_CHARS)
            canonical_quote = text[start:end]
            if len(canonical_quote) < SOURCE_QUOTE_MIN_CHARS:
                _fail(
                    f"{span_path}.quote",
                    f"must contain at least {SOURCE_QUOTE_MIN_CHARS} characters",
                )
            key = (start, end)
            if key in seen_offsets:
                _fail(span_path, "duplicates an earlier canonical source span")
            seen_offsets.add(key)
            canonical.append(
                {"start": start, "end": end, "quote": canonical_quote}
            )
        claim["source_spans"] = canonical
    return result


def materialize_entity_references(artifact: object) -> dict:
    """Complete the entity inventory from claim-level references.

    Claim/entity links are useful retrieval metadata, but requiring a second
    full-source completion because the model omitted a clerical inventory row
    is wasteful. Missing rows are derived conservatively from the claims that
    named them. This never creates a page candidate; editorial selection remains
    explicit in ``page_candidates``.
    """
    if type(artifact) is not dict:
        _fail("$", "must be an object")
    result = copy.deepcopy(artifact)
    claims = _list(result.get("claims"), "$.claims")
    entities = _list(result.get("entities"), "$.entities")

    declared: dict[str, dict] = {}
    for index, raw in enumerate(entities):
        path = f"$.entities[{index}]"
        if type(raw) is not dict:
            _fail(path, "must be an object")
        name = raw.get("name")
        if type(name) is not str or not name.strip():
            _fail(f"{path}.name", "must be a non-empty string")
        key = normalized_name(name)
        if key in declared:
            _fail(f"{path}.name", f"duplicates entity {name!r}")
        declared[key] = raw

    missing: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(claims):
        path = f"$.claims[{index}]"
        if type(raw) is not dict:
            _fail(path, "must be an object")
        claim_id = raw.get("id")
        text = raw.get("text")
        importance = raw.get("importance")
        references = raw.get("entities")
        if type(claim_id) is not str or not claim_id:
            _fail(f"{path}.id", "must be a non-empty string")
        if type(text) is not str or not text.strip():
            _fail(f"{path}.text", "must be a non-empty string")
        if type(importance) is not int or not 1 <= importance <= 5:
            _fail(f"{path}.importance", "must be an integer from 1 through 5")
        if type(references) is not list:
            _fail(f"{path}.entities", "must be an array")
        for reference_index, name in enumerate(references):
            if type(name) is not str or not name.strip():
                _fail(
                    f"{path}.entities[{reference_index}]",
                    "must be a non-empty string",
                )
            key = normalized_name(name)
            entity = declared.get(key)
            if entity is not None:
                claim_ids = entity.get("claim_ids")
                if type(claim_ids) is not list:
                    _fail("$.entities[].claim_ids", "must be an array")
                if claim_id not in claim_ids:
                    claim_ids.append(claim_id)
                continue
            row = missing.setdefault(
                key,
                {
                    "name": name,
                    "importance": importance,
                    "claim_ids": [],
                    "roles": [],
                },
            )
            row["importance"] = max(int(row["importance"]), importance)
            if claim_id not in row["claim_ids"]:
                row["claim_ids"].append(claim_id)
            if text not in row["roles"]:
                row["roles"].append(text)

    for row in missing.values():
        role = "；".join(row.pop("roles"))[:1000]
        entities.append({
            "name": row["name"],
            "type": "concept",
            "aliases": [],
            "importance": row["importance"],
            "role": role,
            "page_hint": "none",
            "claim_ids": row["claim_ids"],
        })
    return result


def materialize_unique_aliases(artifact: object) -> dict:
    """Assign each normalized surface form to at most one planned page.

    Canonical entity/topic names always own their surface form. When multiple
    entities declare the same non-canonical alias, the stronger page candidate
    keeps it; the others lose only that redundant alias. This is identity
    bookkeeping, not an editorial page decision.
    """
    if type(artifact) is not dict:
        _fail("$", "must be an object")
    result = copy.deepcopy(artifact)
    entities = _list(result.get("entities"), "$.entities")
    topics = _list(result.get("topics"), "$.topics")
    candidates = _list(result.get("page_candidates"), "$.page_candidates")

    candidate_strength: dict[str, tuple[int, int, int]] = {}
    for index, raw in enumerate(candidates):
        path = f"$.page_candidates[{index}]"
        if type(raw) is not dict:
            _fail(path, "must be an object")
        if raw.get("page_type") != "entity":
            continue
        name = _string(raw.get("name"), f"{path}.name", max_length=512)
        importance = _integer(raw.get("importance"), f"{path}.importance")
        required = _boolean(raw.get("required"), f"{path}.required")
        claim_ids = _list(raw.get("claim_ids"), f"{path}.claim_ids")
        candidate_strength[normalized_name(name)] = (
            int(required), importance, len(claim_ids)
        )

    # The entity inventory includes context-only records (page_hint=none), so
    # only entity page candidates reserve an H1. Every topic is a page candidate
    # by contract and therefore reserves its canonical name.
    canonical: dict[str, str] = {}
    for collection_name, records in (("entities", entities), ("topics", topics)):
        for index, raw in enumerate(records):
            path = f"$.{collection_name}[{index}]"
            if type(raw) is not dict:
                _fail(path, "must be an object")
            name = _string(raw.get("name"), f"{path}.name", max_length=512)
            key = normalized_name(name)
            if collection_name == "entities" and key not in candidate_strength:
                continue
            previous = canonical.get(key)
            if previous is not None:
                _fail(f"{path}.name", f"duplicates wiki identity owned by {previous}")
            page_type = "entity" if collection_name == "entities" else "topic"
            canonical[key] = f"{page_type}:{name}"

    alias_claimants: dict[str, list[int]] = {}
    normalized_aliases: list[list[tuple[str, str]]] = []
    for index, raw in enumerate(entities):
        path = f"$.entities[{index}]"
        aliases = _list(raw.get("aliases"), f"{path}.aliases")
        rows: list[tuple[str, str]] = []
        seen: set[str] = set()
        for alias_index, value in enumerate(aliases):
            alias = _string(
                value, f"{path}.aliases[{alias_index}]", max_length=512
            )
            key = normalized_name(alias)
            if key in seen:
                continue
            seen.add(key)
            rows.append((key, alias))
            entity_name = normalized_name(raw.get("name", ""))
            if entity_name in candidate_strength and key not in canonical:
                alias_claimants.setdefault(key, []).append(index)
        normalized_aliases.append(rows)

    alias_owners: dict[str, int] = {}
    for key, claimants in alias_claimants.items():
        alias_owners[key] = max(
            claimants,
            key=lambda index: (
                candidate_strength.get(
                    normalized_name(entities[index]["name"]), (0, 0, 0)
                ),
                _integer(entities[index].get("importance"),
                         f"$.entities[{index}].importance"),
                len(_list(entities[index].get("claim_ids"),
                          f"$.entities[{index}].claim_ids")),
                -index,
            ),
        )

    for index, raw in enumerate(entities):
        entity_name = normalized_name(raw["name"])
        if entity_name not in candidate_strength:
            raw["aliases"] = [alias for _key, alias in normalized_aliases[index]]
            continue
        raw["aliases"] = [
            alias
            for key, alias in normalized_aliases[index]
            if key not in canonical and alias_owners.get(key) == index
        ]
    return result


def materialize_claim_coverage(artifact: object) -> dict:
    """Canonicalize reverse claim coverage from page-candidate claim ids.

    The model chooses claims, page candidates, and each candidate's claim ids.
    Repeating that same many-to-many relation in ``claim_coverage`` is clerical
    JSON work, so derive covered references deterministically. Claims with no
    page candidate still retain and require the model's explicit skip reason.
    """
    if type(artifact) is not dict:
        _fail("$", "must be an object")
    result = copy.deepcopy(artifact)
    claims = _list(result.get("claims"), "$.claims")
    candidates = _list(result.get("page_candidates"), "$.page_candidates")
    coverage = _list(result.get("claim_coverage"), "$.claim_coverage")

    existing: dict[str, object] = {}
    for index, raw in enumerate(coverage):
        path = f"$.claim_coverage[{index}]"
        if type(raw) is not dict:
            _fail(path, "must be an object")
        claim_id = raw.get("claim_id")
        if type(claim_id) is not str or not claim_id:
            _fail(f"{path}.claim_id", "must be a non-empty string")
        if claim_id in existing:
            _fail(f"{path}.claim_id", f"duplicates coverage for {claim_id!r}")
        existing[claim_id] = raw.get("skip_reason")

    references: dict[str, list[dict[str, str]]] = {}
    for index, raw in enumerate(candidates):
        path = f"$.page_candidates[{index}]"
        if type(raw) is not dict:
            _fail(path, "must be an object")
        page_type = raw.get("page_type")
        name = raw.get("name")
        claim_ids = raw.get("claim_ids")
        if type(page_type) is not str or type(name) is not str or type(claim_ids) is not list:
            _fail(path, "needs string page_type/name and array claim_ids")
        reference = {"page_type": page_type, "name": name}
        for claim_id in claim_ids:
            if type(claim_id) is not str:
                _fail(f"{path}.claim_ids", "must contain strings")
            references.setdefault(claim_id, []).append(reference)

    canonical: list[dict] = []
    for index, raw in enumerate(claims):
        path = f"$.claims[{index}]"
        if type(raw) is not dict or type(raw.get("id")) is not str:
            _fail(path, "must be an object with a string id")
        claim_id = raw["id"]
        refs = references.get(claim_id, [])
        canonical.append({
            "claim_id": claim_id,
            "page_candidates": refs,
            "skip_reason": None if refs else existing.get(claim_id),
        })
    result["claim_coverage"] = canonical
    return result


def materialize_page_hints(artifact: object) -> dict:
    """Derive entity page hints from the authoritative page-candidate list."""
    if type(artifact) is not dict:
        _fail("$", "must be an object")
    result = copy.deepcopy(artifact)
    entities = _list(result.get("entities"), "$.entities")
    candidates = _list(result.get("page_candidates"), "$.page_candidates")
    entity_candidates: set[str] = set()
    for index, raw in enumerate(candidates):
        path = f"$.page_candidates[{index}]"
        if type(raw) is not dict:
            _fail(path, "must be an object")
        if raw.get("page_type") == "entity":
            name = raw.get("name")
            if type(name) is not str or not name.strip():
                _fail(f"{path}.name", "must be a non-empty string")
            entity_candidates.add(normalized_name(name))
    for index, raw in enumerate(entities):
        path = f"$.entities[{index}]"
        if type(raw) is not dict:
            _fail(path, "must be an object")
        name = raw.get("name")
        if type(name) is not str or not name.strip():
            _fail(f"{path}.name", "must be a non-empty string")
        raw["page_hint"] = (
            "entity" if normalized_name(name) in entity_candidates else "none"
        )
    return result


def materialize_response(
    raw: str,
    text: str,
    *,
    source_id_override: str | None = None,
) -> dict:
    """Parse and canonicalize one analyzer response before strict validation."""
    artifact = extract_json_object(raw)
    if source_id_override is not None:
        artifact["source_id"] = source_id_override
    claims = artifact.get("claims")
    if type(claims) is list:
        for claim in claims:
            if type(claim) is dict:
                kind = claim.get("kind")
                claim["kind"] = CLAIM_KIND_ALIASES.get(kind, kind)
    return materialize_claim_coverage(
        materialize_page_hints(
            materialize_unique_aliases(
                materialize_entity_references(
                    materialize_source_spans(artifact, text)
                )
            )
        )
    )


def validate_artifact(
    artifact: object,
    *,
    text: str | None,
    source_id: str,
    source_sha256: str,
    section_label: str,
    prompt_version: str = PROMPT_VERSION,
    ordered_sections: Sequence[str] = (),
    _expected_text_sha256: str | None = None,
) -> dict:
    """Strictly validate one `chapter-intelligence/1` artifact.

    Validation never coerces values or drops malformed records. The returned
    object is the original dictionary after all scalar, span, enum, and
    reference checks have passed.
    """
    if text is not None:
        _string(text, "text", max_length=max(len(text), 1), allow_empty=True)
        expected_text_sha = sha256_text(text)
    else:
        expected_text_sha = _hash(
            _expected_text_sha256, "expected.text_sha256"
        )
    expected_source_id = _string(source_id, "expected.source_id", max_length=256)
    expected_source_sha = _hash(source_sha256, "expected.source_sha256")
    expected_section = _string(
        section_label,
        "expected.section_label",
        max_length=1000,
        allow_empty=True,
    )
    expected_prompt = _string(
        prompt_version, "expected.prompt_version", max_length=128
    )

    obj = _object(artifact, "$", TOP_LEVEL_KEYS)
    fixed_values = {
        "schema": SCHEMA_VERSION,
        "source_id": expected_source_id,
        "source_sha256": expected_source_sha,
        "text_sha256": expected_text_sha,
        "section_label": expected_section,
        "prompt_version": expected_prompt,
    }
    for field, expected in fixed_values.items():
        actual = _string(
            obj[field],
            f"$.{field}",
            max_length=max(1000, len(expected)),
            allow_empty=field == "section_label",
        )
        if actual != expected:
            _fail(f"$.{field}", f"expected {expected!r}, got {actual!r}")

    _string(obj["language"], "$.language", max_length=32)
    _string(obj["summary"], "$.summary", max_length=4000)
    _string(obj["central_question"], "$.central_question", max_length=4000)
    _string(obj["chapter_claim"], "$.chapter_claim", max_length=8000)
    if obj["builds_on"] is not None:
        _string(obj["builds_on"], "$.builds_on", max_length=4000)

    claims = _list(obj["claims"], "$.claims")
    if not claims:
        _fail("$.claims", "must contain at least one claim")
    claim_ids: set[str] = set()
    claim_entity_references: list[tuple[str, str]] = []
    for index, value in enumerate(claims):
        path = f"$.claims[{index}]"
        claim = _object(
            value,
            path,
            {"id", "kind", "text", "importance", "source_spans", "entities"},
        )
        claim_id = _string(claim["id"], f"{path}.id", max_length=64)
        if not CLAIM_ID_RX.fullmatch(claim_id):
            _fail(f"{path}.id", "must match c[1-9][0-9]*")
        if claim_id in claim_ids:
            _fail(f"{path}.id", f"duplicates claim id {claim_id!r}")
        claim_ids.add(claim_id)
        kind = _string(claim["kind"], f"{path}.kind", max_length=32)
        if kind not in CLAIM_KINDS:
            _fail(f"{path}.kind", f"unsupported claim kind {kind!r}")
        _string(claim["text"], f"{path}.text", max_length=10000)
        importance = _importance(claim["importance"], f"{path}.importance")

        spans = _list(claim["source_spans"], f"{path}.source_spans")
        for span_index, span_value in enumerate(spans):
            span_path = f"{path}.source_spans[{span_index}]"
            span = _object(span_value, span_path, {"start", "end", "quote"})
            start = _integer(span["start"], f"{span_path}.start")
            end = _integer(span["end"], f"{span_path}.end")
            quote = _string(
                span["quote"],
                f"{span_path}.quote",
                max_length=SOURCE_QUOTE_MAX_CHARS,
            )
            if len(quote) < SOURCE_QUOTE_MIN_CHARS:
                _fail(
                    f"{span_path}.quote",
                    f"must contain at least {SOURCE_QUOTE_MIN_CHARS} characters",
                )
            if start < 0 or end <= start:
                _fail(
                    span_path,
                    f"span [{start}, {end}) has invalid bounds",
                )
            if text is not None:
                if end > len(text):
                    _fail(
                        span_path,
                        f"span [{start}, {end}) is outside text length {len(text)}",
                    )
                excerpt = text[start:end]
                if quote != excerpt:
                    _fail(
                        f"{span_path}.quote",
                        "does not match the extracted text slice",
                    )

        if importance >= 4 and not spans:
            _fail(
                f"{path}.source_spans",
                "importance 4-5 claims require at least one validated source span",
            )

        names = _string_list(
            claim["entities"], f"{path}.entities", max_length=512
        )
        claim_entity_references.extend((f"{path}.entities", name) for name in names)

    forbidden_names = {normalized_name(expected_section)}
    for index, section in enumerate(ordered_sections):
        section_string = _string(
            section,
            f"ordered_sections[{index}]",
            max_length=1000,
            allow_empty=True,
        )
        forbidden_names.add(normalized_name(section_string))

    identity_owners: dict[str, str] = {}

    def reserve_identity(value: str, path: str, owner: str) -> None:
        key = normalized_name(value)
        previous = identity_owners.get(key)
        if previous is not None:
            _fail(path, f"duplicates wiki identity owned by {previous}")
        identity_owners[key] = owner

    entities = _list(obj["entities"], "$.entities")
    entity_names: set[str] = set()
    entity_page_hints: dict[str, str] = {}
    for index, value in enumerate(entities):
        path = f"$.entities[{index}]"
        entity = _object(
            value,
            path,
            {"name", "type", "aliases", "importance", "role", "page_hint", "claim_ids"},
        )
        name = _string(entity["name"], f"{path}.name", max_length=512)
        normalized = normalized_name(name)
        if normalized in entity_names:
            _fail(f"{path}.name", f"duplicates entity name {name!r}")
        if normalized in forbidden_names:
            _fail(f"{path}.name", "must be a reusable concept, not a section label")
        entity_names.add(normalized)
        owner = f"entity:{name}"
        _string(entity["type"], f"{path}.type", max_length=128)
        aliases = _string_list(
            entity["aliases"], f"{path}.aliases", max_items=100, max_length=512
        )
        alias_names = {normalized_name(alias) for alias in aliases}
        if len(alias_names) != len(aliases):
            _fail(f"{path}.aliases", "contains aliases that differ only by case or spacing")
        _importance(entity["importance"], f"{path}.importance")
        _string(entity["role"], f"{path}.role", max_length=4000)
        page_hint = _string(entity["page_hint"], f"{path}.page_hint", max_length=16)
        if page_hint not in ENTITY_PAGE_HINTS:
            _fail(f"{path}.page_hint", f"must be one of {sorted(ENTITY_PAGE_HINTS)}")
        if page_hint == "entity":
            reserve_identity(name, f"{path}.name", owner)
            for alias_index, alias in enumerate(aliases):
                reserve_identity(alias, f"{path}.aliases[{alias_index}]", owner)
        entity_page_hints[normalized] = page_hint
        _claim_references(entity["claim_ids"], f"{path}.claim_ids", claim_ids)

    for path, entity_name in claim_entity_references:
        if normalized_name(entity_name) not in entity_names:
            _fail(path, f"references undeclared entity {entity_name!r}")

    topics = _list(obj["topics"], "$.topics")
    topic_names: set[str] = set()
    for index, value in enumerate(topics):
        path = f"$.topics[{index}]"
        topic = _object(
            value,
            path,
            {"name", "question", "synthesis_angle", "importance", "claim_ids"},
        )
        name = _string(topic["name"], f"{path}.name", max_length=512)
        normalized = normalized_name(name)
        if normalized in topic_names:
            _fail(f"{path}.name", f"duplicates topic name {name!r}")
        if normalized in forbidden_names:
            _fail(f"{path}.name", "must be a reusable concept, not a section label")
        topic_names.add(normalized)
        reserve_identity(name, f"{path}.name", f"topic:{name}")
        _string(topic["question"], f"{path}.question", max_length=4000)
        _string(topic["synthesis_angle"], f"{path}.synthesis_angle", max_length=4000)
        _importance(topic["importance"], f"{path}.importance")
        _claim_references(topic["claim_ids"], f"{path}.claim_ids", claim_ids)

    if not entities and not topics:
        _fail("$", "must contain usable entity or topic coverage")

    relations = _list(obj["relations"], "$.relations")
    relation_keys: set[tuple[str, str, str]] = set()
    for index, value in enumerate(relations):
        path = f"$.relations[{index}]"
        relation = _object(value, path, {"from", "to", "rel"})
        source = _string(relation["from"], f"{path}.from", max_length=64)
        target = _string(relation["to"], f"{path}.to", max_length=64)
        rel = _string(relation["rel"], f"{path}.rel", max_length=32)
        if source not in claim_ids:
            _fail(f"{path}.from", f"references unknown claim {source!r}")
        if target not in claim_ids:
            _fail(f"{path}.to", f"references unknown claim {target!r}")
        if source == target:
            _fail(path, "relation endpoints must be distinct")
        if rel not in RELATION_KINDS:
            _fail(f"{path}.rel", f"unsupported relation {rel!r}")
        key = (source, target, rel)
        if key in relation_keys:
            _fail(path, "duplicates an earlier relation")
        relation_keys.add(key)

    candidates = _list(obj["page_candidates"], "$.page_candidates")
    candidate_keys: set[tuple[str, str]] = set()
    candidate_claims: dict[tuple[str, str], set[str]] = {}
    for index, value in enumerate(candidates):
        path = f"$.page_candidates[{index}]"
        candidate = _object(
            value,
            path,
            {
                "page_type",
                "name",
                "importance",
                "required",
                "claim_ids",
                "reason",
            },
        )
        page_type = _string(candidate["page_type"], f"{path}.page_type", max_length=16)
        if page_type not in PAGE_TYPES:
            _fail(f"{path}.page_type", f"must be one of {sorted(PAGE_TYPES)}")
        name = _string(candidate["name"], f"{path}.name", max_length=512)
        normalized = normalized_name(name)
        declared = entity_names if page_type == "entity" else topic_names
        if normalized not in declared:
            _fail(f"{path}.name", f"does not name a declared {page_type}")
        key = (page_type, normalized)
        if key in candidate_keys:
            _fail(path, f"duplicates page candidate {page_type}:{name}")
        candidate_keys.add(key)
        importance = _importance(candidate["importance"], f"{path}.importance")
        required = _boolean(candidate["required"], f"{path}.required")
        if importance >= 4 and not required:
            _fail(
                f"{path}.required",
                "must be true for importance 4-5 page candidates",
            )
        refs = _claim_references(
            candidate["claim_ids"], f"{path}.claim_ids", claim_ids
        )
        candidate_claims[key] = set(refs)
        _string(candidate["reason"], f"{path}.reason", max_length=4000)

    coverage = _list(obj["claim_coverage"], "$.claim_coverage")
    covered_claim_ids: set[str] = set()
    coverage_pairs: set[tuple[str, tuple[str, str]]] = set()
    for index, value in enumerate(coverage):
        path = f"$.claim_coverage[{index}]"
        item = _object(value, path, {"claim_id", "page_candidates", "skip_reason"})
        claim_id = _string(item["claim_id"], f"{path}.claim_id", max_length=64)
        if claim_id not in claim_ids:
            _fail(f"{path}.claim_id", f"references unknown claim {claim_id!r}")
        if claim_id in covered_claim_ids:
            _fail(f"{path}.claim_id", f"duplicates coverage for {claim_id!r}")
        covered_claim_ids.add(claim_id)

        references = _list(item["page_candidates"], f"{path}.page_candidates")
        seen_references: set[tuple[str, str]] = set()
        for ref_index, ref_value in enumerate(references):
            ref_path = f"{path}.page_candidates[{ref_index}]"
            reference = _object(ref_value, ref_path, {"page_type", "name"})
            page_type = _string(
                reference["page_type"], f"{ref_path}.page_type", max_length=16
            )
            name = _string(reference["name"], f"{ref_path}.name", max_length=512)
            key = (page_type, normalized_name(name))
            if key not in candidate_keys:
                _fail(ref_path, f"references unknown page candidate {page_type}:{name}")
            if key in seen_references:
                _fail(ref_path, "duplicates a page candidate reference")
            if claim_id not in candidate_claims[key]:
                _fail(ref_path, f"page candidate does not declare claim {claim_id!r}")
            seen_references.add(key)
            coverage_pairs.add((claim_id, key))

        skip_reason = item["skip_reason"]
        if references:
            if skip_reason is not None:
                _fail(f"{path}.skip_reason", "must be null when page candidates cover the claim")
        else:
            _string(skip_reason, f"{path}.skip_reason", max_length=4000)

    missing_coverage = sorted(claim_ids - covered_claim_ids)
    if missing_coverage:
        _fail("$.claim_coverage", f"missing claims {missing_coverage}")

    for candidate_key, references in candidate_claims.items():
        for claim_id in references:
            if (claim_id, candidate_key) not in coverage_pairs:
                _fail(
                    "$.claim_coverage",
                    "missing reverse coverage for "
                    f"{claim_id!r} and {candidate_key[0]}:{candidate_key[1]}",
                )

    for entity_name, page_hint in entity_page_hints.items():
        key = ("entity", entity_name)
        if page_hint == "entity" and key not in candidate_keys:
            _fail(
                "$.page_candidates",
                f"entity {entity_name!r} has page_hint=entity but no page candidate",
            )
        if page_hint == "none" and key in candidate_keys:
            _fail(
                "$.page_candidates",
                f"entity {entity_name!r} has page_hint=none but is a page candidate",
            )
    for topic_name in topic_names:
        if ("topic", topic_name) not in candidate_keys:
            _fail(
                "$.page_candidates",
                f"topic {topic_name!r} has no page candidate",
            )

    _string_list(
        obj["open_questions"], "$.open_questions", max_items=1000, max_length=4000
    )
    return obj


def extract_json_object(raw: str) -> dict:
    """Extract one JSON object from a plain or fenced completion response."""
    if type(raw) is not str or not raw.strip():
        raise ValueError("LLM returned no text")
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw):
        try:
            value, _ = decoder.raw_decode(raw[match.start() :])
        except json.JSONDecodeError:
            continue
        if type(value) is dict:
            return value
    raise ValueError("no parseable JSON object in LLM output")
