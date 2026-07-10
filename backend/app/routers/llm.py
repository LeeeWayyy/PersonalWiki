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


@router.post("/translate")
async def translate(request: Request, x_auth_token: str | None = Header(None)):
    require_auth(x_auth_token)
    b = await json_object(request)
    text = optional_string(b.get("text"), "text").strip()
    if not text:
        raise HTTPException(400, "text required")
    h = _cache_hash("translate", settings.TRANSLATE_PROMPT_VERSION, settings.TRANSLATE_LANG, text)
    cached = await asyncio.to_thread(_translation_cache_get, h)
    if cached:
        return {
            "translation": cached["translation"],
            "cached": True,
            "target_lang": cached["lang"],
            **_cache_payload(cached),
        }
    LOGGER.info("translate cache miss target_lang=%s chars=%d", settings.TRANSLATE_LANG, len(text))
    if not llm_client.configured():
        return {
            "translation": (
                "(translation needs an LLM - set PW_LLM_PROVIDER=codex, configure a custom LLM_CMD, "
                "or explicitly enable the API fallback with PW_LLM_API_ENABLED=1 and "
                "PW_LLM_API_KEY in backend/.env)"
            ),
            "cached": False,
            "configured": False,
        }
    prompt = f"Translate to natural {settings.TRANSLATE_LANG}. Output only the translation, no notes.\n\n{text}"
    try:
        tr = await asyncio.to_thread(llm_client.complete, prompt, timeout=120) or "(no output)"
    except Exception as e:  # noqa
        raise HTTPException(502, f"LLM call failed: {e}")
    prompt_version, provider, model = _llm_cache_meta(settings.TRANSLATE_PROMPT_VERSION)
    await asyncio.to_thread(
        _translation_cache_put,
        h=h,
        context="translate",
        lang=settings.TRANSLATE_LANG,
        translation=tr,
        prompt_version=prompt_version,
        provider=provider,
        model=model,
    )
    return {
        "translation": tr,
        "cached": False,
        "target_lang": settings.TRANSLATE_LANG,
        "prompt_version": prompt_version,
        "llm_provider": provider,
        "llm_model": model,
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
    h = _cache_hash("assist", settings.ASSIST_PROMPT_VERSION, mode, lang, text)
    cached = await asyncio.to_thread(_translation_cache_get, h)
    if cached:
        return {"result": cached["translation"], "mode": mode, "cached": True, **_cache_payload(cached)}
    LOGGER.info("assist cache miss mode=%s lang=%s chars=%d", mode, lang, len(text))
    if not llm_client.configured():
        return {
            "result": (
                "(AI assist needs an LLM - set PW_LLM_PROVIDER=codex, configure a custom LLM_CMD, "
                "or explicitly enable the API fallback with PW_LLM_API_ENABLED=1 and "
                "PW_LLM_API_KEY in backend/.env)"
            ),
            "mode": mode,
            "cached": False,
            "configured": False,
        }
    prompt = ASSIST_PROMPTS[mode].format(lang=lang) + "\n\n" + text
    try:
        out = await asyncio.to_thread(llm_client.complete, prompt, timeout=120) or "(no output)"
    except Exception as e:  # noqa
        raise HTTPException(502, f"LLM call failed: {e}")
    prompt_version, provider, model = _llm_cache_meta(settings.ASSIST_PROMPT_VERSION)
    await asyncio.to_thread(
        _translation_cache_put,
        h=h,
        context=mode,
        lang=lang,
        translation=out,
        prompt_version=prompt_version,
        provider=provider,
        model=model,
    )
    return {
        "result": out,
        "mode": mode,
        "cached": False,
        "prompt_version": prompt_version,
        "llm_provider": provider,
        "llm_model": model,
    }
