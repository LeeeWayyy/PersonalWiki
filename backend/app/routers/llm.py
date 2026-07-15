"""On-demand LLM routes and cache helpers."""
from __future__ import annotations

import asyncio
import hashlib
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from .. import db
from .. import llm as llm_client
from .. import settings
from ..auth import require_auth
from ..validation import json_object, optional_string

router = APIRouter()
LOGGER = logging.getLogger(__name__)


def _cache_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _cache_payload(row) -> dict:
    return {
        "prompt_version": row["prompt_version"],
        "llm_provider": row["llm_provider"],
        "llm_model": row["llm_model"],
    }


def _llm_cache_meta(prompt_version: str) -> tuple[str, str | None, str | None]:
    ident = llm_client.identity()
    return prompt_version, ident["provider"], ident["model"]


def _translation_cache_get(h: str):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT translation,context,lang,prompt_version,llm_provider,llm_model FROM translations WHERE text_hash=?",
            (h,),
        ).fetchone()
    finally:
        conn.close()


def _translation_cache_put(
    *,
    h: str,
    context: str,
    lang: str,
    translation: str,
    prompt_version: str,
    provider: str | None,
    model: str | None,
) -> None:
    conn = db.connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO translations(
                 text_hash,context,lang,translation,prompt_version,llm_provider,llm_model,created
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (h, context, lang, translation, prompt_version, provider, model, db.now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


async def _cached_completion(*, text: str, context: str, lang: str,
                             prompt_version: str, prompt: str,
                             hash_parts: tuple[str, ...], unavailable: str) -> tuple[str, bool, dict]:
    h = _cache_hash(*hash_parts, text)
    cached = await asyncio.to_thread(_translation_cache_get, h)
    if cached:
        return cached["translation"], True, _cache_payload(cached)
    LOGGER.info("%s cache miss lang=%s chars=%d", context, lang, len(text))
    if not llm_client.configured():
        return unavailable, False, {"configured": False}
    try:
        raw = await asyncio.to_thread(llm_client.complete, prompt, timeout=120)
    except Exception as exc:
        LOGGER.exception("%s LLM call failed lang=%s chars=%d", context, lang, len(text))
        raise HTTPException(502, f"LLM call failed: {exc}") from exc
    output = (raw or "").strip()
    prompt_version, provider, model = _llm_cache_meta(prompt_version)
    if output:
        await asyncio.to_thread(
            _translation_cache_put, h=h, context=context, lang=lang,
            translation=output, prompt_version=prompt_version, provider=provider, model=model,
        )
    else:
        LOGGER.warning("%s LLM returned empty output lang=%s chars=%d", context, lang, len(text))
        output = "(no output)"
    return output, False, {
        "prompt_version": prompt_version,
        "llm_provider": provider,
        "llm_model": model,
    }


@router.post("/translate")
async def translate(request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    b = await json_object(request)
    text = optional_string(b.get("text"), "text").strip()
    if not text:
        raise HTTPException(400, "text required")
    prompt = f"Translate to natural {settings.TRANSLATE_LANG}. Output only the translation, no notes.\n\n{text}"
    output, cached, meta = await _cached_completion(
        text=text, context="translate", lang=settings.TRANSLATE_LANG,
        prompt_version=settings.TRANSLATE_PROMPT_VERSION, prompt=prompt,
        hash_parts=("translate", settings.TRANSLATE_PROMPT_VERSION, settings.TRANSLATE_LANG),
        unavailable=(
            "(translation needs an LLM - set PW_LLM_PROVIDER=codex, configure a custom LLM_CMD, "
            "or explicitly enable the API fallback with PW_LLM_API_ENABLED=1 and "
            "PW_LLM_API_KEY in backend/.env)"
        ),
    )
    return {
        "translation": output,
        "cached": cached,
        "target_lang": settings.TRANSLATE_LANG,
        **meta,
    }


ASSIST_PROMPTS = {
    "explain": (
        "Explain the following passage clearly and concisely for a curious reader - "
        "what it means and why it matters. Answer in {lang}."
    ),
    "summarize": "Summarize the key point(s) of the following passage in 1-3 sentences. Answer in {lang}.",
    "define": "Define and briefly explain the key term(s) in the following text. Answer in {lang}.",
}


@router.post("/assist")
async def assist(request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    b = await json_object(request)
    text = optional_string(b.get("text"), "text").strip()
    mode = optional_string(b.get("mode"), "mode", "explain").lower()
    if mode not in ASSIST_PROMPTS:
        raise HTTPException(400, "mode must be explain, summarize, or define")
    if not text:
        raise HTTPException(400, "text required")
    lang = optional_string(b.get("lang"), "lang", settings.TRANSLATE_LANG) or settings.TRANSLATE_LANG
    prompt = ASSIST_PROMPTS[mode].format(lang=lang) + "\n\n" + text
    output, cached, meta = await _cached_completion(
        text=text, context=mode, lang=lang,
        prompt_version=settings.ASSIST_PROMPT_VERSION, prompt=prompt,
        hash_parts=("assist", settings.ASSIST_PROMPT_VERSION, mode, lang),
        unavailable=(
            "(AI assist needs an LLM - set PW_LLM_PROVIDER=codex, configure a custom LLM_CMD, "
            "or explicitly enable the API fallback with PW_LLM_API_ENABLED=1 and "
            "PW_LLM_API_KEY in backend/.env)"
        ),
    )
    return {
        "result": output,
        "mode": mode,
        "cached": cached,
        **meta,
    }
