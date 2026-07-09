"""Ingest control-plane and job routes."""
from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from .. import ingest_runner as ir
from .. import settings
from ..auth import require_auth
from ..validation import json_object, normalize_ingest_options, optional_string, parse_json_object

router = APIRouter()


async def _write_upload(file: UploadFile, dest: Path) -> int:
    total = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(settings.UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > settings.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"upload exceeds PW_MAX_UPLOAD_MB={settings.MAX_UPLOAD_MB}")
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return total


@router.post("/ingest")
async def ingest(
    request: Request,
    file: UploadFile | None = File(None),
    options: str | None = Form(None),
    x_auth_token: str | None = Header(None),
):
    require_auth(x_auth_token)
    if file is not None:
        opts = normalize_ingest_options(parse_json_object(options, "options"))
        settings.STAGE_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = Path((file.filename or "upload").replace("\\", "/")).name
        if safe_name in ("", ".", ".."):
            safe_name = "upload"
        dest = settings.STAGE_DIR / f"{dt.datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')}-{safe_name}"
        await _write_upload(file, dest)
        target = str(dest)
    else:
        body = await json_object(request)
        target = optional_string(body.get("url"), "url").strip()
        opts = normalize_ingest_options(body.get("options"))
        if not target:
            raise HTTPException(400, "provide a file or a url")
    job_id = ir.start_job(target, opts)
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
def job_status(job_id: str, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    job = ir.get_job(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return {
        "id": job.id,
        "status": job.status,
        "lines": job.visible_lines(),
        "dropped_lines": job.dropped_lines,
        "result": job.result,
    }


@router.get("/jobs/{job_id}/events")
async def job_events(
    job_id: str,
    request: Request,
    x_auth_token: str | None = Header(None),
):
    require_auth(x_auth_token)
    job = ir.get_job(job_id)
    if not job:
        raise HTTPException(404, "unknown job")

    async def gen():
        cursor = 0
        while True:
            for seq, line in job.events_after(cursor):
                cursor = seq + 1
                if line == "__END__":
                    yield f"event: done\ndata: {job.status}\n\n"
                    return
                yield f"data: {line}\n\n"
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    job = await ir.cancel_job(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return {"id": job.id, "status": job.status, "result": job.result}


@router.get("/preflight")
def preflight(kind: str = "auto", x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    kind = normalize_ingest_options({"kind": kind})["kind"]
    ok, msg, offending = ir.preflight({"kind": kind})
    return {"ok": ok, "message": msg, "offending": offending}
