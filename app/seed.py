"""
app/seed.py — Database seeding on startup.

Seeds:
  1. Loads data/sample_cache.json into extraction_cache (offline demo).
  2. Creates a few historical "done" run records so the dashboard isn't empty.
  3. Populates the audit log for those seeded runs.

This is idempotent — safe to call on every startup. Existing rows are
not overwritten (ON CONFLICT DO NOTHING or presence checks).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from app.config import SAMPLE_CACHE_FILE
from app.database import get_db, log_audit, _lock

logger = logging.getLogger(__name__)


def _load_sample_cache() -> None:
    """Load data/sample_cache.json into the extraction_cache table."""
    if not SAMPLE_CACHE_FILE.exists():
        logger.info("No sample_cache.json found — skipping cache preload.")
        return

    try:
        data: dict = json.loads(SAMPLE_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read sample_cache.json: %s", exc)
        return

    loaded = 0
    with _lock, get_db() as conn:
        for sha256, entry in data.items():
            extraction = entry.get("extraction")
            if not extraction:
                continue
            conn.execute(
                """INSERT INTO extraction_cache (sha256, extraction_json, created_at, from_sample_cache)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(sha256) DO UPDATE SET extraction_json=excluded.extraction_json""",
                (sha256, json.dumps(extraction), datetime.utcnow().isoformat()),
            )
            loaded += 1

    logger.info("Preloaded %d entries from sample_cache.json", loaded)


# Historical seed data — realistic-looking runs for dashboard
_SEED_RUNS = [
    {
        "id": "seed-run-001",
        "filename": "happy_path.pdf",
        "file_sha256": "seed001" + "0" * 57,
        "ingest_method": "text",
        "status": "done",
        "decision": "AUTO_APPROVED",
        "vendor_name": "Acme Supplies Inc",
        "invoice_number": "INV-1001",
        "invoice_total": 7450.00,
        "currency": "USD",
        "po_number": "PO-2024-001",
        "created_at": (datetime.utcnow() - timedelta(days=15)).isoformat(),
        "completed_at": (datetime.utcnow() - timedelta(days=15, seconds=-12)).isoformat(),
        "processing_ms": 12480,
        "is_sample": 1,
    },
    {
        "id": "seed-run-002",
        "filename": "split_bill_1.pdf",
        "file_sha256": "seed002" + "0" * 57,
        "ingest_method": "text",
        "status": "done",
        "decision": "APPROVED_WITH_NOTE",
        "vendor_name": "Sunrise Software Pvt Ltd",
        "invoice_number": "INV-SS-2024-301",
        "invoice_total": 177000.00,
        "currency": "INR",
        "po_number": "PO-2024-004",
        "created_at": (datetime.utcnow() - timedelta(days=12)).isoformat(),
        "completed_at": (datetime.utcnow() - timedelta(days=12, seconds=-18)).isoformat(),
        "processing_ms": 18230,
        "is_sample": 1,
    },
    {
        "id": "seed-run-003",
        "filename": "split_bill_2.pdf",
        "file_sha256": "seed003" + "0" * 57,
        "ingest_method": "text",
        "status": "done",
        "decision": "APPROVED_WITH_NOTE",
        "vendor_name": "Sunrise Software Pvt Ltd",
        "invoice_number": "INV-SS-2024-302",
        "invoice_total": 236000.00,
        "currency": "INR",
        "po_number": "PO-2024-004",
        "created_at": (datetime.utcnow() - timedelta(days=10)).isoformat(),
        "completed_at": (datetime.utcnow() - timedelta(days=10, seconds=-22)).isoformat(),
        "processing_ms": 22100,
        "is_sample": 1,
    },
    {
        "id": "seed-run-004",
        "filename": "INV-RL-2024-0099.pdf",
        "file_sha256": "seed004" + "0" * 57,
        "ingest_method": "ocr",
        "status": "done",
        "decision": "REQUEST_INFO",
        "vendor_name": "Rapid Logistics Ltd",
        "invoice_number": None,
        "invoice_total": 3400.00,
        "currency": "GBP",
        "po_number": None,
        "created_at": (datetime.utcnow() - timedelta(days=7)).isoformat(),
        "completed_at": (datetime.utcnow() - timedelta(days=7, seconds=-35)).isoformat(),
        "processing_ms": 35600,
        "is_sample": 1,
    },
    {
        "id": "seed-run-005",
        "filename": "INV-PC-2024-0078.pdf",
        "file_sha256": "seed005" + "0" * 57,
        "ingest_method": "text",
        "status": "done",
        "decision": "REJECTED",
        "vendor_name": "Pinnacle Consulting Group",
        "invoice_number": "INV-PC-0078",
        "invoice_total": 58000.00,
        "currency": "USD",
        "po_number": "PO-2024-005",
        "created_at": (datetime.utcnow() - timedelta(days=3)).isoformat(),
        "completed_at": (datetime.utcnow() - timedelta(days=3, seconds=-9)).isoformat(),
        "processing_ms": 9800,
        "is_sample": 1,
    },
]


def _seed_runs() -> None:
    """Insert historical run records if they don't already exist."""
    with _lock, get_db() as conn:
        for run in _SEED_RUNS:
            existing = conn.execute(
                "SELECT id FROM runs WHERE id = ?", (run["id"],)
            ).fetchone()
            if existing:
                continue

            conn.execute(
                """INSERT INTO runs
                   (id, filename, file_sha256, ingest_method, status, decision,
                    vendor_name, invoice_number, invoice_total, currency, po_number,
                    created_at, completed_at, processing_ms, is_sample)
                   VALUES
                   (:id, :filename, :file_sha256, :ingest_method, :status, :decision,
                    :vendor_name, :invoice_number, :invoice_total, :currency, :po_number,
                    :created_at, :completed_at, :processing_ms, :is_sample)""",
                run,
            )
            # Audit trail
            conn.execute(
                """INSERT INTO audit_log (run_id, event, detail, actor, created_at)
                   VALUES (?, 'RUN_STARTED', 'Seeded historical run', 'system', ?)""",
                (run["id"], run["created_at"]),
            )
            conn.execute(
                """INSERT INTO audit_log (run_id, event, detail, actor, created_at)
                   VALUES (?, 'RUN_COMPLETED', ?, 'system', ?)""",
                (run["id"], f"decision={run['decision']}", run["completed_at"]),
            )

    logger.info("Seeded %d historical run records.", len(_SEED_RUNS))


