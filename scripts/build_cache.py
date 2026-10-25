"""
scripts/build_cache.py — Pre-compute LLM extraction for all sample invoices.

Run this ONCE after regenerating samples:
    .venv\\Scripts\\python.exe scripts\\build_cache.py

What it does:
  1. Reads every PDF in samples/.
  2. Computes SHA-256 of the raw bytes (the cache key).
  3. Runs the full Stage 1 (ingest) + Stage 2 (LLM extract) pipeline.
  4. Writes the results to data/sample_cache.json as:
       { "<sha256>": { "extraction": {...}, "filename": "..." }, ... }

On startup, app/database.py loads this file into the extraction_cache
table so the app works without a live Groq key.

The cache key is the SHA-256 of the PDF bytes, so re-running this after
regenerating samples will correctly update stale entries.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

SAMPLES_DIR = ROOT / "samples"
CACHE_FILE = ROOT / "data" / "sample_cache.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def build_cache() -> None:
    from app.config import GROQ_API_KEY
    from app.pipeline.stage1_ingest import ingest
    from app.pipeline.stage2_extract import extract

    use_live_groq = bool(GROQ_API_KEY)
    if not use_live_groq:
        print("NOTICE: GROQ_API_KEY is not set in .env. Pre-seeding cache from verified sample fixtures...")

    pdfs = sorted(SAMPLES_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {SAMPLES_DIR}. Run scripts/generate_samples.py first.")
        sys.exit(1)

    # Load existing cache to avoid re-calling Groq for unchanged files
    existing: dict = {}
    if CACHE_FILE.exists():
        existing = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        print(f"Loaded {len(existing)} existing cache entries.")

    cache: dict = dict(existing)
    new_count = 0

    for pdf_path in pdfs:
        print(f"\nProcessing: {pdf_path.name}")
        pdf_bytes = pdf_path.read_bytes()
        digest = sha256(pdf_bytes)

        if digest in cache:
            print(f"  SKIP (unchanged, sha256={digest[:16]}…)")
            continue

        try:
            # Stage 1
            ingest_result = ingest(pdf_bytes, pdf_path.name)
            text = ingest_result["text"]
            ocr_used = ingest_result["method"].value == "ocr"
            print(f"  Ingested: method={ingest_result['method'].value}, chars={len(text)}")

            from app import database
            database.init_db()

            if use_live_groq:
                # Stage 2 — live LLM call
                extraction = await extract(text, digest + "_FORCE_MISS", ocr_used=ocr_used)
                extraction_data = json.loads(extraction.model_dump_json())
            else:
                from tests.conftest import EXTRACTION_STUBS
                stub = EXTRACTION_STUBS.get(pdf_path.name)
                if stub:
                    extraction_data = stub
                else:
                    raise ValueError(f"No stub found for {pdf_path.name}")

            cache[digest] = {
                "filename": pdf_path.name,
                "extraction": extraction_data,
            }
            new_count += 1
            print(f"  ✓ Cached (sha256={digest[:16]}…)")

        except Exception as exc:
            print(f"  ERROR: {exc}")
            # Still store a minimal placeholder so the entry exists
            cache[digest] = {
                "filename": pdf_path.name,
                "extraction": None,
                "error": str(exc),
            }

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    print(f"\n=== Cache built: {len(cache)} entries ({new_count} new) → {CACHE_FILE} ===")


if __name__ == "__main__":
    asyncio.run(build_cache())
