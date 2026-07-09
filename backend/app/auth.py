"""Authentication helpers for private backend routes."""
from __future__ import annotations

from fastapi import HTTPException

from . import settings


def require_auth(token: str | None):
    if not settings.AUTH_TOKEN:
        raise HTTPException(503, "PW_AUTH_TOKEN must be set on the backend")
    if token != settings.AUTH_TOKEN:
        raise HTTPException(401, "missing or invalid auth token")
