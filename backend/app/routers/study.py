"""Vocabulary bank, FSRS review, and export routes."""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import logging

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from .. import db
from ..auth import require_auth
from ..fsrs import Card, schedule
from ..validation import json_object, optional_string

router = APIRouter()
LOGGER = logging.getLogger(__name__)


def _add_vocab_item(kind: str, lemma: str, payload: dict) -> tuple[int, str]:
    key = db.normalize_key(kind, lemma)
    conn = db.connect()
    try:
        cur = conn.execute(
            """INSERT INTO items(kind,norm_key,lemma,reading,pos,gloss,example,source_id,anchor,created,due)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(kind,norm_key) DO UPDATE SET
                 reading=COALESCE(NULLIF(excluded.reading,''),items.reading),
                 pos=COALESCE(NULLIF(excluded.pos,''),items.pos),
                 gloss=COALESCE(NULLIF(excluded.gloss,''),items.gloss),
                 example=COALESCE(NULLIF(excluded.example,''),items.example),
                 source_id=COALESCE(NULLIF(excluded.source_id,''),items.source_id),
                 anchor=COALESCE(NULLIF(excluded.anchor,''),items.anchor)
               RETURNING id""",
            (
                kind,
                key,
                lemma,
                payload.get("reading"),
                payload.get("pos"),
                payload.get("gloss"),
                payload.get("example"),
                payload.get("source_id"),
                payload.get("anchor"),
                db.now_iso(),
                db.today().isoformat(),
            ),
        )
        item_id = cur.fetchone()[0]
        conn.commit()
        return item_id, key
    finally:
        conn.close()


def _update_vocab_item(item_id: int, fields: dict[str, str], status: str | None):
    sets: list[str] = []
    vals: list[str | int] = []
    for field in ("reading", "pos", "gloss", "example", "source_id", "anchor"):
        if field in fields:
            sets.append(f"{field}=?")
            vals.append(fields[field])
    if status == "known":
        sets.extend(["status=?", "state=1", "due=NULL", "last_review=?"])
        vals.extend([status, db.now_iso()])
    elif status == "new":
        sets.extend([
            "status=?",
            "state=0",
            "stability=0",
            "difficulty=0",
            "reps=0",
            "lapses=0",
            "due=?",
            "last_review=NULL",
        ])
        vals.extend([status, db.today().isoformat()])
    elif status == "learning":
        sets.append("status=?")
        vals.append(status)
    if not sets:
        return "nothing"

    conn = db.connect()
    try:
        vals.append(item_id)
        cur = conn.execute(f"UPDATE items SET {','.join(sets)} WHERE id=?", vals)
        conn.commit()
        if cur.rowcount == 0:
            return None
        return conn.execute("SELECT kind,norm_key,status FROM items WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()


def _list_vocab(kind: str | None):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT * FROM items" + (" WHERE kind=?" if kind else "") + " ORDER BY created DESC",
            (kind,) if kind else (),
        ).fetchall()
    finally:
        conn.close()


def _review_queue(limit: int):
    conn = db.connect()
    try:
        today = db.today().isoformat()
        return conn.execute(
            "SELECT * FROM items WHERE state=0 OR (due IS NOT NULL AND due<=?) ORDER BY due ASC LIMIT ?",
            (today, limit),
        ).fetchall()
    finally:
        conn.close()


def _grade_item(item_id: int, grade: int):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            conn.rollback()
            return None
        elapsed = 0.0
        if row["last_review"]:
            last = db.local_date_from_iso(row["last_review"])
            elapsed = (db.today() - last).days
        card = Card(
            stability=row["stability"],
            difficulty=row["difficulty"],
            state=row["state"],
            reps=row["reps"],
            lapses=row["lapses"],
        )
        card, interval = schedule(card, grade, elapsed)
        due = (db.today() + dt.timedelta(days=interval)).isoformat()
        status = "known" if interval >= 21 else "learning"
        conn.execute(
            """UPDATE items SET stability=?,difficulty=?,state=?,reps=?,lapses=?,due=?,last_review=?,status=?
               WHERE id=?""",
            (
                card.stability,
                card.difficulty,
                card.state,
                card.reps,
                card.lapses,
                due,
                db.now_iso(),
                status,
                item_id,
            ),
        )
        conn.execute(
            "INSERT INTO reviews(item_id,grade,reviewed,interval) VALUES(?,?,?,?)",
            (item_id, grade, db.now_iso(), interval),
        )
        conn.commit()
        return {
            "interval": interval,
            "due": due,
            "kind": row["kind"],
            "norm_key": row["norm_key"],
        }
    finally:
        conn.close()


