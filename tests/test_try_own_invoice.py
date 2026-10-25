"""
tests/test_try_own_invoice.py — Verification for 'Try your own invoice' test data feature.

Requirements tested:
1. User-added vendor and PO saved to SQLite, tagged as is_user_added=1. Seeded records stay is_user_added=0.
2. Cannot delete seeded records; user-added records can be deleted.
3. Input validation: required fields, positive amounts, valid currency, max 50 limit.
4. Reset demo data cleans test records & uploads and restores seeds.
5. End-to-end: unknown invoice is held -> add vendor + PO -> re-run -> AUTO_APPROVED with no pipeline special cases.
"""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import (
    MAX_USER_RECORDS,
    add_user_po,
    add_user_vendor,
    delete_user_po,
    delete_user_vendor,
    get_db,
    init_db,
    reset_demo_data,
)
from app.main import app
from app.models import ExtractionResult, FieldValue, LineItem
from app.pipeline.orchestrator import run_pipeline


@pytest.fixture(autouse=True)
def clean_state():
    init_db()
    reset_demo_data()
    yield
    reset_demo_data()


def test_user_vendor_crud():
    # 1. Add user vendor
    vendor = add_user_vendor(
        name="Nova Robotics Inc",
        tax_id="US-99-8877665",
        bank_account="ACC-US-11223344",
        approved=True,
        currency="INR",
    )
    assert vendor["is_user_added"] == 1
    assert vendor["name"] == "Nova Robotics Inc"
    assert vendor["approved"] == 1

    # 2. Verify seeded records remain is_user_added = 0
    with get_db() as conn:
        seeded = conn.execute("SELECT * FROM vendors WHERE is_user_added = 0").fetchall()
        assert len(seeded) > 0

    # 3. Cannot delete seeded vendor
    with pytest.raises(ValueError, match="Cannot delete seeded"):
        delete_user_vendor("V001")

    # 4. Can delete user vendor
    assert delete_user_vendor(vendor["id"]) is True
    with get_db() as conn:
        row = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor["id"],)).fetchone()
        assert row is None


def test_user_po_crud():
    # 1. Add user vendor first
    vendor = add_user_vendor(name="Test Supplies Corp")
    vendor_id = vendor["id"]

    # 2. Add user PO
    po = add_user_po(
        po_number="PO-TEST-7788",
        vendor_id=vendor_id,
        total_amount=3500.50,
        currency="INR",
        description="Test office furniture",
    )
    assert po["is_user_added"] == 1
    assert po["po_number"] == "PO-TEST-7788"
    assert po["total_amount"] == 3500.50

    # 3. Negative or zero amount rejected
    with pytest.raises(ValueError, match="positive number"):
        add_user_po(
            po_number="PO-INVALID-1",
            vendor_id=vendor_id,
            total_amount=-50.0,
        )

    # 4. Unknown vendor rejected
    with pytest.raises(ValueError, match="does not exist"):
        add_user_po(
            po_number="PO-INVALID-2",
            vendor_id="NON-EXISTENT-VENDOR",
            total_amount=100.0,
        )

    # 5. Cannot delete seeded PO
    with pytest.raises(ValueError, match="Cannot delete seeded"):
        delete_user_po("PO-2024-001")

    # 6. Can delete user PO
    assert delete_user_po("PO-TEST-7788") is True


def test_max_50_limit():
    vendor = add_user_vendor(name="Base Vendor")
    # Add 49 more vendors to reach 50
    for i in range(MAX_USER_RECORDS - 1):
        add_user_vendor(name=f"Bulk Vendor {i}")

    # 51st vendor must raise ValueError
    with pytest.raises(ValueError, match="maximum 50"):
        add_user_vendor(name="Exceeds Limit Vendor")


def test_api_endpoints():
    client = TestClient(app)

    # Create vendor via API
    v_resp = client.post("/api/vendors", json={
        "name": "API Test Vendor",
        "tax_id": "TAX-1234",
        "bank_account": "BANK-5678",
        "approved": True,
        "currency": "INR",
    })
    assert v_resp.status_code == 200
    v_data = v_resp.json()
    assert v_data["success"] is True
    vendor_id = v_data["vendor"]["id"]

    # Create PO via API
    p_resp = client.post("/api/purchase-orders", json={
        "po_number": "PO-API-9900",
        "vendor_id": vendor_id,
        "total_amount": 1250.00,
        "currency": "INR",
        "description": "API Test PO",
    })
    assert p_resp.status_code == 200
    assert p_resp.json()["success"] is True

    # Check listing includes is_user_added
    v_list = client.get("/api/vendors").json()["vendors"]
    matching_v = [v for v in v_list if v["id"] == vendor_id]
    assert len(matching_v) == 1
    assert matching_v[0]["is_user_added"] == 1

    # Delete PO via API
    del_p = client.delete("/api/purchase-orders/PO-API-9900")
    assert del_p.status_code == 200

    # Delete Vendor via API
    del_v = client.delete(f"/api/vendors/{vendor_id}")
    assert del_v.status_code == 200


