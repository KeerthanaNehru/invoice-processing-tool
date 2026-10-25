"""
models.py — Pydantic v2 models for every stage of the pipeline.

Separation of concerns:
  - Extraction models:  what the LLM returns (+ confidence metadata)
  - Validation models:  deterministic check results
  - DB row models:      what gets persisted to SQLite
  - API response models: what the HTTP layer returns
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Decision(str, Enum):
    AUTO_APPROVED = "AUTO_APPROVED"
    APPROVED_WITH_NOTE = "APPROVED_WITH_NOTE"
    HOLD_FOR_REVIEW = "HOLD_FOR_REVIEW"
    REQUEST_INFO = "REQUEST_INFO"
    REJECTED = "REJECTED"


class IngestMethod(str, Enum):
    TEXT = "text"
    OCR = "ocr"


class FraudSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Stage 2 — Extraction
# ---------------------------------------------------------------------------

class FieldValue(BaseModel):
    """A single extracted field with provenance metadata."""
    value: Any = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_snippet: Optional[str] = None


class LineItem(BaseModel):
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    """
    The structured output we demand from the LLM.
    Every monetary/date field comes with a confidence score and snippet
    so downstream stages can weight their decisions.
    """
    vendor_name: FieldValue = Field(default_factory=FieldValue)
    vendor_tax_id: FieldValue = Field(default_factory=FieldValue)
    invoice_number: FieldValue = Field(default_factory=FieldValue)
    invoice_date: FieldValue = Field(default_factory=FieldValue)
    due_date: FieldValue = Field(default_factory=FieldValue)
    po_reference: FieldValue = Field(default_factory=FieldValue)
    currency: FieldValue = Field(default_factory=FieldValue)
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: FieldValue = Field(default_factory=FieldValue)
    tax_amount: FieldValue = Field(default_factory=FieldValue)
    tax_details: FieldValue = Field(default_factory=FieldValue)
    total: FieldValue = Field(default_factory=FieldValue)
    bank_account: FieldValue = Field(default_factory=FieldValue)
    vendor_email: FieldValue = Field(default_factory=FieldValue)

    # Populated by the pipeline, not the LLM
    from_cache: bool = False
    ocr_used: bool = False

    @property
    def avg_confidence(self) -> float:
        """Average confidence across all top-level scalar fields."""
        fields = [
            self.vendor_name, self.vendor_tax_id, self.invoice_number,
            self.invoice_date, self.total, self.currency,
        ]
        confs = [f.confidence for f in fields if f.value is not None]
        return sum(confs) / len(confs) if confs else 0.0


# ---------------------------------------------------------------------------
# Stage 3 — Validation
# ---------------------------------------------------------------------------

class ValidationIssue(BaseModel):
    field: str
    message: str
    severity: str = "error"  # "error" | "warning"


class ValidationResult(BaseModel):
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 4 — Vendor check
# ---------------------------------------------------------------------------

class FraudFlag(BaseModel):
    code: str
    message: str
    severity: FraudSeverity


class VendorCheckResult(BaseModel):
    matched: bool
    vendor_id: Optional[str] = None
    match_score: float = 0.0
    approved: bool = False
    fraud_flags: list[FraudFlag] = Field(default_factory=list)
    master_bank_account: Optional[str] = None


# ---------------------------------------------------------------------------
# Stage 5 — Duplicate check
# ---------------------------------------------------------------------------

class DuplicateCheckResult(BaseModel):
    is_duplicate: bool
    duplicate_run_id: Optional[str] = None
    reason: Optional[str] = None
    normalised_invoice_no: Optional[str] = None


# ---------------------------------------------------------------------------
# Stage 6 — PO match
# ---------------------------------------------------------------------------

class POMatchResult(BaseModel):
    po_number: Optional[str] = None
    match_type: str = "none"          # "explicit" | "inferred" | "none"
    match_confidence: float = 0.0
    po_total: Optional[float] = None
    billed_to_date: float = 0.0
    remaining_balance: Optional[float] = None
    invoice_total: float = 0.0
    variance_amount: float = 0.0
    variance_pct: float = 0.0
    currency_match: bool = True
    po_currency: Optional[str] = None


# ---------------------------------------------------------------------------
# Stage 7 — Rules
# ---------------------------------------------------------------------------

class RuleResult(BaseModel):
    rule_id: str
    description: str
    passed: bool
    details: str


class RulesEvaluationResult(BaseModel):
    results: list[RuleResult] = Field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failed_rules(self) -> list[RuleResult]:
        return [r for r in self.results if not r.passed]


# ---------------------------------------------------------------------------
# Stage 8 — Decision
# ---------------------------------------------------------------------------

class DecisionResult(BaseModel):
    decision: Decision
    reason_codes: list[str] = Field(default_factory=list)
    explanation: str = ""           # plain-English for finance person
    vendor_email_draft: Optional[str] = None   # only for REQUEST_INFO
    overridden: bool = False
    override_note: Optional[str] = None
    override_by: Optional[str] = None
    override_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Full pipeline result — one row in runs table
# ---------------------------------------------------------------------------

class StageEvent(BaseModel):
    stage_index: int
    stage_name: str
    status: StageStatus
    payload: Optional[dict] = None
    duration_ms: Optional[int] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class RunRecord(BaseModel):
    """
    Persisted in SQLite; returned by GET /api/runs/{id}.
    stage_results is a JSON blob of StageEvent list.
    """
    id: str
    filename: str
    file_sha256: str
    ingest_method: Optional[str] = None
    status: str = "pending"          # pending | running | done | failed
    decision: Optional[str] = None
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_total: Optional[float] = None
    currency: Optional[str] = None
    po_number: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    processing_ms: Optional[int] = None
    is_sample: bool = False
    stage_results: Optional[str] = None   # JSON of list[StageEvent]
    extraction_json: Optional[str] = None
    decision_json: Optional[str] = None


# ---------------------------------------------------------------------------
# API request/response shapes
# ---------------------------------------------------------------------------

class OverrideRequest(BaseModel):
    decision: Decision
    note: str = Field(min_length=1, max_length=500)


class RulesConfig(BaseModel):
    """
    The editable rules.json schema.
    Validated on every PUT /api/rules call.
    """
    amount_tolerance_pct: float = Field(default=0.02, ge=0.0, le=1.0)
    amount_tolerance_fixed: float = Field(default=500.0, ge=0.0)
    max_auto_approve_amount: float = Field(default=50_000.0, ge=0.0)
    min_extraction_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    min_inferred_po_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    currency_must_match: bool = True
    require_po_for_amount_above: float = Field(default=10_000.0, ge=0.0)
    duplicate_date_window_days: int = Field(default=3, ge=1)
    minutes_per_invoice_manual: float = Field(default=30.0, ge=1.0)
