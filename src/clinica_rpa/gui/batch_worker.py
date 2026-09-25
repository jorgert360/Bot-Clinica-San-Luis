"""Background worker that runs a batch off the Tk main thread (Phase 1E).

Same threading discipline as :mod:`clinica_rpa.gui.worker`: the worker
thread never touches a Tk widget, only posts messages onto a
``queue.Queue`` that the GUI thread drains via ``root.after(...)``.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from loguru import logger

from clinica_rpa.domain.models import InvoiceBatchItem
from clinica_rpa.services.batch_invoice_service import process_batch

ITEM_DONE = "item_done"
BATCH_DONE = "batch_done"
BATCH_ERROR = "batch_error"


def start_batch_worker(
    items: list[InvoiceBatchItem], root_destination: Path, result_queue: "queue.Queue"
) -> threading.Thread:
    """Start a daemon thread that processes ``items`` sequentially via
    :func:`process_batch`, posting one ``(ITEM_DONE, BatchInvoiceItemResult)``
    message per completed item, then exactly one ``(BATCH_DONE, None)``
    when the whole batch finishes. A truly unexpected exception escaping
    ``process_batch`` (it should not, since ``process_invoice`` never
    raises) is converted into one ``(BATCH_ERROR, safe_message)`` message
    instead of crashing the thread silently.
    """

    def _run() -> None:
        try:
            for item_result in process_batch(items, root_destination):
                result_queue.put((ITEM_DONE, item_result))
            result_queue.put((BATCH_DONE, None))
        except Exception:  # defensive: process_batch/process_invoice should not raise
            logger.exception("Excepcion inesperada escapando de process_batch (fuera de contrato)")
            result_queue.put((BATCH_ERROR, "Ocurrio un error inesperado durante el procesamiento del lote."))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread
