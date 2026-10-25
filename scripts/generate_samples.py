"""
scripts/generate_samples.py — Generate all sample invoice PDFs for demo and testing.

Run with:
    .venv\\Scripts\\python.exe scripts\\generate_samples.py

Generates 10 invoices in 3 layouts:
  Layout A (clean):   header-left logo, table, totals box
  Layout B (compact): narrow margins, coloured header band
  Layout C (scanned): rendered to PNG with noise/rotation, saved image-only PDF

Also creates tests/expected_decisions.json listing expected outcomes.

Edge cases included:
  1. happy_path.pdf          — clean, known vendor, explicit PO → AUTO_APPROVED
  2. within_tolerance.pdf    — 1.5% over PO balance → AUTO_APPROVED
  3. over_tolerance.pdf      — 8% over PO balance → HOLD_FOR_REVIEW
  4. scanned_no_po.pdf       — image-only, no PO number → needs OCR
  5. split_bill_1.pdf        — PO-2024-004 part 1 of 3 (150000 INR) → APPROVED_WITH_NOTE
  6. split_bill_2.pdf        — PO-2024-004 part 2 of 3 (200000 INR) → APPROVED_WITH_NOTE
  7. split_bill_3.pdf        — PO-2024-004 part 3 (200000 INR, exceeds remaining 150000) → HOLD_FOR_REVIEW
  8. near_duplicate.pdf      — same as happy_path but number INV1001-A → REJECTED
  9. missing_fields.pdf      — no invoice number, unclear total → REQUEST_INFO
  10. fraud_bank_change.pdf  — known vendor but different bank → HOLD_FOR_REVIEW (fraud flag)
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Ensure UTF-8 output on Windows (needed for ✓ characters)
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Ensure project root is on sys.path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

SAMPLES_DIR = ROOT / "samples"
SAMPLES_DIR.mkdir(exist_ok=True)

W, H = A4
STYLES = getSampleStyleSheet()

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
BLUE = colors.HexColor("#1a56db")
LIGHT_BLUE = colors.HexColor("#e8f0fe")
DARK = colors.HexColor("#1f2937")
GREY = colors.HexColor("#6b7280")
LIGHT_GREY = colors.HexColor("#f3f4f6")
GREEN = colors.HexColor("#065f46")
ACCENT = colors.HexColor("#7c3aed")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _heading(text: str, size: int = 18, color=BLUE) -> Paragraph:
    style = ParagraphStyle(
        "h", fontName="Helvetica-Bold", fontSize=size, textColor=color, spaceAfter=4
    )
    return Paragraph(text, style)


def _body(text: str, size: int = 10, color=DARK) -> Paragraph:
    style = ParagraphStyle(
        "b", fontName="Helvetica", fontSize=size, textColor=color, spaceAfter=2
    )
    return Paragraph(text, style)


def _small(text: str, color=GREY) -> Paragraph:
    style = ParagraphStyle(
        "s", fontName="Helvetica", fontSize=8, textColor=color
    )
    return Paragraph(text, style)


def _line_table(items: list[dict], currency: str = "USD") -> Table:
    sym = "Rs. " if currency == "INR" else ("£" if currency == "GBP" else ("€" if currency == "EUR" else "$"))
    data = [["Description", "Qty", "Unit Price", "Amount"]]
    for item in items:
        data.append([
            item["description"],
            str(item["qty"]),
            f"{sym}{item['unit_price']:,.2f}",
            f"{sym}{item['amount']:,.2f}",
        ])
    t = Table(data, colWidths=[90 * mm, 20 * mm, 35 * mm, 35 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GREY]),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d1d5db")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _totals_table(subtotal: float, tax: float, total: float,
                  currency: str = "USD", tax_label: str = "Tax") -> Table:
    sym = "Rs. " if currency == "INR" else ("£" if currency == "GBP" else ("€" if currency == "EUR" else "$"))
    data = [
        ["Subtotal", f"{sym}{subtotal:,.2f}"],
        [tax_label, f"{sym}{tax:,.2f}"],
        ["TOTAL DUE", f"{sym}{total:,.2f}"],
    ]
    t = Table(data, colWidths=[120 * mm, 60 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -2), "Helvetica"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("BACKGROUND", (0, -1), (-1, -1), LIGHT_BLUE),
        ("TEXTCOLOR", (0, -1), (-1, -1), BLUE),
        ("FONTSIZE", (0, -1), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEABOVE", (0, -1), (-1, -1), 1, BLUE),
    ]))
    return t


# ---------------------------------------------------------------------------
# Layout A — clean professional
# ---------------------------------------------------------------------------

def layout_a(
    filename: str,
    invoice_no: str,
    vendor_name: str,
    vendor_address: str,
    vendor_tax: str,
    vendor_email: str,
    vendor_bank: str,
    bill_to: str,
    invoice_date: date,
    due_date: date,
    po_ref: str | None,
    items: list[dict],
    currency: str = "USD",
    tax_rate: float = 0.0,
    tax_label: str = "Tax (0%)",
    notes: str = "",
) -> None:
    """Standard professional layout with blue header."""
    path = SAMPLES_DIR / filename
    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    sym = "Rs. " if currency == "INR" else ("£" if currency == "GBP" else ("€" if currency == "EUR" else "$"))
    subtotal = sum(i["amount"] for i in items)
    tax = round(subtotal * tax_rate, 2)
    total = subtotal + tax

    story = [
        _heading(vendor_name, 20),
        _small(vendor_address),
        _small(f"Tax ID: {vendor_tax}  |  Email: {vendor_email}"),
        _small(f"Bank: {vendor_bank}"),
        Spacer(1, 8*mm),
        _heading("TAX INVOICE", 16),
        Spacer(1, 3*mm),
    ]

    meta = [
        ["Invoice No.", invoice_no, "Invoice Date", invoice_date.strftime("%d %b %Y")],
        ["Bill To", bill_to, "Due Date", due_date.strftime("%d %b %Y")],
    ]
    if po_ref:
        meta.append(["PO Reference", po_ref, "Currency", currency])
    meta_t = Table(meta, colWidths=[35*mm, 65*mm, 35*mm, 45*mm])
    meta_t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTNAME", (3, 0), (3, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GREY),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d1d5db")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [meta_t, Spacer(1, 6*mm), _line_table(items, currency),
              Spacer(1, 4*mm), _totals_table(subtotal, tax, total, currency, tax_label)]
    if notes:
        story += [Spacer(1, 6*mm), _body(f"Notes: {notes}", 9, GREY)]

    story += [Spacer(1, 8*mm),
              _small("Thank you for your business. Please include invoice number in payment reference.")]

    doc.build(story)
    print(f"  ✓ {filename}  ({path.stat().st_size // 1024} KB)")
    return {"subtotal": subtotal, "tax": tax, "total": total}


# ---------------------------------------------------------------------------
# Layout B — compact coloured band
# ---------------------------------------------------------------------------

def layout_b(
    filename: str,
    invoice_no: str,
    vendor_name: str,
    vendor_tax: str,
    vendor_email: str,
    vendor_bank: str,
    invoice_date: date,
    due_date: date,
    po_ref: str | None,
    items: list[dict],
    currency: str = "USD",
    tax_rate: float = 0.0,
    tax_label: str = "Tax",
    notes: str = "",
) -> dict:
    """Compact layout with purple accent band."""
    path = SAMPLES_DIR / filename
    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=10*mm, bottomMargin=10*mm)
    sym = "Rs. " if currency == "INR" else ("£" if currency == "GBP" else ("€" if currency == "EUR" else "$"))
    subtotal = sum(i["amount"] for i in items)
    tax = round(subtotal * tax_rate, 2)
    total = subtotal + tax

    header_data = [[
        Paragraph(f'<font color="white"><b>{vendor_name}</b><br/>'
                  f'<font size="8">{vendor_tax} | {vendor_email}</font></font>',
                  ParagraphStyle("hdr", fontName="Helvetica-Bold", fontSize=13,
                                 textColor=colors.white)),
        Paragraph(f'<font color="white"><b>INVOICE</b><br/>'
                  f'<font size="9">#{invoice_no}</font></font>',
                  ParagraphStyle("hdr2", fontName="Helvetica-Bold", fontSize=13,
                                 textColor=colors.white, alignment=2)),
    ]]
    header_t = Table(header_data, colWidths=[100*mm, 60*mm])
    header_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (0, 0), 8),
        ("RIGHTPADDING", (-1, 0), (-1, 0), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))

    info_rows = [
        ["Bank Account", vendor_bank, "Date", invoice_date.strftime("%Y-%m-%d")],
        ["", "", "Due Date", due_date.strftime("%Y-%m-%d")],
    ]
    if po_ref:
        info_rows.append(["PO Reference", po_ref, "Currency", currency])
    info_t = Table(info_rows, colWidths=[30*mm, 80*mm, 28*mm, 42*mm])
    info_t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    story = [header_t, Spacer(1, 4*mm), info_t, Spacer(1, 5*mm),
             _line_table(items, currency), Spacer(1, 3*mm),
             _totals_table(subtotal, tax, total, currency, tax_label)]
    if notes:
        story.append(Spacer(1, 4*mm))
        story.append(_body(f"Notes: {notes}", 8, GREY))

    doc.build(story)
    print(f"  ✓ {filename}  ({path.stat().st_size // 1024} KB)")
    return {"subtotal": subtotal, "tax": tax, "total": total}


# ---------------------------------------------------------------------------
# Layout C — "scanned" image PDF
# ---------------------------------------------------------------------------

def layout_c_scanned(
    filename: str,
    text_content: str,
) -> None:
    """
    Create an image-only PDF that simulates a scanned document.
    Steps:
      1. Render the text to a PIL image using Pillow drawing.
      2. Add slight rotation (±1.5°) and Gaussian noise.
      3. Save as a single-page image-only PDF (no searchable text layer).
    """
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    import random, math

    W_px, H_px = 1654, 2339   # A4 at 200 DPI
    img = Image.new("RGB", (W_px, H_px), color=(252, 250, 248))
    draw = ImageDraw.Draw(img)

    # Draw text lines
    try:
        font_large = ImageFont.truetype("arial.ttf", 32)
        font_med = ImageFont.truetype("arial.ttf", 26)
        font_small = ImageFont.truetype("arial.ttf", 22)
    except Exception:
        font_large = font_med = font_small = ImageFont.load_default()

    x0, y = 80, 80
    for line in text_content.strip().split("\n"):
        stripped = line.strip()
        if not stripped:
            y += 20
            continue
        font = font_large if stripped.startswith("##") else (
            font_med if stripped.startswith("#") else font_small
        )
        clean = stripped.lstrip("#").strip()
        draw.text((x0, y), clean, fill=(20, 20, 20), font=font)
        y += font_large.size + 8 if font == font_large else font_small.size + 6

    # Add Gaussian noise for realism
    import random
    import numpy as np
    arr = np.array(img, dtype=np.int16)
    noise = np.random.normal(0, 4, arr.shape).astype(np.int16)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr)

    # Slight rotation
    angle = random.uniform(-1.5, 1.5)
    img = img.rotate(angle, fillcolor=(252, 250, 248), expand=False)

    # Save as image-only PDF
    path = SAMPLES_DIR / filename
    img.save(str(path), "PDF", resolution=200)
    print(f"  ✓ {filename} [SCANNED/IMAGE-ONLY]  ({path.stat().st_size // 1024} KB)")


# ---------------------------------------------------------------------------
# Invoice definitions
# ---------------------------------------------------------------------------

TODAY = date(2024, 7, 15)

def generate_all() -> None:
    print("\n=== Generating sample invoices ===\n")

    # 1. Happy path — clean, known vendor, explicit PO
    layout_a(
        "happy_path.pdf",
        invoice_no="INV-1001",
        vendor_name="Acme Supplies Inc",
        vendor_address="123 Commerce Way, New York, NY 10001",
        vendor_tax="US-47-1234567",
        vendor_email="accounts@acmesupplies.com",
        vendor_bank="ACC-US-9876543210",
        bill_to="Zamp Technologies Ltd\n456 Enterprise Blvd, San Francisco, CA 94105",
        invoice_date=TODAY - timedelta(days=5),
        due_date=TODAY + timedelta(days=25),
        po_ref="PO-2024-001",
        items=[
            {"description": "Premium Office Paper (A4, 80gsm) - 50 reams", "qty": 50, "unit_price": 120.00, "amount": 6000.00},
            {"description": "Ballpoint Pens (Box of 50)", "qty": 10, "unit_price": 45.00, "amount": 450.00},
            {"description": "Sticky Notes Assorted - 48 packs", "qty": 48, "unit_price": 15.00, "amount": 720.00},
            {"description": "File Folders Manila (Box of 100)", "qty": 8, "unit_price": 35.00, "amount": 280.00},
        ],
        currency="USD",
        notes="Net 30 payment terms. Wire transfer preferred.",
    )

    # 2. Within-tolerance — 1.5% over PO-2024-001 remaining (~200 balance after happy_path)
    layout_a(
        "within_tolerance.pdf",
        invoice_no="INV-1002",
        vendor_name="Acme Supplies Inc",
        vendor_address="123 Commerce Way, New York, NY 10001",
        vendor_tax="US-47-1234567",
        vendor_email="accounts@acmesupplies.com",
        vendor_bank="ACC-US-9876543210",
        bill_to="Zamp Technologies Ltd",
        invoice_date=TODAY - timedelta(days=3),
        due_date=TODAY + timedelta(days=27),
        po_ref="PO-2024-007",
        items=[
            {"description": "Laser Printer Cartridges HP 85A (4-pack)", "qty": 4, "unit_price": 220.00, "amount": 880.00},
            {"description": "Whiteboard Markers Assorted (Box)", "qty": 6, "unit_price": 28.00, "amount": 168.00},
            {"description": "Stapler Heavy Duty", "qty": 3, "unit_price": 42.00, "amount": 126.00},
        ],
        currency="USD",
        notes="Remainder of PO-2024-007 stationery order.",
    )

    # 3. Over-tolerance — 8% over PO-2024-006
    layout_b(
        "over_tolerance.pdf",
        invoice_no="INV-CB-2024-087",
        vendor_name="CloudBase Systems",
        vendor_tax="US-47-3332211",
        vendor_email="ar@cloudbase.io",
        vendor_bank="ACC-US-1112223333",
        invoice_date=TODAY - timedelta(days=2),
        due_date=TODAY + timedelta(days=28),
        po_ref="PO-2024-006",
        items=[
            {"description": "Cloud Compute (c5.4xlarge) - 720 hours", "qty": 720, "unit_price": 25.00, "amount": 18000.00},
            {"description": "Managed Kubernetes Cluster (monthly)", "qty": 1, "unit_price": 4500.00, "amount": 4500.00},
            {"description": "Data Transfer Out (TB)", "qty": 12, "unit_price": 250.00, "amount": 3000.00},
        ],
        currency="USD",
        notes="Additional compute usage due to load testing. Exceeds PO by ~8%.",
    )

    # 4. Scanned invoice — image-only, no PO number
    scanned_text = """
