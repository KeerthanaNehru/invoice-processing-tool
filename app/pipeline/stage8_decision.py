"""
pipeline/stage8_decision.py — Final decision + LLM explanation.

WHY the decision is deterministic (not LLM):
  The decision tree maps rule failures to outcomes. This is fully auditable
  and reproducible. The LLM only WRITES the English explanation — it cannot
  change the decision, and it may only reference facts passed to it
  (no access to raw invoice text).

Decision tree (evaluated in order):
  REJECTED          — duplicate, or HIGH fraud flag
  REQUEST_INFO      — validation failed (missing fields)
  HOLD_FOR_REVIEW   — vendor unknown/unapproved, amount over balance, low conf
  APPROVED_WITH_NOTE — inferred PO, or minor warnings
  AUTO_APPROVED     — all rules pass, below auto-approve limit
"""
from __future__ import annotations

import logging
from typing import Optional

from app.llm import call_llm_text
from app.models import (
    Decision,
    DecisionResult,
    DuplicateCheckResult,
    ExtractionResult,
    FraudSeverity,
    POMatchResult,
    RulesEvaluationResult,
    ValidationResult,
    VendorCheckResult,
)

logger = logging.getLogger(__name__)


def _make_decision(
    extraction: ExtractionResult,
    validation: ValidationResult,
    vendor: VendorCheckResult,
    duplicate: DuplicateCheckResult,
    po: POMatchResult,
    rules_result: RulesEvaluationResult,
) -> tuple[Decision, list[str]]:
    """
    Pure function: map rule failures to a decision + reason codes.
    Order matters — earlier conditions are more severe.
    """
    codes: list[str] = []
    failed = {r.rule_id for r in rules_result.failed_rules}

    # REJECTED: duplicate or critical fraud
    if duplicate.is_duplicate:
        codes.append("DUPLICATE_INVOICE")
        return Decision.REJECTED, codes

    high_fraud = [f for f in vendor.fraud_flags if f.severity == FraudSeverity.HIGH]
    if high_fraud:
        codes.extend(f.code for f in high_fraud)
        return Decision.REJECTED, codes

    # REQUEST_INFO: missing critical fields
    if "R1_VALIDATION" in failed:
        codes.append("VALIDATION_FAILED")
        missing = [i.field for i in validation.issues if i.severity == "error"]
        codes.extend(f"MISSING_{f.upper()}" for f in missing)
        return Decision.REQUEST_INFO, codes

    # HOLD_FOR_REVIEW: vendor issues, budget exceeded, low confidence
    hold_rules = {"R2_VENDOR_KNOWN", "R5_PO_REQUIRED", "R6_AMOUNT_WITHIN_BALANCE",
                  "R8_CONFIDENCE", "R7_CURRENCY"}
    failing_holds = failed & hold_rules
    if failing_holds:
        codes.extend(failing_holds)
        return Decision.HOLD_FOR_REVIEW, codes

    # APPROVED_WITH_NOTE: inferred PO or R9 (over auto-approve limit)
    note_rules = {"R9_AUTO_APPROVE_LIMIT", "R10_INFERRED_PO_CONFIDENCE"}
    failing_notes = failed & note_rules
    if failing_notes or po.match_type == "inferred":
        codes.extend(failing_notes)
        if po.match_type == "inferred":
            codes.append("INFERRED_PO_MATCH")
        return Decision.APPROVED_WITH_NOTE, codes

    # Warn-only fraud flags (non-HIGH)
    medium_flags = [f for f in vendor.fraud_flags if f.severity != FraudSeverity.HIGH]
    if medium_flags:
        codes.extend(f.code for f in medium_flags)
        return Decision.APPROVED_WITH_NOTE, codes

    # AUTO_APPROVED: all rules pass
    codes.append("ALL_RULES_PASSED")
    return Decision.AUTO_APPROVED, codes


def _currency_symbol(curr_code: str | None) -> str:
    c = (curr_code or "").strip().upper()
    if c == "USD":
        return "$"
    elif c == "INR":
        return "₹"
    elif c == "EUR":
        return "€"
    elif c == "GBP":
        return "£"
    elif c == "CAD":
        return "CA$"
    return c or "$"


