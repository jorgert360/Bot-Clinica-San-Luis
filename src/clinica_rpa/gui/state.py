"""GUI state constants and pure formatting/mapping helpers (Phase 1D).

No Tk imports here and no calls into the automation engine -- this module
is plain, independently testable logic used by :mod:`clinica_rpa.gui.app`.
"""

from __future__ import annotations

from clinica_rpa.domain.errors import ErrorCode


class GuiState:
    """Closed vocabulary of GUI states.

    ``process_invoice`` exposes no per-stage callback (Phase 1D explicitly
    keeps the validated engine unchanged), so only a subset of these are
    ever actually reached by the current app: IDLE, PREPARING (briefly,
    right after the button click, before the worker thread starts),
    QUERYING_GO (the whole duration of the blocking call -- shown as one
    "processing" state, not resolved into the finer sub-states below),
    COMPLETED, and ERROR. The remaining names are kept as a closed,
    documented vocabulary for a future version that does add granular
    progress, so the UI layer does not have to invent new tokens later.
    """

    IDLE = "IDLE"
    PREPARING = "PREPARING"
    QUERYING_GO = "QUERYING_GO"
    OPENING_DOCUMENT = "OPENING_DOCUMENT"
    OPENING_VIEWER = "OPENING_VIEWER"
    EXPORTING = "EXPORTING"
    SAVING = "SAVING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


# Status line text/color per state -- only the states this app version
# actually reaches (see GuiState docstring) need real copy; the rest fall
# back to a generic "Procesando..." via STATUS_LABELS.get(...).
STATUS_LABELS: dict[str, str] = {
    GuiState.IDLE: "Listo",
    GuiState.PREPARING: "Preparando...",
    GuiState.QUERYING_GO: "Procesando...",
    GuiState.COMPLETED: "Completado",
    GuiState.ERROR: "Error",
}

STATUS_COLORS: dict[str, str] = {
    GuiState.IDLE: "#555555",
    GuiState.PREPARING: "#b58900",
    GuiState.QUERYING_GO: "#b58900",
    GuiState.COMPLETED: "#1a7f37",
    GuiState.ERROR: "#c0392b",
}

DEFAULT_STATUS_COLOR = "#b58900"

PROCESSING_HINT = (
    "Consultando factura en GO. Esta operacion puede tardar aproximadamente 1 minuto."
)
BEFORE_START_HINT = "Verifique que GO este abierto, autenticado y visible."
WHILE_PROCESSING_HINT = "No use la sesion de GO mientras el robot esta procesando."
CLOSE_WHILE_BUSY_MESSAGE = "Hay una factura en procesamiento. Espere a que finalice."

# GUI-safe error labels (Regla 6: never show raw tracebacks / clinical
# content). Keys are ErrorCode string constants; "UNEXPECTED" covers the
# defensive case where process_invoice itself raised instead of returning
# a classified InvoiceDownloadResult.
ERROR_LABELS: dict[str, str] = {
    ErrorCode.GO_NOT_RUNNING: "GO no encontrado",
    ErrorCode.GO_NOT_AUTHENTICATED: "GO no esta autenticado",
    ErrorCode.GO_STATE_UNKNOWN: "Estado de GO desconocido",
    ErrorCode.TRAZABILIDAD_OPEN_FAILED: "No se pudo abrir Trazabilidad de Factura",
    ErrorCode.INVOICE_INPUT_NOT_FOUND: "Error inesperado",
    ErrorCode.INVOICE_WRITE_FAILED: "Error inesperado",
    ErrorCode.INVOICE_SEARCH_FAILED: "Timeout de GO",
    ErrorCode.INVOICE_NOT_FOUND: "Factura no encontrada",
    ErrorCode.DOCUMENT_ORIGIN_NOT_FOUND: "Documento origen no encontrado",
    ErrorCode.REPORT_VIEWER_TIMEOUT: "Visor no disponible",
    ErrorCode.EXPORT_ARROW_NOT_FOUND: "Error al exportar PDF",
    ErrorCode.PDF_FILE_NOT_FOUND: "Error al exportar PDF",
    ErrorCode.PDF_OPTIONS_TIMEOUT: "Error al exportar PDF",
    ErrorCode.SAVE_DIALOG_TIMEOUT: "Error al guardar",
    ErrorCode.FILE_NOT_CREATED: "Error al guardar",
    ErrorCode.PDF_INVALID: "PDF invalido",
    ErrorCode.FINAL_DIALOG_TIMEOUT: "Error al guardar",
    ErrorCode.UI_ACTION_AMBIGUOUS: "Error inesperado",
}
UNEXPECTED_ERROR_CODE = "UNEXPECTED"
DEFAULT_ERROR_LABEL = "Error inesperado"


def error_label_for(error_code: str | None) -> str:
    """Map an ``ErrorCode`` string (or None/unknown) to a safe UI label."""
    if not error_code:
        return DEFAULT_ERROR_LABEL
    return ERROR_LABELS.get(error_code, DEFAULT_ERROR_LABEL)


def format_elapsed(seconds: float) -> str:
    """Format a duration as ``mm:ss`` (e.g. ``01:43``)."""
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def format_size(size_bytes: int | None) -> str:
    """Human-readable file size in KB or MB. ``None`` -> ``"N/D"``."""
    if size_bytes is None:
        return "N/D"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def format_pages(pages: int | None) -> str:
    """``pypdf`` is optional and often unavailable -- a missing page count
    is expected, not an error."""
    return "N/D" if pages is None else str(pages)


def mask_invoice_for_log(value: str) -> str:
    """Mask an invoice number for the GUI's own local log file (Regla 6).

    Deliberately re-implemented here (not imported from
    ``clinica_rpa.automation.trazabilidad``) so the ``gui`` package never
    depends on the ``automation`` package -- masking is a privacy/format
    convention, not automation logic. Shows only the first 3 and last 2
    characters, mirroring the engine's own convention.
    """
    if len(value) <= 5:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 5)}{value[-2:]}"
