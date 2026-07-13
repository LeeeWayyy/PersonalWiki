"""Standalone chapter-intelligence extraction, validation, and caching.

The artifact is derived data. Source text remains the evidence authority; this
module only records a validated, reusable analysis of one extracted section.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Callable, Mapping, Sequence

import llm_client


SCHEMA_VERSION = "chapter-intelligence/1"
CACHE_MANIFEST_SCHEMA = "chapter-intelligence-cache-entry/1"
PROMPT_VERSION = "v3"
DEFAULT_TIMEOUT_S = 1800
SOURCE_QUOTE_MIN_CHARS = 20
SOURCE_QUOTE_MAX_CHARS = 240
DEFAULT_SCHEMA_INGEST_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "schema-ingest.md"
)
ANALYZER_SCHEMA_SECTIONS = (
    "Page Selection And Coverage",
    "Page Types",
    "Voice And Attribution",
    "Language And Naming",
)

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
SAFE_COMPONENT_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

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


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def json_digest(value: object) -> str:
    return sha256_text(canonical_json(value))


def select_analyzer_schema_rules(text: str) -> str:
    """Select only schema blocks that affect analyzer page planning.

    Headerless rule packs remain supported for focused tests and private
    deployments. The production rule pack is block-structured and fails closed
    if a required analyzer block is renamed or removed.
    """
    if type(text) is not str or not text.strip():
        raise ValueError("schema ingest rules must not be empty")
    preamble: list[str] = []
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            blocks[current] = [line]
        elif current is None:
            preamble.append(line)
        else:
            blocks[current].append(line)
    if not blocks:
        return text.rstrip() + "\n"
    missing = [name for name in ANALYZER_SCHEMA_SECTIONS if name not in blocks]
    if missing:
        raise ValueError(
            "schema ingest rules missing analyzer section(s): " + ", ".join(missing)
        )
    selected = "\n".join(preamble).rstrip() + "\n\n"
    selected += "\n".join(
        "\n".join(blocks[name]).rstrip() + "\n"
        for name in ANALYZER_SCHEMA_SECTIONS
    )
    return selected


def selected_schema_rules(path: str | Path = DEFAULT_SCHEMA_INGEST_PATH) -> str:
    return select_analyzer_schema_rules(Path(path).read_text(encoding="utf-8"))


def selected_schema_digest(path: str | Path = DEFAULT_SCHEMA_INGEST_PATH) -> str:
    return sha256_text(selected_schema_rules(path))


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


MODEL_IDENTITY_FIELDS = (
    "provider",
    "model",
    "reasoning",
    "verbosity",
    "api_base_url",
    "command_fingerprint",
    "codex_binary_fingerprint",
    "codex_config_fingerprint",
    "codex_automation_fingerprint",
)


def canonical_model_identity(identity: Mapping[str, object]) -> dict[str, str | None]:
    """Validate and complete the analyzer's non-secret execution identity."""
    if not isinstance(identity, Mapping) or isinstance(identity, (str, bytes)):
        raise ValueError("model_identity must be an object")
    unknown = set(identity) - set(MODEL_IDENTITY_FIELDS)
    if unknown or not {"provider", "model"}.issubset(identity):
        raise ValueError(
            "model_identity must contain provider/model and only supported "
            "execution fields"
        )
    result: dict[str, str | None] = {}
    for field in MODEL_IDENTITY_FIELDS:
        value = identity.get(field)
        if value is not None and (type(value) is not str or not value.strip()):
            raise ValueError(f"model_identity.{field} must be a non-empty string or null")
        max_length = 2048 if field == "api_base_url" else 256
        if isinstance(value, str) and len(value) > max_length:
            raise ValueError(f"model_identity.{field} is too long")
        result[field] = value
    return result


def _validated_prior_chapters(prior_chapters: Sequence[Mapping[str, object]]) -> list[dict]:
    result: list[dict] = []
    for index, item in enumerate(prior_chapters):
        path = f"prior_chapters[{index}]"
        chapter = _object(
            item, path, {"section_label", "central_question", "chapter_claim"}
        )
        result.append(
            {
                "section_label": _string(
                    chapter["section_label"],
                    f"{path}.section_label",
                    max_length=1000,
                    allow_empty=True,
                ),
                "central_question": _string(
                    chapter["central_question"],
                    f"{path}.central_question",
                    max_length=4000,
                ),
                "chapter_claim": _string(
                    chapter["chapter_claim"], f"{path}.chapter_claim", max_length=8000
                ),
            }
        )
    return result


