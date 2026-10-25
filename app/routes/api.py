"""
app/routes/api.py — REST & SSE endpoints for invoice processing.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import (
    MAX_FILE_SIZE_BYTES,
    RATE_LIMIT,
    RULES_FILE,
    SAMPLES_DIR,
    UPLOAD_DIR,
)
from app.database import (
    add_user_po,
    add_user_vendor,
    delete_user_po,
    delete_user_vendor,
    get_db,
    get_run,
    list_runs,
    log_audit,
    reset_demo_data,
    update_run,
)
from app.pipeline.orchestrator import run_pipeline

logger = logging.getLogger(__name__)


def get_client_ip(request: Request) -> str:
    """Extract real client IP honoring X-Forwarded-For for proxy/HuggingFace deployment."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


limiter = Limiter(key_func=get_client_ip)
router = APIRouter(prefix="/api")

# In-memory storage for active run payloads (uploaded bytes waiting for stream connect)
_PENDING_RUNS: dict[str, dict] = {}


class RunSampleRequest(BaseModel):
    filename: str


class OverrideRequest(BaseModel):
    decision: str
    reason: str
    actor: Optional[str] = "finance_manager"


class CreateVendorRequest(BaseModel):
    name: str
    tax_id: Optional[str] = None
    bank_account: Optional[str] = None
    approved: Optional[bool] = True
    currency: Optional[str] = "INR"


class CreatePORequest(BaseModel):
    po_number: str
    vendor_id: str
    total_amount: float
    currency: Optional[str] = "INR"
    description: Optional[str] = None


class RulesUpdateRequest(BaseModel):
    amount_tolerance_pct: float
    amount_tolerance_fixed: float
    max_auto_approve_amount: float
    min_extraction_confidence: float
    min_inferred_po_confidence: float
    currency_must_match: bool
    require_po_for_amount_above: float
    duplicate_date_window_days: int
    minutes_per_invoice_manual: float


