"""Personal-wiki backend app assembly.

Runs locally on the Mac, reached only over Tailscale. `/health` is open for
supervision; routes that expose private state, mutate data, run ingest, or spend
LLM budget require `PW_AUTH_TOKEN` and fail closed when it is not configured.
"""
from __future__ import annotations

import logging
import mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import ingest_runner, settings
from .routers import annotations, health, ingest, llm, study

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ingest_runner.sweep_stage_dir()
    try:
        yield
    finally:
        await ingest_runner.shutdown_jobs()


app = FastAPI(title="Personal Wiki backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

mimetypes.add_type("audio/mp4", ".m4a")  # default guess audio/mp4a-latm breaks <audio>

# Audio blobs for the site reader's synced player (lang/sources/.media/ is
# gitignored, so neither git nor the Astro build carries them). Read-only and
# unauthenticated like the site itself: <audio src> cannot send auth headers,
# and StaticFiles gives us the Range requests seeking needs.
app.mount("/media",
          StaticFiles(directory=ingest_runner.CONTENT_DIR / "lang" / "sources" / ".media",
                      check_dir=False),
          name="media")

app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(study.router)
app.include_router(llm.router)
app.include_router(annotations.router)
