"""Source Reader annotation routes."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import subprocess
import uuid

from fastapi import APIRouter, Header, HTTPException, Request

from .. import db
from .. import ingest_runner as ir
from .. import promote as promote_mod
from ..auth import require_auth
from ..validation import json_object, optional_object, optional_string

router = APIRouter()
LOGGER = logging.getLogger(__name__)

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


def _optional_int(value, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(400, f"{field} must be an integer")
    if value < 0:
        raise HTTPException(400, f"{field} must be >= 0")
    return value


def _validate_region(value) -> dict | None:
    if value is None:
        return None
    region = optional_object(value, "target.selector.region")
    out: dict[str, float] = {}
    for key in ("x", "y", "w", "h"):
        raw = region.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise HTTPException(400, f"target.selector.region.{key} must be a number")
        val = float(raw)
        if not math.isfinite(val) or val < 0 or val > 1:
            raise HTTPException(400, f"target.selector.region.{key} must be between 0 and 1")
        out[key] = val
    if out["w"] <= 0 or out["h"] <= 0:
        raise HTTPException(400, "target.selector.region width/height must be > 0")
    if out["x"] + out["w"] > 1 or out["y"] + out["h"] > 1:
        raise HTTPException(400, "target.selector.region must stay within image bounds")
    return out


def _validate_annotation_parts(target_raw: dict) -> tuple[dict, dict, dict]:
    target = optional_object(target_raw, "target")
    selector_raw = optional_object(target.get("selector"), "target.selector")
    context_raw = optional_object(target.get("context"), "target.context")
    context = {
        "prev_block_id": optional_string(context_raw.get("prev_block_id"), "target.context.prev_block_id"),
        "next_block_id": optional_string(context_raw.get("next_block_id"), "target.context.next_block_id"),
    }
    start = _optional_int(selector_raw.get("start"), "target.selector.start")
    end = _optional_int(selector_raw.get("end"), "target.selector.end")
    if start is not None and end is not None and start > end:
        raise HTTPException(400, "target.selector.start must be <= end")
    selector = {
        "quote": optional_string(selector_raw.get("quote"), "target.selector.quote"),
        "prefix": optional_string(selector_raw.get("prefix"), "target.selector.prefix"),
        "suffix": optional_string(selector_raw.get("suffix"), "target.selector.suffix"),
        "start": start,
        "end": end,
    }
    region = _validate_region(selector_raw.get("region"))
    if region is not None:
        selector["region"] = region
    clean_target = {
        "block_id": optional_string(target.get("block_id"), "target.block_id"),
        "section_id": optional_string(target.get("section_id"), "target.section_id"),
    }
    return clean_target, context, selector


def _annotation_dict(row) -> dict:
    sel = {
        "quote": row["quote"],
        "prefix": row["prefix"],
        "suffix": row["suffix"],
        "start": row["sel_start"],
        "end": row["sel_end"],
    }
    region = row["region"]
    if region:
        try:
            sel["region"] = json.loads(region)
        except (TypeError, ValueError):
            pass
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "target": {
            "block_id": row["block_id"],
            "section_id": row["section_id"],
            "context": {"prev_block_id": row["prev_block_id"], "next_block_id": row["next_block_id"]},
            "selector": sel,
        },
        "body": row["body"],
        "color": row["color"],
        "tags": json.loads(row["tags"] or "[]"),
        "links": json.loads(row["links"] or "[]"),
        "created": row["created"],
        "updated": row["updated"],
    }


def _insert_annotation(
    *,
    aid: str,
    source_id: str,
    target: dict,
    context: dict,
    selector: dict,
    body: str,
    color: str,
    tags: list[str],
    links: list[dict],
    now: str,
):
    conn = db.connect()
    try:
        region = selector.get("region")
        conn.execute(
            """INSERT INTO annotations(id,source_id,block_id,section_id,prev_block_id,next_block_id,
                 quote,prefix,suffix,sel_start,sel_end,region,body,color,tags,links,created,updated)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                aid,
                source_id,
                target.get("block_id"),
                target.get("section_id"),
                context.get("prev_block_id"),
                context.get("next_block_id"),
                selector.get("quote"),
                selector.get("prefix"),
                selector.get("suffix"),
                selector.get("start"),
                selector.get("end"),
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
        return conn.execute("SELECT * FROM annotations WHERE id=?", (aid,)).fetchone()
    finally:
        conn.close()


def _list_annotations(source_id: str):
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM annotations WHERE source_id=? ORDER BY created", (source_id,)).fetchall()
    finally:
        conn.close()