def analysis_context(
    *,
    ordered_sections: Sequence[str],
    prior_chapters: Sequence[Mapping[str, object]],
) -> dict:
    """Return the canonical prompt context shared by rendering and caching."""
    return {
        "ordered_sections": [
            _string(
                value,
                f"ordered_sections[{index}]",
                max_length=1000,
                allow_empty=True,
            )
            for index, value in enumerate(ordered_sections)
        ],
        "previous_chapter_spine": _validated_prior_chapters(prior_chapters),
    }


def cache_inputs(
    *,
    source_sha256: str,
    text_sha256: str,
    section_label: str,
    prompt_version: str,
    model_identity: Mapping[str, object],
    schema_ingest_sha256: str,
    ordered_sections: Sequence[str],
    prior_chapters: Sequence[Mapping[str, object]],
    prompt_template_sha256: str,
) -> dict:
    """Canonicalize every input that can change the analyzer prompt."""
    _hash(source_sha256, "source_sha256")
    _hash(text_sha256, "text_sha256")
    _string(section_label, "section_label", max_length=1000, allow_empty=True)
    _string(prompt_version, "prompt_version", max_length=128)
    _hash(schema_ingest_sha256, "schema_ingest_sha256")
    _hash(prompt_template_sha256, "prompt_template_sha256")
    return {
        "artifact_schema": SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "text_sha256": text_sha256,
        "section_label": section_label,
        "prompt_version": prompt_version,
        "model_identity": canonical_model_identity(model_identity),
        "schema_ingest_sha256": schema_ingest_sha256,
        "analysis_context": analysis_context(
            ordered_sections=ordered_sections,
            prior_chapters=prior_chapters,
        ),
        "prompt_template_sha256": prompt_template_sha256,
    }


def canonical_cache_inputs(value: object) -> dict:
    """Validate and canonicalize cache inputs loaded from a manifest."""
    obj = _object(
        value,
        "$.cache_inputs",
        {
            "artifact_schema",
            "source_sha256",
            "text_sha256",
            "section_label",
            "prompt_version",
            "model_identity",
            "schema_ingest_sha256",
            "analysis_context",
            "prompt_template_sha256",
        },
    )
    if obj["artifact_schema"] != SCHEMA_VERSION:
        _fail("$.cache_inputs.artifact_schema", "is unsupported")
    context = _object(
        obj["analysis_context"],
        "$.cache_inputs.analysis_context",
        {"ordered_sections", "previous_chapter_spine"},
    )
    canonical = cache_inputs(
        source_sha256=obj["source_sha256"],
        text_sha256=obj["text_sha256"],
        section_label=obj["section_label"],
        prompt_version=obj["prompt_version"],
        model_identity=obj["model_identity"],
        schema_ingest_sha256=obj["schema_ingest_sha256"],
        ordered_sections=context["ordered_sections"],
        prior_chapters=context["previous_chapter_spine"],
        prompt_template_sha256=obj["prompt_template_sha256"],
    )
    if canonical != obj:
        _fail("$.cache_inputs", "is not canonical")
    return canonical


def cache_key(
    *,
    source_sha256: str,
    text_sha256: str,
    section_label: str,
    prompt_version: str,
    model_identity: Mapping[str, object],
    schema_ingest_sha256: str,
    ordered_sections: Sequence[str],
    prior_chapters: Sequence[Mapping[str, object]],
    prompt_template_sha256: str,
) -> str:
    """Return the complete cache-input digest for one chapter analysis."""
    return json_digest(
        cache_inputs(
            source_sha256=source_sha256,
            text_sha256=text_sha256,
            section_label=section_label,
            prompt_version=prompt_version,
            model_identity=model_identity,
            schema_ingest_sha256=schema_ingest_sha256,
            ordered_sections=ordered_sections,
            prior_chapters=prior_chapters,
            prompt_template_sha256=prompt_template_sha256,
        )
    )


def cache_path(
    cache_dir: str | Path,
    *,
    prompt_version: str,
    source_id: str,
    key: str,
) -> Path:
    """Return `<cache>/<prompt-version>/<source-id>/<key>.json`."""
    for value, name in ((prompt_version, "prompt_version"), (source_id, "source_id")):
        if type(value) is not str or not SAFE_COMPONENT_RX.fullmatch(value):
            raise ValueError(f"{name} is not a safe cache path component")
    if type(key) is not str or not SHA256_RX.fullmatch(key):
        raise ValueError("key must be a SHA-256 hex digest")
    return Path(cache_dir) / prompt_version / source_id / f"{key}.json"


