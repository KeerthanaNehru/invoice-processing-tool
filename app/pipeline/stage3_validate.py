"""
pipeline/stage3_validate.py — Deterministic invoice validation.

WHY deterministic (not LLM):
  Math checks must be exact and auditable. An LLM might "round" or
  rationalise errors. This stage catches every discrepancy with precise
  numbers so the finance team can trust the output.

Checks performed:
  1. Required fields: invoice_number, invoice_date, total, vendor_name
  2. Line items sum to subtotal (within rounding tolerance)
  3. subtotal + tax_amount = total (within rounding tolerance)
  4. invoice_date is not in the future
  5. due_date (if present) is after invoice_date
  6. All monetary amounts are non-negative
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from app.models import ExtractionResult, ValidationIssue, ValidationResult

logger = logging.getLogger(__name__)

ROUNDING_TOLERANCE = 0.02   # absolute dollar/rupee tolerance for math checks


def _safe_float(fv) -> float | None:
    """Safely extract a numeric value from a FieldValue."""
    if fv is None:
        return None
    v = fv.value
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_str(fv) -> str | None:
    if fv is None or fv.value is None:
        return None
    return str(fv.value).strip()


def _parse_date(fv) -> date | None:
    if fv is None or fv.value is None:
        return None
    raw = str(fv.value)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def validate(extraction: ExtractionResult) -> ValidationResult:
    """
    Stage 3: Run all deterministic checks and return a ValidationResult.

    Args:
        extraction: The ExtractionResult from Stage 2.

    Returns:
        ValidationResult with passed flag and list of issues.
    """
    issues: list[ValidationIssue] = []

    # 1. Required fields
    required = {
        "invoice_number": extraction.invoice_number,
        "invoice_date": extraction.invoice_date,
        "total": extraction.total,
        "vendor_name": extraction.vendor_name,
    }
    for field_name, fv in required.items():
        if fv is None or fv.value is None:
            issues.append(ValidationIssue(
                field=field_name,
                message=f"Required field '{field_name}' is missing.",
                severity="error",
            ))

    # 2. Line items sum
    line_items_total = sum(
        float(li.amount) for li in extraction.line_items
        if li.amount is not None
    )
    subtotal = _safe_float(extraction.subtotal)
    if subtotal is not None and extraction.line_items:
        diff = abs(line_items_total - subtotal)
        if diff > ROUNDING_TOLERANCE:
            issues.append(ValidationIssue(
                field="subtotal",
                message=(
                    f"Line items sum ({line_items_total:.2f}) does not match "
                    f"subtotal ({subtotal:.2f}). Difference: {diff:.2f}."
                ),
                severity="error",
            ))

    # 3. subtotal + tax = total
    tax = _safe_float(extraction.tax_amount) or 0.0
    total = _safe_float(extraction.total)
    if subtotal is not None and total is not None:
        expected_total = subtotal + tax
        diff = abs(expected_total - total)
        if diff > ROUNDING_TOLERANCE:
            issues.append(ValidationIssue(
                field="total",
                message=(
                    f"subtotal ({subtotal:.2f}) + tax ({tax:.2f}) = {expected_total:.2f} "
                    f"but total shows {total:.2f}. Difference: {diff:.2f}."
                ),
                severity="error",
            ))

    # 4. Invoice date not in the future
    inv_date = _parse_date(extraction.invoice_date)
    today = date.today()
    if inv_date is not None and inv_date > today:
        issues.append(ValidationIssue(
            field="invoice_date",
            message=f"Invoice date {inv_date} is in the future.",
            severity="error",
        ))

    # 5. Due date after invoice date
    due_date = _parse_date(extraction.due_date)
    if inv_date and due_date and due_date < inv_date:
        issues.append(ValidationIssue(
            field="due_date",
            message=f"Due date {due_date} is before invoice date {inv_date}.",
            severity="warning",
        ))

    # 6. Non-negative amounts
    for field_name in ("subtotal", "tax_amount", "total"):
        val = _safe_float(getattr(extraction, field_name))
        if val is not None and val < 0:
            issues.append(ValidationIssue(
                field=field_name,
                message=f"'{field_name}' is negative ({val:.2f}).",
                severity="error",
            ))

    error_count = sum(1 for i in issues if i.severity == "error")
    passed = error_count == 0

    logger.info("Validation: passed=%s, issues=%d", passed, len(issues))
    return ValidationResult(passed=passed, issues=issues)
