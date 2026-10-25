"""
config.py — Centralised, environment-driven configuration.

All values come from the .env file (or real env vars in Docker).
No hard-coded secrets; import this module everywhere instead of reading
os.environ directly.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (two levels up from app/)
_ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(_ENV_PATH, override=False)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"
SAMPLES_DIR = ROOT_DIR / "samples"
RULES_FILE = ROOT_DIR / "rules.json"
DB_PATH = DATA_DIR / "invoices.db"
VENDORS_CSV = DATA_DIR / "vendors.csv"
PO_CSV = DATA_DIR / "purchase_orders.csv"
SAMPLE_CACHE_FILE = DATA_DIR / "sample_cache.json"
UPLOAD_DIR = DATA_DIR / "uploads"
STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# Groq LLM
# ---------------------------------------------------------------------------
GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL: str = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FALLBACK_MODEL: str = os.environ.get("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")

# Maximum concurrent LLM calls (prevents runaway costs on Groq free tier)
LLM_CONCURRENCY_LIMIT: int = int(os.environ.get("LLM_CONCURRENCY_LIMIT", "3"))

# ---------------------------------------------------------------------------
# Tesseract
# ---------------------------------------------------------------------------
TESSERACT_CMD: str = os.environ.get("TESSERACT_CMD", "tesseract")

# ---------------------------------------------------------------------------
# Upload constraints
# ---------------------------------------------------------------------------
MAX_FILE_SIZE_BYTES: int = 5 * 1024 * 1024   # 5 MB
MAX_PAGES: int = 10

# Text-length threshold below which we treat a PDF page as scanned
SCANNED_CHAR_THRESHOLD: int = 50

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
RATE_LIMIT: str = os.environ.get("RATE_LIMIT", "60/hour")

# ---------------------------------------------------------------------------
# Pipeline thresholds
# ---------------------------------------------------------------------------
# Minimum average extraction confidence to auto-approve
MIN_EXTRACTION_CONFIDENCE: float = 0.70

# Maximum invoice total eligible for AUTO_APPROVE (USD equivalent)
MAX_AUTO_APPROVE_AMOUNT: float = 50_000.0

# PO amount tolerance: approve if variance ≤ this fraction OR fixed amount
PO_TOLERANCE_PCT: float = 0.02          # 2 %
PO_TOLERANCE_FIXED: float = 500.0       # USD / INR (same-currency check)

# Minimum confidence for an INFERRED PO match to be used
MIN_INFERRED_PO_CONFIDENCE: float = 0.65

# Duplicate detection: same vendor + same total within this many days
DUPLICATE_DATE_WINDOW_DAYS: int = 3

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
PORT: int = int(os.environ.get("PORT", "7860"))
HOST: str = os.environ.get("HOST", "0.0.0.0")