def atomic_write_json(path: str | Path, data: Mapping[str, object]) -> None:
    """Durably write JSON through a same-directory temporary and os.replace."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def cache_manifest_path(artifact_path: str | Path) -> Path:
    return Path(artifact_path).with_suffix(".manifest")


def write_cache_entry(
    path: str | Path,
    artifact: Mapping[str, object],
    inputs: Mapping[str, object],
) -> None:
    """Atomically publish an artifact and its independently verifiable identity."""
    destination = Path(path)
    canonical_inputs = canonical_cache_inputs(copy.deepcopy(dict(inputs)))
    key = json_digest(canonical_inputs)
    if destination.stem != key:
        raise ValueError("cache artifact filename does not match cache inputs")
    atomic_write_json(destination, artifact)
    atomic_write_json(
        cache_manifest_path(destination),
        {
            "schema": CACHE_MANIFEST_SCHEMA,
            "cache_key": key,
            "cache_inputs": canonical_inputs,
            "artifact_sha256": json_digest(artifact),
        },
    )


def read_cache_entry(path: str | Path, *, require_manifest: bool = True) -> dict:
    """Read an artifact and verify its manifest, cache key, and content digest."""
    artifact_path = Path(path)
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    if type(artifact) is not dict:
        raise ValueError("cache artifact must be an object")
    manifest_path = cache_manifest_path(artifact_path)
    if not manifest_path.is_file():
        if require_manifest:
            raise ValueError("cache manifest missing")
        return {"artifact": artifact, "manifest": None}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if type(manifest) is not dict or set(manifest) != {
        "schema",
        "cache_key",
        "cache_inputs",
        "artifact_sha256",
    }:
        raise ValueError("cache manifest has an invalid shape")
    if manifest["schema"] != CACHE_MANIFEST_SCHEMA:
        raise ValueError("cache manifest schema is unsupported")
    inputs = canonical_cache_inputs(manifest["cache_inputs"])
    manifest["cache_inputs"] = inputs
    expected_key = json_digest(inputs)
    if (
        manifest["cache_key"] != expected_key
        or artifact_path.stem != expected_key
    ):
        raise ValueError("cache manifest key does not match artifact path")
    if manifest["artifact_sha256"] != json_digest(artifact):
        raise ValueError("cache artifact digest does not match manifest")
    return {"artifact": artifact, "manifest": manifest}


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


def _artifact_shape(fixed: Mapping[str, object]) -> dict:
    return {
        **fixed,
        "language": "detected BCP-47 language tag",
        "summary": "compact account of this section's contribution",
        "central_question": "question this section answers",
        "chapter_claim": "principal answer or explanatory move",
        "builds_on": "one concise dependency on the prior chapter spine, or null",
        "claims": [
            {
                "id": "c1",
                "kind": "claim",
                "text": "complete human-readable proposition",
                "importance": 5,
                "source_spans": [{"quote": "verbatim exact source excerpt"}],
                "entities": ["exact declared entity name"],
            },
            {
                "id": "c2",
                "kind": "evidence",
                "text": "complete supporting observation",
                "importance": 4,
                "source_spans": [{"quote": "another verbatim exact excerpt"}],
                "entities": [],
            },
        ],
        "entities": [
            {
                "name": "reusable entity name",
                "type": "semantic type",
                "aliases": ["established English name", "标准中文名"],
                "importance": 5,
                "role": "role in this section's reasoning",
                "page_hint": "entity",
                "claim_ids": ["c2"],
            }
        ],
        "topics": [
            {
                "name": "reusable synthesis topic",
                "question": "question the topic should answer",
                "synthesis_angle": "how claims or entities fit together",
                "importance": 5,
                "claim_ids": ["c1"],
            }
        ],
        "relations": [{"from": "c2", "to": "c1", "rel": "supports"}],
        "page_candidates": [
            {
                "page_type": "entity",
                "name": "exact declared entity or topic name",
                "importance": 5,
                "required": True,
                "claim_ids": ["c1"],
                "reason": "why this deserves a reusable wiki page",
            }
        ],
        "claim_coverage": [
            {
                "claim_id": "c1",
                "page_candidates": [
                    {"page_type": "entity", "name": "exact page candidate name"}
                ],
                "skip_reason": None,
            },
            {
                "claim_id": "c2",
                "page_candidates": [],
                "skip_reason": "specific reason this claim should not drive a page",
            },
        ],
        "open_questions": [],
    }


def build_prompt(
    text: str,
    *,
    source_id: str,
    source_sha256: str,
    section_label: str,
    schema_ingest_rules: str,
    prompt_version: str = PROMPT_VERSION,
    ordered_sections: Sequence[str] = (),
    prior_chapters: Sequence[Mapping[str, object]] = (),
) -> str:
    """Build the structured, source-grounded analyzer prompt."""
    _string(text, "text", max_length=max(len(text), 1), allow_empty=True)
    source_id = _string(source_id, "source_id", max_length=256)
    source_sha256 = _hash(source_sha256, "source_sha256")
    section_label = _string(
        section_label, "section_label", max_length=1000, allow_empty=True
    )
    prompt_version = _string(prompt_version, "prompt_version", max_length=128)
    schema_ingest_rules = _string(
        schema_ingest_rules,
        "schema_ingest_rules",
        max_length=max(len(schema_ingest_rules), 1),
    )
    context = analysis_context(
        ordered_sections=ordered_sections,
        prior_chapters=prior_chapters,
    )

    fixed = {
        "schema": SCHEMA_VERSION,
        "source_id": source_id,
        "source_sha256": source_sha256,
        "text_sha256": sha256_text(text),
        "section_label": section_label,
        "prompt_version": prompt_version,
    }
    shape = _artifact_shape(fixed)
    return _render_prompt(text, shape, context, schema_ingest_rules)


def _render_prompt(text: str, shape: Mapping[str, object], context: Mapping[str, object],
                   schema_ingest_rules: str) -> str:
    return f"""You are a chapter-intelligence analyzer. Read only the current
