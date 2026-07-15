"""Health route."""
from __future__ import annotations

from fastapi import APIRouter

from .. import ingest_runner as ir
from .. import llm
from .. import settings

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
