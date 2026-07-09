"""Source Reader annotation routes."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Header, HTTPException, Request

from .. import db
from .. import ingest_runner as ir
from .. import promote as promote_mod
from ..auth import require_auth
from ..validation import json_object, optional_object, optional_string

router = APIRouter()

ALLOWED_ANNOTATION_COLORS = {"note", "question", "important"}


def _validate_annotation_color(color: str | None) -> str:
    color = color or "note"
    if color not in ALLOWED_ANNOTATION_COLORS:
        raise HTTPException(400, "invalid annotation color")
    return color


def _validate_tags(tags) -> list[str]:
    if tags is None:
        return []
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise HTTPException(400, "tags must be a list of strings")
    return tags


def _valid_wiki_rel(rel: str) -> bool:
    if not rel or rel.startswith("/") or "\\" in rel:
        return False
    return all(part not in ("", ".", "..") for part in rel.split("/"))


def _validate_links(links) -> list[dict]:
    if links is None:
        return []
    if not isinstance(links, list):
        raise HTTPException(400, "links must be a list")
    out = []
    for link in links:
        if not isinstance(link, dict):
            raise HTTPException(400, "links must contain objects")
        if link.get("type") != "human-zone":
            raise HTTPException(400, "unsupported annotation link type")
        wiki_rel = link.get("wiki_rel")
        href = link.get("href")
        if not isinstance(wiki_rel, str) or not _valid_wiki_rel(wiki_rel):
            raise HTTPException(400, "invalid annotation link wiki_rel")
        if not isinstance(href, str) or not href.startswith("/wiki/"):
            raise HTTPException(400, "invalid annotation link href")
        out.append({"type": "human-zone", "wiki_rel": wiki_rel, "href": href})
    return out


def _annotation_dict(r) -> dict:
    sel = {
        "quote": r["quote"],
        "prefix": r["prefix"],
        "suffix": r["suffix"],
        "start": r["sel_start"],
        "end": r["sel_end"],
    }
    region = r["region"]
    if region:
        try:
            sel["region"] = json.loads(region)
        except (TypeError, ValueError):
            pass
    return {
        "id": r["id"],
        "source_id": r["source_id"],
        "target": {
            "block_id": r["block_id"],
            "section_id": r["section_id"],
            "context": {"prev_block_id": r["prev_block_id"], "next_block_id": r["next_block_id"]},
            "selector": sel,
        },
        "body": r["body"],
        "color": r["color"],
        "tags": json.loads(r["tags"] or "[]"),
        "links": json.loads(r["links"] or "[]"),
        "created": r["created"],
        "updated": r["updated"],
    }


@router.post("/annotations")
async def create_annotation(request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    b = await json_object(request)
    source_id = optional_string(b.get("source_id"), "source_id").strip()
    if not source_id:
        raise HTTPException(400, "source_id required")
    tgt = optional_object(b.get("target"), "target")
    sel = optional_object(tgt.get("selector"), "target.selector")
    ctx = optional_object(tgt.get("context"), "target.context")
    color = _validate_annotation_color(b.get("color"))
    tags = _validate_tags(b.get("tags"))
    links = _validate_links(b.get("links"))
    body = optional_string(b.get("body"), "body")
    aid = "an_" + uuid.uuid4().hex[:16]
    now = db.now_iso()
    conn = db.connect()
    region = sel.get("region")
    conn.execute(
        """INSERT INTO annotations(id,source_id,block_id,section_id,prev_block_id,next_block_id,
             quote,prefix,suffix,sel_start,sel_end,region,body,color,tags,links,created,updated)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            aid,
            source_id,
            tgt.get("block_id"),
            tgt.get("section_id"),
            ctx.get("prev_block_id"),
            ctx.get("next_block_id"),
            sel.get("quote"),
            sel.get("prefix"),
            sel.get("suffix"),
            sel.get("start"),
            sel.get("end"),
            json.dumps(region, ensure_ascii=False) if region else None,
            body,
            color,
            json.dumps(tags, ensure_ascii=False),
            json.dumps(links, ensure_ascii=False),
            now,
            now,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM annotations WHERE id=?", (aid,)).fetchone()
    conn.close()
    return _annotation_dict(row)


@router.get("/annotations")
def list_annotations(source_id: str, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    conn = db.connect()
    rows = conn.execute("SELECT * FROM annotations WHERE source_id=? ORDER BY created", (source_id,)).fetchall()
    conn.close()
    return [_annotation_dict(r) for r in rows]


@router.patch("/annotations/{aid}")
async def update_annotation(aid: str, request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    b = await json_object(request)
    sets, vals = [], []
    if "body" in b:
        sets.append("body=?")
        vals.append(optional_string(b["body"], "body"))
    if "color" in b:
        sets.append("color=?")
        vals.append(_validate_annotation_color(b["color"]))
    for k in ("tags", "links"):
        if k in b:
            val = _validate_tags(b[k]) if k == "tags" else _validate_links(b[k])
            sets.append(f"{k}=?")
            vals.append(json.dumps(val, ensure_ascii=False))
    if not sets:
        raise HTTPException(400, "nothing to update")
    sets.append("updated=?")
    vals.append(db.now_iso())
    vals.append(aid)
    conn = db.connect()
    cur = conn.execute(f"UPDATE annotations SET {','.join(sets)} WHERE id=?", vals)
    conn.commit()
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(404, "unknown annotation")
    row = conn.execute("SELECT * FROM annotations WHERE id=?", (aid,)).fetchone()
    conn.close()
    return _annotation_dict(row)


@router.post("/annotations/{aid}/promote")
async def promote_annotation(aid: str, request: Request, x_auth_token: str | None = Header(None)):
    """Write this annotation into a wiki page's human-zone and commit it.

    Serialized behind the ingest lock so it cannot race an ingest commit.
    """
    require_auth(x_auth_token)
    b = await json_object(request)
    wiki_rel = optional_string(b.get("wiki_rel"), "wiki_rel").strip()
    if not wiki_rel:
        raise HTTPException(400, "wiki_rel required")
    if not _valid_wiki_rel(wiki_rel):
        raise HTTPException(400, "invalid wiki_rel")
    source_title = optional_string(b.get("source_title"), "source_title")
    conn = db.connect()
    row = conn.execute("SELECT * FROM annotations WHERE id=?", (aid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "unknown annotation")
    anno = _annotation_dict(row)
    async with ir.LOCK:
        try:
            result = promote_mod.promote_to_page(anno, source_title, ir.CONTENT_DIR, wiki_rel)
        except ValueError as e:
            conn.close()
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            conn.close()
            raise HTTPException(500, f"git commit failed: {e}")
    links = [
        l
        for l in (anno.get("links") or [])
        if not (isinstance(l, dict) and l.get("wiki_rel") == wiki_rel)
    ]
    links.append({"type": "human-zone", "wiki_rel": wiki_rel, "href": result["href"]})
    conn.execute(
        "UPDATE annotations SET links=?, updated=? WHERE id=?",
        (json.dumps(links, ensure_ascii=False), db.now_iso(), aid),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM annotations WHERE id=?", (aid,)).fetchone()
    conn.close()
    return {**result, "annotation": _annotation_dict(updated)}


@router.delete("/annotations/{aid}")
def delete_annotation(aid: str, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    conn = db.connect()
    cur = conn.execute("DELETE FROM annotations WHERE id=?", (aid,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "unknown annotation")
    return {"ok": True}
