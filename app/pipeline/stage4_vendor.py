"""
pipeline/stage4_vendor.py — Fuzzy vendor matching and fraud detection.

WHY fuzzy matching (rapidfuzz):
  Vendor names on invoices rarely match the master exactly
  ("Acme Corp." vs "ACME CORPORATION"). A threshold of 80 gives good
  recall without false positives in practice.

Bank account fraud flag:
  Changing bank details is a common invoice fraud vector. If the invoice
  states bank details that differ from the vendor master, we raise a HIGH
  severity flag — a human must approve.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from rapidfuzz import fuzz, process

from app.config import VENDORS_CSV
from app.models import (
    ExtractionResult,
    FraudFlag,
    FraudSeverity,
    VendorCheckResult,
)

logger = logging.getLogger(__name__)

FUZZY_MATCH_THRESHOLD = 80   # minimum score (0-100) to consider a match
TAX_ID_BONUS = 10            # score boost when tax IDs also match


def _load_vendors() -> list[dict]:
    """Load vendors from SQLite database (seeded + user-added). Fall back to CSV."""
    from app.database import get_db
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM vendors").fetchall()
            if rows:
                return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("Could not read vendors from DB, falling back to CSV: %s", exc)

    path = Path(VENDORS_CSV)
    if not path.exists():
        logger.warning("vendors.csv not found at %s", path)
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _normalise_bank(account: str | None) -> str:
    """Normalise bank account string for comparison (digits and letters only)."""
    if not account:
        return ""
    return "".join(c for c in str(account) if c.isalnum()).upper()


def check_vendor(extraction: ExtractionResult) -> VendorCheckResult:
    """
    Stage 4: Fuzzy-match vendor name against vendors.csv.
    Also checks bank account details for fraud indicators.

    Args:
        extraction: ExtractionResult from Stage 2.

    Returns:
        VendorCheckResult with match details and any fraud flags.
    """
    vendors = _load_vendors()
    fraud_flags: list[FraudFlag] = []

    invoice_vendor = extraction.vendor_name.value
    invoice_tax_id = extraction.vendor_tax_id.value
    invoice_bank = _normalise_bank(
        extraction.bank_account.value if extraction.bank_account else None
    )

    if not invoice_vendor or not vendors:
        return VendorCheckResult(
            matched=False,
            approved=False,
            fraud_flags=[FraudFlag(
                code="UNKNOWN_VENDOR",
                message="Vendor name not extracted or vendor master is empty.",
                severity=FraudSeverity.MEDIUM,
            )],
        )

    # Build lookup: vendor name → row
    vendor_names = [v.get("name", "") for v in vendors]
    matches = process.extract(
        str(invoice_vendor),
        vendor_names,
        scorer=fuzz.token_sort_ratio,
        limit=3,
    )

    if not matches or matches[0][1] < FUZZY_MATCH_THRESHOLD:
        return VendorCheckResult(
            matched=False,
            approved=False,
            match_score=matches[0][1] if matches else 0.0,
            fraud_flags=[FraudFlag(
                code="UNKNOWN_VENDOR",
                message=f"Vendor '{invoice_vendor}' not found in vendor master "
                        f"(best match score: {matches[0][1] if matches else 0:.0f}/100).",
                severity=FraudSeverity.MEDIUM,
            )],
        )

    best_name, score, _ = matches[0]
    matched_vendor = next(v for v in vendors if v.get("name") == best_name)

    # Boost score if tax IDs also match
    if invoice_tax_id and matched_vendor.get("tax_id"):
        if str(invoice_tax_id).strip() == str(matched_vendor["tax_id"]).strip():
            score = min(100.0, score + TAX_ID_BONUS)
        else:
            # Tax ID present but mismatched — suspicious
            fraud_flags.append(FraudFlag(
                code="TAX_ID_MISMATCH",
                message=(
                    f"Invoice tax ID '{invoice_tax_id}' does not match "
                    f"vendor master '{matched_vendor.get('tax_id')}' for '{best_name}'."
                ),
                severity=FraudSeverity.HIGH,
            ))

    # Bank account check
    master_bank = _normalise_bank(matched_vendor.get("bank_account"))
    if invoice_bank and master_bank and invoice_bank != master_bank:
        fraud_flags.append(FraudFlag(
            code="BANK_DETAILS_MISMATCH",
            message=(
                f"Invoice bank account does not match vendor master for '{best_name}'. "
                "This is a HIGH severity fraud indicator — manual review required."
            ),
            severity=FraudSeverity.HIGH,
        ))

    approved = str(matched_vendor.get("approved", "1")).strip().lower() in ("1", "true", "yes")

    logger.info(
        "Vendor check: matched='%s' score=%.0f approved=%s flags=%d",
        best_name, score, approved, len(fraud_flags),
    )

    return VendorCheckResult(
        matched=True,
        vendor_id=matched_vendor.get("id"),
        match_score=score,
        approved=approved,
        fraud_flags=fraud_flags,
        master_bank_account=matched_vendor.get("bank_account"),
    )