EXTRACTED_SOURCE_TEXT as evidence and return one JSON object, with no prose or
markdown fences.

Validation and coverage rules:
- Claims are complete propositions, not keywords. Claim ids are unique c1, c2,
  ... tokens. Claim `kind` MUST be one of:
  {json.dumps(sorted(CLAIM_KINDS), ensure_ascii=False)}
  Relation `rel` MUST be one of:
  {json.dumps(sorted(RELATION_KINDS), ensure_ascii=False)}
- Emit source spans by exact quote, copied verbatim from EXTRACTED_SOURCE_TEXT.
  Do not try to calculate offsets. You may include approximate integer start/end
  hints, but the analyzer ignores them for a unique quote and uses them for a
  repeated quote only when they exactly identify one occurrence. Otherwise it
  chooses the first identical occurrence deterministically. Unmatched quotes
  are rejected. Keep each quote to one contiguous, distinctive 20-240 character
  excerpt copied directly from the source. Never join separated phrases, add an
  ellipsis that is not present, or reproduce a long paragraph from memory. When
  a short excerpt repeats, extend it only with adjacent verbatim source text.
  Importance 4-5 claims require at least one source span; lower-importance
  claims may use an empty source_spans array.
- Every entity and topic claim_ids entry and every relation endpoint must name
  a declared claim. Every name in claims[].entities must name a declared entity.
- Importance is an integer from 1 through 5, never a string or decimal.
- `entities` is a recall inventory; `page_candidates` is the authoritative
  editorial decision. Set an entity's `page_hint` to `entity` when it has a
  matching page candidate and `none` otherwise (the pipeline canonicalizes this
  redundant field). A central structure or mechanism needed to explain the
  causal chain may deserve a page even when it appears in only one claim.
- Every item in `topics` is a durable synthesis-page recommendation and needs a
  matching page candidate. Prefer the shortest broad canonical topic that can
  accumulate evidence across later chapters or sources. Merge overlapping
  sub-questions into that topic instead of creating a topic per argument step.
  For example, `真核细胞起源` can contain its one-time occurrence, failed
  precursor models, and competing hypotheses; those do not automatically need
  three narrower topic pages. A named hypothesis normally remains an Entity and
  should not also get a duplicate `<hypothesis>的模型` Topic.
- Name a Topic for the durable question or phenomenon, not merely for one answer
  mechanism. For example, a sustained argument asking why organelle genes remain
  should become a broad gene-retention Topic; local redox regulation is the
  explanation inside it. Likewise, energy constraints belong inside a broad
  origin-of-life Topic when that is the section's actual question.
- Page candidates are not a noun dump and never chapter or section titles.
  `required` is a boolean and MUST be true when page-candidate importance is 4
  or 5.
