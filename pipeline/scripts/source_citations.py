"""Source-citation encoding and parsing shared by pipeline consumers.

Section labels use ``#sec=<percent-encoded UTF-8>``.  Encoding makes citation
list delimiters unambiguous while legacy ``#plain`` and structured media
anchors remain readable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote


BRACKETED_CITATION_RX = re.compile(r"\[([^\[\]]*\bsrc:[^\[\]]*)\]")
SOURCE_ID_RX = re.compile(r"^[A-Z0-9]{26}$")


@dataclass(frozen=True)
class SourceCitation:
    source_id: str
    anchor: str = ""
    raw_anchor: str = ""


def encode_section_anchor(label: str) -> str:
    """Return the canonical delimiter-safe anchor for a section label."""
    return "sec=" + quote(str(label), safe="")


def decode_source_anchor(anchor: str) -> str:
    """Decode canonical section anchors; preserve legacy/media anchors."""
    value = str(anchor or "").strip().removeprefix("#")
    return unquote(value[4:]) if value.startswith("sec=") else value


def source_citation_ref(source_id: str, section_label: str = "") -> str:
    """Build ``src:<id>`` with a canonical encoded section anchor if present."""
    ref = f"src:{source_id}"
    return f"{ref}#{encode_section_anchor(section_label)}" if section_label else ref


def source_citation(source_id: str, section_label: str = "") -> str:
    """Build one bracketed source citation."""
    return f"[{source_citation_ref(source_id, section_label)}]"


def parse_citation_parts(value: str) -> list[SourceCitation]:
    """Parse the interior of one citation group.

    Commas are safe delimiters because canonical section labels encode them.
    A repeated ``src:`` prefix is optional for backward compatibility.
    """
    citations: list[SourceCitation] = []
    for raw_part in str(value or "").split(","):
        part = re.sub(r"^src:\s*", "", raw_part.strip(), flags=re.IGNORECASE)
        if not part:
            continue
        source_id, separator, raw_anchor = part.partition("#")
        source_id = source_id.strip()
        if not source_id:
            continue
        raw_anchor = raw_anchor.strip() if separator else ""
        citations.append(SourceCitation(
            source_id=source_id,
            anchor=decode_source_anchor(raw_anchor),
            raw_anchor=raw_anchor,
        ))
    return citations


def iter_source_citations(text: str) -> list[SourceCitation]:
    """Return citations in bracket/body order, including duplicates."""
    citations: list[SourceCitation] = []
    for match in BRACKETED_CITATION_RX.finditer(text):
        citations.extend(parse_citation_parts(match.group(1)))
    return citations
