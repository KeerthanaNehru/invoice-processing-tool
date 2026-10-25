"""
pipeline/ocr.py — Pluggable OCR function.

Design: a single public function `ocr_page_image(pil_image) -> str`.
Swapping the backend (e.g. from Tesseract to Google Vision) only requires
changing this one file.

Tesseract is optional: if the binary isn't found we raise a clear
`OCRUnavailableError` instead of crashing with a cryptic FileNotFoundError.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image as PILImage

logger = logging.getLogger(__name__)

_tesseract_available: bool | None = None  # cached after first probe


class OCRUnavailableError(RuntimeError):
    """Raised when Tesseract cannot be located."""


def _probe_tesseract() -> bool:
    """Return True if pytesseract can find the Tesseract binary."""
    import pytesseract
    from app.config import TESSERACT_CMD

    if TESSERACT_CMD and TESSERACT_CMD != "tesseract":
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception as exc:
        logger.warning("Tesseract not available: %s", exc)
        return False


def ocr_page_image(image: "PILImage.Image") -> str:
    """
    Run OCR on a PIL Image and return the extracted text.

    Args:
        image: A PIL Image (RGB or L mode).

    Returns:
        The extracted text string (may be empty for blank pages).

    Raises:
        OCRUnavailableError: If Tesseract binary cannot be found.
    """
    global _tesseract_available

    if _tesseract_available is None:
        _tesseract_available = _probe_tesseract()

    if not _tesseract_available:
        raise OCRUnavailableError(
            "Tesseract OCR is not available. "
            "Set TESSERACT_CMD in .env to the full path of the Tesseract binary, "
            "or install tesseract-ocr (apt-get install tesseract-ocr on Linux)."
        )

    import pytesseract

    text: str = pytesseract.image_to_string(image, lang="eng", config="--psm 6")
    return text
