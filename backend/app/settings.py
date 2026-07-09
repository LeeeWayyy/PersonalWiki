"""Runtime settings for the personal-wiki backend.

Values are intentionally read at import time to match the existing backend
startup behavior and the test suite's environment setup.
"""
from __future__ import annotations

import os
from pathlib import Path

AUTH_TOKEN = os.environ.get("PW_AUTH_TOKEN", "")
STAGE_DIR = Path(
    os.environ.get("PW_STAGE_DIR", Path(__file__).resolve().parent.parent / "data" / "stage")
)
MAX_UPLOAD_MB = int(os.environ.get("PW_MAX_UPLOAD_MB", "250"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
TRANSLATE_LANG = os.environ.get("PW_TRANSLATE_LANG", "Simplified Chinese")
TRANSLATE_PROMPT_VERSION = os.environ.get("PW_TRANSLATE_PROMPT_VERSION", "translate:v1")
ASSIST_PROMPT_VERSION = os.environ.get("PW_ASSIST_PROMPT_VERSION", "assist:v1")
LLM_HEALTH_TIMEOUT_S = int(os.environ.get("PW_LLM_HEALTH_TIMEOUT_S", "20"))
DEFAULT_CORS = "http://localhost:4321,http://127.0.0.1:4321"
INGEST_KINDS = {"auto", "wiki", "lang", "video", "audio", "image_note"}


def cors_origins() -> list[str]:
    return [o.strip() for o in os.environ.get("PW_CORS", DEFAULT_CORS).split(",") if o.strip()]
