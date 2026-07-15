"""Chapter-intelligence prompting, caching, and orchestration."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Sequence

import llm_client
from chapter_artifact import (
    ArtifactValidationError,
    CLAIM_KIND_ALIASES,
    CLAIM_KINDS,
    CLAIM_PROJECTION_FIELDS,
    ENTITY_PROJECTION_FIELDS,
    PROMPT_VERSION,
    RELATION_KINDS,
    SCHEMA_VERSION,
    SHA256_RX,
    SOURCE_QUOTE_MIN_CHARS,
    extract_json_object,
    materialize_claim_coverage,
    materialize_entity_references,
    materialize_page_hints,
    materialize_response,
    materialize_source_spans,
    materialize_unique_aliases,
    normalized_name,
    sha256_text,
    validate_artifact,
)
from chapter_artifact import _fail, _hash, _object, _string



CACHE_MANIFEST_SCHEMA = "chapter-intelligence-cache-entry/1"

DEFAULT_TIMEOUT_S = 1800

DEFAULT_SCHEMA_INGEST_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "schema-ingest.md"
)
ANALYZER_SCHEMA_SECTIONS = (
    "Page Selection And Coverage",
    "Page Types",
    "Voice And Attribution",
    "Language And Naming",
)


SAFE_COMPONENT_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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
    """Load and revalidate the exact cache entry; stale or malformed data is a miss."""
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
    try:
        entry = read_cache_entry(path, require_manifest=False)
        original = copy.deepcopy(entry["artifact"])
        artifact = materialize_unique_aliases(entry["artifact"])
        validated = validate_artifact(
            artifact, text=text, source_id=source_id, source_sha256=source_sha256,
            section_label=section_label, prompt_version=prompt_version,
            ordered_sections=ordered_sections,
        )
        if artifact != original or entry["manifest"] is None:
            write_cache_entry(path, validated, inputs)
        return validated
    except (OSError, json.JSONDecodeError, ArtifactValidationError, ValueError):
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