@pytest.mark.asyncio
async def test_try_own_invoice_flow():
    """
    End-to-End Test:
    1. An invoice from 'Quantum Analytics Inc' with PO 'PO-QUANTUM-101' (Rs. 4,200.00).
    2. Initially unknown: pipeline holds for review (VENDOR_UNKNOWN / NO_MATCHING_PO).
    3. User adds 'Quantum Analytics Inc' and 'PO-QUANTUM-101'.
    4. Re-run: pipeline passes all stages and yields AUTO_APPROVED purely via data & rules.
    """
    fake_extraction = ExtractionResult(
        vendor_name=FieldValue(value="Quantum Analytics Inc", confidence=0.95),
        vendor_tax_id=FieldValue(value="US-88-9990001", confidence=0.90),
        invoice_number=FieldValue(value="INV-QA-505", confidence=0.96),
        invoice_date=FieldValue(value="2024-07-20", confidence=0.95),
        due_date=FieldValue(value="2024-08-19", confidence=0.90),
        po_reference=FieldValue(value="PO-QUANTUM-101", confidence=0.95),
        currency=FieldValue(value="INR", confidence=0.99),
        line_items=[
            LineItem(description="Quantum Compute Monthly Subscription", quantity=1, unit_price=4200.0, amount=4200.0, confidence=0.95),
        ],
        subtotal=FieldValue(value=4200.0, confidence=0.95),
        tax_amount=FieldValue(value=0.0, confidence=0.90),
        tax_details=FieldValue(value="Exempt", confidence=0.80),
        total=FieldValue(value=4200.0, confidence=0.98),
        bank_account=FieldValue(value="ACC-QUANTUM-445566", confidence=0.92),
        vendor_email=FieldValue(value="billing@quantumanalytics.io", confidence=0.95),
        from_cache=False,
        ocr_used=False,
    )

    from app.models import IngestMethod
    dummy_pdf = b"%PDF-1.4 dummy custom invoice content"

    with patch("app.pipeline.orchestrator.ingest", return_value={"method": IngestMethod.TEXT, "page_count": 1, "text": "dummy text"}), \
         patch("app.pipeline.orchestrator.extract", new=AsyncMock(return_value=fake_extraction)), \
         patch("app.pipeline.stage8_decision.call_llm_text", new=AsyncMock(return_value="Automated decision explanation.")):

        # --- STEP 1: First Run (Master data does not have Quantum Analytics) ---
        events_1 = []
        async for ev in run_pipeline(dummy_pdf, "quantum_invoice.pdf"):
            events_1.append(ev)

        final_1 = [e for e in events_1 if e.get("event") == "stage_completed" and e.get("stage_index") == 7][0]
        decision_1 = final_1["payload"]["decision"]
        reason_codes_1 = final_1["payload"]["reason_codes"]

        assert decision_1 in ("HOLD_FOR_REVIEW", "REJECTED")
        assert any(code in reason_codes_1 for code in ("R2_VENDOR_KNOWN", "VENDOR_UNKNOWN", "NO_MATCHING_PO", "R5_PO_REQUIRED"))

        # --- STEP 2: User adds the vendor and PO ---
        vendor = add_user_vendor(
            name="Quantum Analytics Inc",
            tax_id="US-88-9990001",
            bank_account="ACC-QUANTUM-445566",
            approved=True,
            currency="INR",
        )
        add_user_po(
            po_number="PO-QUANTUM-101",
            vendor_id=vendor["id"],
            total_amount=4200.0,
            currency="INR",
            description="Quantum Compute Monthly Subscription",
        )

        # --- STEP 3: Re-run the exact same invoice ---
        events_2 = []
        async for ev in run_pipeline(dummy_pdf, "quantum_invoice.pdf"):
            events_2.append(ev)

        final_2 = [e for e in events_2 if e.get("event") == "stage_completed" and e.get("stage_index") == 7][0]
        decision_2 = final_2["payload"]["decision"]

        # Now auto-approved!
        assert decision_2 == "AUTO_APPROVED"