def _seed_csv_master_data() -> None:
    """Sync vendors.csv and purchase_orders.csv into SQLite master tables."""
    import csv
    from app.config import PO_CSV, VENDORS_CSV

    with _lock, get_db() as conn:
        if VENDORS_CSV.exists():
            with open(VENDORS_CSV, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    conn.execute(
                        """INSERT INTO vendors (id, name, tax_id, approved, bank_account, email, currency)
                           VALUES (:id, :name, :tax_id, :approved, :bank_account, :email, :currency)
                           ON CONFLICT(id) DO UPDATE SET name=excluded.name, bank_account=excluded.bank_account, approved=excluded.approved, currency=excluded.currency""",
                        row,
                    )
        if PO_CSV.exists():
            with open(PO_CSV, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    conn.execute(
                        """INSERT INTO purchase_orders (po_number, vendor_id, description, total_amount, currency, issue_date, expiry_date)
                           VALUES (:po_number, :vendor_id, :description, :total_amount, :currency, :issue_date, :expiry_date)
                           ON CONFLICT(po_number) DO UPDATE SET total_amount=excluded.total_amount, currency=excluded.currency""",
                        row,
                    )


def seed_all() -> None:
    """Entry point called from app startup."""
    _seed_csv_master_data()
    _load_sample_cache()
    _seed_runs()
    logger.info("Database seeding complete.")