@router.post("/upload")
@limiter.limit(RATE_LIMIT)
async def upload_invoice(request: Request, file: UploadFile = File(...)):
    """Upload PDF endpoint with rate limiting based on client IP."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds maximum allowed size of {MAX_FILE_SIZE_BYTES // (1024*1024)} MB.",
        )

    if not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid PDF document.")

    import uuid
    run_id = str(uuid.uuid4())
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_pdf = UPLOAD_DIR / f"{run_id}.pdf"
    stored_pdf.write_bytes(content)

    _PENDING_RUNS[run_id] = {
        "pdf_bytes": content,
        "filename": file.filename,
        "is_sample": False,
        "pdf_path": str(stored_pdf),
        "created_at": datetime.utcnow().isoformat(),
    }

    return {"run_id": run_id, "filename": file.filename, "size_bytes": len(content)}


@router.post("/run-sample")
async def run_sample_invoice(payload: RunSampleRequest):
    """Trigger processing for a pre-generated sample invoice."""
    pdf_path = SAMPLES_DIR / payload.filename
    if not pdf_path.exists() or not pdf_path.is_file():
        raise HTTPException(status_code=404, detail=f"Sample file '{payload.filename}' not found.")

    content = pdf_path.read_bytes()
    import uuid
    run_id = str(uuid.uuid4())
    _PENDING_RUNS[run_id] = {
        "pdf_bytes": content,
        "filename": payload.filename,
        "is_sample": True,
        "pdf_path": str(pdf_path),
        "created_at": datetime.utcnow().isoformat(),
    }

    return {"run_id": run_id, "filename": payload.filename, "is_sample": True}


@router.get("/stream/{run_id}")
async def stream_pipeline(run_id: str):
    """
    Server-Sent Events (SSE) streaming endpoint for live 8-stage execution.
    If the run has already finished, yields stored stages immediately.
    """
    # Check if run is already stored in DB
    existing_run = get_run(run_id)
    if existing_run and existing_run.get("status") in ("done", "failed"):
        async def replay_stream():
            stages_raw = existing_run.get("stage_results")
            stages = json.loads(stages_raw) if stages_raw else []
            yield f"data: {json.dumps({'event': 'run_started', 'run_id': run_id, 'filename': existing_run['filename']})}\n\n"
            for s in stages:
                yield f"data: {json.dumps(s)}\n\n"
            yield f"data: {json.dumps({'event': 'run_completed', 'run_id': run_id, 'decision': existing_run.get('decision')})}\n\n"

        return StreamingResponse(replay_stream(), media_type="text/event-stream")

    # Pending run to execute
    pending = _PENDING_RUNS.pop(run_id, None)
    if not pending:
        # Fallback check if run exists in DB with pending status
        if not existing_run:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found or already consumed.")
        
        pdf_disk_path = existing_run.get("pdf_path")
        if pdf_disk_path and Path(pdf_disk_path).exists():
            pending = {
                "pdf_bytes": Path(pdf_disk_path).read_bytes(),
                "filename": existing_run["filename"],
                "is_sample": bool(existing_run.get("is_sample")),
                "pdf_path": pdf_disk_path,
            }
        else:
            sample_pdf_path = SAMPLES_DIR / existing_run["filename"]
            if sample_pdf_path.exists():
                pending = {
                    "pdf_bytes": sample_pdf_path.read_bytes(),
                    "filename": existing_run["filename"],
                    "is_sample": bool(existing_run.get("is_sample")),
                    "pdf_path": str(sample_pdf_path),
                }
            else:
                raise HTTPException(status_code=404, detail="Run bytes not available to stream.")

    async def event_generator():
        try:
            async for event in run_pipeline(
                pdf_bytes=pending["pdf_bytes"],
                filename=pending["filename"],
                is_sample=pending.get("is_sample", False),
                run_id=run_id,
                pdf_path=pending.get("pdf_path"),
            ):
                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0.05)  # Smooth UI transition
        except Exception as exc:
            logger.exception("Error in SSE event_generator for run %s", run_id)
            err_ev = {"event": "run_failed", "run_id": run_id, "error": str(exc)}
            yield f"data: {json.dumps(err_ev)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs")
def get_runs_list(status: Optional[str] = None, decision: Optional[str] = None, limit: int = 50, offset: int = 0):
    """List historical runs with optional status/decision filter."""
    with get_db() as conn:
        query = "SELECT * FROM runs WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if decision:
            query += " AND decision = ?"
            params.append(decision)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        runs = [dict(r) for r in rows]

        # Count total
        count_query = "SELECT COUNT(*) as total FROM runs WHERE 1=1"
        count_params = []
        if status:
            count_query += " AND status = ?"
            count_params.append(status)
        if decision:
            count_query += " AND decision = ?"
            count_params.append(decision)
        total = conn.execute(count_query, count_params).fetchone()["total"]

    return {"runs": runs, "total": total, "limit": limit, "offset": offset}


@router.get("/runs/{run_id}")
def get_run_details(run_id: str):
    """Get single run full details, stages, extraction, and audit log."""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found.")

    with get_db() as conn:
        audit_rows = conn.execute(
            "SELECT * FROM audit_log WHERE run_id = ? ORDER BY created_at ASC",
            (run_id,),
        ).fetchall()
        audit_trail = [dict(r) for r in audit_rows]

    # Parse JSON fields
    stage_results = json.loads(run["stage_results"]) if run.get("stage_results") else []
    extraction = json.loads(run["extraction_json"]) if run.get("extraction_json") else None
    decision_details = json.loads(run["decision_json"]) if run.get("decision_json") else None

    return {
        "run": run,
        "stages": stage_results,
        "extraction": extraction,
        "decision_details": decision_details,
        "audit_trail": audit_trail,
    }


@router.post("/runs/{run_id}/override")
def override_decision(run_id: str, payload: OverrideRequest):
    """Human-in-the-loop manual decision override."""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found.")

    old_decision = run.get("decision")
    update_run(
        run_id,
        decision=payload.decision,
    )
    detail_msg = f"Decision overridden from '{old_decision}' to '{payload.decision}'. Justification: {payload.reason}"
    log_audit(run_id, "MANUAL_OVERRIDE", detail_msg, actor=payload.actor or "finance_manager")

    return {
        "success": True,
        "run_id": run_id,
        "old_decision": old_decision,
        "new_decision": payload.decision,
    }


@router.get("/samples")
def list_sample_invoices():
    """List available sample invoices and their expected behaviors."""
    expected_path = Path(__file__).parent.parent.parent / "tests" / "expected_decisions.json"
    expected = {}
    if expected_path.exists():
        expected = json.loads(expected_path.read_text(encoding="utf-8"))

    samples = []
    if SAMPLES_DIR.exists():
        for p in sorted(SAMPLES_DIR.glob("*.pdf")):
            exp_info = expected.get(p.name, {})
            samples.append({
                "filename": p.name,
                "size_bytes": p.stat().st_size,
                "expected_decision": exp_info.get("decision") or exp_info.get("decision_in"),
                "expected_reason": exp_info.get("reason", "Sample invoice for testing"),
            })
    return {"samples": samples}


@router.get("/samples/{filename}/view")
def view_sample_pdf(filename: str):
    """Safely stream a sample invoice PDF for viewing inline."""
    clean_name = Path(filename).name
    if not clean_name.lower().endswith(".pdf") or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    pdf_path = (SAMPLES_DIR / clean_name).resolve()
    if not pdf_path.is_file() or not str(pdf_path).startswith(str(SAMPLES_DIR.resolve())):
        raise HTTPException(status_code=404, detail="Sample not found.")

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{clean_name}"'},
    )


@router.get("/samples/{filename}/download")
def download_sample_pdf(filename: str):
    """Safely serve a sample invoice PDF as a downloadable attachment."""
    clean_name = Path(filename).name
    if not clean_name.lower().endswith(".pdf") or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    pdf_path = (SAMPLES_DIR / clean_name).resolve()
    if not pdf_path.is_file() or not str(pdf_path).startswith(str(SAMPLES_DIR.resolve())):
        raise HTTPException(status_code=404, detail="Sample not found.")

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=clean_name,
        headers={"Content-Disposition": f'attachment; filename="{clean_name}"'},
    )


@router.get("/metrics")
def get_metrics():
    """Aggregated dashboard metrics."""
    with get_db() as conn:
        total_runs = conn.execute("SELECT COUNT(*) as c FROM runs").fetchone()["c"]
        completed = conn.execute("SELECT COUNT(*) as c FROM runs WHERE status = 'done'").fetchone()["c"]
        auto_approved = conn.execute("SELECT COUNT(*) as c FROM runs WHERE decision = 'AUTO_APPROVED'").fetchone()["c"]
        held = conn.execute("SELECT COUNT(*) as c FROM runs WHERE decision = 'HOLD_FOR_REVIEW'").fetchone()["c"]
        rejected = conn.execute("SELECT COUNT(*) as c FROM runs WHERE decision = 'REJECTED'").fetchone()["c"]
        need_info = conn.execute("SELECT COUNT(*) as c FROM runs WHERE decision = 'REQUEST_INFO'").fetchone()["c"]
        with_note = conn.execute("SELECT COUNT(*) as c FROM runs WHERE decision = 'APPROVED_WITH_NOTE'").fetchone()["c"]

        avg_time_row = conn.execute("SELECT AVG(processing_ms) as a FROM runs WHERE processing_ms IS NOT NULL").fetchone()
        avg_processing_ms = round(avg_time_row["a"] or 0)

        total_amount_row = conn.execute("SELECT SUM(invoice_total) as s FROM runs WHERE invoice_total IS NOT NULL").fetchone()
        total_amount = round(total_amount_row["s"] or 0, 2)

        # Read rules for minutes per invoice
        rules = {}
        if RULES_FILE.exists():
            rules = json.loads(RULES_FILE.read_text(encoding="utf-8"))
        mins_per_inv = rules.get("minutes_per_invoice_manual", 30.0)
        hours_saved = round((auto_approved + with_note) * (mins_per_inv / 60.0), 1)

        # Count caught fraud in audit log
        fraud_caught = conn.execute(
            "SELECT COUNT(DISTINCT run_id) as c FROM audit_log WHERE detail LIKE '%BANK_DETAILS_MISMATCH%' OR detail LIKE '%FRAUD%'"
        ).fetchone()["c"]

    auto_approval_rate = round((auto_approved / completed * 100) if completed else 0, 1)

    return {
        "total_processed": total_runs,
        "completed_count": completed,
        "auto_approved_count": auto_approved,
        "auto_approval_rate_pct": auto_approval_rate,
        "held_count": held,
        "rejected_count": rejected,
        "request_info_count": need_info,
        "approved_with_note_count": with_note,
        "avg_processing_ms": avg_processing_ms,
        "total_amount_processed": total_amount,
        "hours_saved": hours_saved,
        "fraud_caught_count": fraud_caught,
        "decisions_breakdown": {
            "AUTO_APPROVED": auto_approved,
            "APPROVED_WITH_NOTE": with_note,
            "HOLD_FOR_REVIEW": held,
            "REJECTED": rejected,
            "REQUEST_INFO": need_info,
        },
    }


@router.get("/rules")
def get_rules():
    """Retrieve active rules and threshold parameters."""
    if not RULES_FILE.exists():
        raise HTTPException(status_code=404, detail="rules.json not found.")
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


@router.post("/rules")
def update_rules(payload: RulesUpdateRequest):
    """Update active business rules and thresholds."""
    data = payload.model_dump()
    RULES_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"success": True, "rules": data}


@router.get("/vendors")
def get_vendors_list():
    """List registered vendor master records."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM vendors ORDER BY name ASC").fetchall()
        return {"vendors": [dict(r) for r in rows]}


