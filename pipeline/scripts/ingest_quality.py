"""Pure helpers for the post-apply chapter-intelligence quality gate."""

from __future__ import annotations

import dataclasses
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePath
from typing import Iterable, Mapping, Sequence

import yaml

from _util import normalize_name
from source_citations import (
    BRACKETED_CITATION_RX,
    encode_section_anchor,
    parse_citation_parts,
    source_citation_ref,
)


RECEIPT_SCHEMA = "ingest-quality-receipt/1"
HIGH_IMPORTANCE = 4
FACT_CARD_MAX_ALNUMERIC_CHARS = 180

LLM_OPEN = "<!-- llm-zone -->"
LLM_CLOSE = "<!-- /llm-zone -->"

_ZONE_MARKER_RX = re.compile(
    r"^\s*<!--\s*(/)?(llm-zone|human-zone)\s*-->\s*$"
)
_H1_RX = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_CALLOUT_RX = re.compile(r"^\s*>\s?(.*)$")
_AI_CALLOUT_RX = re.compile(r"^\[!AI\](?:\s+.*)?$", re.IGNORECASE)
_HEADING_RX = re.compile(r"^#{1,6}\s+")
_FENCE_RX = re.compile(r"^(`{3,}|~{3,})")
_LIST_ITEM_RX = re.compile(r"^(?:[-+*]|\d+[.)])\s+")
_MEDIA_ANCHOR_RX = re.compile(
    r"(?:card-[1-9]\d*|frame-[1-9]\d*|"
    r"\d{1,2}:\d{2}(?::\d{2})?-\d{1,2}:\d{2}(?::\d{2})?)"
)
_CITATION_GROUP_RX = re.compile(r"\[[^\[\]]*\bsrc:[^\[\]]*\]")
_WIKILINK_RX = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
_IMAGE_TRANSCLUDE_RX = re.compile(r"!\[\[[^\]]+\]\]")
_MARKDOWN_LINK_RX = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_HTML_RX = re.compile(r"<[^>]+>")
_SENTENCE_END_RX = re.compile(r"[.!?。！？]+(?:[\"'”’）)】》]*)")

_FORBIDDEN_ENGLISH = (
    "the chapter",
    "the section",
    "the source",
    "the text",
    "the book",
    "the author",
    "according to",
    "it is argued that",
)
_FORBIDDEN_CHINESE = (
    "按照本章",
    "作者认为",
    "作者指出",
    "书中认为",
    "书中指出",
    "书中提出",
    "这一章",
    "本章",
    "本节",
    "本书",
    "该章",
    "该节",
    "文中",
    "书中",
    "文献",
)
_CHINESE_CHAPTER_RX = re.compile(
    r"第\s*(?:[0-9]+|[一二三四五六七八九十百千零〇两]+|N)\s*章",
    re.IGNORECASE,
)

class IntelligenceValidationError(ValueError):
    """Raised when the quality gate cannot trust an intelligence artifact."""


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    path: str | None = None
    line: int | None = None
    candidate: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


@dataclass(frozen=True)
class Candidate:
    name: str
    page_type: str
    importance: int | None
    central: bool
    accepted_names: tuple[str, ...]
    claim_ids: tuple[str, ...]


@dataclass(frozen=True)
class Zone:
    kind: str
    start_line: int
    lines: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class Paragraph:
    text: str
    line: int

    @property
    def signature(self) -> str:
        return re.sub(r"\s+", " ", self.text).strip()


@dataclass(frozen=True)
class PageInput:
    path: str
    text: str
    baseline_text: str | None = None
    disposition: str = "modified"


@dataclass(frozen=True)
class ParsedPage:
    path: str
    page_type: str | None
    names: frozenset[str]
    paragraphs: tuple[Paragraph, ...]
    issues: tuple[Issue, ...]


