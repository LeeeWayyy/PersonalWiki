"""Ingest control-plane and job routes."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from .. import ingest_runner as ir
from .. import settings
from ..auth import require_auth
from ..validation import json_object, normalize_ingest_options, optional_string, parse_json_object

router = APIRouter(dependencies=[Depends(require_auth)])


def _sse_data(line: str) -> str:
    parts = line.splitlines() or [""]
    return "".join(f"data: {part}\n" for part in parts) + "\n"


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
                await asyncio.to_thread(out.write, chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return total


async def _stage_upload(file: UploadFile) -> Path:
    settings.STAGE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path((file.filename or "upload").replace("\\", "/")).name
    if safe_name in ("", ".", ".."):
        safe_name = "upload"
    stage = settings.STAGE_DIR / dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    stage.mkdir()
    dest = stage / safe_name
    try:
        await _write_upload(file, dest)
    except BaseException:
        dest.unlink(missing_ok=True)
        stage.rmdir()
        raise
    return dest


def _validated_url(target: str) -> str:
    if not target:
        raise HTTPException(400, "provide a file or a url")
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(400, "url must start with http:// or https://")
    return target


@router.post("/ingest")
async def ingest(
    request: Request,
    file: UploadFile | None = File(None),
    options: str | None = Form(None),
):
    if file is not None:
        opts = normalize_ingest_options(parse_json_object(options, "options"))
        target = str(await _stage_upload(file))
    else:
        body = await json_object(request)
        opts = normalize_ingest_options(body.get("options"))
        target = _validated_url(optional_string(body.get("url"), "url").strip())
    job_id = ir.start_job(target, opts)
    return {"job_id": job_id}


_SOURCE_ID_RX = re.compile(r"[A-Za-z0-9_-]{1,64}")


@router.post("/lang/merge")
async def lang_merge(request: Request):
    """Merge a book lang reader with an audio one into a NEW committed reader.

    Long (LLM calibration ~30 min), so it runs as a background job like ingest —
    stream progress over GET /jobs/{id}/events. The book supplies text/grammar/
    vocab (upgraded to natural kanji), the audio supplies the timeline.
    """
    body = await json_object(request)
    book_id = optional_string(body.get("book_id"), "book_id").strip()
    audio_id = optional_string(body.get("audio_id"), "audio_id").strip()
    if not _SOURCE_ID_RX.fullmatch(book_id) or not _SOURCE_ID_RX.fullmatch(audio_id):
        raise HTTPException(400, "book_id and audio_id must be valid source ids")
    if book_id == audio_id:
        raise HTTPException(400, "book_id and audio_id must differ")
    # calibrate=False → the "book" is a hand-calibrated transcript (already natural
    # kanji): align to the audio timeline only, skip the kana→kanji LLM pass.
    opts = {"kind": "lang-merge", "book_id": book_id, "audio_id": audio_id,
            "refresh": bool(body.get("refresh")),
            "calibrate": body.get("calibrate") is not False}
    return {"job_id": ir.start_job("", opts)}


_DELETE_LANG_SCRIPT = ir.REPO / "pipeline" / "scripts" / "delete-lang-source.py"


@router.post("/lang/source/remove")
async def lang_source_remove(request: Request):
    """Delete one lang reader (its pages + sidecar/asset) after a confirmation.

    Fast (rm + commit), so it runs synchronously under the ingest lock like
    /wiki/page/remove, then rebuilds the site.
    """
    body = await json_object(request)
    source_id = optional_string(body.get("source_id"), "source_id").strip()
    confirmation = optional_string(body.get("confirmation"), "confirmation").strip()
    if not _SOURCE_ID_RX.fullmatch(source_id):
        raise HTTPException(400, "source_id must be a valid source id")
    if confirmation != source_id:
        raise HTTPException(400, "confirmation must exactly match source_id")

    async with ir.LOCK:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["uv", "run", str(_DELETE_LANG_SCRIPT), "--source-id", source_id],
            capture_output=True, text=True, timeout=ir.JOB_TIMEOUT_S, cwd=str(ir.CONTENT_DIR),
            env={**os.environ, "PW_CONTENT_DIR": str(ir.CONTENT_DIR),
                 "VAULT_CONTENT_DIR": str(ir.CONTENT_DIR)},
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "delete failed").strip().splitlines()
            raise HTTPException(422, detail[-1] if detail else "delete failed")

        rebuilt = False
        rebuild_warning = None
        if ir.REBUILD_CMD:
            rebuild = await asyncio.to_thread(
                subprocess.run, ir.REBUILD_CMD, shell=True,
                capture_output=True, text=True, timeout=ir.JOB_TIMEOUT_S,
            )
            rebuilt = rebuild.returncode == 0
            if not rebuilt:
                lines = (rebuild.stderr or rebuild.stdout or "site rebuild failed").strip().splitlines()
                rebuild_warning = lines[-1] if lines else "site rebuild failed"
    return {"removed": source_id, "rebuilt": rebuilt,
            **({"rebuild_warning": rebuild_warning} if rebuild_warning else {})}


_EXTRACT_SCRIPT = ir.REPO / "pipeline" / "scripts" / "extract.py"
_SOURCE_IDENTITY_SCRIPT = ir.REPO / "pipeline" / "scripts" / "source-identity.py"
_SECTIONS_TIMEOUT_S = 120


async def _run_sections_tool(*argv: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_SECTIONS_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            504, f"section listing timed out after {_SECTIONS_TIMEOUT_S}s"
        ) from exc
    finally:
        if proc.returncode is None:
            await ir._kill_process_group(proc, lambda _line: None, "section listing")
    if proc.returncode != 0:
        detail = (err or out).decode(errors="replace").strip().splitlines()
        raise HTTPException(
            422,
            "section listing failed: " + (detail[-1] if detail else f"exit code {proc.returncode}"),
        )
    return out


async def _list_sections(target: str) -> list[str]:
    """Fetch safely when needed, then run extract.py locally without vault writes."""
    fetched: Path | None = None
    try:
        if urlparse(target).scheme in {"http", "https"}:
            settings.STAGE_DIR.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="sections-", suffix=".html", dir=settings.STAGE_DIR)
            os.close(fd)
            fetched = Path(name)
            await _run_sections_tool(
                str(_SOURCE_IDENTITY_SCRIPT), "--fetch-only", target, str(fetched),
            )
            target = str(fetched)
        out = await _run_sections_tool(str(_EXTRACT_SCRIPT), target, "--list-sections")
        return [line.rstrip() for line in out.decode(errors="replace").splitlines() if line.strip()]
    finally:
        if fetched is not None:
            fetched.unlink(missing_ok=True)


@router.post("/ingest/sections")
async def ingest_sections(
    request: Request,
    file: UploadFile | None = File(None),
):
    """List a source's `## ` section headings so the UI can offer a chapter picker."""
    staged: Path | None = None
    if file is not None:
        staged = await _stage_upload(file)
        target = str(staged)
    else:
        body = await json_object(request)
        target = _validated_url(optional_string(body.get("url"), "url").strip())
    try:
        sections = await _list_sections(target)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
            staged.parent.rmdir()
    return {"sections": sections}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str):
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
):
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
                yield _sse_data(line)
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = await ir.cancel_job(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return {"id": job.id, "status": job.status, "result": job.result}


@router.get("/preflight")
def preflight(kind: str = "auto"):
    kind = normalize_ingest_options({"kind": kind})["kind"]
    ok, msg, offending = ir.preflight({"kind": kind})
    return {"ok": ok, "message": msg, "offending": offending}