@router.get("/purchase-orders")
def get_purchase_orders_list():
    """List purchase orders master records."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM purchase_orders ORDER BY po_number ASC").fetchall()
        return {"purchase_orders": [dict(r) for r in rows]}


@router.post("/runs/{run_id}/rerun")
async def rerun_invoice(run_id: str):
    """
    Re-run the pipeline on the exact same PDF as run_id.
    Links the new run to the stored PDF.
    """
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

    content: bytes | None = None
    pdf_path_str = run.get("pdf_path")
    if pdf_path_str and Path(pdf_path_str).exists():
        content = Path(pdf_path_str).read_bytes()
    elif (UPLOAD_DIR / f"{run_id}.pdf").exists():
        content = (UPLOAD_DIR / f"{run_id}.pdf").read_bytes()
    else:
        sample_path = SAMPLES_DIR / run["filename"]
        if sample_path.exists():
            content = sample_path.read_bytes()

    if not content:
        raise HTTPException(status_code=404, detail=f"PDF document for run '{run_id}' not found on disk.")

    import uuid
    new_run_id = str(uuid.uuid4())
    new_pdf_path: str | None = None
    if not run.get("is_sample"):
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = UPLOAD_DIR / f"{new_run_id}.pdf"
        target.write_bytes(content)
        new_pdf_path = str(target)
    else:
        new_pdf_path = str(SAMPLES_DIR / run["filename"])

    _PENDING_RUNS[new_run_id] = {
        "pdf_bytes": content,
        "filename": run["filename"],
        "is_sample": bool(run.get("is_sample")),
        "pdf_path": new_pdf_path,
        "created_at": datetime.utcnow().isoformat(),
    }

    return {
        "run_id": new_run_id,
        "filename": run["filename"],
        "is_sample": bool(run.get("is_sample")),
    }


@router.post("/vendors")
def create_vendor(payload: CreateVendorRequest):
    """Add a test (user-added) vendor to master data."""
    try:
        vendor = add_user_vendor(
            name=payload.name,
            tax_id=payload.tax_id,
            bank_account=payload.bank_account,
            approved=payload.approved if payload.approved is not None else True,
            currency=payload.currency or "INR",
        )
        return {"success": True, "vendor": vendor}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/vendors/{vendor_id}")
def remove_vendor(vendor_id: str):
    """Delete a user-added test vendor."""
    try:
        delete_user_vendor(vendor_id)
        return {"success": True, "deleted_id": vendor_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/purchase-orders")
def create_purchase_order(payload: CreatePORequest):
    """Add a test (user-added) purchase order to master data."""
    try:
        po = add_user_po(
            po_number=payload.po_number,
            vendor_id=payload.vendor_id,
            total_amount=payload.total_amount,
            currency=payload.currency or "INR",
            description=payload.description,
        )
        return {"success": True, "purchase_order": po}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/purchase-orders/{po_number}")
def remove_purchase_order(po_number: str):
    """Delete a user-added test purchase order."""
    try:
        delete_user_po(po_number)
        return {"success": True, "deleted_po": po_number}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/reset-demo")
def reset_demo():
    """Reset demo data: removes user test data & uploads, re-seeds master tables."""
    reset_demo_data()
    return {"success": True, "message": "Demo data reset successfully."}
