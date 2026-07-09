"""Shared request validation helpers for backend routes."""
from __future__ import annotations

import json

from fastapi import HTTPException, Request

from . import settings


async def json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
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


def normalize_ingest_options(value) -> dict:
    options = optional_object(value, "options")
    kind = optional_string(options.get("kind"), "options.kind", "auto").strip() or "auto"
    if kind == "media":
        kind = "video"
    if kind not in settings.INGEST_KINDS:
        raise HTTPException(400, "options.kind must be auto, wiki, lang, video, audio, or image_note")
    section = optional_string(options.get("section_label"), "options.section_label").strip() or None
    if kind == "lang" and section:
        raise HTTPException(400, "options.section_label is not supported for lang ingest")
    return {"kind": kind, "section_label": section}
