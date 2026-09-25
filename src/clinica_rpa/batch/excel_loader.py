"""Excel batch loader (Phase 1E) -- definitive format.

Reads ONLY "No. Factura" (column A) and "Nit" (column H) from the "VARIAS"
sheet. Every other column (Ingreso, Documento Paciente, Fecha Factura,
Nombre usuario, Nombre Unidad Funcional, Valor Entidad, Nombre Tercero)
is never read into any structure, logged, displayed, or persisted
(Regla 6) -- the row iterator below only ever indexes cells 1 and 8 of
each row and immediately discards the rest of the row tuple.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import openpyxl

from clinica_rpa.domain.models import InvoiceBatchItem

SHEET_NAME = "VARIAS"
INVOICE_HEADER = "No. Factura"
NIT_HEADER = "Nit"
INVOICE_COLUMN_INDEX = 1  # column A (1-based)
NIT_COLUMN_INDEX = 8  # column H (1-based)

REASON_INVALID_INVOICE = "INVALID_INVOICE"
REASON_INVALID_NIT = "INVALID_NIT"

_NIT_PATTERN = re.compile(r"^[0-9]+$")


class BatchFormatInvalidError(Exception):
    """Raised when the workbook does not match the definitive format
    (missing sheet, or the required headers are missing/in the wrong
    column). Carries a safe, UI-displayable message only -- never raw
    cell content."""

    def __init__(self, message_safe: str) -> None:
        super().__init__(message_safe)
        self.message_safe = message_safe


def mask_invoice(value: str) -> str:
    """First 3 + last 2 characters -- mirrors the engine's own masking
    convention (Regla 6)."""
    if len(value) <= 5:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 5)}{value[-2:]}"


@dataclass(frozen=True)
class InvalidBatchRow:
    """One rejected row: its 1-based row number, the invoice number
    masked (or ``'***'`` if the invoice itself was not even usable enough
    to mask), and a closed reason code. Never carries any other column's
    value."""

    row_number: int
    invoice_number_masked: str
    reason: str


@dataclass(frozen=True)
class BatchExcelSummary:
    """Safe-to-display summary of a loaded batch file (spec section 14) --
    counts only, never a raw row or any non-A/H column value."""

    total_rows: int
    valid_count: int
    invalid_rows: list[InvalidBatchRow]
    distinct_nit_count: int


def _normalize_invoice(raw_value: object) -> str | None:
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    if any(ord(c) < 32 for c in text):
        return None
    return text


def _normalize_nit(raw_value: object) -> str | None:
    if isinstance(raw_value, bool):
        return None  # bool is an int subclass -- reject explicitly, never True/False as a NIT
    if isinstance(raw_value, int):
        text = str(raw_value)
    elif isinstance(raw_value, float):
        if raw_value != int(raw_value):
            return None
        text = str(int(raw_value))
    elif isinstance(raw_value, str):
        text = raw_value.strip()
    else:
        return None
    if not _NIT_PATTERN.fullmatch(text):
        return None
    return text


def _header_text(header_row: tuple, one_based_index: int) -> str:
    zero_based = one_based_index - 1
    if zero_based >= len(header_row):
        return ""
    value = header_row[zero_based]
    return str(value).strip() if value is not None else ""


def load_invoice_batch(xlsx_path: Path) -> tuple[list[InvoiceBatchItem], BatchExcelSummary]:
    """Load and validate a batch Excel file against the definitive format.

    Raises :class:`BatchFormatInvalidError` (safe message, no traceback
    detail) if the sheet name or header text/position does not match --
    nothing is processed in that case. Otherwise returns every valid row
    as exactly one :class:`InvoiceBatchItem` (one loop, one item per row
    -- never two parallel lists that could desync) plus a safe summary.
    An individual row that fails normalization is skipped and recorded in
    the summary, never silently dropped and never raised as a whole-batch
    failure.
    """
    try:
        workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    except Exception as exc:
        raise BatchFormatInvalidError("No se pudo abrir el archivo Excel.") from exc

    if SHEET_NAME not in workbook.sheetnames:
        raise BatchFormatInvalidError(f"No se encontro la hoja '{SHEET_NAME}'.")
    sheet = workbook[SHEET_NAME]

    header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
    if header_row is None:
        raise BatchFormatInvalidError("El archivo no tiene fila de encabezados.")

    if _header_text(header_row, INVOICE_COLUMN_INDEX) != INVOICE_HEADER:
        raise BatchFormatInvalidError(f"La columna A debe tener el encabezado '{INVOICE_HEADER}'.")
    if _header_text(header_row, NIT_COLUMN_INDEX) != NIT_HEADER:
        raise BatchFormatInvalidError(f"La columna H debe tener el encabezado '{NIT_HEADER}'.")

    items: list[InvoiceBatchItem] = []
    invalid_rows: list[InvalidBatchRow] = []
    total_rows = 0

    for row_number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        if row is None or all(v is None for v in row):
            continue  # fully blank trailing row -- not a data row

        raw_invoice = row[INVOICE_COLUMN_INDEX - 1] if len(row) >= INVOICE_COLUMN_INDEX else None
        raw_nit = row[NIT_COLUMN_INDEX - 1] if len(row) >= NIT_COLUMN_INDEX else None
        # Every other index of `row` is discarded here -- never bound to a
        # name, never logged, never carried past this iteration.

        total_rows += 1

        invoice_number = _normalize_invoice(raw_invoice)
        if invoice_number is None:
            invalid_rows.append(InvalidBatchRow(row_number, "***", REASON_INVALID_INVOICE))
            continue

        nit = _normalize_nit(raw_nit)
        if nit is None:
            invalid_rows.append(InvalidBatchRow(row_number, mask_invoice(invoice_number), REASON_INVALID_NIT))
            continue

        items.append(InvoiceBatchItem(invoice_number=invoice_number, nit=nit))

    summary = BatchExcelSummary(
        total_rows=total_rows,
        valid_count=len(items),
        invalid_rows=invalid_rows,
        distinct_nit_count=len({item.nit for item in items}),
    )
    return items, summary
