"""Authentication helpers for private backend routes."""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from . import settings


def require_auth(x_auth_token: str | None = Header(None)):
    if not settings.AUTH_TOKEN:
        raise HTTPException(503, "PW_AUTH_TOKEN must be set on the backend")
    supplied = (x_auth_token or "").encode("utf-8")
    expected = settings.AUTH_TOKEN.encode("utf-8")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "missing or invalid auth token")