def _claim_text_by_id(artifact: object) -> dict[str, str]:
    if type(artifact) is not dict or type(artifact.get("claims")) is not list:
        return {}
    result: dict[str, str] = {}
    for raw in artifact["claims"]:
        if type(raw) is not dict:
            continue
        claim_id = raw.get("id")
        text = raw.get("text")
        if type(claim_id) is str and type(text) is str and claim_id and text.strip():
            result[claim_id] = text.strip()
    return result


def _intelligence_error(path: str, message: str) -> IntelligenceValidationError:
    return IntelligenceValidationError(f"{path}: {message}")


def validate_intelligence(
    artifact: object, *, source_id: str, section_label: str
) -> tuple[Candidate, ...]:
    """Validate with the analyzer contract, then project mandatory candidates."""
    try:
        import chapter_intelligence as analyzer

        if type(artifact) is not dict:
            raise ValueError("artifact must be an object")
        analyzer.validate_artifact(
            artifact,
            text=None,
            source_id=source_id,
            source_sha256=artifact.get("source_sha256"),
            section_label=section_label,
            prompt_version=artifact.get("prompt_version"),
            _expected_text_sha256=artifact.get("text_sha256"),
        )
    except (ImportError, TypeError, ValueError) as exc:
        raise _intelligence_error("$", f"production artifact is invalid: {exc}") from exc

    aliases = {
        normalize_name(entity["name"]): tuple(entity["aliases"])
        for entity in artifact["entities"]
    }

    required_candidates: list[Candidate] = []
    for raw in artifact["page_candidates"]:
        importance = raw["importance"]
        required = raw["required"]
        if importance < HIGH_IMPORTANCE and not required:
            continue
        name = raw["name"]
        page_type = raw["page_type"]
        accepted = tuple(dict.fromkeys((name, *aliases.get(normalize_name(name), ()))))
        required_candidates.append(
            Candidate(
                name=name,
                page_type=page_type,
                importance=importance,
                central=(importance == 5 or (required and importance < HIGH_IMPORTANCE)),
                accepted_names=accepted,
                claim_ids=tuple(raw["claim_ids"]),
            )
        )
    return tuple(required_candidates)


def _path_page_type(path: str) -> str | None:
    parts = PurePath(path.replace("\\", "/")).parts
    for index in range(len(parts) - 1):
        if parts[index] != "wiki":
            continue
        if parts[index + 1] == "entities":
            return "entity"
        if parts[index + 1] == "topics":
            return "topic"
    return None


def _frontmatter(text: str, path: str) -> tuple[dict[str, object] | None, str, list[Issue]]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text, [
            Issue("page.frontmatter_missing", "page must start with YAML frontmatter", path)
        ]
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return None, text, [
            Issue("page.frontmatter_unclosed", "YAML frontmatter has no closing marker", path)
        ]
    try:
        value = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        problem = str(exc).splitlines()[0]
        return None, "\n".join(lines[end + 1 :]), [
            Issue("page.frontmatter_malformed", f"invalid YAML: {problem}", path)
        ]
    if type(value) is not dict:
        return None, "\n".join(lines[end + 1 :]), [
            Issue("page.frontmatter_malformed", "frontmatter must be a mapping", path)
        ]
    return value, "\n".join(lines[end + 1 :]), []


def validate_zones(text: str, path: str) -> tuple[tuple[Zone, ...], tuple[Issue, ...]]:
    """Validate marker ordering and return completed zone spans."""
    lines = text.splitlines()
    stack: list[tuple[str, int, int]] = []
    zones: list[Zone] = []
    issues: list[Issue] = []
    for index, line in enumerate(lines):
        match = _ZONE_MARKER_RX.match(line)
        if not match:
            continue
        closing, kind = match.groups()
        line_number = index + 1
        if not closing:
            if stack:
                issues.append(
                    Issue(
                        "zones.nested",
                        f"{kind} opens before {stack[-1][0]} closes",
                        path,
                        line_number,
                    )
                )
            stack.append((kind, index, line_number))
            continue
        if not stack:
            issues.append(
                Issue("zones.unmatched_close", f"unmatched closing {kind}", path, line_number)
            )
            continue
        open_kind, open_index, open_line = stack.pop()
        if open_kind != kind:
            issues.append(
                Issue(
                    "zones.mismatched_close",
                    f"closing {kind} does not match open {open_kind} from line {open_line}",
                    path,
                    line_number,
                )
            )
            continue
        zones.append(
            Zone(
                kind=kind,
                start_line=open_line,
                lines=tuple(
                    (body_index + 1, lines[body_index])
                    for body_index in range(open_index + 1, index)
                ),
            )
        )
    for kind, _index, line_number in stack:
        issues.append(
            Issue("zones.unclosed", f"unclosed {kind}", path, line_number)
        )
    if not any(zone.kind == "llm-zone" for zone in zones):
        issues.append(
            Issue("zones.llm_missing", "page has no balanced llm-zone", path)
        )
    return tuple(zones), tuple(issues)


