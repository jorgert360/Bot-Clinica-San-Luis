"""Background worker that calls ``process_invoice`` off the Tk main thread
(Phase 1D).

The worker thread NEVER touches a Tk widget. It only calls the already
validated engine and posts a message onto a ``queue.Queue``; the GUI thread
is the sole consumer, polled via ``root.after(...)``.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from loguru import logger

from clinica_rpa.domain.models import InvoiceDownloadResult
from clinica_rpa.gui.state import UNEXPECTED_ERROR_CODE
from clinica_rpa.services.invoice_download_service import process_invoice

DONE = "done"
ERROR = "error"


def start_invoice_worker(invoice_number: str, destination_directory: Path, result_queue: "queue.Queue") -> threading.Thread:
    """Start a daemon thread that calls ``process_invoice`` once and posts
    exactly one ``(DONE, InvoiceDownloadResult)`` or ``(ERROR, InvoiceDownloadResult)``
    message onto ``result_queue``.

    ``process_invoice`` already classifies every expected failure into a
    non-raising ``InvoiceDownloadResult`` (status = an ``ErrorCode``
    string) -- this wrapper only defends against a truly unexpected
    exception escaping it, converting that case into a synthetic result
    with ``status=error_code="UNEXPECTED"`` so the caller never has to
    branch on "did the worker crash" separately from "did the engine
    report a failure".
    """

    def _run() -> None:
        try:
            result = process_invoice(invoice_number=invoice_number, destination_directory=destination_directory)
            result_queue.put((DONE, result))
        except Exception as exc:  # defensive: process_invoice should not raise
            logger.exception("Excepcion inesperada escapando de process_invoice (fuera de contrato)")
            synthetic = InvoiceDownloadResult(
                invoice_number_masked="",
                status=UNEXPECTED_ERROR_CODE,
                pdf_created=False,
                pdf_path=None,
                pdf_size_bytes=None,
                pdf_pages=None,
                elapsed_seconds=0.0,
                error_code=UNEXPECTED_ERROR_CODE,
                error_message_safe="Ocurrio un error inesperado durante el procesamiento.",
            )
            result_queue.put((ERROR, synthetic))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread
