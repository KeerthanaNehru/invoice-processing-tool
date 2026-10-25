"""
pipeline/stage1_ingest.py — PDF ingestion and text extraction.

WHY this stage exists:
  Downstream stages need clean text. We must decide BEFORE calling the LLM
  whether to use pdfplumber (text PDFs) or PyMuPDF+OCR (scanned image PDFs).
  We also enforce upload constraints here so nothing expensive happens on
  bad inputs.

Detection heuristic:
  We extract text from each page with pdfplumber. If the total text across
  all pages is < SCANNED_CHAR_THRESHOLD chars, we treat the whole document
  as scanned and re-process with PyMuPDF → PIL → Tesseract.
"""
from __future__ import annotations

import io
import logging
from app.config import MAX_FILE_SIZE_BYTES, MAX_PAGES, SCANNED_CHAR_THRESHOLD
from app.models import IngestMethod
from app.pipeline.ocr import OCRUnavailableError, ocr_page_image

logger = logging.getLogger(__name__)

# Keywords that must appear somewhere in the document text for it to be
# considered an invoice (case-insensitive). Graceful rejection otherwise.
INVOICE_KEYWORDS = {
    "invoice", "bill", "receipt", "tax invoice", "proforma",
    "purchase order", "amount due", "total due", "payable",
    "remittance", "vendor", "supplier",
}


class IngestError(ValueError):
    """Raised for invalid uploads (wrong type, too large, etc.)."""


class NotAnInvoiceError(ValueError):
    """Raised when the document doesn't look like an invoice."""


def _extract_text_pdfplumber(pdf_bytes: bytes) -> tuple[str, int]:
    """Extract text from all pages using pdfplumber. Returns (text, page_count)."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = len(pdf.pages)
        if page_count > MAX_PAGES:
            raise IngestError(f"PDF has {page_count} pages. Maximum is {MAX_PAGES}.")
        texts = []
        for page in pdf.pages:
            t = page.extract_text() or ""
            texts.append(t)
            try:
                page.flush_cache()
            except Exception:
                pass
        return "\n\n".join(texts), page_count


def _extract_text_ocr(pdf_bytes: bytes) -> tuple[str, int]:
    """
    Render each page to an image with PyMuPDF, then OCR with Tesseract.
    Returns (text, page_count).
    Memory-optimised for 512 MB environments (Render Free Tier):
      - 150 DPI resolution
      - Grayscale colorspace (csGRAY / 'L' mode, 1 byte/pixel vs 3 bytes/pixel)
      - Immediate image disposal per page
    Raises OCRUnavailableError if Tesseract is not installed.
    """
    import pymupdf as fitz
    from PIL import Image

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_count = doc.page_count
    if page_count > MAX_PAGES:
        doc.close()
        raise IngestError(f"PDF has {page_count} pages. Maximum is {MAX_PAGES}.")

    texts = []
    try:
        for page_num in range(page_count):
            page = doc[page_num]
            # 150 DPI in grayscale (cuts memory by >80% vs 200 DPI RGB)
            mat = fitz.Matrix(150 / 72, 150 / 72)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            del pix  # Free raw pixmap buffer immediately

            try:
                page_text = ocr_page_image(img)
                texts.append(page_text)
            finally:
                img.close()
                del img

            logger.debug("OCR page %d: %d chars", page_num + 1, len(texts[-1]))
    finally:
        doc.close()

    return "\n\n".join(texts), page_count


def _is_invoice(text: str) -> bool:
    """Return True if at least one invoice keyword appears in the text."""
    lower = text.lower()
    return any(kw in lower for kw in INVOICE_KEYWORDS)


def ingest(pdf_bytes: bytes, filename: str) -> dict:
    """
    Stage 1: Ingest a PDF and return a result dict with:
      - text: str
      - page_count: int
      - method: IngestMethod
      - filename: str

    Raises:
      IngestError: bad input (size, type, pages)
      NotAnInvoiceError: file doesn't look like an invoice
      OCRUnavailableError: scanned PDF but Tesseract missing
    """
    # --- Constraint checks ---
    if len(pdf_bytes) > MAX_FILE_SIZE_BYTES:
        raise IngestError(
            f"File too large ({len(pdf_bytes) / 1_048_576:.1f} MB). Maximum is 5 MB."
        )

    # Verify PDF magic bytes
    if not pdf_bytes.startswith(b"%PDF"):
        raise IngestError("Only PDF files are accepted.")

    # --- Try text extraction ---
    try:
        text, page_count = _extract_text_pdfplumber(pdf_bytes)
    except Exception as exc:
        raise IngestError(f"Failed to open PDF: {exc}") from exc

    if page_count > MAX_PAGES:
        raise IngestError(f"PDF has {page_count} pages. Maximum is {MAX_PAGES}.")

    # --- Decide method ---
    if len(text.strip()) < SCANNED_CHAR_THRESHOLD:
        logger.info("PDF appears scanned (text=%d chars). Switching to OCR.", len(text.strip()))
        text, page_count = _extract_text_ocr(pdf_bytes)
        method = IngestMethod.OCR
    else:
        method = IngestMethod.TEXT

    # --- Invoice check ---
    if not _is_invoice(text):
        raise NotAnInvoiceError(
            "This document does not appear to be an invoice. "
            "Could not find expected keywords (invoice, bill, total, vendor, etc.)."
        )

    logger.info(
        "Ingested '%s': %d pages, method=%s, chars=%d",
        filename, page_count, method.value, len(text),
    )
    return {
        "text": text,
        "page_count": page_count,
        "method": method,
        "filename": filename,
    }
