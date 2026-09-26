"""Application service: process a batch of invoices, routed into
per-NIT subfolders (Phase 1E).

Reuses :func:`clinica_rpa.services.invoice_download_service.process_invoice`
UNCHANGED for every item -- this module's only added responsibility is
computing the right ``destination_directory`` (the NIT subfolder, created
lazily) and sequencing items one at a time. It never opens a second GO
session concurrently: items are processed strictly sequentially, exactly
like a human running the single-invoice flow repeatedly.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from pathlib import Path

from loguru import logger

from clinica_rpa.automation import go_session, report_viewer, trazabilidad
from clinica_rpa.automation.waits import wait_until_window_closed
from clinica_rpa.batch.excel_loader import mask_invoice
from clinica_rpa.domain.errors import ClinicaRpaError, ErrorCode
from clinica_rpa.domain.models import BatchInvoiceItemResult, InvoiceBatchItem
from clinica_rpa.services.invoice_download_service import _go_discovery, process_invoice

_NIT_PATTERN = re.compile(r"^[0-9]+$")

VIEWER_CLOSE_TIMEOUT_SECONDS = 10.0


def _prepare_go_for_next_item() -> None:
    """Phase 1E.1: close the "Visor de Reportes" for the just-downloaded
    invoice (if still open), THEN click "Deshacer" once, so the next
    item's invoice field is writable. Operator-confirmed live
    (2026-09-25): "Deshacer" does not work while the viewer is still open.

    Fresh GO discovery every call (Regla 1 -- never reuse a stale handle
    across items). Raises :class:`ClinicaRpaError` if GO can't be
    found/verified, the viewer doesn't confirm closed, or the click
    fails/is ambiguous -- the caller must treat this as ABORT THE BATCH,
    never continue clicking (spec section 6/11).
    """
    pids = go_session.find_target_process_ids()
    if not pids:
        raise ClinicaRpaError(ErrorCode.GO_NOT_RUNNING, "GO ya no esta en ejecucion.")

    viewer_hwnd = go_session.win32_pure_find_window(pids[0], report_viewer.REPORT_VIEWER_TITLE_HINTS)
    if viewer_hwnd is not None:
        go_session.close_window_once(viewer_hwnd)
        closed = wait_until_window_closed(viewer_hwnd, timeout_seconds=VIEWER_CLOSE_TIMEOUT_SECONDS)
        if not closed:
            raise ClinicaRpaError(
                ErrorCode.RESET_SCREEN_FAILED, "El Visor de Reportes no se cerro a tiempo antes de preparar la siguiente factura."
            )

    go_window, _window, _controls, _all_windows = _go_discovery(pids)
    trazabilidad.reset_result_screen_once(go_window)


def process_batch(items: list[InvoiceBatchItem], root_destination: Path) -> Iterator[BatchInvoiceItemResult]:
    """Process every item sequentially, yielding one
    :class:`BatchInvoiceItemResult` as soon as each item finishes (never
    the whole list at once) so a caller (the GUI batch worker) can render
    live progress.

    ``root_destination / item.nit`` is created lazily, right before the
    first invoice for that NIT is processed -- a NIT folder that already
    exists (because an earlier item in this same batch used it) is simply
    reused via ``mkdir(parents=True, exist_ok=True)``, never treated as an
    error. One item's failure never aborts the rest of the batch -- but if
    GO cannot be safely prepared for the NEXT item (state unknown, click
    ambiguous), the WHOLE REMAINING BATCH is aborted (spec section 6/11:
    never keep clicking against an unsafe/unknown GO state). One final
    ``BATCH_ABORTED`` result is yielded in that case so the caller can
    report it.
    """
    last_index = len(items) - 1
    for index, item in enumerate(items):
        _t0 = time.monotonic()
        masked = mask_invoice(item.invoice_number)

        # Defensive re-check (Regla 1): never trust validation done two
        # modules away without a cheap local re-check before touching the
        # filesystem, even though the loader already guarantees this.
        if not _NIT_PATTERN.fullmatch(item.nit):
            logger.warning("BATCH: NIT invalido detectado en el servicio (factura enmascarada={})", masked)
            yield BatchInvoiceItemResult(
                invoice_number_masked=masked,
                nit=item.nit,
                status="INVALID_NIT",
                pdf_path=None,
                elapsed_seconds=time.monotonic() - _t0,
                error_code="INVALID_NIT",
                error_message_safe="El NIT de esta fila no es valido (revalidacion de seguridad).",
            )
            continue

        nit_directory = root_destination / item.nit
        try:
            nit_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("BATCH: no se pudo crear/usar la carpeta del NIT (factura enmascarada={}): {!r}", masked, exc)
            yield BatchInvoiceItemResult(
                invoice_number_masked=masked,
                nit=item.nit,
                status="FOLDER_ERROR",
                pdf_path=None,
                elapsed_seconds=time.monotonic() - _t0,
                error_code="FOLDER_ERROR",
                error_message_safe="No se pudo crear o usar la carpeta de destino para este NIT.",
            )
            continue

        logger.info("BATCH: procesando factura (enmascarada={}, nit_dir={})", masked, nit_directory)
        result = process_invoice(invoice_number=item.invoice_number, destination_directory=nit_directory)

        yield BatchInvoiceItemResult(
            invoice_number_masked=result.invoice_number_masked,
            nit=item.nit,
            status=result.status,
            pdf_path=result.pdf_path,
            elapsed_seconds=result.elapsed_seconds,
            error_code=result.error_code,
            error_message_safe=result.error_message_safe,
        )

        if index == last_index:
            continue

        try:
            _prepare_go_for_next_item()
        except ClinicaRpaError as exc:
            logger.error(
                "BATCH: no se pudo preparar GO para la siguiente factura ({}) -- ABORTANDO el resto del lote.",
                exc.error_code,
            )
            yield BatchInvoiceItemResult(
                invoice_number_masked="",
                nit="",
                status="BATCH_ABORTED",
                pdf_path=None,
                elapsed_seconds=0.0,
                error_code="BATCH_ABORTED",
                error_message_safe=f"Lote abortado: no se pudo preparar GO para la siguiente factura ({exc.error_code}).",
            )
            return
