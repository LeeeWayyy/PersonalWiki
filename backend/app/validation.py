"""Shared request validation helpers for backend routes."""
from __future__ import annotations

import json
import unicodedata

from fastapi import HTTPException, Request

from . import settings


_SECTION_KINDS = {"auto", "wiki"}
_SECTION_MAX_CHARS = 200


async def json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    return body


def parse_json_object(value: str | None, field: str) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"{field} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(400, f"{field} must be a JSON object")
    return parsed


def optional_object(value, field: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(400, f"{field} must be a JSON object")
    return value


def optional_string(value, field: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise HTTPException(400, f"{field} must be a string")
    return value


def _optional_section(value, field: str) -> str | None:
    raw = optional_string(value, field)
    if any(unicodedata.category(char) == "Cc" for char in raw):
        raise HTTPException(400, f"{field} must not contain control characters or newlines")
    normalized = raw.strip()
    if len(normalized) > _SECTION_MAX_CHARS:
        raise HTTPException(400, f"{field} must be <= {_SECTION_MAX_CHARS} characters")
    return normalized or None


def normalize_ingest_options(value) -> dict:
    options = optional_object(value, "options")
    kind = optional_string(options.get("kind"), "options.kind", "auto").strip() or "auto"
    if kind == "media":
        kind = "video"
    if kind not in settings.INGEST_KINDS:
        raise HTTPException(400, "options.kind must be auto, wiki, lang, video, audio, or image_note")
    if options.get("section_label") is not None:
        raise HTTPException(400, "options.section_label is not supported; use options.section_heading")
    section_heading = _optional_section(options.get("section_heading"), "options.section_heading")
    if kind not in _SECTION_KINDS and section_heading:
        raise HTTPException(400, f"section selection is not supported for {kind} ingest")
    return {
        "kind": kind,
        "section_heading": section_heading,
    }
