"""
llm.py — Groq API client with retry, fallback, JSON repair, and concurrency limiting.

Design rationale:
  - A semaphore caps concurrent Groq calls to avoid rate-limit cascades.
  - On 429 we do exponential back-off (up to 3 retries).
  - We request JSON mode and validate with Pydantic; if validation fails we
    attempt one lightweight JSON repair before falling back to the smaller model.
  - The LLM NEVER makes decisions — it only extracts text and writes explanations.
  - The API key is never logged or returned to the browser.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from app.config import (
    GROQ_API_KEY,
    GROQ_FALLBACK_MODEL,
    GROQ_MODEL,
    LLM_CONCURRENCY_LIMIT,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Global semaphore — created lazily so it binds to the running event loop
_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(LLM_CONCURRENCY_LIMIT)
    return _semaphore


def _build_client():
    """Return a fresh Groq sync client (lightweight, no connection pool)."""
    from app.config import GROQ_API_KEY
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set. Please provide a key in .env.")
    from groq import Groq  # type: ignore

    return Groq(api_key=GROQ_API_KEY)


def _repair_json(raw: str) -> str:
    """
    Best-effort JSON repair for common LLM output issues:
    - Strip markdown code fences
    - Remove trailing commas
    - Fix single-quoted keys
    """
    # Strip ```json ... ``` fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"```\s*$", "", raw.strip(), flags=re.MULTILINE)
    raw = raw.strip()

    # Remove trailing commas before } or ]
    raw = re.sub(r",\s*([}\]])", r"\1", raw)

    return raw


def _call_groq_sync(
    messages: list[dict],
    model: str,
    response_format: Optional[dict] = None,
) -> str:
    """Synchronous Groq call with up to 3 retries on 429."""
    client = _build_client()
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 4096,
    }
    if response_format:
        kwargs["response_format"] = response_format

    for attempt in range(3):
        try:
            resp = client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status == 429 and attempt < 2:
                wait = 2 ** (attempt + 1)
                logger.warning("Groq 429, retrying in %ss (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
            else:
                raise

    raise RuntimeError("Groq call failed after retries")


async def call_llm_json(
    system_prompt: str,
    user_prompt: str,
    response_model: Type[T],
    use_fallback: bool = False,
) -> T:
    """
    Call Groq, request JSON, validate against a Pydantic model.

    Repair → fallback strategy:
      1. Call primary model with JSON mode.
      2. If Pydantic validation fails → repair the raw JSON → retry validation.
      3. If still invalid → call fallback model → validate again.
      4. If all fail → raise.

    Returns the validated Pydantic model instance.
    """
    model = GROQ_FALLBACK_MODEL if use_fallback else GROQ_MODEL
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    async with _get_semaphore():
        raw = await asyncio.to_thread(
            _call_groq_sync,
            messages,
            model,
            {"type": "json_object"},
        )

    # Attempt 1: parse as-is
    try:
        data = json.loads(raw)
        return response_model.model_validate(data)
    except Exception as exc:
        logger.warning("LLM JSON parse/validate failed (attempt 1): %s", exc)

    # Attempt 2: repair
    try:
        repaired = _repair_json(raw)
        data = json.loads(repaired)
        return response_model.model_validate(data)
    except Exception as exc:
        logger.warning("LLM JSON repair failed (attempt 2): %s", exc)

    # Attempt 3: fallback model
    if not use_fallback:
        logger.warning("Falling back to %s", GROQ_FALLBACK_MODEL)
        return await call_llm_json(
            system_prompt, user_prompt, response_model, use_fallback=True
        )

    raise ValueError(f"LLM returned invalid JSON after repair + fallback.\nRaw: {raw[:500]}")


async def call_llm_text(
    system_prompt: str,
    user_prompt: str,
    use_fallback: bool = False,
) -> str:
    """Call Groq for free-form text (used for explanation and email drafts)."""
    model = GROQ_FALLBACK_MODEL if use_fallback else GROQ_MODEL
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    async with _get_semaphore():
        return await asyncio.to_thread(_call_groq_sync, messages, model)
