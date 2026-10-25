"""
pipeline/stage7_rules.py — Deterministic rules engine.

WHY a JSON file (not hard-coded constants):
  Finance teams adjust thresholds frequently. Externalising to rules.json
  lets an admin change a limit without a code deploy. The UI exposes an
  edit form. The file is re-read on every request so changes are live.

Rule evaluation is purely functional: given the outputs of stages 2-6,
return a list of pass/fail results. No LLM involved.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import RULES_FILE
from app.models import (
    DuplicateCheckResult,
    ExtractionResult,
    FraudSeverity,
    POMatchResult,
    RuleResult,
    RulesConfig,
    RulesEvaluationResult,
    ValidationResult,
    VendorCheckResult,
)

logger = logging.getLogger(__name__)


def load_rules() -> RulesConfig:
    """Load rules.json; return defaults if file missing or malformed."""
    path = Path(RULES_FILE)
    if not path.exists():
        logger.warning("rules.json not found, using defaults.")
        return RulesConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RulesConfig.model_validate(data)
    except Exception as exc:
        logger.error("Failed to parse rules.json: %s — using defaults.", exc)
        return RulesConfig()


def evaluate_rules(
    extraction: ExtractionResult,
    validation: ValidationResult,
    vendor: VendorCheckResult,
    duplicate: DuplicateCheckResult,
    po: POMatchResult,
) -> RulesEvaluationResult:
    """
    Stage 7: Evaluate all rules and return pass/fail for each.

    Args:
        extraction, validation, vendor, duplicate, po: outputs of stages 2-6.

    Returns:
        RulesEvaluationResult with one RuleResult per rule.
    """
    rules = load_rules()
    results: list[RuleResult] = []

    def add(rule_id: str, description: str, passed: bool, details: str) -> None:
        results.append(RuleResult(
            rule_id=rule_id,
            description=description,
            passed=passed,
            details=details,
        ))

    invoice_total = float(extraction.total.value or 0)
    currency = str(extraction.currency.value or "INR").upper()

    # R1: Validation passed (math, required fields, dates)
    if not validation.passed:
        errs = "; ".join(i.message for i in validation.issues if i.severity == "error")
        add("R1_VALIDATION", "Invoice passes basic validation",
            False, f"Validation errors: {errs}")
    else:
        add("R1_VALIDATION", "Invoice passes basic validation",
            True, "All required fields present, math checks out.")

    # R2: Known and approved vendor
    if not vendor.matched:
        add("R2_VENDOR_KNOWN", "Vendor is in approved vendor master",
            False, "Vendor not found in vendor master.")
    elif not vendor.approved:
        add("R2_VENDOR_KNOWN", "Vendor is in approved vendor master",
            False, f"Vendor matched (score {vendor.match_score:.0f}) but is marked unapproved.")
    else:
        add("R2_VENDOR_KNOWN", "Vendor is in approved vendor master",
            True, f"Matched vendor (score {vendor.match_score:.0f}/100), approved.")

    # R3: No HIGH severity fraud flags
    high_flags = [f for f in vendor.fraud_flags if f.severity == FraudSeverity.HIGH]
    if high_flags:
        msgs = "; ".join(f.message for f in high_flags)
        add("R3_NO_FRAUD", "No high-severity fraud flags",
            False, msgs)
    else:
        add("R3_NO_FRAUD", "No high-severity fraud flags",
            True, "No bank mismatch or tax ID fraud flags detected.")

    # R4: Not a duplicate
    add("R4_NOT_DUPLICATE", "Invoice is not a duplicate",
        not duplicate.is_duplicate,
        duplicate.reason or "No matching invoice found.")

    # R5: PO match (if required for this amount)
    if invoice_total > rules.require_po_for_amount_above:
        has_good_po = (
            po.match_type == "explicit"
            or (po.match_type == "inferred" and po.match_confidence >= rules.min_inferred_po_confidence)
        )
        if not has_good_po:
            add("R5_PO_REQUIRED", f"PO required for invoices above {rules.require_po_for_amount_above:.0f}",
                False,
                f"No PO matched (type={po.match_type}, confidence={po.match_confidence:.2f}).")
        else:
            add("R5_PO_REQUIRED", f"PO required for invoices above {rules.require_po_for_amount_above:.0f}",
                True,
                f"PO {po.po_number} matched ({po.match_type}, confidence={po.match_confidence:.2f}).")
    else:
        add("R5_PO_REQUIRED", "PO check (below threshold, not required)",
            True,
            f"Invoice total {invoice_total:.2f} is below PO-required threshold "
            f"{rules.require_po_for_amount_above:.2f}.")

    # R6: Amount within PO remaining balance (only if PO matched)
    if po.match_type != "none" and po.remaining_balance is not None:
        tol = max(
            po.remaining_balance * rules.amount_tolerance_pct,
            rules.amount_tolerance_fixed,
        )
        within = (invoice_total - po.remaining_balance) <= tol
        add("R6_AMOUNT_WITHIN_BALANCE",
            "Invoice amount within PO remaining balance (+tolerance)",
            within,
            f"Invoice {invoice_total:.2f}, remaining balance {po.remaining_balance:.2f}, "
            f"tolerance {tol:.2f}, variance {po.variance_amount:+.2f}.")
    else:
        add("R6_AMOUNT_WITHIN_BALANCE", "Amount vs PO balance (no PO matched)",
            True, "No PO match — skipping balance check.")

    # R7: Currency match
    if rules.currency_must_match and po.match_type != "none" and not po.currency_match:
        add("R7_CURRENCY", "Currency matches PO",
            False, f"Invoice currency '{currency}' does not match PO currency.")
    else:
        add("R7_CURRENCY", "Currency matches PO",
            True, f"Currency check passed (currency={currency}).")

    # R8: Extraction confidence meets minimum
    avg_conf = extraction.avg_confidence
    conf_ok = avg_conf >= rules.min_extraction_confidence
    add("R8_CONFIDENCE",
        f"Extraction confidence >= {rules.min_extraction_confidence:.0%}",
        conf_ok,
        f"Average extraction confidence: {avg_conf:.0%}.")

    # R9: Below auto-approve limit
    below_limit = invoice_total <= rules.max_auto_approve_amount
    add("R9_AUTO_APPROVE_LIMIT",
        f"Invoice below auto-approve limit ({rules.max_auto_approve_amount:,.0f})",
        below_limit,
        f"Invoice total: {invoice_total:,.2f}.")

    # R10: Inferred PO confidence (warn if used)
    if po.match_type == "inferred":
        conf_sufficient = po.match_confidence >= rules.min_inferred_po_confidence
        add("R10_INFERRED_PO_CONFIDENCE",
            f"Inferred PO confidence >= {rules.min_inferred_po_confidence:.0%}",
            conf_sufficient,
            f"Inferred PO confidence: {po.match_confidence:.0%}.")

    logger.info(
        "Rules: %d passed, %d failed",
        sum(1 for r in results if r.passed),
        sum(1 for r in results if not r.passed),
    )
    return RulesEvaluationResult(results=results)
