"""
database.py — SQLite setup using the stdlib sqlite3 module.

Design rationale:
  - No ORM overhead for a single-container demo.
  - A threading.Lock guards every write so concurrent FastAPI requests
    don't corrupt the WAL when running in development mode.
  - Tables are created on first import via init_db().
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

from app.config import DB_PATH, UPLOAD_DIR

_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Return a new connection with row_factory set."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Context-manager that yields a connection and commits/rolls-back."""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    ingest_method TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    decision TEXT,
    vendor_name TEXT,
    invoice_number TEXT,
    invoice_total REAL,
    currency TEXT,
    po_number TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    processing_ms INTEGER,
    is_sample INTEGER NOT NULL DEFAULT 0,
    stage_results TEXT,
    extraction_json TEXT,
    decision_json TEXT,
    pdf_path TEXT
);

CREATE TABLE IF NOT EXISTS extraction_cache (
    sha256 TEXT PRIMARY KEY,
    extraction_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    from_sample_cache INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vendors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tax_id TEXT,
    approved INTEGER NOT NULL DEFAULT 1,
    bank_account TEXT,
    email TEXT,
    currency TEXT DEFAULT 'INR',
    is_user_added INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    po_number TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL,
    description TEXT,
    total_amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'INR',
    issue_date TEXT,
    expiry_date TEXT,
    is_user_added INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (vendor_id) REFERENCES vendors(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT,
    actor TEXT DEFAULT 'system',
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
"""


def init_db() -> None:
    """Create all tables and perform incremental migrations if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript(DDL)
        # Incremental migrations for existing databases
        for table, col, col_type in [
            ("vendors", "is_user_added", "INTEGER NOT NULL DEFAULT 0"),
            ("purchase_orders", "is_user_added", "INTEGER NOT NULL DEFAULT 0"),
            ("runs", "pdf_path", "TEXT"),
        ]:
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


# ---------------------------------------------------------------------------
# Run CRUD helpers
# ---------------------------------------------------------------------------

def insert_run(run: dict) -> None:
    with _lock, get_db() as conn:
        conn.execute(
            """INSERT INTO runs
               (id, filename, file_sha256, status, created_at, is_sample, pdf_path)
               VALUES (:id, :filename, :file_sha256, :status, :created_at, :is_sample, :pdf_path)""",
            {
                "id": run["id"],
                "filename": run["filename"],
                "file_sha256": run["file_sha256"],
                "status": run.get("status", "pending"),
                "created_at": run["created_at"],
                "is_sample": run.get("is_sample", 0),
                "pdf_path": run.get("pdf_path"),
            },
        )


def update_run(run_id: str, **kwargs) -> None:
    """Update arbitrary columns on a run row."""
    if not kwargs:
        return
    set_clause = ", ".join(f"{k} = :{k}" for k in kwargs)
    kwargs["run_id"] = run_id
    with _lock, get_db() as conn:
        conn.execute(f"UPDATE runs SET {set_clause} WHERE id = :run_id", kwargs)


def get_run(run_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def list_runs(limit: int = 100, offset: int = 0) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_stage_result(run_id: str, stage_events: list[dict]) -> None:
    """Persist the growing list of stage events as a JSON blob."""
    with _lock, get_db() as conn:
        conn.execute(
            "UPDATE runs SET stage_results = ? WHERE id = ?",
            (json.dumps(stage_events), run_id),
        )


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def cache_get(sha256: str) -> Optional[str]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT extraction_json FROM extraction_cache WHERE sha256 = ?",
            (sha256,),
        ).fetchone()
        return row["extraction_json"] if row else None


def cache_put(sha256: str, extraction_json: str, from_sample: bool = False) -> None:
    with _lock, get_db() as conn:
        conn.execute(
            """INSERT INTO extraction_cache (sha256, extraction_json, created_at, from_sample_cache)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(sha256) DO UPDATE SET extraction_json=excluded.extraction_json""",
            (sha256, extraction_json, datetime.utcnow().isoformat(), int(from_sample)),
        )


def is_from_sample_cache(sha256: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT from_sample_cache FROM extraction_cache WHERE sha256 = ?",
            (sha256,),
        ).fetchone()
        return bool(row["from_sample_cache"]) if row else False


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def log_audit(run_id: str, event: str, detail: str = "", actor: str = "system") -> None:
    with _lock, get_db() as conn:
        conn.execute(
            """INSERT INTO audit_log (run_id, event, detail, actor, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, event, detail, actor, datetime.utcnow().isoformat()),
        )


def get_audit_log(run_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Stats helper
# ---------------------------------------------------------------------------

def get_stats() -> dict:
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM runs WHERE status='done'").fetchone()[0]
        auto = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE decision='AUTO_APPROVED'"
        ).fetchone()[0]
        avg_ms = conn.execute(
            "SELECT AVG(processing_ms) FROM runs WHERE status='done'"
        ).fetchone()[0] or 0
        by_decision = conn.execute(
            "SELECT decision, COUNT(*) as cnt FROM runs WHERE status='done' GROUP BY decision"
        ).fetchall()
        held_amt = conn.execute(
            "SELECT COALESCE(SUM(invoice_total),0) FROM runs WHERE decision IN ('HOLD_FOR_REVIEW','REJECTED')"
        ).fetchone()[0]
        return {
            "total_processed": total,
            "auto_approved": auto,
            "touchless_rate": round(auto / total, 4) if total else 0,
            "avg_processing_ms": round(avg_ms),
            "by_decision": {r["decision"]: r["cnt"] for r in by_decision},
            "amount_held_or_blocked": held_amt,
        }


# ---------------------------------------------------------------------------
# Test Data (User-Added) CRUD & Reset Helpers
# ---------------------------------------------------------------------------

MAX_USER_RECORDS = 50


def add_user_vendor(
    name: str,
    tax_id: Optional[str] = None,
    bank_account: Optional[str] = None,
    approved: bool = True,
    currency: str = "INR",
) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("Vendor name is required.")
    currency = (currency or "INR").strip().upper()

    with _lock, get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM vendors WHERE is_user_added = 1").fetchone()[0]
        if count >= MAX_USER_RECORDS:
            raise ValueError(f"Limit reached: maximum {MAX_USER_RECORDS} user-added vendors allowed.")

        import uuid
        vendor_id = f"V-USER-{uuid.uuid4().hex[:6].upper()}"
        conn.execute(
            """INSERT INTO vendors (id, name, tax_id, approved, bank_account, currency, is_user_added)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (vendor_id, name, tax_id.strip() if tax_id else None, 1 if approved else 0,
             bank_account.strip() if bank_account else None, currency),
        )
        row = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
        return dict(row)


def delete_user_vendor(vendor_id: str) -> bool:
    with _lock, get_db() as conn:
        v = conn.execute("SELECT is_user_added FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
        if not v:
            raise ValueError(f"Vendor '{vendor_id}' not found.")
        if not v["is_user_added"]:
            raise ValueError("Cannot delete seeded master vendors. Only test (user-added) records can be removed.")

        conn.execute("DELETE FROM purchase_orders WHERE vendor_id = ? AND is_user_added = 1", (vendor_id,))
        conn.execute("DELETE FROM vendors WHERE id = ?", (vendor_id,))
        return True


def add_user_po(
    po_number: str,
    vendor_id: str,
    total_amount: float,
    currency: str = "INR",
    description: Optional[str] = None,
) -> dict:
    po_number = (po_number or "").strip()
    if not po_number:
        raise ValueError("PO number is required.")
    vendor_id = (vendor_id or "").strip()
    if not vendor_id:
        raise ValueError("Vendor selection is required.")
    if total_amount <= 0:
        raise ValueError("PO total amount must be a positive number.")
    currency = (currency or "INR").strip().upper()

    with _lock, get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE is_user_added = 1").fetchone()[0]
        if count >= MAX_USER_RECORDS:
            raise ValueError(f"Limit reached: maximum {MAX_USER_RECORDS} user-added purchase orders allowed.")

        v = conn.execute("SELECT id FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
        if not v:
            raise ValueError(f"Vendor ID '{vendor_id}' does not exist.")

        existing = conn.execute("SELECT po_number FROM purchase_orders WHERE po_number = ?", (po_number,)).fetchone()
        if existing:
            raise ValueError(f"Purchase order '{po_number}' already exists.")

        conn.execute(
            """INSERT INTO purchase_orders (po_number, vendor_id, description, total_amount, currency, is_user_added)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (po_number, vendor_id, description.strip() if description else "Test Purchase Order",
             float(total_amount), currency),
        )
        row = conn.execute("SELECT * FROM purchase_orders WHERE po_number = ?", (po_number,)).fetchone()
        return dict(row)


def delete_user_po(po_number: str) -> bool:
    with _lock, get_db() as conn:
        p = conn.execute("SELECT is_user_added FROM purchase_orders WHERE po_number = ?", (po_number,)).fetchone()
        if not p:
            raise ValueError(f"PO '{po_number}' not found.")
        if not p["is_user_added"]:
            raise ValueError("Cannot delete seeded master purchase orders. Only test (user-added) records can be removed.")

        conn.execute("DELETE FROM purchase_orders WHERE po_number = ?", (po_number,))
        return True


def reset_demo_data() -> None:
    """Remove user-added test records, clean uploaded files, and re-seed defaults."""
    init_db()
    from app.seed import seed_all

    with _lock, get_db() as conn:
        conn.execute("DELETE FROM purchase_orders WHERE is_user_added = 1")
        conn.execute("DELETE FROM vendors WHERE is_user_added = 1")
        conn.execute("DELETE FROM audit_log WHERE run_id IN (SELECT id FROM runs WHERE id NOT LIKE 'seed-run-%')")
        conn.execute("DELETE FROM runs WHERE id NOT LIKE 'seed-run-%'")

    if UPLOAD_DIR.exists():
        for f in UPLOAD_DIR.glob("*.pdf"):
            try:
                f.unlink()
            except Exception:
                pass

    seed_all()
