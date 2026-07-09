"""Personal-wiki backend app assembly.

Runs locally on the Mac, reached only over Tailscale. `/health` is open for
supervision; routes that expose private state, mutate data, run ingest, or spend
LLM budget require `PW_AUTH_TOKEN` and fail closed when it is not configured.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import settings
from .routers import annotations, health, ingest, llm, study

app = FastAPI(title="Personal Wiki backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(study.router)
app.include_router(llm.router)
app.include_router(annotations.router)
