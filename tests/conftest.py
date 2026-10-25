"""
tests/conftest.py — Pytest fixtures and mocking setup.

Key design: tests NEVER call Groq.
  - The extraction stage is mocked to return fixture data loaded from
    a pre-built JSON file (tests/fixtures/extractions.json).
  - If that file doesn't exist, we use minimal inline stubs.
  - All other stages are deterministic and run for real.

The pipeline runs against the real SQLite (in-memory, created per-session).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Ensure project root is on path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Minimal extraction stubs used when the fixture file doesn't exist.
# Indexed by sample filename.
# ---------------------------------------------------------------------------
EXTRACTION_STUBS: dict[str, dict] = {
    "happy_path.pdf": {
        "vendor_name": {"value": "Acme Supplies Inc", "confidence": 0.95, "source_snippet": "Acme Supplies Inc"},
        "vendor_tax_id": {"value": "US-47-1234567", "confidence": 0.90, "source_snippet": "US-47-1234567"},
        "invoice_number": {"value": "INV-1001", "confidence": 0.97, "source_snippet": "INV-1001"},
        "invoice_date": {"value": "2024-07-10", "confidence": 0.95, "source_snippet": "10 Jul 2024"},
        "due_date": {"value": "2024-08-09", "confidence": 0.90, "source_snippet": "09 Aug 2024"},
        "po_reference": {"value": "PO-2024-001", "confidence": 0.95, "source_snippet": "PO-2024-001"},
        "currency": {"value": "USD", "confidence": 0.99, "source_snippet": "USD"},
        "line_items": [
            {"description": "Premium Office Paper", "quantity": 50, "unit_price": 120.0, "amount": 6000.0, "confidence": 0.95},
            {"description": "Ballpoint Pens", "quantity": 10, "unit_price": 45.0, "amount": 450.0, "confidence": 0.95},
            {"description": "Sticky Notes Assorted", "quantity": 48, "unit_price": 15.0, "amount": 720.0, "confidence": 0.93},
            {"description": "File Folders Manila", "quantity": 8, "unit_price": 35.0, "amount": 280.0, "confidence": 0.93},
        ],
        "subtotal": {"value": 7450.0, "confidence": 0.95, "source_snippet": "$7,450.00"},
        "tax_amount": {"value": 0.0, "confidence": 0.90, "source_snippet": "$0.00"},
        "tax_details": {"value": "No tax", "confidence": 0.80, "source_snippet": ""},
        "total": {"value": 7450.0, "confidence": 0.97, "source_snippet": "$7,450.00"},
        "bank_account": {"value": "ACC-US-9876543210", "confidence": 0.90, "source_snippet": "ACC-US-9876543210"},
        "vendor_email": {"value": "accounts@acmesupplies.com", "confidence": 0.95, "source_snippet": "accounts@acmesupplies.com"},
        "from_cache": True, "ocr_used": False,
    },
    "within_tolerance.pdf": {
        "vendor_name": {"value": "Acme Supplies Inc", "confidence": 0.95, "source_snippet": "Acme Supplies Inc"},
        "vendor_tax_id": {"value": "US-47-1234567", "confidence": 0.90, "source_snippet": "US-47-1234567"},
        "invoice_number": {"value": "INV-1002", "confidence": 0.97, "source_snippet": "INV-1002"},
        "invoice_date": {"value": "2024-07-12", "confidence": 0.95, "source_snippet": "12 Jul 2024"},
        "due_date": {"value": "2024-08-11", "confidence": 0.90, "source_snippet": ""},
        "po_reference": {"value": "PO-2024-007", "confidence": 0.95, "source_snippet": "PO-2024-007"},
        "currency": {"value": "USD", "confidence": 0.99, "source_snippet": "USD"},
        "line_items": [
            {"description": "Laser Printer Cartridges", "quantity": 4, "unit_price": 220.0, "amount": 880.0, "confidence": 0.93},
            {"description": "Whiteboard Markers", "quantity": 6, "unit_price": 28.0, "amount": 168.0, "confidence": 0.93},
            {"description": "Stapler Heavy Duty", "quantity": 3, "unit_price": 42.0, "amount": 126.0, "confidence": 0.93},
        ],
        "subtotal": {"value": 1174.0, "confidence": 0.95, "source_snippet": "$1,174.00"},
        "tax_amount": {"value": 0.0, "confidence": 0.90, "source_snippet": ""},
        "tax_details": {"value": None, "confidence": 0.0, "source_snippet": ""},
        "total": {"value": 1174.0, "confidence": 0.97, "source_snippet": "$1,174.00"},
        "bank_account": {"value": "ACC-US-9876543210", "confidence": 0.90, "source_snippet": ""},
        "vendor_email": {"value": "accounts@acmesupplies.com", "confidence": 0.90, "source_snippet": ""},
        "from_cache": True, "ocr_used": False,
    },
    "over_tolerance.pdf": {
        "vendor_name": {"value": "CloudBase Systems", "confidence": 0.95, "source_snippet": "CloudBase Systems"},
        "vendor_tax_id": {"value": "US-47-3332211", "confidence": 0.90, "source_snippet": ""},
        "invoice_number": {"value": "INV-CB-2024-087", "confidence": 0.97, "source_snippet": "INV-CB-2024-087"},
        "invoice_date": {"value": "2024-07-13", "confidence": 0.95, "source_snippet": ""},
        "due_date": {"value": "2024-08-12", "confidence": 0.90, "source_snippet": ""},
        "po_reference": {"value": "PO-2024-006", "confidence": 0.95, "source_snippet": "PO-2024-006"},
        "currency": {"value": "USD", "confidence": 0.99, "source_snippet": "USD"},
        "line_items": [
            {"description": "Cloud Compute", "quantity": 720, "unit_price": 25.0, "amount": 18000.0, "confidence": 0.90},
            {"description": "Managed Kubernetes", "quantity": 1, "unit_price": 5700.0, "amount": 5700.0, "confidence": 0.90},
            {"description": "Data Transfer Out", "quantity": 13, "unit_price": 250.0, "amount": 3250.0, "confidence": 0.90},
        ],
        "subtotal": {"value": 26950.0, "confidence": 0.95, "source_snippet": "$26,950.00"},
        "tax_amount": {"value": 0.0, "confidence": 0.90, "source_snippet": ""},
        "tax_details": {"value": None, "confidence": 0.0, "source_snippet": ""},
        "total": {"value": 26950.0, "confidence": 0.97, "source_snippet": "$26,950.00"},
        "bank_account": {"value": "ACC-US-1112223333", "confidence": 0.90, "source_snippet": ""},
        "vendor_email": {"value": "ar@cloudbase.io", "confidence": 0.90, "source_snippet": ""},
        "from_cache": True, "ocr_used": False,
    },
    "near_duplicate.pdf": {
        "vendor_name": {"value": "Acme Supplies Inc", "confidence": 0.95, "source_snippet": "Acme Supplies Inc"},
        "vendor_tax_id": {"value": "US-47-1234567", "confidence": 0.90, "source_snippet": ""},
        "invoice_number": {"value": "INV1001-A", "confidence": 0.97, "source_snippet": "INV1001-A"},
        "invoice_date": {"value": "2024-07-10", "confidence": 0.95, "source_snippet": ""},
        "due_date": {"value": "2024-08-09", "confidence": 0.90, "source_snippet": ""},
        "po_reference": {"value": "PO-2024-001", "confidence": 0.95, "source_snippet": ""},
        "currency": {"value": "USD", "confidence": 0.99, "source_snippet": "USD"},
        "line_items": [
            {"description": "Premium Office Paper", "quantity": 50, "unit_price": 120.0, "amount": 6000.0, "confidence": 0.93},
            {"description": "Ballpoint Pens", "quantity": 10, "unit_price": 45.0, "amount": 450.0, "confidence": 0.93},
            {"description": "Sticky Notes Assorted", "quantity": 48, "unit_price": 15.0, "amount": 720.0, "confidence": 0.93},
            {"description": "File Folders Manila", "quantity": 8, "unit_price": 35.0, "amount": 280.0, "confidence": 0.93},
        ],
        "subtotal": {"value": 7450.0, "confidence": 0.95, "source_snippet": ""},
        "tax_amount": {"value": 0.0, "confidence": 0.90, "source_snippet": ""},
        "tax_details": {"value": None, "confidence": 0.0, "source_snippet": ""},
        "total": {"value": 7450.0, "confidence": 0.97, "source_snippet": ""},
        "bank_account": {"value": "ACC-US-9876543210", "confidence": 0.90, "source_snippet": ""},
        "vendor_email": {"value": "accounts@acmesupplies.com", "confidence": 0.90, "source_snippet": ""},
        "from_cache": True, "ocr_used": False,
    },
    "missing_fields.pdf": {
        "vendor_name": {"value": "Global Tech Solutions", "confidence": 0.90, "source_snippet": "Global Tech Solutions"},
        "vendor_tax_id": {"value": "US-47-9876543", "confidence": 0.85, "source_snippet": ""},
        "invoice_number": {"value": None, "confidence": 0.0, "source_snippet": "[UNCLEAR]"},
        "invoice_date": {"value": "2024-07-14", "confidence": 0.85, "source_snippet": ""},
        "due_date": {"value": "2024-08-13", "confidence": 0.80, "source_snippet": ""},
        "po_reference": {"value": None, "confidence": 0.0, "source_snippet": ""},
        "currency": {"value": "USD", "confidence": 0.90, "source_snippet": ""},
        "line_items": [
            {"description": "Software Development Services", "quantity": 1, "unit_price": 0.0, "amount": 0.0, "confidence": 0.50},
        ],
        "subtotal": {"value": 0.0, "confidence": 0.40, "source_snippet": ""},
        "tax_amount": {"value": 0.0, "confidence": 0.40, "source_snippet": ""},
        "tax_details": {"value": None, "confidence": 0.0, "source_snippet": ""},
        "total": {"value": None, "confidence": 0.0, "source_snippet": "PENDING APPROVAL"},
        "bank_account": {"value": "ACC-US-1234567890", "confidence": 0.85, "source_snippet": ""},
        "vendor_email": {"value": "billing@globaltech.com", "confidence": 0.90, "source_snippet": ""},
        "from_cache": True, "ocr_used": False,
    },
    "fraud_bank_change.pdf": {
        "vendor_name": {"value": "Pinnacle Consulting Group", "confidence": 0.95, "source_snippet": "Pinnacle Consulting Group"},
        "vendor_tax_id": {"value": "US-47-5551234", "confidence": 0.90, "source_snippet": ""},
        "invoice_number": {"value": "INV-PC-2024-112", "confidence": 0.97, "source_snippet": "INV-PC-2024-112"},
        "invoice_date": {"value": "2024-07-11", "confidence": 0.95, "source_snippet": ""},
        "due_date": {"value": "2024-08-10", "confidence": 0.90, "source_snippet": ""},
        "po_reference": {"value": "PO-2024-005", "confidence": 0.95, "source_snippet": "PO-2024-005"},
        "currency": {"value": "USD", "confidence": 0.99, "source_snippet": ""},
        "line_items": [
            {"description": "Strategy Consulting Phase 1", "quantity": 1, "unit_price": 45000.0, "amount": 45000.0, "confidence": 0.92},
            {"description": "Market Research Report", "quantity": 1, "unit_price": 8000.0, "amount": 8000.0, "confidence": 0.92},
            {"description": "Executive Presentation", "quantity": 1, "unit_price": 5000.0, "amount": 5000.0, "confidence": 0.92},
        ],
        "subtotal": {"value": 58000.0, "confidence": 0.95, "source_snippet": ""},
        "tax_amount": {"value": 0.0, "confidence": 0.90, "source_snippet": ""},
        "tax_details": {"value": None, "confidence": 0.0, "source_snippet": ""},
        "total": {"value": 58000.0, "confidence": 0.97, "source_snippet": "$58,000.00"},
        "bank_account": {"value": "ACC-US-FRAUDSTER-9999999", "confidence": 0.90, "source_snippet": "ACC-US-FRAUDSTER-9999999"},
        "vendor_email": {"value": "finance@pinnacle.com", "confidence": 0.90, "source_snippet": ""},
        "from_cache": True, "ocr_used": False,
    },
    "split_bill_1.pdf": {
        "vendor_name": {"value": "Sunrise Software Pvt Ltd", "confidence": 0.95, "source_snippet": "Sunrise Software Pvt Ltd"},
        "vendor_tax_id": {"value": "IN-29AABCS1234K1ZX", "confidence": 0.90, "source_snippet": ""},
        "invoice_number": {"value": "INV-SS-2024-301", "confidence": 0.97, "source_snippet": "INV-SS-2024-301"},
        "invoice_date": {"value": "2024-05-15", "confidence": 0.95, "source_snippet": ""},
        "due_date": {"value": "2024-06-14", "confidence": 0.90, "source_snippet": ""},
        "po_reference": {"value": "PO-2024-004", "confidence": 0.95, "source_snippet": "PO-2024-004"},
        "currency": {"value": "INR", "confidence": 0.99, "source_snippet": "INR"},
        "line_items": [
            {"description": "IT Support Services April 2024", "quantity": 1, "unit_price": 80000.0, "amount": 80000.0, "confidence": 0.92},
            {"description": "Software License Fees Q1", "quantity": 1, "unit_price": 50000.0, "amount": 50000.0, "confidence": 0.92},
            {"description": "Hardware Maintenance", "quantity": 1, "unit_price": 20000.0, "amount": 20000.0, "confidence": 0.92},
        ],
        "subtotal": {"value": 150000.0, "confidence": 0.95, "source_snippet": "150,000.00"},
        "tax_amount": {"value": 27000.0, "confidence": 0.92, "source_snippet": "GST 18%"},
        "tax_details": {"value": "GST (18%)", "confidence": 0.90, "source_snippet": "GST (18%)"},
        "total": {"value": 177000.0, "confidence": 0.97, "source_snippet": "177,000.00"},
        "bank_account": {"value": "ACC-IN-HDFC9876543210", "confidence": 0.90, "source_snippet": ""},
        "vendor_email": {"value": "accounts@sunrisesoftware.in", "confidence": 0.90, "source_snippet": ""},
        "from_cache": True, "ocr_used": False,
    },
    "split_bill_2.pdf": {
        "vendor_name": {"value": "Sunrise Software Pvt Ltd", "confidence": 0.95, "source_snippet": "Sunrise Software Pvt Ltd"},
        "vendor_tax_id": {"value": "IN-29AABCS1234K1ZX", "confidence": 0.90, "source_snippet": ""},
        "invoice_number": {"value": "INV-SS-2024-302", "confidence": 0.97, "source_snippet": "INV-SS-2024-302"},
        "invoice_date": {"value": "2024-06-15", "confidence": 0.95, "source_snippet": ""},
        "due_date": {"value": "2024-07-15", "confidence": 0.90, "source_snippet": ""},
        "po_reference": {"value": "PO-2024-004", "confidence": 0.95, "source_snippet": "PO-2024-004"},
        "currency": {"value": "INR", "confidence": 0.99, "source_snippet": "INR"},
        "line_items": [
            {"description": "IT Support May 2024", "quantity": 1, "unit_price": 80000.0, "amount": 80000.0, "confidence": 0.92},
            {"description": "IT Support June 2024", "quantity": 1, "unit_price": 80000.0, "amount": 80000.0, "confidence": 0.92},
            {"description": "Network Security Audit", "quantity": 1, "unit_price": 40000.0, "amount": 40000.0, "confidence": 0.92},
        ],
        "subtotal": {"value": 200000.0, "confidence": 0.95, "source_snippet": "200,000.00"},
        "tax_amount": {"value": 36000.0, "confidence": 0.92, "source_snippet": "GST 18%"},
        "tax_details": {"value": "GST (18%)", "confidence": 0.90, "source_snippet": ""},
        "total": {"value": 236000.0, "confidence": 0.97, "source_snippet": "236,000.00"},
        "bank_account": {"value": "ACC-IN-HDFC9876543210", "confidence": 0.90, "source_snippet": ""},
        "vendor_email": {"value": "accounts@sunrisesoftware.in", "confidence": 0.90, "source_snippet": ""},
        "from_cache": True, "ocr_used": False,
    },
    "split_bill_3.pdf": {
        "vendor_name": {"value": "Sunrise Software Pvt Ltd", "confidence": 0.95, "source_snippet": "Sunrise Software Pvt Ltd"},
        "vendor_tax_id": {"value": "IN-29AABCS1234K1ZX", "confidence": 0.90, "source_snippet": ""},
        "invoice_number": {"value": "INV-SS-2024-303", "confidence": 0.97, "source_snippet": "INV-SS-2024-303"},
        "invoice_date": {"value": "2024-07-13", "confidence": 0.95, "source_snippet": ""},
        "due_date": {"value": "2024-08-12", "confidence": 0.90, "source_snippet": ""},
        "po_reference": {"value": "PO-2024-004", "confidence": 0.95, "source_snippet": "PO-2024-004"},
        "currency": {"value": "INR", "confidence": 0.99, "source_snippet": "INR"},
        "line_items": [
            {"description": "IT Support July 2024", "quantity": 1, "unit_price": 80000.0, "amount": 80000.0, "confidence": 0.92},
            {"description": "Server Infrastructure Upgrade", "quantity": 1, "unit_price": 90000.0, "amount": 90000.0, "confidence": 0.92},
            {"description": "Data Backup Solution", "quantity": 1, "unit_price": 30000.0, "amount": 30000.0, "confidence": 0.92},
        ],
        "subtotal": {"value": 200000.0, "confidence": 0.95, "source_snippet": "200,000.00"},
        "tax_amount": {"value": 36000.0, "confidence": 0.92, "source_snippet": ""},
        "tax_details": {"value": "GST (18%)", "confidence": 0.90, "source_snippet": ""},
        "total": {"value": 236000.0, "confidence": 0.97, "source_snippet": "236,000.00"},
        "bank_account": {"value": "ACC-IN-HDFC9876543210", "confidence": 0.90, "source_snippet": ""},
        "vendor_email": {"value": "accounts@sunrisesoftware.in", "confidence": 0.90, "source_snippet": ""},
        "from_cache": True, "ocr_used": False,
    },
    "scanned_no_po.pdf": {
        "vendor_name": {"value": "Rapid Logistics Ltd", "confidence": 0.75, "source_snippet": "Rapid Logistics Ltd"},
        "vendor_tax_id": {"value": "UK-GB-12345678", "confidence": 0.70, "source_snippet": ""},
        "invoice_number": {"value": "INV-RL-2024-445", "confidence": 0.75, "source_snippet": "INV-RL-2024-445"},
        "invoice_date": {"value": "2024-07-12", "confidence": 0.72, "source_snippet": "12 July 2024"},
        "due_date": {"value": "2024-08-11", "confidence": 0.70, "source_snippet": ""},
        "po_reference": {"value": None, "confidence": 0.0, "source_snippet": ""},
        "currency": {"value": "GBP", "confidence": 0.80, "source_snippet": "GBP"},
        "line_items": [
            {"description": "Express Freight San Francisco", "quantity": 3, "unit_price": 850.0, "amount": 2550.0, "confidence": 0.70},
            {"description": "Customs Clearance Fee", "quantity": 1, "unit_price": 320.0, "amount": 320.0, "confidence": 0.70},
            {"description": "Insurance", "quantity": 1, "unit_price": 180.0, "amount": 180.0, "confidence": 0.68},
            {"description": "Fuel Surcharge", "quantity": 1, "unit_price": 150.0, "amount": 150.0, "confidence": 0.68},
        ],
        "subtotal": {"value": 3200.0, "confidence": 0.75, "source_snippet": "£3,200.00"},
        "tax_amount": {"value": 0.0, "confidence": 0.70, "source_snippet": ""},
        "tax_details": {"value": None, "confidence": 0.0, "source_snippet": ""},
        "total": {"value": 3200.0, "confidence": 0.75, "source_snippet": "£3,200.00"},
        "bank_account": {"value": "ACC-UK-5551234567", "confidence": 0.70, "source_snippet": ""},
        "vendor_email": {"value": "invoices@rapidlogistics.co.uk", "confidence": 0.72, "source_snippet": ""},
        "from_cache": True, "ocr_used": True,
    },
}


@pytest.fixture(scope="session")
def extraction_stubs() -> dict:
    """Load extraction stubs from fixture file, falling back to inline stubs."""
    fixture_file = ROOT / "tests" / "fixtures" / "extractions.json"
    if fixture_file.exists():
        return json.loads(fixture_file.read_text(encoding="utf-8"))
    return EXTRACTION_STUBS


@pytest.fixture(scope="function")
def mock_extract(extraction_stubs):
    """
    Patch stage2_extract.extract so it returns stub data without calling Groq.
    Also patches the LLM decision explanation calls to return canned text.
    """
    from app.models import ExtractionResult

    async def fake_extract(text: str, sha256: str, ocr_used: bool = False) -> ExtractionResult:
        # Use the sha256 to look up filename — stored in stub by filename
        # We pass the filename as a hint in the sha256 for tests
        for filename, stub in extraction_stubs.items():
            if filename in sha256:
                return ExtractionResult.model_validate(stub)
        # Fallback: return happy_path stub
        return ExtractionResult.model_validate(extraction_stubs.get(
            "happy_path.pdf", list(extraction_stubs.values())[0]
        ))

    with patch("app.pipeline.stage2_extract.extract", new=fake_extract), \
         patch("app.pipeline.stage8_decision.call_llm_text",
               new=AsyncMock(return_value="This invoice has been processed by the automated system.")):
        yield
