"""Domain models for the reusable invoice-download engine.

Phase 1A scope: a single result type returned by
``services.invoice_download_service.process_invoice`` and a small closed set
of GO/Indigo screen states used (read-only) to decide which automation step
runs next. Neither type ever carries a raw invoice number, patient data, or
clinical content (Regla 6) -- only masked/structural values.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StageTiming:
    """One instrumented stage's timing (Phase 1B). Never carries clinical
    data or a raw invoice number -- only the stage name, elapsed
    milliseconds, and a status token."""

    stage: str
    elapsed_ms: float
    status: str  # "OK", "SKIPPED" (stage not needed this run), or "FAIL"


@dataclass
class InvoiceDownloadResult:
    """Outcome of one ``process_invoice()`` call.

    ``status`` is either ``"COMPLETED"`` or an ``error_code`` string (see
    :class:`clinica_rpa.domain.errors.ErrorCode`) -- so a caller can always
    branch on ``status`` alone. ``error_code``/``error_message_safe`` are
    populated only when ``status != "COMPLETED"``.

    ``stage_timings`` (Phase 1B): per-stage instrumentation, always
    populated up to the point of success/failure -- safe to log/print in
    full, never contains clinical data.
    """

    invoice_number_masked: str
    status: str
    pdf_created: bool
    pdf_path: str | None
    pdf_size_bytes: int | None
    pdf_pages: int | None
    elapsed_seconds: float
    error_code: str | None
    error_message_safe: str | None
    stage_timings: list[StageTiming] = field(default_factory=list)


@dataclass(frozen=True)
class InvoiceBatchItem:
    """One row from a validated batch source (Phase 1E), carrying the
    invoice number and its NIT together so they can never desync into two
    separate lists. ``nit`` is already normalized to a plain digit string
    by the loader that produced this item."""

    invoice_number: str
    nit: str


@dataclass(frozen=True)
class BatchInvoiceItemResult:
    """Outcome of one item within a batch (Phase 1E). Mirrors
    :class:`InvoiceDownloadResult` but never carries a raw invoice number
    (masked only) and adds the NIT used for folder routing. Never carries
    any other Excel column's value (Regla 6)."""

    invoice_number_masked: str
    nit: str
    status: str
    pdf_path: str | None
    elapsed_seconds: float
    error_code: str | None
    error_message_safe: str | None


class GoState:
    """Closed vocabulary of GO/Indigo screen states relevant to this engine.

    Phase 1A performs no aggressive state recovery (mission Step 4): if the
    engine detects a state it does not expect for the step it is on, it
    returns a safe error instead of guessing or attempting an alternate
    recovery path.
    """

    MIS_FAVORITOS = "MIS_FAVORITOS"
    TRAZABILIDAD_EMPTY = "TRAZABILIDAD_EMPTY"
    TRAZABILIDAD_RESULT = "TRAZABILIDAD_RESULT"
    REPORT_VIEWER = "REPORT_VIEWER"
    EXPORT_MENU = "EXPORT_MENU"
    PDF_OPTIONS = "PDF_OPTIONS"
    SAVE_AS = "SAVE_AS"
    FINAL_EXPORT_DIALOG = "FINAL_EXPORT_DIALOG"
    UNKNOWN = "UNKNOWN"

    ALL: tuple[str, ...] = (
        MIS_FAVORITOS,
        TRAZABILIDAD_EMPTY,
        TRAZABILIDAD_RESULT,
        REPORT_VIEWER,
        EXPORT_MENU,
        PDF_OPTIONS,
        SAVE_AS,
        FINAL_EXPORT_DIALOG,
        UNKNOWN,
    )