# TechPro Logistics Services
## Tax Invoice

Vendor: Rapid Logistics Ltd
Tax ID: UK-GB-12345678
Bank: ACC-UK-5551234567
Email: invoices@rapidlogistics.co.uk

Invoice No:  INV-RL-2024-445
Invoice Date: 12 July 2024
Due Date: 11 August 2024

Bill To:
Zamp Technologies Ltd
456 Enterprise Blvd
San Francisco, CA 94105

# Line Items

Description                           Qty    Unit Price    Amount
Express Freight - San Francisco        3        £850.00      £2,550.00
Customs Clearance Fee                  1        £320.00        £320.00
Insurance (0.5% of cargo value)        1        £180.00        £180.00
Fuel Surcharge                         1        £150.00        £150.00

Subtotal:   £3,200.00
Tax:            £0.00
TOTAL DUE:  £3,200.00

Payment Terms: Net 30
Currency: GBP

Thank you for choosing Rapid Logistics Ltd.
"""
    layout_c_scanned("scanned_no_po.pdf", scanned_text)

    # 5. Split bill 1 of 3 — PO-2024-004 (500000 INR), part 1: 150000 INR
    layout_b(
        "split_bill_1.pdf",
        invoice_no="INV-SS-2024-301",
        vendor_name="Sunrise Software Pvt Ltd",
        vendor_tax="IN-29AABCS1234K1ZX",
        vendor_email="accounts@sunrisesoftware.in",
        vendor_bank="ACC-IN-HDFC9876543210",
        invoice_date=TODAY - timedelta(days=60),
        due_date=TODAY - timedelta(days=30),
        po_ref="PO-2024-004",
        items=[
            {"description": "IT Support Services - April 2024", "qty": 1, "unit_price": 80000.00, "amount": 80000.00},
            {"description": "Software License Fees Q1", "qty": 1, "unit_price": 50000.00, "amount": 50000.00},
            {"description": "Hardware Maintenance", "qty": 1, "unit_price": 20000.00, "amount": 20000.00},
        ],
        currency="INR",
        tax_rate=0.18,
        tax_label="GST (18%)",
        notes="Invoice 1 of 3 against PO-2024-004. Annual IT Support Contract.",
    )

    # 6. Split bill 2 of 3 — 200000 INR + 18% GST
    layout_b(
        "split_bill_2.pdf",
        invoice_no="INV-SS-2024-302",
        vendor_name="Sunrise Software Pvt Ltd",
        vendor_tax="IN-29AABCS1234K1ZX",
        vendor_email="accounts@sunrisesoftware.in",
        vendor_bank="ACC-IN-HDFC9876543210",
        invoice_date=TODAY - timedelta(days=30),
        due_date=TODAY,
        po_ref="PO-2024-004",
        items=[
            {"description": "IT Support Services - May 2024", "qty": 1, "unit_price": 80000.00, "amount": 80000.00},
            {"description": "IT Support Services - June 2024", "qty": 1, "unit_price": 80000.00, "amount": 80000.00},
            {"description": "Network Security Audit", "qty": 1, "unit_price": 40000.00, "amount": 40000.00},
        ],
        currency="INR",
        tax_rate=0.18,
        tax_label="GST (18%)",
        notes="Invoice 2 of 3 against PO-2024-004.",
    )

    # 7. Split bill 3 of 3 — 200000 INR + 18% GST (exceeds remaining ~73200)
    layout_b(
        "split_bill_3.pdf",
        invoice_no="INV-SS-2024-303",
        vendor_name="Sunrise Software Pvt Ltd",
        vendor_tax="IN-29AABCS1234K1ZX",
        vendor_email="accounts@sunrisesoftware.in",
        vendor_bank="ACC-IN-HDFC9876543210",
        invoice_date=TODAY - timedelta(days=2),
        due_date=TODAY + timedelta(days=28),
        po_ref="PO-2024-004",
        items=[
            {"description": "IT Support Services - July 2024", "qty": 1, "unit_price": 80000.00, "amount": 80000.00},
            {"description": "Server Infrastructure Upgrade", "qty": 1, "unit_price": 90000.00, "amount": 90000.00},
            {"description": "Data Backup Solution Implementation", "qty": 1, "unit_price": 30000.00, "amount": 30000.00},
        ],
        currency="INR",
        tax_rate=0.18,
        tax_label="GST (18%)",
        notes="Invoice 3 of 3 against PO-2024-004. This invoice exceeds the remaining PO balance.",
    )

    # 8. Near-duplicate — same as happy_path but number INV1001-A
    layout_a(
        "near_duplicate.pdf",
        invoice_no="INV1001-A",
        vendor_name="Acme Supplies Inc",
        vendor_address="123 Commerce Way, New York, NY 10001",
        vendor_tax="US-47-1234567",
        vendor_email="accounts@acmesupplies.com",
        vendor_bank="ACC-US-9876543210",
        bill_to="Zamp Technologies Ltd",
        invoice_date=TODAY - timedelta(days=5),
        due_date=TODAY + timedelta(days=25),
        po_ref="PO-2024-001",
        items=[
            {"description": "Premium Office Paper (A4, 80gsm) - 50 reams", "qty": 50, "unit_price": 120.00, "amount": 6000.00},
            {"description": "Ballpoint Pens (Box of 50)", "qty": 10, "unit_price": 45.00, "amount": 450.00},
            {"description": "Sticky Notes Assorted - 48 packs", "qty": 48, "unit_price": 15.00, "amount": 720.00},
            {"description": "File Folders Manila (Box of 100)", "qty": 8, "unit_price": 35.00, "amount": 280.00},
        ],
        currency="USD",
        notes="REVISED — resubmission of INV-1001.",
    )

    # 9. Missing fields — no invoice number in a standard spot
    layout_a(
        "missing_fields.pdf",
        invoice_no="[UNCLEAR]",
        vendor_name="Global Tech Solutions",
        vendor_address="789 Silicon Avenue, Austin, TX 78701",
        vendor_tax="US-47-9876543",
        vendor_email="billing@globaltech.com",
        vendor_bank="ACC-US-1234567890",
        bill_to="Zamp Technologies Ltd",
        invoice_date=TODAY - timedelta(days=1),
        due_date=TODAY + timedelta(days=29),
        po_ref=None,
        items=[
            {"description": "Software Development Services (see attached SOW)", "qty": 1, "unit_price": 0.00, "amount": 0.00},
        ],
        currency="USD",
        notes="Please see attached Statement of Work for details. [TOTAL AMOUNT PENDING APPROVAL]",
    )

    # 10. Fraud — bank details changed
    layout_a(
        "fraud_bank_change.pdf",
        invoice_no="INV-PC-2024-112",
        vendor_name="Pinnacle Consulting Group",
        vendor_address="300 Park Avenue, New York, NY 10022",
        vendor_tax="US-47-5551234",
        vendor_email="finance@pinnacle.com",
        vendor_bank="ACC-US-FRAUDSTER-9999999",   # <-- does NOT match master
        bill_to="Zamp Technologies Ltd",
        invoice_date=TODAY - timedelta(days=4),
        due_date=TODAY + timedelta(days=26),
        po_ref="PO-2024-005",
        items=[
            {"description": "Strategy Consulting - Phase 1 Deliverables", "qty": 1, "unit_price": 45000.00, "amount": 45000.00},
            {"description": "Market Research Report", "qty": 1, "unit_price": 8000.00, "amount": 8000.00},
            {"description": "Executive Presentation Preparation", "qty": 1, "unit_price": 5000.00, "amount": 5000.00},
        ],
        currency="USD",
        notes="IMPORTANT: Please update our bank details in your system. New account: ACC-US-FRAUDSTER-9999999",
    )

    print("\n=== All invoices generated ===")
    print(f"Output directory: {SAMPLES_DIR}\n")


# ---------------------------------------------------------------------------
# Expected decisions (for test assertions)
# ---------------------------------------------------------------------------

EXPECTED_DECISIONS = {
    "happy_path.pdf": {
        "decision": "AUTO_APPROVED",
        "reason": "Clean invoice, known vendor, explicit PO, within balance",
    },
    "within_tolerance.pdf": {
        "decision": "AUTO_APPROVED",
        "reason": "Slightly over PO but within 2% tolerance",
    },
    "over_tolerance.pdf": {
        "decision": "HOLD_FOR_REVIEW",
        "reason": "8% over PO remaining balance",
        "expected_rule_failures": ["R6_AMOUNT_WITHIN_BALANCE"],
    },
    "scanned_no_po.pdf": {
        "decision": "APPROVED_WITH_NOTE",
        "reason": "OCR scanned invoice with inferred PO match",
    },
    "split_bill_1.pdf": {
        "decision": "APPROVED_WITH_NOTE",
        "reason": "First split bill — inferred PO match (note added)",
    },
    "split_bill_2.pdf": {
        "decision": "APPROVED_WITH_NOTE",
        "reason": "Second split bill — inferred PO, within remaining balance",
    },
    "split_bill_3.pdf": {
        "decision": "HOLD_FOR_REVIEW",
        "reason": "Third split bill exceeds remaining PO balance",
        "expected_rule_failures": ["R6_AMOUNT_WITHIN_BALANCE"],
    },
    "near_duplicate.pdf": {
        "decision": "REJECTED",
        "reason": "INV1001-A normalises to same key as INV-1001 (duplicate)",
        "expected_reason_codes": ["DUPLICATE_INVOICE"],
    },
    "missing_fields.pdf": {
        "decision": "REQUEST_INFO",
        "reason": "No valid invoice number, zero total — critical fields missing",
    },
    "fraud_bank_change.pdf": {
        "decision": "REJECTED",
        "reason": "Bank account differs from vendor master — critical fraud block",
        "expected_fraud_codes": ["BANK_DETAILS_MISMATCH"],
    },
}


def write_expected_decisions() -> None:
    tests_dir = ROOT / "tests"
    tests_dir.mkdir(exist_ok=True)
    path = tests_dir / "expected_decisions.json"
    path.write_text(json.dumps(EXPECTED_DECISIONS, indent=2), encoding="utf-8")
    print(f"  ✓ tests/expected_decisions.json")


if __name__ == "__main__":
    generate_all()
    write_expected_decisions()
