"""
pipeline/stage2_extract.py — LLM-based field extraction with cache.

WHY the LLM is used here (and ONLY here for extraction):
  Invoices come in hundreds of layouts. A rule-based parser would need
  thousands of templates. The LLM generalises across formats.

WHY the LLM does NOT decide approval:
  LLMs can hallucinate or be prompt-injected via invoice text. Decisions
  must be fully deterministic and auditable — that's the rules engine.

Cache strategy:
  SHA-256 of the raw PDF bytes → SQLite. On a cache hit we skip the Groq
  call entirely and set from_cache=True so the UI shows a "cached" badge.
  The sample_cache.json file is loaded on startup for offline demo.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.database import cache_get, cache_put, is_from_sample_cache
from app.llm import call_llm_json
from app.models import ExtractionResult, FieldValue

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an invoice data extraction assistant. Extract all fields from the invoice text provided.

Return ONLY a valid JSON object with exactly this structure. Use null for any field not found.
Confidence is a float 0.0-1.0 reflecting how certain you are about each value.
source_snippet is a short verbatim excerpt from the text that supports the value (max 60 chars).
For OCR text, lower confidence scores by 0.1-0.2 to reflect potential OCR errors.
NEVER invent values. If a field is absent, set value to null and confidence to 0.

{
  "vendor_name": {"value": "...", "confidence": 0.95, "source_snippet": "..."},
  "vendor_tax_id": {"value": "...", "confidence": 0.90, "source_snippet": "..."},
  "invoice_number": {"value": "...", "confidence": 0.95, "source_snippet": "..."},
  "invoice_date": {"value": "YYYY-MM-DD", "confidence": 0.90, "source_snippet": "..."},
  "due_date": {"value": "YYYY-MM-DD", "confidence": 0.90, "source_snippet": "..."},
  "po_reference": {"value": "...", "confidence": 0.85, "source_snippet": "..."},
  "currency": {"value": "INR", "confidence": 0.95, "source_snippet": "..."},
  "line_items": [
    {"description": "...", "quantity": 1.0, "unit_price": 100.0, "amount": 100.0, "confidence": 0.90}
  ],
  "subtotal": {"value": 0.0, "confidence": 0.95, "source_snippet": "..."},
  "tax_amount": {"value": 0.0, "confidence": 0.90, "source_snippet": "..."},
  "tax_details": {"value": "18% GST", "confidence": 0.85, "source_snippet": "..."},
  "total": {"value": 0.0, "confidence": 0.95, "source_snippet": "..."},
  "bank_account": {"value": "...", "confidence": 0.80, "source_snippet": "..."},
  "vendor_email": {"value": "...", "confidence": 0.90, "source_snippet": "..."}
}"""


def _build_user_prompt(text: str, ocr_used: bool) -> str:
    prefix = (
        "[NOTE: This text was extracted via OCR and may contain recognition errors. "
        "Lower your confidence scores by 0.1-0.2 accordingly.]\n\n"
        if ocr_used
        else ""
    )
    # Truncate to ~8000 chars to stay within context limits
    truncated = text[:8000]
    if len(text) > 8000:
        truncated += "\n... [text truncated]"
    return f"{prefix}Invoice text:\n\n{truncated}"


async def extract(
    text: str,
    sha256: str,
    ocr_used: bool = False,
) -> ExtractionResult:
    """
    Stage 2: Extract structured fields from invoice text.

    Checks the SQLite cache first. If the SHA-256 is found, returns
    the cached extraction without calling Groq.

    Args:
        text: Full text from Stage 1.
        sha256: SHA-256 hex digest of the original PDF bytes (cache key).
        ocr_used: Whether OCR was used (affects confidence calibration).

    Returns:
        ExtractionResult with all extracted fields and provenance metadata.
    """
    # --- Cache lookup ---
    cached = cache_get(sha256)
    if cached:
        logger.info("Cache hit for sha256=%s", sha256[:16])
        data = json.loads(cached)
        result = ExtractionResult.model_validate(data)
        result.from_cache = True
        result.ocr_used = ocr_used
        return result

    # --- LLM call ---
    logger.info("Cache miss for sha256=%s — calling Groq", sha256[:16])
    user_prompt = _build_user_prompt(text, ocr_used)

    result = await call_llm_json(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_model=ExtractionResult,
    )
    result.ocr_used = ocr_used
    result.from_cache = False

    # --- Persist to cache ---
    cache_put(sha256, result.model_dump_json(), from_sample=False)
    logger.info("Cached extraction for sha256=%s", sha256[:16])

    return result
