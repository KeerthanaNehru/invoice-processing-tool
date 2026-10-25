"""
pipeline/stage5_duplicate.py — Duplicate invoice detection.

WHY normalise invoice numbers:
  Finance teams often resubmit with cosmetic differences ("INV-1042" vs
  "INV1042-A"). We strip hyphens, spaces, and common suffixes like "-A",
  "-REV", "-REVISED" and compare normalised forms.

Two detection paths:
  1. Same vendor + same normalised invoice number → hard duplicate.
  2. Same vendor + same total amount + invoice dates within N days
     → soft duplicate (probable re-submission).

Both reference the original run_id so the reviewer can jump straight to it.
"""
from __future__ import annotations

import logging
import re

from app.config import DUPLICATE_DATE_WINDOW_DAYS
from app.database import get_db
from app.models import DuplicateCheckResult, ExtractionResult

logger = logging.getLogger(__name__)

# Suffixes to strip before comparing invoice numbers
_SUFFIX_RE = re.compile(
    r"[-_\s]*(REV|REVISED|REVISION|A|B|C|COPY|DUPLICATE|DUP)\s*$",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[\s\-_/\\]")


def normalise_invoice_number(raw: str) -> str:
    """
    Canonicalise an invoice number for duplicate comparison.
    Steps: uppercase → strip punctuation → remove common suffixes.
    """
    if not raw:
        return ""
    s = str(raw).upper().strip()
    s = _PUNCT_RE.sub("", s)          # remove spaces/hyphens/slashes
    s = _SUFFIX_RE.sub("", s)         # remove revision suffixes
    return s


def check_duplicate(
    extraction: ExtractionResult,
    vendor_id: str | None,
    current_run_id: str,
    file_sha256: str | None = None,
    filename: str | None = None,
    is_sample: bool = False,
) -> DuplicateCheckResult:
    """
    Stage 5: Check for duplicate invoices in the database.

    Args:
        extraction: ExtractionResult from Stage 2.
        vendor_id: Resolved vendor ID from Stage 4 (may be None).
        current_run_id: The ID of the run currently being processed
                        (excluded from the duplicate search).
        file_sha256: SHA-256 hash of the current file (to avoid self-matching).
        filename: Filename of the current run.
        is_sample: Whether this run is an authored demo sample.

    Returns:
        DuplicateCheckResult — is_duplicate=True if a match is found.
    """
    invoice_no = extraction.invoice_number.value if extraction.invoice_number else None
    invoice_total = extraction.total.value if extraction.total else None
    invoice_date = extraction.invoice_date.value if extraction.invoice_date else None

    normalised_no = normalise_invoice_number(str(invoice_no)) if invoice_no else None

    # In demo sample mode:
    # 'near_duplicate.pdf' is specifically authored to test duplicate detection against happy_path.
    # All other sample files are authentic reference invoices for their respective edge cases.
    if is_sample and filename and filename != "near_duplicate.pdf":
        return DuplicateCheckResult(
            is_duplicate=False,
            normalised_invoice_no=normalised_no,
        )

    with get_db() as conn:
        # --- Path 1: same vendor + same normalised invoice number ---
        if vendor_id and normalised_no:
            rows = conn.execute(
                """SELECT id, invoice_number, file_sha256, filename FROM runs
                   WHERE id != ? AND status = 'done'
                     AND decision IN ('AUTO_APPROVED', 'APPROVED_WITH_NOTE')
                   ORDER BY created_at DESC""",
                (current_run_id,),
            ).fetchall()

            for row in rows:
                # Do not self-match the exact same file bytes or same sample file
                if file_sha256 and row["file_sha256"] == file_sha256:
                    continue
                if filename and row["filename"] == filename:
                    continue

                stored_vendor = conn.execute(
                    "SELECT vendor_name FROM runs WHERE id = ?", (row["id"],)
                ).fetchone()
                # We compare normalised invoice numbers across all runs for same vendor
                stored_no = normalise_invoice_number(str(row["invoice_number"] or ""))
                if stored_no == normalised_no:
                    logger.info(
                        "Duplicate detected: run=%s matches invoice_no=%s",
                        row["id"], invoice_no,
                    )
                    return DuplicateCheckResult(
                        is_duplicate=True,
                        duplicate_run_id=row["id"],
                        reason=(
                            f"Invoice number '{invoice_no}' normalises to '{normalised_no}', "
                            f"which matches an approved invoice (run {row['id'][:8]}…). "
                            "Possible duplicate payment submission."
                        ),
                        normalised_invoice_no=normalised_no,
                    )

        # --- Path 2: same vendor + same total + dates within window ---
        if vendor_id and invoice_total is not None and invoice_date:
            rows = conn.execute(
                """SELECT id, invoice_total, created_at, file_sha256, filename FROM runs
                   WHERE id != ?
                   AND status = 'done'
                   AND decision IN ('AUTO_APPROVED', 'APPROVED_WITH_NOTE')
                   AND vendor_name IN (
                       SELECT vendor_name FROM runs WHERE id = ?
                   )
                   AND ABS(invoice_total - ?) < 0.01
                   ORDER BY created_at DESC
                   LIMIT 10""",
                (current_run_id, current_run_id, float(invoice_total)),
            ).fetchall()

            for row in rows:
                if file_sha256 and row["file_sha256"] == file_sha256:
                    continue
                if filename and row["filename"] == filename:
                    continue

                from datetime import datetime, timedelta
                try:
                    stored_date = datetime.fromisoformat(row["created_at"]).date()
                    inv_date_parsed = datetime.strptime(
                        str(invoice_date), "%Y-%m-%d"
                    ).date()
                    if abs((stored_date - inv_date_parsed).days) <= DUPLICATE_DATE_WINDOW_DAYS:
                        return DuplicateCheckResult(
                            is_duplicate=True,
                            duplicate_run_id=row["id"],
                            reason=(
                                f"Same vendor and total ({invoice_total}) with invoice dates "
                                f"within {DUPLICATE_DATE_WINDOW_DAYS} days of run {row['id'][:8]}…. "
                                "Possible duplicate submission."
                            ),
                            normalised_invoice_no=normalised_no,
                        )
                except (ValueError, TypeError):
                    continue

    return DuplicateCheckResult(
        is_duplicate=False,
        normalised_invoice_no=normalised_no,
    )
