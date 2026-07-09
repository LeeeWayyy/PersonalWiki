"""Health and LLM probe routes."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from .. import ingest_runner as ir
from .. import llm
from .. import settings
from ..auth import require_auth

router = APIRouter()


@router.get("/health")
def health():
    return {
        "ok": True,
        "auth": bool(settings.AUTH_TOKEN),
        "stub": ir.STUB,
        "llm": llm.configured(),
        "llm_provider": llm.provider(),
        "content": str(ir.CONTENT_DIR),
    }


@router.get("/health/llm")
async def health_llm(x_auth_token: str | None = Header(None)):
    """Probe the local LLM command path for daemon/headless auth debugging."""
    require_auth(x_auth_token)
    if not llm.command_configured():
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "configured": False,
                "provider": llm.provider(),
                "model": llm.model(),
                "message": "Local LLM command/provider is not configured; /health/llm probes local LLM only",
            },
        )
    prompt = "Reply with exactly: ok"
    started = time.monotonic()
    try:
        out = await asyncio.to_thread(llm.complete_command, prompt, timeout=settings.LLM_HEALTH_TIMEOUT_S)
    except Exception as e:  # noqa
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "configured": True,
                "provider": llm.provider(),
                "model": llm.model(),
                "latency_ms": round((time.monotonic() - started) * 1000),
                "error": str(e),
            },
        )
    cleaned = (out or "").strip()
    if not cleaned:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "configured": True,
                "provider": llm.provider(),
                "model": llm.model(),
                "latency_ms": round((time.monotonic() - started) * 1000),
                "error": "Local LLM provider exited successfully but returned no output",
            },
        )
    return {
        "ok": True,
        "configured": True,
        "provider": llm.provider(),
        "model": llm.model(),
        "latency_ms": round((time.monotonic() - started) * 1000),
        "matched_expected": cleaned.lower().strip(".") == "ok",
        "output_preview": cleaned[:120],
    }
