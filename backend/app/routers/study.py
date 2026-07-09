"""Vocabulary bank, FSRS review, and export routes."""
from __future__ import annotations

import csv
import datetime as dt
import io

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from .. import db
from ..auth import require_auth
from ..fsrs import Card, schedule
from ..validation import json_object, optional_string

router = APIRouter()


@router.post("/vocab")
async def add_vocab(request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    b = await json_object(request)
    kind = b.get("kind", "word")
    if kind not in ("word", "grammar"):
        raise HTTPException(400, "kind must be word or grammar")
    lemma = optional_string(b.get("lemma"), "lemma").strip()
    if not lemma:
        raise HTTPException(400, "lemma required")
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
                b.get("reading"),
                b.get("pos"),
                b.get("gloss"),
                b.get("example"),
                b.get("source_id"),
                b.get("anchor"),
                db.now_iso(),
                db.today().isoformat(),
            ),
        )
        item_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {"id": item_id, "norm_key": key}


@router.patch("/vocab/{item_id}")
async def update_vocab(item_id: int, request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    b = await json_object(request)
    status = b.get("status")
    if status not in ("new", "learning", "known"):
        raise HTTPException(400, "invalid status")
    conn = db.connect()
    if status == "known":
        cur = conn.execute(
            "UPDATE items SET status=?,state=1,due=NULL,last_review=? WHERE id=?",
            (status, db.now_iso(), item_id),
        )
    elif status == "new":
        cur = conn.execute(
            """UPDATE items SET status=?,state=0,stability=0,difficulty=0,reps=0,lapses=0,
               due=?,last_review=NULL WHERE id=?""",
            (status, db.today().isoformat(), item_id),
        )
    else:
        cur = conn.execute("UPDATE items SET status=? WHERE id=?", (status, item_id))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "unknown item")
    return {"ok": True}


@router.get("/vocab")
def list_vocab(kind: str | None = None, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM items" + (" WHERE kind=?" if kind else "") + " ORDER BY created DESC",
        (kind,) if kind else (),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/review/queue")
def review_queue(limit: int = Query(40, ge=1, le=200), x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    conn = db.connect()
    today = db.today().isoformat()
    rows = conn.execute(
        "SELECT * FROM items WHERE state=0 OR (due IS NOT NULL AND due<=?) ORDER BY due ASC LIMIT ?",
        (today, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/review/{item_id}/grade")
async def grade(item_id: int, request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    b = await json_object(request)
    try:
        g = int(b.get("grade", 3))
    except (TypeError, ValueError):
        raise HTTPException(400, "grade must be an integer from 1 to 4")
    if g < 1 or g > 4:
        raise HTTPException(400, "grade must be an integer from 1 to 4")
    conn = db.connect()
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "unknown item")
    elapsed = 0.0
    if row["last_review"]:
        last = dt.datetime.strptime(row["last_review"][:10], "%Y-%m-%d").date()
        elapsed = (db.today() - last).days
    card = Card(
        stability=row["stability"],
        difficulty=row["difficulty"],
        state=row["state"],
        reps=row["reps"],
        lapses=row["lapses"],
    )
    card, ivl = schedule(card, g, elapsed)
    due = (db.today() + dt.timedelta(days=ivl)).isoformat()
    status = "known" if ivl >= 21 else "learning"
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
        (item_id, g, db.now_iso(), ivl),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "interval": ivl, "due": due}


@router.get("/review/stats")
def review_stats(x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    conn = db.connect()
    today = db.today().isoformat()
    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    new = conn.execute("SELECT COUNT(*) FROM items WHERE state=0").fetchone()[0]
    due = conn.execute(
        "SELECT COUNT(*) FROM items WHERE state=0 OR (due IS NOT NULL AND due<=?)",
        (today,),
    ).fetchone()[0]
    conn.close()
    return {"total": total, "new": new, "due": due}


@router.get("/export")
def export(format: str = "csv", x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    if format != "csv":
        raise HTTPException(400, "unsupported export format")
    conn = db.connect()
    rows = conn.execute("SELECT lemma,reading,gloss,example,kind,source_id FROM items").fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["lemma", "reading", "gloss", "example", "kind", "source_id"])
    for r in rows:
        w.writerow([r["lemma"], r["reading"], r["gloss"], r["example"], r["kind"], r["source_id"]])
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=wordbank.csv"},
    )