def _review_stats() -> dict:
    conn = db.connect()
    try:
        today = db.today().isoformat()
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        new = conn.execute("SELECT COUNT(*) FROM items WHERE state=0").fetchone()[0]
        due = conn.execute(
            "SELECT COUNT(*) FROM items WHERE state=0 OR (due IS NOT NULL AND due<=?)",
            (today,),
        ).fetchone()[0]
        return {"total": total, "new": new, "due": due}
    finally:
        conn.close()


def _export_rows():
    conn = db.connect()
    try:
        return conn.execute("SELECT lemma,reading,gloss,example,kind,source_id FROM items").fetchall()
    finally:
        conn.close()


@router.post("/vocab")
async def add_vocab(request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    body = await json_object(request)
    kind = optional_string(body.get("kind"), "kind", "word")
    if kind not in ("word", "grammar"):
        raise HTTPException(400, "kind must be word or grammar")
    lemma = optional_string(body.get("lemma"), "lemma").strip()
    if not lemma:
        raise HTTPException(400, "lemma required")
    payload = {
        field: optional_string(body[field], field)
        for field in ("reading", "pos", "gloss", "example", "source_id", "anchor")
        if field in body
    }
    item_id, key = await asyncio.to_thread(_add_vocab_item, kind, lemma, payload)
    LOGGER.info("study vocab upsert item_id=%s kind=%s norm_key=%s", item_id, kind, key)
    return {"id": item_id, "norm_key": key}


@router.patch("/vocab/{item_id}")
async def update_vocab(item_id: int, request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    body = await json_object(request)
    status = body.get("status")
    if status is not None and status not in ("new", "learning", "known"):
        raise HTTPException(400, "invalid status")
    fields = {
        field: optional_string(body[field], field)
        for field in ("reading", "pos", "gloss", "example", "source_id", "anchor")
        if field in body
    }
    row = await asyncio.to_thread(_update_vocab_item, item_id, fields, status)
    if row == "nothing":
        raise HTTPException(400, "nothing to update")
    if row is None:
        raise HTTPException(404, "unknown item")
    LOGGER.info(
        "study vocab update item_id=%s kind=%s norm_key=%s status=%s",
        item_id,
        row["kind"],
        row["norm_key"],
        row["status"],
    )
    return {"ok": True}


@router.get("/vocab")
async def list_vocab(kind: str | None = None, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    rows = await asyncio.to_thread(_list_vocab, kind)
    return [dict(r) for r in rows]


@router.get("/review/queue")
async def review_queue(limit: int = Query(40, ge=1, le=200), x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    rows = await asyncio.to_thread(_review_queue, limit)
    return [dict(r) for r in rows]


@router.post("/review/{item_id}/grade")
async def grade(item_id: int, request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    body = await json_object(request)
    try:
        grade_value = int(body.get("grade", 3))
    except (TypeError, ValueError):
        raise HTTPException(400, "grade must be an integer from 1 to 4")
    if grade_value < 1 or grade_value > 4:
        raise HTTPException(400, "grade must be an integer from 1 to 4")
    result = await asyncio.to_thread(_grade_item, item_id, grade_value)
    if result is None:
        raise HTTPException(404, "unknown item")
    LOGGER.info(
        "study review grade item_id=%s kind=%s norm_key=%s grade=%s interval=%s",
        item_id,
        result["kind"],
        result["norm_key"],
        grade_value,
        result["interval"],
    )
    return {"ok": True, "interval": result["interval"], "due": result["due"]}


@router.get("/review/stats")
async def review_stats(x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    return await asyncio.to_thread(_review_stats)


@router.get("/export")
async def export(format: str = "csv", x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    if format != "csv":
        raise HTTPException(400, "unsupported export format")
    rows = await asyncio.to_thread(_export_rows)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["lemma", "reading", "gloss", "example", "kind", "source_id"])
    for row in rows:
        writer.writerow([row["lemma"], row["reading"], row["gloss"], row["example"], row["kind"], row["source_id"]])
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=wordbank.csv"},
    )
