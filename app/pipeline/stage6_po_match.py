"""
pipeline/stage6_po_match.py — Purchase Order matching.

WHY both explicit and inferred matching:
  Vendors don't always include a PO number (especially for recurring services
  or scanned invoices where OCR missed it). Inferred matching by vendor +
  amount proximity + description similarity gives a safety net.

Split billing:
  We sum all invoice_total values from previous APPROVED runs that reference
  the same PO number. The remaining_balance = PO total - billed_to_date.
  If the current invoice would exceed the balance, the rule engine holds it.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from rapidfuzz import fuzz

from app.config import PO_CSV
from app.models import ExtractionResult, POMatchResult

logger = logging.getLogger(__name__)

DESCRIPTION_SIM_WEIGHT = 0.4   # weight of description similarity in inferred score
AMOUNT_PROX_WEIGHT = 0.6       # weight of amount proximity in inferred score
AMOUNT_TOLERANCE_PCT = 0.20    # ±20% for inferred amount proximity


def _load_pos() -> list[dict]:
    """Load POs from SQLite database (seeded + user-added). Fall back to CSV."""
    from app.database import get_db
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM purchase_orders").fetchall()
            if rows:
                return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("Could not read POs from DB, falling back to CSV: %s", exc)

    path = Path(PO_CSV)
    if not path.exists():
        logger.warning("purchase_orders.csv not found at %s", path)
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _get_billed_to_date(
    po_number: str,
    current_run_id: str,
    current_invoice_number: str | None = None,
    filename: str | None = None,
) -> float:
    """
    Sum invoice totals of all approved DISTINCT invoices referencing this PO,
    excluding the invoice currently being processed.

    If an invoice is re-run multiple times in testing or re-evaluated,
    its amount is only counted ONCE towards the PO billed total.
    """
    from app.database import get_db
    with get_db() as conn:
        inv_clean = current_invoice_number.strip().lower() if current_invoice_number else ""
        fn_clean = filename.strip().lower() if filename else ""

        query = """
            SELECT COALESCE(SUM(inv_amt), 0.0) as billed
            FROM (
                SELECT 
                    COALESCE(NULLIF(LOWER(TRIM(invoice_number)), ''), LOWER(TRIM(filename))) as inv_key,
                    MAX(invoice_total) as inv_amt
                FROM runs
                WHERE po_number = ?
                  AND id != ?
                  AND decision IN ('AUTO_APPROVED', 'APPROVED_WITH_NOTE')
        """
        params: list = [po_number, current_run_id]

        if inv_clean:
            query += " AND (invoice_number IS NULL OR LOWER(TRIM(invoice_number)) != ?)"
            params.append(inv_clean)
        if fn_clean:
            query += " AND (filename IS NULL OR LOWER(TRIM(filename)) != ?)"
            params.append(fn_clean)

        query += """
                GROUP BY COALESCE(NULLIF(LOWER(TRIM(invoice_number)), ''), LOWER(TRIM(filename)), id)
            )
        """

        row = conn.execute(query, params).fetchone()
        return float(row["billed"]) if row else 0.0


def _description_similarity(line_items: list, po_description: str) -> float:
    """Average fuzzy similarity between invoice line descriptions and PO description."""
    if not line_items or not po_description:
        return 0.0
    scores = [
        fuzz.partial_ratio(str(li.description or "").lower(), po_description.lower())
        for li in line_items
    ]
    return max(scores) / 100.0 if scores else 0.0


def _inferred_match_score(
    pos: list[dict],
    vendor_id: str | None,
    invoice_total: float,
    line_items: list,
) -> tuple[dict | None, float]:
    """
    Score each PO for the given vendor and return the best match + score.
    Score = weighted combination of amount proximity and description similarity.
    """
    best_po = None
    best_score = 0.0

    for po in pos:
        # Must match vendor
        if vendor_id and po.get("vendor_id") != vendor_id:
            continue

        try:
            po_total = float(po.get("total_amount", 0))
        except (ValueError, TypeError):
            continue

        # Amount proximity score (0-1): 1 if exact, 0 if > AMOUNT_TOLERANCE_PCT apart
        if po_total > 0:
            pct_diff = abs(invoice_total - po_total) / po_total
            amount_score = max(0.0, 1.0 - pct_diff / AMOUNT_TOLERANCE_PCT)
        else:
            amount_score = 0.0

        desc_score = _description_similarity(line_items, po.get("description", ""))

        combined = AMOUNT_PROX_WEIGHT * amount_score + DESCRIPTION_SIM_WEIGHT * desc_score

        if combined > best_score:
            best_score = combined
            best_po = po

    return best_po, best_score


def match_po(
    extraction: ExtractionResult,
    vendor_id: str | None,
    vendor_currency: str | None,
    current_run_id: str,
    filename: str | None = None,
) -> POMatchResult:
    """
    Stage 6: Match invoice to a Purchase Order.

    Explicit match: PO reference field → look up in POs table.
    Inferred match: vendor + amount proximity + description similarity.

    Args:
        extraction: ExtractionResult from Stage 2.
        vendor_id: Resolved vendor ID from Stage 4.
        vendor_currency: Expected currency from vendor master.
        current_run_id: ID of the current run (excluded from billed_to_date).
        filename: Optional filename of current document.

    Returns:
        POMatchResult with match details, remaining balance, and variance.
    """
    pos = _load_pos()
    invoice_total = float(extraction.total.value or 0)
    invoice_currency = str(extraction.currency.value or "").upper()
    invoice_line_items = extraction.line_items
    po_ref = extraction.po_reference.value if extraction.po_reference else None
    invoice_num = str(extraction.invoice_number.value or "").strip() if extraction.invoice_number else ""

    matched_po: dict | None = None
    match_type = "none"
    match_confidence = 0.0

    # --- Explicit match ---
    if po_ref:
        for po in pos:
            if po.get("po_number", "").strip().upper() == str(po_ref).strip().upper():
                matched_po = po
                match_type = "explicit"
                match_confidence = 1.0
                break

    # --- Inferred match (if no explicit match) ---
    if matched_po is None:
        matched_po, match_confidence = _inferred_match_score(
            pos, vendor_id, invoice_total, invoice_line_items
        )
        if matched_po is not None:
            match_type = "inferred"

    if matched_po is None:
        return POMatchResult(
            match_type="none",
            match_confidence=0.0,
            invoice_total=invoice_total,
        )

    po_total = float(matched_po.get("total_amount", 0))
    po_currency = matched_po.get("currency", "INR").upper()
    po_number = matched_po.get("po_number", "")

    # Billed to date (split billing support)
    billed_to_date = _get_billed_to_date(
        po_number,
        current_run_id,
        current_invoice_number=invoice_num,
        filename=filename,
    )
    remaining = po_total - billed_to_date
    variance_amount = invoice_total - remaining
    variance_pct = abs(variance_amount / remaining) if remaining > 0 else 0.0
    currency_match = (invoice_currency == po_currency) or (
        vendor_currency and invoice_currency == vendor_currency.upper()
    )

    logger.info(
        "PO match: po=%s type=%s confidence=%.2f remaining=%.2f invoice=%.2f",
        po_number, match_type, match_confidence, remaining, invoice_total,
    )

    return POMatchResult(
        po_number=po_number,
        match_type=match_type,
        match_confidence=match_confidence,
        po_total=po_total,
        billed_to_date=billed_to_date,
        remaining_balance=remaining,
        invoice_total=invoice_total,
        variance_amount=variance_amount,
        variance_pct=variance_pct,
        currency_match=bool(currency_match),
        po_currency=po_currency,
    )