- A dedicated Entity page normally needs at least two linked explanatory claims,
  recurrence across the section, a named technical term that the source defines
  or develops, or clear reuse across later sources. A one-claim person, organism,
  experiment, or example stays as context inside a broader page unless it names
  a central theory/mechanism or carries indispensable evidence. This is a
  selectivity rule, not a numeric page cap.
- Prefer the shortest canonical concept name and put source variants in aliases.
  For reusable entities, include both the established English and Chinese name
  in aliases even when only one language appears in the source. Do not invent a
  translation when no established counterpart exists.
  Do not narrow a mature mechanism into a historical `hypothesis` page merely
  because the source discusses how it was proposed; retain the suffix only for
  genuinely named hypotheses such as a specific origin model.
- claim_coverage MUST contain exactly one entry for every claim. A covered claim
  lists all page candidates that use it and has skip_reason null. An uncovered
  claim has an empty page_candidates array and a concrete, claim-specific
  skip_reason. Do not silently omit a claim from page planning.
- There is NO fixed entity, topic, claim, relation, or page-candidate cap. Retain
  every materially important item needed to explain the section, while omitting
  incidental mentions that have no explanatory role.
- Before emitting JSON, perform a coverage audit over the source in order. Every
  sustained subsection and every major argumentative move near the beginning,
  middle, and end must contribute a claim. Preserve explicit definitions, named
  technical terms/acronyms developed in prose, quantitative or experimental
  evidence, objections and rejected alternatives, exceptions, and bridge
  mechanisms between major causal steps. Do not let the chapter's central thesis
  crowd these supporting moves out of the artifact.
- builds_on may use PREVIOUS_CHAPTER_SPINE for dependency context. Claims and
  spans may come only from the current EXTRACTED_SOURCE_TEXT. `builds_on` MUST
  be one concise JSON string when a dependency exists, or JSON null when it does
  not; never emit an object or array.

WIKI_SCHEMA_RULES (selection guidance; source text remains authoritative):
<WIKI_SCHEMA_RULES>
{schema_ingest_rules}
</WIKI_SCHEMA_RULES>

The object MUST have exactly this structure and the six fixed metadata values
must be copied byte-for-byte:
{json.dumps(shape, ensure_ascii=False, indent=2)}

ANALYSIS_CONTEXT:
{json.dumps(context, ensure_ascii=False, indent=2)}

