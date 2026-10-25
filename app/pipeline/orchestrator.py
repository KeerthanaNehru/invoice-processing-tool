"""
pipeline/orchestrator.py — Runs all 8 stages and emits SSE-compatible events.

Design:
  - Each stage is timed and its result is immediately persisted to SQLite
    (so the UI can reload a run if the SSE stream drops).
  - Events are yielded as dicts; the SSE route serialises them to
    `data: {...}\n\n` format.
  - Stage failures are caught: the pipeline aborts with a FAILED status
    rather than crashing the whole request.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime
from typing import AsyncGenerator

from app.database import (
    get_run,
    insert_run,
    log_audit,
    update_run,
    upsert_stage_result,
)
from app.models import StageEvent, StageStatus
from app.pipeline.stage1_ingest import IngestError, NotAnInvoiceError, ingest
from app.pipeline.stage2_extract import extract
from app.pipeline.stage3_validate import validate
from app.pipeline.stage4_vendor import check_vendor
from app.pipeline.stage5_duplicate import check_duplicate
from app.pipeline.stage6_po_match import match_po
from app.pipeline.stage7_rules import evaluate_rules
from app.pipeline.stage8_decision import decide

logger = logging.getLogger(__name__)

STAGE_NAMES = [
    "Ingest",
    "Extract",
    "Validate",
    "Vendor Check",
    "Duplicate Check",
    "PO Match",
    "Rules Engine",
    "Decision",
]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _event(
    event_type: str,
    stage_index: int,
    stage_name: str,
    status: StageStatus,
    payload: dict | None = None,
    duration_ms: int | None = None,
) -> dict:
    return {
        "event": event_type,
        "stage_index": stage_index,
        "stage_name": stage_name,
        "status": status.value,
        "payload": payload or {},
        "duration_ms": duration_ms,
        "ts": datetime.utcnow().isoformat(),
    }


async def run_pipeline(
    pdf_bytes: bytes,
    filename: str,
    is_sample: bool = False,
    run_id: str | None = None,
    pdf_path: str | None = None,
) -> AsyncGenerator[dict, None]:
    """
    Full pipeline coroutine. Yields event dicts for SSE streaming.

    Args:
        pdf_bytes: Raw PDF file content.
        filename: Original filename (for display only).
        is_sample: True if processing a sample invoice.
        run_id: Pre-assigned run ID (optional; generated if not provided).
        pdf_path: Path on disk to the stored PDF file (optional).

    Yields:
        Event dicts with keys: event, stage_index, stage_name, status,
        payload, duration_ms, ts.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())

    sha256 = _sha256(pdf_bytes)
    stage_events: list[dict] = []

    # Create the run record
    insert_run({
        "id": run_id,
        "filename": filename,
        "file_sha256": sha256,
        "status": "running",
        "created_at": datetime.utcnow().isoformat(),
        "is_sample": int(is_sample),
        "pdf_path": pdf_path,
    })
    log_audit(run_id, "RUN_STARTED", f"file={filename}, sha256={sha256[:16]}")

    yield {"event": "run_started", "run_id": run_id, "filename": filename}

    # -------------------------------------------------------------------------
    # Stage 1 — Ingest
    # -------------------------------------------------------------------------
    t0 = time.monotonic()
    yield _event("stage_started", 0, "Ingest", StageStatus.RUNNING)
    try:
        ingest_result = ingest(pdf_bytes, filename)
        dur = int((time.monotonic() - t0) * 1000)
        ev = _event("stage_completed", 0, "Ingest", StageStatus.PASSED,
                    {"method": ingest_result["method"].value,
                     "page_count": ingest_result["page_count"],
                     "char_count": len(ingest_result["text"])},
                    dur)
        stage_events.append(ev)
        upsert_stage_result(run_id, stage_events)
        update_run(run_id, ingest_method=ingest_result["method"].value)
        yield ev
    except (IngestError, NotAnInvoiceError) as exc:
        dur = int((time.monotonic() - t0) * 1000)
        ev = _event("stage_completed", 0, "Ingest", StageStatus.FAILED,
                    {"error": str(exc)}, dur)
        stage_events.append(ev)
        upsert_stage_result(run_id, stage_events)
        update_run(run_id, status="failed", decision="REJECTED",
                   completed_at=datetime.utcnow().isoformat())
        log_audit(run_id, "RUN_FAILED", str(exc))
        yield ev
        yield {"event": "run_failed", "run_id": run_id, "error": str(exc)}
        return
    except Exception as exc:
        logger.exception("Unexpected error in Stage 1")
        yield {"event": "run_failed", "run_id": run_id, "error": f"Internal error: {exc}"}
        update_run(run_id, status="failed")
        return

    text = ingest_result["text"]
    ocr_used = ingest_result["method"].value == "ocr"

    # -------------------------------------------------------------------------
    # Stage 2 — Extract
    # -------------------------------------------------------------------------
    t0 = time.monotonic()
    yield _event("stage_started", 1, "Extract", StageStatus.RUNNING)
    try:
        extraction = await extract(text, sha256, ocr_used=ocr_used)
        dur = int((time.monotonic() - t0) * 1000)
        ev = _event("stage_completed", 1, "Extract", StageStatus.PASSED,
                    {"extraction": extraction.model_dump(),
                     "from_cache": extraction.from_cache,
                     "avg_confidence": extraction.avg_confidence},
                    dur)
        stage_events.append(ev)
        upsert_stage_result(run_id, stage_events)
        update_run(
            run_id,
            vendor_name=str(extraction.vendor_name.value or ""),
            invoice_number=str(extraction.invoice_number.value or ""),
            invoice_total=float(extraction.total.value or 0),
            currency=str(extraction.currency.value or ""),
            extraction_json=extraction.model_dump_json(),
        )
        yield ev
    except Exception as exc:
        logger.exception("Stage 2 error")
        dur = int((time.monotonic() - t0) * 1000)
        ev = _event("stage_completed", 1, "Extract", StageStatus.FAILED,
                    {"error": str(exc)}, dur)
        stage_events.append(ev)
        upsert_stage_result(run_id, stage_events)
        update_run(run_id, status="failed")
        yield ev
        yield {"event": "run_failed", "run_id": run_id, "error": str(exc)}
        return

    # -------------------------------------------------------------------------
    # Stage 3 — Validate
    # -------------------------------------------------------------------------
    t0 = time.monotonic()
    yield _event("stage_started", 2, "Validate", StageStatus.RUNNING)
    validation = validate(extraction)
    dur = int((time.monotonic() - t0) * 1000)
    v_status = StageStatus.PASSED if validation.passed else StageStatus.WARNING
    ev = _event("stage_completed", 2, "Validate", v_status,
                {"passed": validation.passed,
                 "issues": [i.model_dump() for i in validation.issues]},
                dur)
    stage_events.append(ev)
    upsert_stage_result(run_id, stage_events)
    yield ev

    # -------------------------------------------------------------------------
    # Stage 4 — Vendor Check
    # -------------------------------------------------------------------------
    t0 = time.monotonic()
    yield _event("stage_started", 3, "Vendor Check", StageStatus.RUNNING)
    vendor_result = check_vendor(extraction)
    dur = int((time.monotonic() - t0) * 1000)
    v_status = (StageStatus.PASSED if vendor_result.matched and vendor_result.approved
                and not any(f.severity.value == "HIGH" for f in vendor_result.fraud_flags)
                else StageStatus.WARNING)
    ev = _event("stage_completed", 3, "Vendor Check", v_status,
                vendor_result.model_dump(), dur)
    stage_events.append(ev)
    upsert_stage_result(run_id, stage_events)
    yield ev

    # -------------------------------------------------------------------------
    # Stage 5 — Duplicate Check
    # -------------------------------------------------------------------------
    t0 = time.monotonic()
    yield _event("stage_started", 4, "Duplicate Check", StageStatus.RUNNING)
    dup_result = check_duplicate(
        extraction,
        vendor_result.vendor_id,
        run_id,
        file_sha256=sha256,
        filename=filename,
        is_sample=is_sample,
    )
    dur = int((time.monotonic() - t0) * 1000)
    v_status = StageStatus.FAILED if dup_result.is_duplicate else StageStatus.PASSED
    ev = _event("stage_completed", 4, "Duplicate Check", v_status,
                dup_result.model_dump(), dur)
    stage_events.append(ev)
    upsert_stage_result(run_id, stage_events)
    yield ev

    # -------------------------------------------------------------------------
    # Stage 6 — PO Match
    # -------------------------------------------------------------------------
    t0 = time.monotonic()
    yield _event("stage_started", 5, "PO Match", StageStatus.RUNNING)
    vendor_currency = None
    po_result = match_po(extraction, vendor_result.vendor_id, vendor_currency, run_id, filename=filename)
    dur = int((time.monotonic() - t0) * 1000)
    po_status = (StageStatus.PASSED if po_result.match_type != "none"
                 else StageStatus.WARNING)
    ev = _event("stage_completed", 5, "PO Match", po_status,
                po_result.model_dump(), dur)
    stage_events.append(ev)
    upsert_stage_result(run_id, stage_events)
    update_run(run_id, po_number=po_result.po_number)
    yield ev

    # -------------------------------------------------------------------------
    # Stage 7 — Rules
    # -------------------------------------------------------------------------
    t0 = time.monotonic()
    yield _event("stage_started", 6, "Rules Engine", StageStatus.RUNNING)
    rules_result = evaluate_rules(extraction, validation, vendor_result, dup_result, po_result)
    dur = int((time.monotonic() - t0) * 1000)
    r_status = StageStatus.PASSED if rules_result.all_passed else StageStatus.WARNING
    ev = _event("stage_completed", 6, "Rules Engine", r_status,
                {"results": [r.model_dump() for r in rules_result.results],
                 "failed_count": len(rules_result.failed_rules)},
                dur)
    stage_events.append(ev)
    upsert_stage_result(run_id, stage_events)
    yield ev

    # -------------------------------------------------------------------------
    # Stage 8 — Decision
    # -------------------------------------------------------------------------
    t0 = time.monotonic()
    yield _event("stage_started", 7, "Decision", StageStatus.RUNNING)
    try:
        decision_result = await decide(
            extraction, validation, vendor_result, dup_result, po_result, rules_result
        )
        dur = int((time.monotonic() - t0) * 1000)
        ev = _event("stage_completed", 7, "Decision", StageStatus.PASSED,
                    decision_result.model_dump(), dur)
        stage_events.append(ev)
        upsert_stage_result(run_id, stage_events)
        total_ms = sum(e.get("duration_ms") or 0 for e in stage_events)
        update_run(
            run_id,
            status="done",
            decision=decision_result.decision.value,
            decision_json=decision_result.model_dump_json(),
            completed_at=datetime.utcnow().isoformat(),
            processing_ms=total_ms,
        )
        log_audit(run_id, "RUN_COMPLETED", f"decision={decision_result.decision.value}")
        yield ev
        yield {
            "event": "run_completed",
            "run_id": run_id,
            "decision": decision_result.decision.value,
            "reason_codes": decision_result.reason_codes,
        }
    except Exception as exc:
        logger.exception("Stage 8 error")
        update_run(run_id, status="failed")
        yield {"event": "run_failed", "run_id": run_id, "error": str(exc)}
