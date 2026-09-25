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

from clinica_rpa.batch.excel_loader import mask_invoice
from clinica_rpa.domain.models import BatchInvoiceItemResult, InvoiceBatchItem
from clinica_rpa.services.invoice_download_service import process_invoice

_NIT_PATTERN = re.compile(r"^[0-9]+$")


def process_batch(items: list[InvoiceBatchItem], root_destination: Path) -> Iterator[BatchInvoiceItemResult]:
    """Process every item sequentially, yielding one
    :class:`BatchInvoiceItemResult` as soon as each item finishes (never
    the whole list at once) so a caller (the GUI batch worker) can render
    live progress.

    ``root_destination / item.nit`` is created lazily, right before the
    first invoice for that NIT is processed -- a NIT folder that already
    exists (because an earlier item in this same batch used it) is simply
    reused via ``mkdir(parents=True, exist_ok=True)``, never treated as an
    error. One item's failure never aborts the rest of the batch.
    """
    for item in items:
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