_EXPLANATION_SYSTEM = """You are an accounts-payable specialist writing for the finance team.
Write a clear, simple, and concise explanation in strictly 2 to 3 sentences explaining the invoice processing outcome.

CRITICAL INSTRUCTIONS:
- Keep it short, natural, and immediately understandable to a finance team member.
- State the final decision (Approved, Placed on Hold, Rejected, or Information Needed) and the plain business reason.
- NEVER mention internal code names, rule IDs (such as R3_NO_FRAUD, R9_AUTO_APPROVE_LIMIT, R6_AMOUNT_WITHIN_BALANCE, etc.), or extraction confidence percentages.
- Instead, use natural business phrasing (e.g. "bank account on the invoice does not match the vendor's registered bank details", "invoice amount exceeds the remaining purchase order balance", "duplicate of an invoice already processed").
- Always cite monetary amounts with the currency symbol that matches the invoice currency (e.g. '$' for USD, '₹' for INR, '£' for GBP, '€' for EUR). Never use Rupees for USD or Dollars for INR.
- Write in clean, professional prose without bullet points."""


_EMAIL_SYSTEM = """You are a professional accounts-payable assistant.
Write a polite, professional email to a vendor requesting the missing information listed.
The email should be concise (under 150 words), include a clear subject line, and list each missing item.
Sign off as 'Accounts Payable Team'."""