def _plain_prose(text: str) -> str:
    text = _CITATION_GROUP_RX.sub("", text)
    text = _IMAGE_TRANSCLUDE_RX.sub("", text)
    text = _WIKILINK_RX.sub(lambda m: m.group(2) or m.group(1), text)
    text = _MARKDOWN_LINK_RX.sub(r"\1", text)
    text = _HTML_RX.sub("", text)
    text = re.sub(r"[`*_~=#>|-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _substantive(text: str) -> bool:
    plain = _plain_prose(text)
    return sum(character.isalnum() for character in plain) >= 2


def callout_paragraphs(
    zones: Iterable[Zone], path: str
) -> tuple[tuple[Paragraph, ...], tuple[Issue, ...]]:
    """Extract substantive Markdown blocks from balanced llm-zone AI callouts."""
    paragraphs: list[Paragraph] = []
    issues: list[Issue] = []
    for zone in zones:
        if zone.kind != "llm-zone":
            continue
        content: list[tuple[int, str]] = []
        for line_number, raw_line in zone.lines:
            if not raw_line.strip():
                content.append((line_number, ""))
                continue
            match = _CALLOUT_RX.match(raw_line)
            if not match:
                issues.append(
                    Issue(
                        "callout.unquoted_content",
                        "nonblank llm-zone content must be inside the AI callout",
                        path,
                        line_number,
                    )
                )
                content.append((line_number, raw_line.strip()))
            else:
                content.append((line_number, match.group(1).rstrip()))

        first = next(((line, value) for line, value in content if value.strip()), None)
        if first is None or not _AI_CALLOUT_RX.match(first[1].strip()):
            issues.append(
                Issue(
                    "callout.ai_missing",
                    "llm-zone must begin with an [!AI] callout",
                    path,
                    zone.start_line,
                )
            )

        block: list[str] = []
        block_line = zone.start_line
        fence: str | None = None

        def flush() -> None:
            nonlocal block, block_line
            if block:
                text = "\n".join(block).strip()
                if _substantive(text):
                    paragraphs.append(Paragraph(text=text, line=block_line))
            block = []

        for line_number, value in content:
            stripped = value.strip()
            if _AI_CALLOUT_RX.match(stripped):
                flush()
                continue
            fence_match = _FENCE_RX.match(stripped)
            if fence_match:
                flush()
                token = fence_match.group(1)[0]
                fence = None if fence == token else token
                continue
            if fence is not None:
                continue
            if not stripped:
                flush()
                continue
            if (
                _HEADING_RX.match(stripped)
                or stripped in {"---", "***", "___"}
                or _IMAGE_TRANSCLUDE_RX.fullmatch(stripped)
                or (stripped.startswith("<!--") and stripped.endswith("-->"))
            ):
                flush()
                continue
            if _LIST_ITEM_RX.match(stripped):
                flush()
                block_line = line_number
                block.append(value)
                continue
            if not block:
                block_line = line_number
            block.append(value)
        flush()
    return tuple(paragraphs), tuple(issues)


def parse_page(page: PageInput) -> ParsedPage:
    """Parse one current page into the fields used by the quality checks."""
    issues: list[Issue] = []
    path_type = _path_page_type(page.path)
    if path_type is None:
        issues.append(
            Issue(
                "page.path_scope",
                "modified path must be under wiki/entities or wiki/topics",
                page.path,
            )
        )

    frontmatter, body, frontmatter_issues = _frontmatter(page.text, page.path)
    issues.extend(frontmatter_issues)
    page_type: str | None = None
    aliases: tuple[str, ...] = ()
    if frontmatter is not None:
        raw_type = frontmatter.get("type")
        if raw_type not in {"Entity", "Topic"}:
            issues.append(
                Issue(
                    "page.type_invalid",
                    "frontmatter type must be exactly Entity or Topic",
                    page.path,
                )
            )
        else:
            page_type = raw_type.casefold()
        if path_type is not None and page_type is not None and page_type != path_type:
            issues.append(
                Issue(
                    "page.type_path_mismatch",
                    f"frontmatter type {raw_type} conflicts with the {path_type} path",
                    page.path,
                )
            )

        raw_aliases = frontmatter.get("aliases", [])
        if type(raw_aliases) is not list or any(type(alias) is not str for alias in raw_aliases):
            issues.append(
                Issue(
                    "page.aliases_invalid",
                    "frontmatter aliases must be an array of strings",
                    page.path,
                )
            )
        else:
            aliases = tuple(alias.strip() for alias in raw_aliases if alias.strip())

    h1_matches = [match.group(1).strip().rstrip("#").rstrip() for match in _H1_RX.finditer(body)]
    h1: str | None = None
    if len(h1_matches) != 1 or not h1_matches[0]:
        issues.append(
            Issue(
                "page.h1_invalid",
                "page body must contain exactly one non-empty H1",
                page.path,
            )
        )
    else:
        h1 = h1_matches[0]

    zones, zone_issues = validate_zones(page.text, page.path)
    issues.extend(zone_issues)
    paragraphs, callout_issues = callout_paragraphs(zones, page.path)
    issues.extend(callout_issues)
    return ParsedPage(
        path=page.path,
        page_type=page_type,
        names=frozenset(
            normalize_name(value)
            for value in ((h1,) + aliases)
            if value and normalize_name(value)
        ),
        paragraphs=paragraphs,
        issues=tuple(issues),
    )


def modified_paragraphs(
    current: Sequence[Paragraph], baseline: Sequence[Paragraph]
) -> tuple[Paragraph, ...]:
    """Return current paragraphs not semantically present in the baseline."""
    old = Counter(paragraph.signature for paragraph in baseline)
    result: list[Paragraph] = []
    for paragraph in current:
        signature = paragraph.signature
        if old[signature]:
            old[signature] -= 1
        else:
            result.append(paragraph)
    return tuple(result)


def expected_citation(source_id: str, section_label: str) -> str:
    return source_citation_ref(source_id, section_label)


def has_exact_citation(text: str, source_id: str, section_label: str) -> bool:
    """Return whether a citation list contains current-run provenance.

    Newly written chapter citations must use the canonical delimiter-safe raw
    anchor. Consumers continue to decode legacy human-readable anchors, but a
    current run cannot emit one and rely on semantic equivalence to pass this
    gate. Whole-source runs accept either the bare source id or a media-specific
    anchor such as a timestamp, card, or frame; the dedicated media-anchor lint
    validates that anchor's capability and range later in the pipeline.
    """
    stripped = text.rstrip()
    matches = list(BRACKETED_CITATION_RX.finditer(stripped))
    if not matches or not re.fullmatch(r"[.!?。！？]*", stripped[matches[-1].end():]):
        return False
    expected_anchor = encode_section_anchor(section_label) if section_label else ""
    for citation in parse_citation_parts(matches[-1].group(1)):
        if citation.source_id != source_id:
            continue
        if section_label:
            if citation.raw_anchor == expected_anchor:
                return True
        else:
            return not citation.raw_anchor or bool(
                _MEDIA_ANCHOR_RX.fullmatch(citation.raw_anchor)
            )
    return False


def forbidden_entity_text_phrases(text: str) -> tuple[str, ...]:
    """Return schema-forbidden chapter/source-as-agent phrases in prose."""
    text = re.sub(r"```.*?```|~~~.*?~~~", "", text, flags=re.DOTALL)
    text = _CITATION_GROUP_RX.sub("", text)
    found: list[str] = []
    for phrase in _FORBIDDEN_ENGLISH:
        words = r"\s+".join(re.escape(word) for word in phrase.split())
        if re.search(rf"\b{words}\b", text, flags=re.IGNORECASE):
            found.append(phrase)
    for phrase in _FORBIDDEN_CHINESE:
        if phrase in text:
            found.append(phrase)
    if _CHINESE_CHAPTER_RX.search(text):
        found.append("第N章")
    return tuple(found)


def _fact_card_metrics(paragraphs: Sequence[Paragraph]) -> tuple[int, int, int]:
    plain = " ".join(_plain_prose(paragraph.text) for paragraph in paragraphs).strip()
    alphanumeric = sum(character.isalnum() for character in plain)
    sentences = len(_SENTENCE_END_RX.findall(plain))
    if plain and sentences == 0:
        sentences = 1
    return len(paragraphs), sentences, alphanumeric


def _already_covers_claims(page: ParsedPage, candidate: Candidate,
                           claim_texts: Mapping[str, str]) -> bool:
    """Conservative, deterministic proof that an unchanged page covers claims.

    Identity alone is insufficient: every claim assigned to the candidate must
    occur verbatim after Markdown/plain-text normalization. Semantic guesses
    remain the renderer's job and cannot silently waive importance-5 coverage.
    """
    if not candidate.claim_ids or any(claim_id not in claim_texts
                                      for claim_id in candidate.claim_ids):
        return False
    page_text = normalize_name(" ".join(
        _plain_prose(paragraph.text) for paragraph in page.paragraphs
    ))
    if not page_text:
        return False
    return all(
        normalize_name(_plain_prose(claim_texts[claim_id])) in page_text
        for claim_id in candidate.claim_ids
    )


def _issue_sort_key(issue: Issue) -> tuple[str, str, int, str, str]:
    return (
        issue.path or "",
        issue.code,
        issue.line or 0,
        issue.candidate or "",
        issue.message,
    )


def evaluate_quality(
    artifact: object,
    *,
    source_id: str,
    section_label: str,
    pages: Sequence[PageInput],
    allow_no_changes: bool = False,
    initial_errors: Sequence[Issue] = (),
    initial_warnings: Sequence[Issue] = (),
) -> dict[str, object]:
    """Evaluate one post-apply working tree and return a deterministic receipt."""
    errors = list(initial_errors)
    warnings = list(initial_warnings)
    required_candidates: tuple[Candidate, ...] = ()
    try:
        required_candidates = validate_intelligence(
            artifact, source_id=source_id, section_label=section_label
        )
    except IntelligenceValidationError as exc:
        errors.append(Issue("intelligence.malformed", str(exc)))

    parsed: list[ParsedPage] = []
    historical: list[ParsedPage] = []
    disposition_by_path: dict[str, str] = {}
    substantive_changed_paths: set[str] = set()
    modified_count = 0
    for page_input in sorted(pages, key=lambda item: item.path):
        if page_input.disposition not in {"modified", "existing"}:
            errors.append(Issue(
                "page.disposition_invalid",
                f"unsupported page disposition {page_input.disposition!r}",
                page_input.path,
            ))
            continue
        page = parse_page(page_input)
        parsed.append(page)
        disposition_by_path[page.path] = page_input.disposition
        if page_input.disposition == "existing":
            continue
        errors.extend(page.issues)

        if page_input.baseline_text is None:
            changed = page.paragraphs
        else:
            baseline = parse_page(
                PageInput(path=page_input.path, text=page_input.baseline_text)
            )
            historical.append(baseline)
            changed = modified_paragraphs(page.paragraphs, baseline.paragraphs)
        modified_count += len(changed)
        if changed:
            substantive_changed_paths.add(page.path)
        for paragraph in changed:
            if not has_exact_citation(
                paragraph.text, source_id=source_id, section_label=section_label
            ):
                errors.append(
                    Issue(
                        "citation.current_missing",
                        "modified substantive callout paragraph lacks exact citation "
                        f"[{expected_citation(source_id, section_label)}]",
                        page.path,
                        paragraph.line,
                    )
                )

        if page.page_type == "entity":
            changed_text = "\n".join(paragraph.text for paragraph in changed)
            for phrase in forbidden_entity_text_phrases(changed_text):
                errors.append(
                    Issue(
                        "entity.forbidden_attribution",
                        f"entity llm-zone contains forbidden phrase {phrase!r}",
                        page.path,
                    )
                )

    represented = 0
    already_covered_count = 0
    candidate_rows: list[dict[str, object]] = []
    central_paths: set[str] = set()
    candidate_matches: list[
        tuple[Candidate, str, list[ParsedPage], list[ParsedPage]]
    ] = []
    represented_claim_ids: set[str] = set()
    has_modified_candidate = bool(substantive_changed_paths)
    claim_texts = _claim_text_by_id(artifact)
    for candidate in required_candidates:
        accepted = {normalize_name(name) for name in candidate.accepted_names}
        exact_matches = [
            page for page in parsed if normalize_name(candidate.name) in page.names
        ]
        identity_matches = exact_matches or [
            page for page in parsed if bool(page.names & accepted)
        ]
        existing_identity_matches = [
            page for page in identity_matches
            if disposition_by_path.get(page.path) == "existing"
            and page.page_type is not None
            and not page.issues
        ]
        existing_identity_matches.extend(
            page for page in historical
            if page.page_type is not None and not page.issues
            and bool(page.names & accepted)
            and page.path not in {match.path for match in existing_identity_matches}
        )
        existing_types = {page.page_type for page in existing_identity_matches}
        resolved_type = candidate.page_type
        if len(existing_types) == 1:
            # A pre-existing global identity owns its historical path/type. The
            # analyzer's Entity/Topic suggestion must adapt rather than create
            # an alias collision that no possible diff can satisfy.
            resolved_type = next(iter(existing_types))
        matches = [
            page for page in identity_matches if page.page_type == resolved_type
        ]
        modified_matches = [
            page for page in matches
            if disposition_by_path.get(page.path) == "modified"
            and page.path in substantive_changed_paths
        ]
        historical_matches = [
            page for page in historical
            if not page.issues and page.page_type == resolved_type
            and bool(page.names & accepted)
            and _already_covers_claims(page, candidate, claim_texts)
        ]
        covered_existing = [
            page for page in matches
            if disposition_by_path.get(page.path) == "existing"
            and not page.issues
            and _already_covers_claims(page, candidate, claim_texts)
        ]
        covered_existing.extend(
            page for page in historical_matches
            if page.path not in {match.path for match in covered_existing}
        )
        candidate_matches.append(
            (candidate, resolved_type, modified_matches, covered_existing)
        )
        if modified_matches or covered_existing:
            represented_claim_ids.update(candidate.claim_ids)

    for candidate, resolved_type, modified_matches, covered_existing in candidate_matches:
        matches = list({page.path: page for page in [*covered_existing, *modified_matches]}.values())
        matched_paths = sorted(page.path for page in matches)
        consolidated = bool(
            not matches
            and candidate.claim_ids
            and set(candidate.claim_ids) <= represented_claim_ids
        )
        if modified_matches:
            disposition = "modified"
        elif covered_existing:
            disposition = "already-covered"
        elif consolidated:
            disposition = "consolidated"
        else:
            disposition = "missing"
        candidate_rows.append(
            {
                "name": candidate.name,
                "page_type": candidate.page_type,
                "resolved_page_type": resolved_type,
                "type_reconciled": resolved_type != candidate.page_type,
                "importance": candidate.importance,
                "central": candidate.central,
                "claim_ids": list(candidate.claim_ids),
                "disposition": disposition,
                "consolidated": consolidated,
                "matched_paths": matched_paths,
            }
        )
        if not matches:
            if consolidated:
                warnings.append(
                    Issue(
                        "coverage.candidate_consolidated",
                        "candidate was not edited separately, but every assigned claim "
                        "is covered by another represented candidate",
                        candidate=f"{candidate.page_type}:{candidate.name}",
                    )
                )
                continue
            if candidate.importance == HIGH_IMPORTANCE:
                warnings.append(
                    Issue(
                        "coverage.recommended_candidate_missing",
                        "importance-4 candidate was neither modified nor "
                        "deterministically already covered and owns an "
                        "unconsolidated claim; review is recommended",
                        candidate=f"{candidate.page_type}:{candidate.name}",
                    )
                )
                continue
            if has_modified_candidate or allow_no_changes:
                warnings.append(
                    Issue(
                        "coverage.required_candidate_omitted",
                        "high-importance candidate was not represented in the "
                        "renderer synthesis and owns an unconsolidated claim; "
                        "review is recommended",
                        candidate=f"{candidate.page_type}:{candidate.name}",
                    )
                )
                continue
            errors.append(
                Issue(
                    "coverage.required_candidate_missing",
                    f"required/high-importance {candidate.page_type} candidate is not "
                    "represented by a modified page or deterministic already-covered "
                    "page and has claims not covered by another represented candidate",
                    candidate=f"{candidate.page_type}:{candidate.name}",
                )
            )
            continue
        represented += 1
        if covered_existing and not modified_matches:
            already_covered_count += 1
            warnings.append(
                Issue(
                    "coverage.already_covered",
                    "unchanged existing page deterministically contains every assigned claim",
                    candidate=f"{resolved_type}:{candidate.name}",
                )
            )
        if len(matches) > 1:
            errors.append(
                Issue(
                    "coverage.candidate_ambiguous",
                    f"candidate matches multiple pages: {', '.join(matched_paths)}; "
                    "resolve alias ownership before recording coverage",
                    candidate=f"{candidate.page_type}:{candidate.name}",
                )
            )
        if candidate.central:
            central_paths.update(page.path for page in modified_matches)

    by_path = {page.path: page for page in parsed}
    for path in sorted(central_paths):
        page = by_path[path]
        paragraph_count, sentence_count, alphanumeric = _fact_card_metrics(page.paragraphs)
        if paragraph_count == 0:
            errors.append(
                Issue(
                    "central_page.no_substantive_prose",
                    "required central page has no substantive callout prose",
                    path,
                )
            )
        elif (
            paragraph_count == 1
            and sentence_count <= 1
            and alphanumeric <= FACT_CARD_MAX_ALNUMERIC_CHARS
        ):
            errors.append(
                Issue(
                    "central_page.fact_card",
                    "required central page is an obvious short one-sentence fact card",
                    path,
                )
            )
        elif paragraph_count == 1 and sentence_count <= 1:
            warnings.append(
                Issue(
                    "central_page.single_sentence",
                    "central page has only one substantive sentence; length kept this a warning",
                    path,
                )
            )

    errors.sort(key=_issue_sort_key)
    warnings.sort(key=_issue_sort_key)
    return {
        "schema": RECEIPT_SCHEMA,
        "ok": not errors,
        "source_id": source_id,
        "section_label": section_label,
        "modified": sorted(
            page.path for page in pages if page.disposition == "modified"
        ),
        "summary": {
            "modified_pages": sum(
                page.disposition == "modified" for page in pages
            ),
            "modified_substantive_paragraphs": modified_count,
            "required_candidates": len(required_candidates),
            "represented_candidates": represented,
            "already_covered_candidates": already_covered_count,
        },
        "candidates": candidate_rows,
        "errors": [issue.as_dict() for issue in errors],
        "warnings": [issue.as_dict() for issue in warnings],
    }