EXTRACTED_SOURCE_TEXT:
<EXTRACTED_SOURCE_TEXT>
{text}
</EXTRACTED_SOURCE_TEXT>
"""


def prompt_template_identity() -> str:
    """Hash the exact static prompt rendering, including shape and enum changes."""
    fixed = {
        "schema": SCHEMA_VERSION,
        "source_id": "<SOURCE_ID>",
        "source_sha256": "<SOURCE_SHA256>",
        "text_sha256": "<TEXT_SHA256>",
        "section_label": "<SECTION_LABEL>",
        "prompt_version": "<PROMPT_VERSION>",
    }
    # Build once through the same shape constructor used above. Replacing fixed
    # metadata with sentinels keeps runtime inputs out of the template digest.
    shape = _artifact_shape(fixed)
    return sha256_text(
        _render_prompt(
            "<EXTRACTED_SOURCE_TEXT>",
            shape,
            {
                "ordered_sections": ["<ORDERED_SECTIONS>"],
                "previous_chapter_spine": ["<PRIOR_CHAPTERS>"],
            },
            "<SELECTED_SCHEMA_RULES>",
        )
    )


def resolve_model_identity(model: str | None = None) -> tuple[dict[str, str | None], str | None]:
    """Resolve the analyzer-specific model override and cache identity."""
    selected_model = (model or os.environ.get("PW_ANALYZE_MODEL", "")).strip() or None
    identity = canonical_model_identity(
        llm_client.execution_identity(selected_model)
    )
    return identity, selected_model


def _invalid_cache_path(
    cache_dir: str | Path,
    *,
    prompt_version: str,
    source_id: str,
    filename: str,
) -> Path:
    return Path(cache_dir) / "_invalid" / prompt_version / source_id / filename


def load_cached_artifact(
    cache_dir: str | Path,
    *,
    text: str,
    source_id: str,
    source_sha256: str,
    section_label: str,
    prompt_version: str,
    model_identity: Mapping[str, object],
    schema_ingest_sha256: str,
    ordered_sections: Sequence[str] = (),
    prior_chapters: Sequence[Mapping[str, object]] = (),
    prompt_template_sha256: str | None = None,
) -> dict | None:
    """Load and revalidate a cache entry; stale or malformed data is a miss.

    A failed ingest may clean up its untracked sidecar and mint a new source id
    on retry. The analysis itself is content-addressed, so a matching cache entry
    under that abandoned id may be adopted after rebinding only the metadata id
    and revalidating every content/hash field.
    """
    template_digest = prompt_template_sha256 or prompt_template_identity()
    inputs = cache_inputs(
        source_sha256=source_sha256,
        text_sha256=sha256_text(text),
        section_label=section_label,
        prompt_version=prompt_version,
        model_identity=model_identity,
        schema_ingest_sha256=schema_ingest_sha256,
        ordered_sections=ordered_sections,
        prior_chapters=prior_chapters,
        prompt_template_sha256=template_digest,
    )
    path = cache_path(
        cache_dir,
        prompt_version=prompt_version,
        source_id=source_id,
        key=json_digest(inputs),
    )
    candidates = [path]
    candidates.extend(
        candidate
        for candidate in sorted(path.parent.parent.glob(f"*/{path.name}"))
        if candidate != path
    )
    for candidate in candidates:
        try:
            entry = read_cache_entry(candidate, require_manifest=False)
            artifact = entry["artifact"]
            if candidate != path:
                if type(artifact) is not dict:
                    continue
                artifact = copy.deepcopy(artifact)
                artifact["source_id"] = source_id
            original = copy.deepcopy(artifact)
            artifact = materialize_unique_aliases(artifact)
            validated = validate_artifact(
                artifact,
                text=text,
                source_id=source_id,
                source_sha256=source_sha256,
                section_label=section_label,
                prompt_version=prompt_version,
                ordered_sections=ordered_sections,
            )
            if candidate != path or artifact != original:
                write_cache_entry(path, validated, inputs)
            elif entry["manifest"] is None:
                write_cache_entry(path, validated, inputs)
            return validated
        except (OSError, json.JSONDecodeError, ArtifactValidationError, ValueError):
            continue

    invalid_path = _invalid_cache_path(
        cache_dir,
        prompt_version=prompt_version,
        source_id=source_id,
        filename=path.name,
    )
    invalid_candidates = [invalid_path]
    if not invalid_path.is_file():
        invalid_candidates.extend(
            sorted(invalid_path.parent.parent.glob(f"*/{path.name}"))
        )
    for candidate in invalid_candidates:
        try:
            diagnostic = json.loads(candidate.read_text(encoding="utf-8"))
            if type(diagnostic) is not dict:
                continue
            if (
                diagnostic.get("schema") != "chapter-intelligence-invalid/1"
                or diagnostic.get("source_sha256") != source_sha256
                or diagnostic.get("section_label") != section_label
                or diagnostic.get("prompt_version") != prompt_version
                or type(diagnostic.get("raw_response")) is not str
            ):
                continue
            artifact = materialize_response(
                diagnostic["raw_response"],
                text,
                source_id_override=source_id,
            )
            validated = validate_artifact(
                artifact,
                text=text,
                source_id=source_id,
                source_sha256=source_sha256,
                section_label=section_label,
                prompt_version=prompt_version,
                ordered_sections=ordered_sections,
            )
            write_cache_entry(path, validated, inputs)
            candidate.unlink(missing_ok=True)
            return validated
        except (
            OSError,
            json.JSONDecodeError,
            ArtifactValidationError,
            ValueError,
        ):
            continue
    return None


def scan_validated_entries(
    cache_dir: str | Path,
    *,
    source_id: str,
    source_sha256: str,
    prompt_version: str,
    ordered_sections: Sequence[str],
):
    """Yield every structurally valid, manifested cache entry for one source.

    Each yield is ``(section_label, mtime_ns, filename, validated_artifact,
    cache_inputs)``. The artifact and its manifest must agree on source id,
    hashes, section label, prompt version, and the current ordered_sections.
    Span quotes are not rechecked because the extracted text is intentionally
    absent; entries were fully checked before the atomic cache write. Callers
    apply any further producer-identity or label predicate and choose the newest
    match. Malformed or mismatched entries are skipped.
    """
    _hash(source_sha256, "source_sha256")
    expected_sections = list(ordered_sections)
    source_dir = cache_path(
        cache_dir, prompt_version=prompt_version, source_id=source_id, key="0" * 64
    ).parent
    if not source_dir.is_dir():
        return
    for path in sorted(source_dir.glob("*.json")):
        try:
            entry = read_cache_entry(path)
            artifact = entry["artifact"]
            inputs = entry["manifest"]["cache_inputs"]
            artifact_source_id = _string(
                artifact.get("source_id"), "$.source_id", max_length=256
            )
            artifact_source_sha = _hash(
                artifact.get("source_sha256"), "$.source_sha256"
            )
            artifact_text_sha = _hash(
                artifact.get("text_sha256"), "$.text_sha256"
            )
            artifact_section = _string(
                artifact.get("section_label"),
                "$.section_label",
                max_length=1000,
                allow_empty=True,
            )
            artifact_prompt = _string(
                artifact.get("prompt_version"), "$.prompt_version", max_length=128
            )
            context = inputs.get("analysis_context")
            if (
                artifact_source_id != source_id
                or artifact_source_sha != source_sha256
                or artifact_prompt != prompt_version
                or inputs.get("artifact_schema") != SCHEMA_VERSION
                or inputs.get("source_sha256") != source_sha256
                or inputs.get("text_sha256") != artifact_text_sha
                or inputs.get("section_label") != artifact_section
                or inputs.get("prompt_version") != prompt_version
                or type(context) is not dict
                or context.get("ordered_sections") != expected_sections
            ):
                continue
            validated = validate_artifact(
                artifact,
                text=None,
                source_id=source_id,
                source_sha256=source_sha256,
                section_label=artifact_section,
                prompt_version=prompt_version,
                ordered_sections=expected_sections,
                _expected_text_sha256=artifact_text_sha,
            )
            modified_ns = path.stat().st_mtime_ns
        except (OSError, json.JSONDecodeError, ArtifactValidationError, ValueError):
            continue
        yield artifact_section, modified_ns, path.name, validated, inputs


def discover_prior_spines(
    cache_dir: str | Path,
    *,
    chapter_outline: Sequence[str],
    current_section_label: str,
    source_id: str,
    source_sha256: str,
    prompt_version: str,
    model_identity: Mapping[str, object],
    schema_ingest_sha256: str,
    prompt_template_sha256: str | None = None,
) -> list[dict]:
    """Discover the coherent earlier-chapter spine in outline order.

    A candidate file must be a structurally valid, manifested cache entry whose
    producer context matches the current outline, model, schema, and template.
    When refreshes leave multiple context versions, each selected chapter must
    declare exactly the prior spine selected before it; the newest matching
    refresh wins.
    Span quotes cannot be rechecked because prior extracted text is intentionally
    absent; those entries were fully checked before the atomic cache write.
    """
    outline: list[str] = []
    seen_labels: set[str] = set()
    for index, value in enumerate(chapter_outline):
        label = _string(
            value,
            f"chapter_outline[{index}]",
            max_length=1000,
            allow_empty=True,
        )
        if label in seen_labels:
            raise ValueError(f"chapter_outline[{index}] duplicates {label!r}")
        seen_labels.add(label)
        outline.append(label)
    _string(
        current_section_label,
        "current_section_label",
        max_length=1000,
        allow_empty=True,
    )
    identity = canonical_model_identity(model_identity)
    _hash(schema_ingest_sha256, "schema_ingest_sha256")
    template_digest = prompt_template_sha256 or prompt_template_identity()

    artifacts_by_label: dict[str, list[tuple[int, str, dict, list[dict]]]] = {}
    for section, modified_ns, name, validated, inputs in scan_validated_entries(
        cache_dir,
        source_id=source_id,
        source_sha256=source_sha256,
        prompt_version=prompt_version,
        ordered_sections=outline,
    ):
        # The producer's model, schema, and prompt template must match the
        # current run; the map consumer deliberately does not require this.
        if (
            inputs.get("model_identity") != identity
            or inputs.get("schema_ingest_sha256") != schema_ingest_sha256
            or inputs.get("prompt_template_sha256") != template_digest
        ):
            continue
        artifacts_by_label.setdefault(section, []).append(
            (
                modified_ns,
                name,
                validated,
                inputs["analysis_context"]["previous_chapter_spine"],
            )
        )

    if current_section_label in outline:
        prior_labels = outline[: outline.index(current_section_label)]
    else:
        prior_labels = [label for label in outline if label != current_section_label]

    spines: list[dict] = []
    for label in prior_labels:
        matches = [
            item
            for item in artifacts_by_label.get(label, [])
            if item[3] == spines
        ]
        if not matches:
            continue
        artifact = max(matches, key=lambda item: (item[0], item[1]))[2]
        spines.append(
            {
                "section_label": label,
                "central_question": artifact["central_question"],
                "chapter_claim": artifact["chapter_claim"],
            }
        )
    return spines


def analyze_chapter(
    text: str,
    *,
    source_id: str,
    source_sha256: str,
    section_label: str,
    ordered_sections: Sequence[str] = (),
    prior_chapters: Sequence[Mapping[str, object]] = (),
    prompt_version: str = PROMPT_VERSION,
    model: str | None = None,
    model_identity: Mapping[str, object] | None = None,
    schema_ingest_path: str | Path = DEFAULT_SCHEMA_INGEST_PATH,
    cache_dir: str | Path | None = None,
    refresh: bool = False,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    complete: Callable[..., str | None] | None = None,
) -> dict:
    """Analyze one section, validate it, and atomically cache the result."""
    schema_path = Path(schema_ingest_path)
    schema_rules = selected_schema_rules(schema_path)
    schema_digest = sha256_text(schema_rules)
    template_digest = prompt_template_identity()
    resolved_identity, selected_model = resolve_model_identity(model)
    if model_identity is not None:
        resolved_identity = canonical_model_identity(model_identity)
        if model is not None:
            resolved_identity["model"] = model

    if type(timeout_s) is not int or timeout_s <= 0:
        raise ValueError("timeout_s must be a positive integer")

    if cache_dir is not None and not refresh:
        cached = load_cached_artifact(
            cache_dir,
            text=text,
            source_id=source_id,
            source_sha256=source_sha256,
            section_label=section_label,
            prompt_version=prompt_version,
            model_identity=resolved_identity,
            schema_ingest_sha256=schema_digest,
            ordered_sections=ordered_sections,
            prior_chapters=prior_chapters,
            prompt_template_sha256=template_digest,
        )
        if cached is not None:
            return cached

    destination: Path | None = None
    invalid_destination: Path | None = None
    inputs: dict | None = None
    if cache_dir is not None:
        inputs = cache_inputs(
            source_sha256=source_sha256,
            text_sha256=sha256_text(text),
            section_label=section_label,
            prompt_version=prompt_version,
            model_identity=resolved_identity,
            schema_ingest_sha256=schema_digest,
            ordered_sections=ordered_sections,
            prior_chapters=prior_chapters,
            prompt_template_sha256=template_digest,
        )
        destination = cache_path(
            cache_dir,
            prompt_version=prompt_version,
            source_id=source_id,
            key=json_digest(inputs),
        )
        invalid_destination = _invalid_cache_path(
            cache_dir,
            prompt_version=prompt_version,
            source_id=source_id,
            filename=destination.name,
        )

    prompt = build_prompt(
        text,
        source_id=source_id,
        source_sha256=source_sha256,
        section_label=section_label,
        schema_ingest_rules=schema_rules,
        prompt_version=prompt_version,
        ordered_sections=ordered_sections,
        prior_chapters=prior_chapters,
    )
    completion = complete or llm_client.complete
    raw = ""
    validation_error = ""
    for attempt in range(2):
        request = prompt if attempt == 0 else (
            f"{prompt}\n\nYour previous response was rejected by the validator: "
            f"{validation_error}\nReturn a corrected complete JSON object only."
        )
        raw = completion(request, timeout=timeout_s, model=selected_model)
        if raw is None:
            detail = (
                "configured provider returned empty output"
                if llm_client.configured()
                else "no LLM provider is configured"
            )
            raise RuntimeError(f"chapter analyzer failed: {detail}")
        try:
            artifact = materialize_response(raw, text)
            validated = validate_artifact(
                artifact,
                text=text,
                source_id=source_id,
                source_sha256=source_sha256,
                section_label=section_label,
                prompt_version=prompt_version,
                ordered_sections=ordered_sections,
            )
            break
        except (ArtifactValidationError, ValueError) as exc:
            validation_error = str(exc)
            if attempt == 0:
                continue
            if invalid_destination is not None:
                atomic_write_json(
                    invalid_destination,
                    {
                        "schema": "chapter-intelligence-invalid/1",
                        "source_id": source_id,
                        "source_sha256": source_sha256,
                        "section_label": section_label,
                        "prompt_version": prompt_version,
                        "error": validation_error,
                        "raw_response": raw,
                    },
                )
            raise

    if destination is not None:
        write_cache_entry(destination, validated, inputs)
        if invalid_destination is not None:
            invalid_destination.unlink(missing_ok=True)
    return validated