async def decide(
    extraction: ExtractionResult,
    validation: ValidationResult,
    vendor: VendorCheckResult,
    duplicate: DuplicateCheckResult,
    po: POMatchResult,
    rules_result: RulesEvaluationResult,
) -> DecisionResult:
    """
    Stage 8: Produce the final decision and LLM-generated explanation.

    Args:
        All stage outputs from stages 2-7.

    Returns:
        DecisionResult with decision code, reason codes, and explanation.
    """
    decision, reason_codes = _make_decision(
        extraction, validation, vendor, duplicate, po, rules_result
    )

    # Build a concise, clean business summary for the LLM
    invoice_no = getattr(extraction.invoice_number, "value", None) or "Not specified"
    vendor_name = getattr(extraction.vendor_name, "value", None) or "Unknown vendor"
    total = float(getattr(extraction.total, "value", None) or 0)
    curr_code = getattr(extraction.currency, "value", None) or "USD"
    sym = _currency_symbol(curr_code)
    po_sym = _currency_symbol(getattr(po, "po_currency", None) or curr_code)
    po_num = po.po_number or "None"

    # Translate technical reasons to plain business explanations
    business_reasons = []
    if "BANK_DETAILS_MISMATCH" in reason_codes:
        business_reasons.append("Bank account on invoice does not match approved vendor master data")
    if "DUPLICATE_INVOICE" in reason_codes:
        business_reasons.append("An identical invoice was already processed and approved")
    if "VALIDATION_FAILED" in reason_codes:
        missing = [i.message for i in validation.issues if i.severity == "error"]
        business_reasons.append(f"Missing required fields: {', '.join(missing) if missing else 'invoice number or total'}")
    if "R6_AMOUNT_WITHIN_BALANCE" in reason_codes:
        business_reasons.append(f"Amount exceeds remaining PO balance by {po_sym}{abs(po.variance_amount):,.2f}")
    if "INFERRED_PO_MATCH" in reason_codes or po.match_type == "inferred":
        business_reasons.append(f"Matched to PO {po_num} via service description and vendor similarity")
    if "R9_AUTO_APPROVE_LIMIT" in reason_codes:
        business_reasons.append("Milestone amount requires standard secondary verification")
    if not business_reasons:
        business_reasons.append("All vendor, line-item, and purchase order checks passed successfully")

    facts = f"""Decision: {decision.value}
Invoice Number: {invoice_no}
Vendor: {vendor_name}
Invoice Total: {sym}{total:,.2f} ({curr_code})
Purchase Order: {po_num}
Business Reason: {'; '.join(business_reasons)}"""

    explanation_prompt = f"""The following invoice was processed. Write a simple 2-3 sentence explanation for the finance manager:\n\n{facts}"""
    try:
        explanation = await call_llm_text(_EXPLANATION_SYSTEM, explanation_prompt)
    except Exception as exc:
        logger.warning("LLM explanation generation failed; using deterministic fallback: %s", exc)
        if decision == Decision.AUTO_APPROVED:
            explanation = f"Invoice {invoice_no} from {vendor_name} for {sym}{total:,.2f} is approved. All line items, vendor details, and purchase order {po_num} were verified with zero discrepancies."
        elif decision == Decision.APPROVED_WITH_NOTE:
            if po.match_type == "inferred":
                explanation = f"Invoice {invoice_no} from {vendor_name} for {sym}{total:,.2f} is approved with an audit note. The invoice was matched to purchase order {po_num} based on vendor identity and service description."
            else:
                explanation = f"Invoice {invoice_no} from {vendor_name} for {sym}{total:,.2f} is approved with an audit note. The milestone instalment is within the total purchase order {po_num} budget."
        elif decision == Decision.HOLD_FOR_REVIEW:
            if "R6_AMOUNT_WITHIN_BALANCE" in reason_codes:
                explanation = f"Invoice {invoice_no} from {vendor_name} for {sym}{total:,.2f} is placed on hold for finance review. The invoice amount exceeds the remaining purchase order {po_num} balance by {po_sym}{abs(po.variance_amount):,.2f} ({po.variance_pct:.1%}), which is beyond the allowable tolerance."
            else:
                explanation = f"Invoice {invoice_no} from {vendor_name} for {sym}{total:,.2f} is on hold for finance team review."
        elif decision == Decision.REJECTED:
            if "BANK_DETAILS_MISMATCH" in reason_codes:
                explanation = f"Invoice {invoice_no} from {vendor_name} ({sym}{total:,.2f}) has been rejected. The bank account on the invoice does not match the vendor's verified bank records on file, stopping payment to protect against fraud."
            elif "DUPLICATE_INVOICE" in reason_codes:
                explanation = f"Invoice {invoice_no} from {vendor_name} ({sym}{total:,.2f}) has been rejected as a duplicate. An identical invoice for this amount and vendor was already processed."
            else:
                explanation = f"Invoice {invoice_no} from {vendor_name} has been rejected due to critical compliance issues."
        else:
            explanation = f"Invoice from {vendor_name} cannot be processed due to missing invoice number and total amount. A clarification email has been prepared to request updated details from the vendor."

    # Draft vendor email for REQUEST_INFO
    vendor_email: Optional[str] = None
    if decision == Decision.REQUEST_INFO:
        missing_fields = [
            code.replace("MISSING_", "").replace("_", " ").title()
            for code in reason_codes
            if code.startswith("MISSING_")
        ]
        if validation.issues:
            missing_fields = [i.message for i in validation.issues if i.severity == "error"]

        vendor_email_addr = getattr(extraction.vendor_email, "value", None) or "[vendor email]"
        email_prompt = (
            f"Vendor: {vendor_name}\n"
            f"Vendor email: {vendor_email_addr}\n"
            f"Invoice number: {invoice_no}\n"
            f"Missing / unclear items:\n" +
            "\n".join(f"- {m}" for m in missing_fields)
        )
        try:
            vendor_email = await call_llm_text(_EMAIL_SYSTEM, email_prompt)
        except Exception as exc:
            logger.warning("LLM email draft generation failed; using deterministic fallback: %s", exc)
            items_str = "\n".join(f"- {m}" for m in (missing_fields or ["Invoice total and line items"]))
            vendor_email = (
                f"Subject: Action Required: Clarification needed for Invoice {invoice_no}\n\n"
                f"Dear {vendor_name} Accounts Team,\n\n"
                f"We received your invoice {invoice_no}, but our automated accounts payable review noted missing or unclear information:\n\n"
                f"{items_str}\n\n"
                f"Please reply with an updated invoice or clarification to ensure timely processing.\n\n"
                f"Best regards,\nAccounts Payable Team"
            )

    logger.info("Decision: %s, codes: %s", decision.value, reason_codes)

    return DecisionResult(
        decision=decision,
        reason_codes=reason_codes,
        explanation=explanation,
        vendor_email_draft=vendor_email,
    )
