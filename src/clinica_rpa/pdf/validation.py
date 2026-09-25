"""Structural PDF validation -- ported from
``scripts/poc_download_invoice_document.py``'s ``validate_pdf_file``.

Success gate (REQUIRED, never weakened): the file exists, is non-empty, has
a ``.pdf`` extension, and its header bytes equal ``b"%PDF"``.

``pypdf`` page-count is OPTIONAL/best-effort informational only: it must
NEVER gate success, whether pypdf is unavailable (``ImportError``) or fails
to parse the file. It blocks success ONLY if it successfully parses the
file and reports exactly 0 pages (a genuinely empty/invalid document).
Never content -- only the page count is ever read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger

PDF_HEADER_BYTES = b"%PDF"


@dataclass
class PdfValidationResult:
    """Structural-only PDF validation result. Never carries file content."""

    exists: bool
    size_bytes: int
    header_valid: bool
    pages: int | None  # None = unknown/unavailable (never gates success)
    success: bool


def validate_pdf_file(path: Path) -> PdfValidationResult:
    """Validate `path` structurally. Never opens/reads document content
    beyond the first 5 header bytes and (best-effort) pypdf's page count.
    """
    if not path.exists():
        return PdfValidationResult(exists=False, size_bytes=0, header_valid=False, pages=None, success=False)

    try:
        size = path.stat().st_size
    except OSError:
        return PdfValidationResult(exists=True, size_bytes=0, header_valid=False, pages=None, success=False)

    if size <= 0 or path.suffix.lower() != ".pdf":
        return PdfValidationResult(exists=True, size_bytes=size, header_valid=False, pages=None, success=False)

    try:
        with path.open("rb") as fh:
            prefix = fh.read(5)
    except OSError:
        return PdfValidationResult(exists=True, size_bytes=size, header_valid=False, pages=None, success=False)

    header_valid = prefix[:4] == PDF_HEADER_BYTES
    if not header_valid:
        return PdfValidationResult(exists=True, size_bytes=size, header_valid=False, pages=None, success=False)

    pages: int | None = None
    try:
        import pypdf
    except ImportError:
        logger.info("pypdf no esta instalado; se omite el conteo de paginas (opcional, no bloquea el resultado).")
        return PdfValidationResult(exists=True, size_bytes=size, header_valid=True, pages=None, success=True)

    try:
        reader = pypdf.PdfReader(str(path))
        pages = len(reader.pages)
    except Exception as exc:
        logger.warning("pypdf no pudo leer el archivo local (informativo, no bloquea header_valid): {}", exc)
        return PdfValidationResult(exists=True, size_bytes=size, header_valid=True, pages=None, success=True)

    success = pages >= 1
    return PdfValidationResult(exists=True, size_bytes=size, header_valid=True, pages=pages, success=success)