def _update_annotation(aid: str, sets: list[str], vals: list):
    conn = db.connect()
    try:
        cur = conn.execute(f"UPDATE annotations SET {','.join(sets)} WHERE id=?", vals)
        conn.commit()
        if cur.rowcount == 0:
            return None
        return conn.execute("SELECT * FROM annotations WHERE id=?", (aid,)).fetchone()
    finally:
        conn.close()


def _get_annotation(aid: str):
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM annotations WHERE id=?", (aid,)).fetchone()
    finally:
        conn.close()


def _merge_annotation_link(aid: str, link: dict):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT links FROM annotations WHERE id=?", (aid,)).fetchone()
        if row is None:
            conn.rollback()
            return None
        try:
            links = json.loads(row["links"] or "[]")
        except (TypeError, ValueError):
            links = []
        links = [
            existing
            for existing in links
            if not (
                isinstance(existing, dict)
                and existing.get("type") == link["type"]
                and existing.get("wiki_rel") == link["wiki_rel"]
            )
        ]
        links.append(link)
        cur = conn.execute(
            "UPDATE annotations SET links=?, updated=? WHERE id=?",
            (json.dumps(links, ensure_ascii=False), db.now_iso(), aid),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return None
        conn.commit()
        return conn.execute("SELECT * FROM annotations WHERE id=?", (aid,)).fetchone()
    finally:
        conn.close()


def _delete_annotation(aid: str):
    conn = db.connect()
    try:
        row = conn.execute("SELECT source_id FROM annotations WHERE id=?", (aid,)).fetchone()
        cur = conn.execute("DELETE FROM annotations WHERE id=?", (aid,))
        conn.commit()
        if cur.rowcount == 0:
            return None
        return row["source_id"] if row else None
    finally:
        conn.close()


@router.post("/annotations")
async def create_annotation(request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    body = await json_object(request)
    source_id = optional_string(body.get("source_id"), "source_id").strip()
    if not source_id:
        raise HTTPException(400, "source_id required")
    target, context, selector = _validate_annotation_parts(body.get("target"))
    color = _validate_annotation_color(body.get("color"))
    tags = _validate_tags(body.get("tags"))
    links = _validate_links(body.get("links"))
    note_body = optional_string(body.get("body"), "body")
    aid = "an_" + uuid.uuid4().hex[:16]
    now = db.now_iso()
    row = await asyncio.to_thread(
        _insert_annotation,
        aid=aid,
        source_id=source_id,
        target=target,
        context=context,
        selector=selector,
        body=note_body,
        color=color,
        tags=tags,
        links=links,
        now=now,
    )
    LOGGER.info("annotation create aid=%s source_id=%s block_id=%s", aid, source_id, target.get("block_id"))
    return _annotation_dict(row)


@router.get("/annotations")
async def list_annotations(source_id: str, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    rows = await asyncio.to_thread(_list_annotations, source_id)
    return [_annotation_dict(row) for row in rows]


@router.patch("/annotations/{aid}")
async def update_annotation(aid: str, request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    body = await json_object(request)
    sets: list[str] = []
    vals: list[str] = []
    if "body" in body:
        sets.append("body=?")
        vals.append(optional_string(body["body"], "body"))
    if "color" in body:
        sets.append("color=?")
        vals.append(_validate_annotation_color(body["color"]))
    for key in ("tags", "links"):
        if key in body:
            val = _validate_tags(body[key]) if key == "tags" else _validate_links(body[key])
            sets.append(f"{key}=?")
            vals.append(json.dumps(val, ensure_ascii=False))
    if not sets:
        raise HTTPException(400, "nothing to update")
    sets.append("updated=?")
    vals.append(db.now_iso())
    vals.append(aid)
    row = await asyncio.to_thread(_update_annotation, aid, sets, vals)
    if row is None:
        raise HTTPException(404, "unknown annotation")
    LOGGER.info("annotation update aid=%s source_id=%s", aid, row["source_id"])
    return _annotation_dict(row)


@router.post("/annotations/{aid}/promote")
async def promote_annotation(aid: str, request: Request, x_auth_token: str | None = Header(None)):
    """Write this annotation into a wiki page's human-zone and commit it.

    Serialized behind the ingest lock so it cannot race an ingest commit.
    """
    require_auth(x_auth_token)
    body = await json_object(request)
    wiki_rel = optional_string(body.get("wiki_rel"), "wiki_rel").strip()
    if not wiki_rel:
        raise HTTPException(400, "wiki_rel required")
    if not _valid_wiki_rel(wiki_rel):
        raise HTTPException(400, "invalid wiki_rel")
    source_title = optional_string(body.get("source_title"), "source_title")
    row = await asyncio.to_thread(_get_annotation, aid)
    if not row:
        raise HTTPException(404, "unknown annotation")
    anno = _annotation_dict(row)
    async with ir.LOCK:
        try:
            result = await asyncio.to_thread(
                promote_mod.promote_to_page,
                anno,
                source_title,
                ir.CONTENT_DIR,
                wiki_rel,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, f"git commit failed: {exc}") from exc
    updated = await asyncio.to_thread(
        _merge_annotation_link,
        aid,
        {"type": "human-zone", "wiki_rel": wiki_rel, "href": result["href"]},
    )
    if updated is None:
        raise HTTPException(409, "annotation disappeared after promotion; wiki page was updated")
    LOGGER.info(
        "annotation promote aid=%s source_id=%s wiki_rel=%s href=%s",
        aid,
        anno["source_id"],
        wiki_rel,
        result["href"],
    )
    return {**result, "annotation": _annotation_dict(updated)}


@router.get("/wiki/human-zone")
async def get_human_zone(rel: str, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    if not _valid_wiki_rel(rel):
        raise HTTPException(400, "invalid rel")
    try:
        return await asyncio.to_thread(promote_mod.get_zone, ir.CONTENT_DIR, rel)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.put("/wiki/human-zone")
async def put_human_zone(request: Request, x_auth_token: str | None = Header(None)):
    """Replace a wiki page's human-zone from the UI and commit it.

    Serialized behind the ingest lock so it cannot race an ingest commit.
    """
    require_auth(x_auth_token)
    body = await json_object(request)
    rel = optional_string(body.get("rel"), "rel").strip()
    if not rel or not _valid_wiki_rel(rel):
        raise HTTPException(400, "invalid rel")
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(400, "text must be a string")
    if len(text) > 65536:
        raise HTTPException(400, "text too large")
    async with ir.LOCK:
        try:
            result = await asyncio.to_thread(promote_mod.set_zone, ir.CONTENT_DIR, rel, text)
        except ValueError as exc:
            status = 404 if "not found" in str(exc) else 400
            raise HTTPException(status, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, f"git commit failed: {exc}") from exc
    LOGGER.info("human-zone edit wiki_rel=%s committed=%s", rel, result["committed"])
    return result


@router.post("/wiki/page/remove")
async def remove_wiki_page(request: Request, x_auth_token: str | None = Header(None)):
    """Delete or merge a content page after an explicit client confirmation."""
    require_auth(x_auth_token)
    body = await json_object(request)
    rel = optional_string(body.get("rel"), "rel").strip()
    merge_into = optional_string(body.get("merge_into"), "merge_into").strip() or None
    confirmation = optional_string(body.get("confirmation"), "confirmation").strip()
    if not rel or not _valid_wiki_rel(rel):
        raise HTTPException(400, "invalid rel")
    if merge_into and not _valid_wiki_rel(merge_into):
        raise HTTPException(400, "invalid merge_into")
    if confirmation != rel:
        raise HTTPException(400, "confirmation must exactly match rel")

    async with ir.LOCK:
        try:
            result = await asyncio.to_thread(
                promote_mod.remove_page, ir.CONTENT_DIR, ir.REPO, rel, merge_into,
            )
        except ValueError as exc:
            status = 404 if "not found" in str(exc) else 400
            raise HTTPException(status, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

        rebuilt = False
        rebuild_warning = None
        if ir.REBUILD_CMD:
            try:
                rebuild = await asyncio.to_thread(
                    subprocess.run,
                    ir.REBUILD_CMD,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=ir.JOB_TIMEOUT_S,
                )
                rebuilt = rebuild.returncode == 0
                if not rebuilt:
                    lines = (rebuild.stderr or rebuild.stdout or "site rebuild failed").strip().splitlines()
                    rebuild_warning = lines[-1] if lines else "site rebuild failed"
            except (OSError, subprocess.TimeoutExpired) as exc:
                rebuild_warning = f"site rebuild failed: {exc}"

    LOGGER.info("wiki page remove rel=%s merge_into=%s rebuilt=%s", rel, merge_into, rebuilt)
    return {**result, "rebuilt": rebuilt, "rebuild_warning": rebuild_warning}


@router.delete("/annotations/{aid}")
async def delete_annotation(aid: str, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    source_id = await asyncio.to_thread(_delete_annotation, aid)
    if source_id is None:
        raise HTTPException(404, "unknown annotation")
    LOGGER.info("annotation delete aid=%s source_id=%s", aid, source_id)
    return {"ok": True}
