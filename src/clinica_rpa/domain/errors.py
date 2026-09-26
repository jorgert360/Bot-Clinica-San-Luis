"""Centralized error vocabulary for the reusable invoice-download engine.

Every automation/service failure surfaces as one of the closed error codes
below, carried by :class:`ClinicaRpaError`. Callers (ultimately
``services.invoice_download_service.process_invoice``) catch this single
exception type and translate it into an
:class:`clinica_rpa.domain.models.InvoiceDownloadResult` -- the caller never
sees a raw pywinauto/COM exception, a raw invoice number, or clinical data in
``error_message_safe`` (Regla 6: full stacktraces go to the technical log via
loguru only, never into the value returned to callers).

This vocabulary intentionally mirrors the PoC scripts' own documented
Regla-14 discipline (never invent an overlapping category, never guess a
result) while staying deliberately smaller/closed, per this phase's own
mission: several PoC-only sub-codes (e.g. DOCUMENT_ORIGIN_NO_EFFECT,
REPORT_VIEWER_UNVERIFIED, FILE_EMPTY/FILE_NOT_STABLE, the various
"*_AMBIGUOUS"/"*_NOT_EMPTY" dialog-structure codes) are intentionally folded
into the nearest closed-vocabulary code below -- every such mapping decision
is documented at its call site in the ``automation``/``services`` modules
that raise it.
"""

from __future__ import annotations


class ErrorCode:
    """Closed vocabulary of error codes for the invoice-download engine.

    Plain string constants (not an :class:`enum.Enum`) so the codes can be
    compared/serialized as plain strings anywhere (log lines, CLI output,
    ``InvoiceDownloadResult.error_code``) without an extra ``.value`` access.
    """

    GO_NOT_RUNNING = "GO_NOT_RUNNING"
    GO_NOT_AUTHENTICATED = "GO_NOT_AUTHENTICATED"
    GO_STATE_UNKNOWN = "GO_STATE_UNKNOWN"
    TRAZABILIDAD_OPEN_FAILED = "TRAZABILIDAD_OPEN_FAILED"
    INVOICE_INPUT_NOT_FOUND = "INVOICE_INPUT_NOT_FOUND"
    INVOICE_WRITE_FAILED = "INVOICE_WRITE_FAILED"
    INVOICE_SEARCH_FAILED = "INVOICE_SEARCH_FAILED"
    INVOICE_NOT_FOUND = "INVOICE_NOT_FOUND"
    DOCUMENT_ORIGIN_NOT_FOUND = "DOCUMENT_ORIGIN_NOT_FOUND"
    REPORT_VIEWER_TIMEOUT = "REPORT_VIEWER_TIMEOUT"
    EXPORT_ARROW_NOT_FOUND = "EXPORT_ARROW_NOT_FOUND"
    PDF_FILE_NOT_FOUND = "PDF_FILE_NOT_FOUND"
    PDF_OPTIONS_TIMEOUT = "PDF_OPTIONS_TIMEOUT"
    SAVE_DIALOG_TIMEOUT = "SAVE_DIALOG_TIMEOUT"
    FILE_NOT_CREATED = "FILE_NOT_CREATED"
    PDF_INVALID = "PDF_INVALID"
    FINAL_DIALOG_TIMEOUT = "FINAL_DIALOG_TIMEOUT"
    # The one case an unexpected exception/timeout right after a mutating
    # action must surface as (Regla 2 of the mission brief): the action may
    # have already succeeded server-side despite a client-side failure, so a
    # retry would be dangerous -- this code means "abort, do not guess,
    # never attempt a second mechanism for that same action".
    UI_ACTION_AMBIGUOUS = "UI_ACTION_AMBIGUOUS"
    # Phase 1E.1 (batch, between-items): the "Deshacer" button on the
    # Trazabilidad result screen could not be resolved or verified before
    # clicking -- the batch service must ABORT the whole batch on this
    # code, never guess or keep clicking (spec: "si el estado de GO queda
    # desconocido, ABORTAR, no continuar haciendo clicks").
    RESET_SCREEN_FAILED = "RESET_SCREEN_FAILED"

    ALL: tuple[str, ...] = (
        GO_NOT_RUNNING,
        GO_NOT_AUTHENTICATED,
        GO_STATE_UNKNOWN,
        TRAZABILIDAD_OPEN_FAILED,
        INVOICE_INPUT_NOT_FOUND,
        INVOICE_WRITE_FAILED,
        INVOICE_SEARCH_FAILED,
        INVOICE_NOT_FOUND,
        DOCUMENT_ORIGIN_NOT_FOUND,
        REPORT_VIEWER_TIMEOUT,
        EXPORT_ARROW_NOT_FOUND,
        PDF_FILE_NOT_FOUND,
        PDF_OPTIONS_TIMEOUT,
        SAVE_DIALOG_TIMEOUT,
        FILE_NOT_CREATED,
        PDF_INVALID,
        FINAL_DIALOG_TIMEOUT,
        UI_ACTION_AMBIGUOUS,
        RESET_SCREEN_FAILED,
    )


class ClinicaRpaError(Exception):
    """Base exception for every classified failure in the engine.

    Carries ``error_code`` (one of :class:`ErrorCode`'s constants) and
    ``message_safe`` -- a human-readable message that is guaranteed, by
    construction at every raise site in this codebase, to never contain a
    raw invoice number or any clinical/patient data (Regla 6). Full
    exception detail (including the original exception, if any) must be
    logged via loguru by the raiser BEFORE raising this; this exception's
    own message is the only thing that reaches
    :class:`clinica_rpa.domain.models.InvoiceDownloadResult`.
    """

    def __init__(self, error_code: str, message_safe: str) -> None:
        super().__init__(message_safe)
        self.error_code = error_code
        self.message_safe = message_safe

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"ClinicaRpaError(error_code={self.error_code!r}, message_safe={self.message_safe!r})"
