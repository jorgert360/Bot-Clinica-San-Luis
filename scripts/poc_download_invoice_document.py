"""F0.5 -- Descarga controlada del documento origen de una factura en GO/Indigo.

Pasos previos ya probados en vivo: F0.3B abrio "Trazabilidad de Factura",
F0.3C identifico el campo de factura (automation_id="INDbteInvoiceNumber"),
F0.4A escribio una factura de prueba (FHC000000) en ese campo, y F0.4C
confirmo que presionar Enter dispara la busqueda real. El usuario confirmo
manualmente, con capturas reales, el flujo COMPLETO para descargar el
documento origen de una factura ya cargada:

  1. Clic en "Factura - FHC000000" (el valor junto a "Documento Origen").
  2. Espera a que se abra "Visor de Reportes".
  3. En su barra de herramientas, clic en "Export Document...".
  4. Aparece el dialogo "Opciones de Exportacion PDF".
  5. Clic en "Aceptar" (opciones por defecto, sin tocar nada mas).
  6. Aparece el dialogo estandar de Windows "Guardar como".
  7. Se guarda el PDF.
  8. Aparece el dialogo "Exportar" -- "Desea abrir este archivo?" (Si/No).
  9. Clic en "No".
  10. Ciclo de descarga completo.

El usuario autorizo explicitamente ejecutar este flujo COMPLETO exactamente
UNA VEZ, de principio a fin, para la misma factura de prueba autorizada
(FHC000000), produciendo un PDF real en runtime/downloads/poc/, y luego
detenerse por completo -- sin segunda factura, sin segunda descarga, sin
"Consulta historias", sin procesamiento por lotes, sin tipificacion.

DISCIPLINA DE UNA SOLA INTERACCION POR PASO (mandatorio, todo el archivo):
cada una de estas es una accion unica, aislada, sin reintento, con
EXACTAMENTE UN call site en execute(): (a) clic en el valor de "Documento
Origen" (PASO 3), (b) apertura exclusiva de la flecha del split button y
seleccion exacta de "PDF File" (PASO 6), (c) clic en "Aceptar" del
dialogo de opciones PDF (PASO 7), (d)
escribir la ruta y clic en "Guardar" en el dialogo Guardar como
(write_save_path_once/click_save_button_once, PASO 8), (e) clic en "No" en
el dialogo final "Desea abrir este archivo?" (click_final_no_once, PASO 10).
Si cualquier mecanismo de una sola accion lanza, o si una ventana/dialogo
posterior requerido no aparece dentro de su plazo, el flujo ABORTA de
inmediato con el codigo especifico del vocabulario de la mision (ver
RESULT_* mas abajo) -- nunca se reintenta esa accion, nunca se improvisa un
camino de UI alternativo, nunca se cae a un mecanismo no autorizado
explicitamente para ese paso.

NUNCA, bajo ningun camino de codigo, se hace clic en "Si"/"Yes" del dialogo
final -- SOLO "No", una unica vez (ver find_no_button_candidates_live(),
que usa comparacion de IGUALDAD EXACTA normalizada, nunca coincidencia de
subcadena, precisamente para que esto sea imposible por construccion).

NUNCA se modifica ninguna opcion del dialogo "Opciones de Exportacion PDF"
-- el codigo de ese paso solo LEE (sanity-check informativo, best-effort) y
hace clic en "Aceptar"; ningun set_text()/toggle()/select() existe alli.

Regla 1 (03_CLAUDE_RULES.md): GO, el control de valor de "Documento Origen"
y cada dialogo posterior se resuelven en vivo cada corrida -- nunca un
PID/handle recordado de una sesion previa. Si emerge un selector estructural
estable para el control de valor de "Documento Origen", se guarda en
runtime/state/trazabilidad_post_search_selectors.json (ver
save_post_search_selectors()) -- nunca se persiste un handle crudo como si
fuera un selector estable, y nunca se persiste el numero de factura real.

Regla 5: el metodo se elige mediante inspeccion de solo lectura ANTES de cada
accion. Nunca se intenta un segundo metodo despues de una excepcion ambigua.
Para la flecha custom-drawn del split button, la coordenada es relativa al
rectangulo vivo del control y exige primero MOVE-ONLY + confirmacion humana.

Regla 6: nunca se loguea/persiste dato de paciente, numero de documento,
diagnostico, direccion, telefono, email, ni el contenido real del PDF --
solo evidencia estructural/tecnica. El numero de factura se enmascara en
cualquier salida persistida (reutilizando _mask_invoice de poc_write_invoice.py).

Regla 8: funciones pequenas, con nombre claro, una por PASO numerado,
llamadas en secuencia estricta desde execute() (fail-fast: ningun paso se
intenta si el anterior no tuvo exito limpio).

Regla 12: ninguna mutacion de ventana/proceso mas alla de los 5 clics/
interacciones explicitamente autorizados. Nunca se llama
SetForegroundWindow/ShowWindow/activate/restore/maximize sobre GO ni sobre
ningun dialogo -- toda espera de ventana es deteccion pasiva de solo
lectura (enumeracion de ventanas top-level + comparacion de titulo/clase).
Los dialogos se cierran a si mismos como consecuencia NATURAL de los clics
autorizados (p.ej. clic en "Aceptar" cierra el dialogo de opciones) -- eso
es distinto de, y nunca se confunde con, cerrar una ventana explicitamente.

Regla 14: cualquier estado ambiguo/inesperado aborta de inmediato con el
codigo especifico -- nunca se adivina, nunca se mejora la situacion, nunca
se cae a un camino de UI distinto del explicitamente autorizado para ese
paso.

Espera de archivo (PASO 9, instruccion explicita de la mision): tras el
clic en "Guardar", NUNCA se hace polling UIA pesado sobre GO/el dialogo --
se hace polling directo del SISTEMA DE ARCHIVOS (pathlib.Path.exists() +
.stat().st_size) cada ~500ms hasta 30s, considerando el archivo "estable"
solo tras 3 lecturas consecutivas de tamano identico. Esta es una disciplina
deliberadamente distinta y mas liviana que el polling UIA de F0.4B/F0.4C.

DOWNLOAD_COMPLETED exige unicamente validacion estructural: el archivo
existe, es estable/no vacio, tiene extension .pdf y comienza por b"%PDF".
pypdf es OPCIONAL/best-effort: si esta disponible y logra abrir el archivo,
su conteo de paginas queda registrado como informacion adicional, pero
nunca bloquea el resultado salvo que pypdf logre parsear el archivo y
reporte explicitamente 0 paginas.

Uso:
    python scripts/poc_download_invoice_document.py --invoice FHC000000

IMPORTANTE: este script NO se ejecuta como parte de esta tarea (ver
coordinacion) -- solo compile-check + revision de seguridad exhaustiva. La
ejecucion real, unica, en vivo, la realiza el usuario con supervision
directa.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from clinica_rpa.infrastructure.logging_config import setup_logging  # noqa: E402
from loguru import logger  # noqa: E402

from diagnose_go_windows import (  # noqa: E402
    TARGET_PROCESS_NAME,
    WindowDiagnostic,
    WindowNotFoundError,
    _classify_and_log,
    enumerate_all_windows,
    find_target_process_ids,
)
from inspect_go import ControlInfo, walk_tree  # noqa: E402
from inspect_trazabilidad_form import (  # noqa: E402
    TrazabilidadNotActiveError,
    _control_info_rect,
    _is_right_and_aligned,
    _matches_any,
    _normalize_for_match,
    validate_screen_active,
)
from poc_move_to_trazabilidad import (  # noqa: E402
    GoAmbiguousError,
    GoNotForegroundError,
    animate_cursor_to,
    find_authenticated_go_window,
    verify_go_foreground,
)
from poc_search_invoice import (  # noqa: E402
    FIELD_LABEL_VARIANTS,
    RESULT_GO_NOT_FOREGROUND,
    RESULT_TRAZABILIDAD_NOT_ACTIVE,
    FieldSnapshot,
    _rect_from_live_element,
    _scan_go_controls,
    _snapshot_target_windows,
    _supports_invoke_pattern,
    read_field_states,
)
from poc_write_invoice import (  # noqa: E402
    TRAZABILIDAD_GATE_MAX_DEPTH,
    TRAZABILIDAD_GATE_TIMEOUT_SECONDS,
    InvalidInvoiceValueError,
    _mask_invoice,
    _validate_invoice_value,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Mission-specified defaults.
DEFAULT_REPORT_VIEWER_TIMEOUT_SECONDS = 20.0
DEFAULT_EXPORT_MENU_TIMEOUT_SECONDS = 10.0
DEFAULT_PDF_OPTIONS_TIMEOUT_SECONDS = 10.0
DEFAULT_FILE_WAIT_TIMEOUT_SECONDS = 30.0
FILE_WAIT_POLL_INTERVAL_SECONDS = 0.5
FILE_STABLE_READS_REQUIRED = 3

# Not given explicit numbers by the mission; documented choices, both fully
# overridable via CLI.
DEFAULT_SAVE_DIALOG_TIMEOUT_SECONDS = 15.0
DEFAULT_FINAL_DIALOG_TIMEOUT_SECONDS = 15.0

# PASO 3's own short "did the click do literally anything" check -- shorter
# than PASO 4's full Visor de Reportes wait, per the mission's own
# documented example range (5-8s). 7.0s chosen as the midpoint.
DEFAULT_DOCUMENTO_ORIGEN_NO_EFFECT_TIMEOUT_SECONDS = 7.0

# Light top-level-window poll interval (window-handle-set diffing +
# title/class heuristics only -- never a deep UIA tree walk per poll, per
# the mission's own explicit "no hacer polling profundo continuo" instruction).
DEFAULT_LIGHT_POLL_INTERVAL_SECONDS = 0.5

DOWNLOADS_DIR = Path(__file__).resolve().parent.parent / "runtime" / "downloads" / "poc"
SELECTORS_OUTPUT_PATH = Path("runtime") / "state" / "trazabilidad_post_search_selectors.json"
VIEWER_RECEIPT_PATH = Path("runtime") / "state" / "f05_viewer_receipt.json"
SCREENSHOT_EXPORT_MENU_PATH = Path("runtime") / "screenshots" / "f05_export_menu.bmp"
SCREENSHOT_PDF_OPTIONS_PATH = Path("runtime") / "screenshots" / "f05_pdf_options.bmp"
SCREENSHOT_SAVE_DIALOG_PATH = Path("runtime") / "screenshots" / "f05_save_dialog.bmp"
DEFAULT_LOG_PATH = Path("runtime") / "logs" / "f05_invoice_download.txt"

# Minimum count of the 16 result-detail fields (FIELD_LABEL_VARIANTS) that
# must still be populated for PASO 1 to consider the invoice result "still
# active" -- documented, conservative threshold (mission's own suggestion:
# "2-3 of the previously-empty fields being populated is sufficient").
MIN_ACTIVE_FIELDS_THRESHOLD = 3

DOCUMENTO_ORIGEN_LABEL_VARIANTS: tuple[str, ...] = ("documento origen",)
# Structural prefix match -- never the full "Factura - FHC000000" literal.
DOCUMENTO_ORIGEN_VALUE_PREFIX = "Factura - "
DOCUMENTO_ORIGEN_AUTOMATION_ID = "INDHleDocument"

REPORT_VIEWER_TITLE_HINTS: tuple[str, ...] = ("visor de reportes",)
# Real-world finding (F0.5, live GO session): "Visor de Reportes" opens as
# an MDI child element INSIDE GO's own top-level window
# (automation_id="FrmReportViewer"), not as a separate top-level OS window
# -- see find_mdi_child_live().
REPORT_VIEWER_AUTOMATION_ID_HINTS: tuple[str, ...] = ("FrmReportViewer",)
PDF_OPTIONS_TITLE_HINTS: tuple[str, ...] = (
    "opciones de exportacion pdf",
    "opciones de exportación pdf",
)
SAVE_DIALOG_TITLE_HINTS: tuple[str, ...] = ("guardar como", "save as")
SAVE_DIALOG_CLASS_HINT = "#32770"
SAVE_FILENAME_AUTOMATION_IDS: tuple[str, ...] = ("1001", "FileNameControlHost")
SAVE_FILENAME_NAME_VARIANTS: tuple[str, ...] = (
    "nombre de archivo",
    "nombre de archivo:",
    "file name",
    "file name:",
)
FINAL_DIALOG_TITLE_HINTS: tuple[str, ...] = ("exportar",)
FINAL_DIALOG_CONTENT_HINTS: tuple[str, ...] = ("desea abrir",)

EXPORT_BUTTON_STRONG_VARIANTS: tuple[str, ...] = ("export document", "exportar documento")
EXPORT_BUTTON_WEAK_VARIANTS: tuple[str, ...] = ("export", "exportar")
EXPORT_ARROW_NAME_VARIANTS: tuple[str, ...] = (
    "drop down",
    "dropdown",
    "open menu",
    "more options",
    "flecha",
)
PDF_FILE_MENU_NAME = "pdf file"

ACCEPT_BUTTON_STRONG_VARIANTS: tuple[str, ...] = ("aceptar",)
ACCEPT_BUTTON_WEAK_VARIANTS: tuple[str, ...] = ("accept", "ok")
SAVE_BUTTON_STRONG_VARIANTS: tuple[str, ...] = ("guardar",)
SAVE_BUTTON_WEAK_VARIANTS: tuple[str, ...] = ("save",)
REJECT_BUTTON_VARIANTS: tuple[str, ...] = ("cancelar", "cancel")

# Best-effort, non-blocking sanity-check keywords for the PDF options dialog
# (PASO 7) -- logged only, never gates the flow.
PDF_OPTIONS_SANITY_KEYWORDS: tuple[str, ...] = (
    "rango de paginas",
    "rango de páginas",
    "convertir imagenes a jpeg",
    "convertir imágenes a jpeg",
    "calidad de imagen",
    "pdf/a",
)

PDF_HEADER_BYTES = b"%PDF"

# Bounded budgets for the small, targeted live-element walks in this file
# (never a full untargeted tree walk of the whole GO window -- always
# scoped to a specific dialog/panel's own live subtree).
LIVE_SEARCH_MAX_DEPTH = 20
LIVE_SEARCH_TIMEOUT_SECONDS = 15.0

# Result vocabulary (Regla 14, mission Section 13 -- closed, exact names).
RESULT_INVOICE_RESULT_NOT_ACTIVE = "INVOICE_RESULT_NOT_ACTIVE"
RESULT_DOCUMENT_ORIGIN_NOT_FOUND = "DOCUMENT_ORIGIN_NOT_FOUND"
RESULT_DOCUMENT_ORIGIN_NO_EFFECT = "DOCUMENT_ORIGIN_NO_EFFECT"
RESULT_REPORT_VIEWER_TIMEOUT = "REPORT_VIEWER_TIMEOUT"
RESULT_REPORT_VIEWER_UNVERIFIED = "REPORT_VIEWER_UNVERIFIED"
RESULT_EXPORT_BUTTON_NOT_FOUND = "EXPORT_BUTTON_NOT_FOUND"
RESULT_EXPORT_ARROW_CONFIRMATION_REQUIRED = "EXPORT_ARROW_CONFIRMATION_REQUIRED"
RESULT_EXPORT_PDF_FILE_NOT_FOUND = "EXPORT_PDF_FILE_NOT_FOUND"
RESULT_PDF_OPTIONS_TIMEOUT = "PDF_OPTIONS_TIMEOUT"
RESULT_SAVE_DIALOG_TIMEOUT = "SAVE_DIALOG_TIMEOUT"
RESULT_FILE_NOT_CREATED = "FILE_NOT_CREATED"
RESULT_FILE_EMPTY = "FILE_EMPTY"
RESULT_FILE_NOT_STABLE = "FILE_NOT_STABLE"
RESULT_PDF_INVALID = "PDF_INVALID"
RESULT_FINAL_DIALOG_TIMEOUT = "FINAL_DIALOG_TIMEOUT"
RESULT_DOWNLOAD_COMPLETED = "DOWNLOAD_COMPLETED"
RESULT_REPORT_VIEWER_READY = "REPORT_VIEWER_READY"
RESULT_PDF_OPTIONS_READY = "PDF_OPTIONS_READY"
RESULT_SAVE_AS_SUBMITTED = "SAVE_AS_SUBMITTED"

STOP_AFTER_CHOICES = ("none", "report_viewer", "pdf_options", "save_as")
# Not part of the mission's closed vocabulary: reused sibling-script
# convention ("ABORTED (<classified_code>)") for a genuinely unenumerated
# failure inside an otherwise-successful step (e.g. a dialog opened, but its
# expected internal button could not be resolved). Flagged for coordinator.
RESULT_ABORTED_PREFIX = "ABORTED"


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass
class ReportState:
    """Accumulates whatever has been resolved so far. Fields default to
    'N/D' so an early abort still prints a complete, well-formed report
    instead of a partial/crashed one (Regla 14)."""

    doc_origen_identificado: str = "N/D"
    doc_origen_backend: str = "N/D"
    doc_origen_metodo: str = "N/D"
    doc_origen_accion_ejecutada: str = "NO"

    viewer_abierto: str = "N/D"
    viewer_handle: str = "N/D"
    viewer_class: str = "N/D"
    viewer_tiempo_apertura: str = "N/D"

    export_identificado: str = "N/D"
    export_backend: str = "N/D"
    export_metodo: str = "N/D"
    export_menu_abierto: str = "NO"
    export_pdf_file_identificado: str = "N/D"
    export_pdf_file_metodo: str = "N/D"

    pdf_options_detectado: str = "N/D"
    pdf_options_aceptar_ejecutado: str = "NO"

    save_detectado: str = "N/D"
    save_ruta_destino: str = "N/D"
    save_guardar_ejecutado: str = "NO"

    archivo_creado: str = "N/D"
    archivo_tamano_bytes: str = "N/D"
    archivo_estable: str = "N/D"
    archivo_pdf_header_valido: str = "N/D"
    archivo_paginas: str = "N/D"
    archivo_ruta: str = "N/D"

    final_dialog_detectado: str = "N/D"
    final_dialog_no_ejecutado: str = "NO"

    resultado: str = "N/D"


@dataclass
class FileWaitResult:
    appeared: bool
    stabilized: bool
    size_bytes: int
    elapsed_first_seen: float | None
    elapsed_stabilized: float | None


@dataclass
class ExportArrowTarget:
    """One preselected way to open only the split-button dropdown."""

    method: str
    element: object
    relative_coords: tuple[int, int] | None = None
    screen_point: tuple[int, int] | None = None


# --------------------------------------------------------------------------
# PASO 9: capture_window_bmp -- COPIED (not imported), same self-containment
# rationale already documented in every sibling script.
# --------------------------------------------------------------------------


def capture_window_bmp(handle: int, output_path: Path) -> Path:
    """Capture handle's current window rect as a .bmp file, pywin32-only.

    Best-effort: never raises on failure -- logs a warning and returns the
    intended path, so a screenshot problem never blocks the rest of the flow.
    """
    import win32con
    import win32gui
    import win32ui

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        left, top, right, bottom = win32gui.GetWindowRect(handle)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise ValueError(f"Rectangulo de ventana invalido para captura: {(left, top, right, bottom)}")

        hwnd_dc = win32gui.GetWindowDC(handle)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()

        save_bitmap = win32ui.CreateBitmap()
        save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(save_bitmap)

        save_dc.BitBlt((0, 0), (width, height), mfc_dc, (0, 0), win32con.SRCCOPY)
        save_bitmap.SaveBitmapFile(save_dc, str(output_path))

        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(handle, hwnd_dc)
        win32gui.DeleteObject(save_bitmap.GetHandle())

        logger.info("Captura de pantalla (BMP) guardada en {} ({}x{})", output_path, width, height)
    except Exception as exc:
        _classify_and_log(exc, f"fallo (no fatal) capturando pantalla BMP de ventana {handle}")

    return output_path


# --------------------------------------------------------------------------
# Small local helpers
# --------------------------------------------------------------------------


def _safe_str(getter) -> str:
    try:
        value = getter()
        return str(value) if value is not None else ""
    except Exception:
        return "?"


def _connect_uia(handle: int):
    """Connect a live UIA wrapper to `handle`. Raises on failure -- callers
    decide how to classify/abort."""
    from pywinauto import Desktop

    element = Desktop(backend="uia").window(handle=handle)
    element.wait("exists", timeout=5)
    return element


def _live_element_control_type(element) -> str:
    try:
        return (element.element_info.control_type or "").lower()
    except Exception:
        return ""


def _live_element_name(element) -> str:
    try:
        return element.element_info.name or ""
    except Exception:
        return ""


def _live_element_automation_id(element) -> str:
    try:
        return element.element_info.automation_id or ""
    except Exception:
        return ""


def _live_element_class_name(element) -> str:
    try:
        return element.element_info.class_name or ""
    except Exception:
        return ""


def _best_effort_tooltip_live(element) -> str:
    """Best-effort UIA HelpText read on a LIVE element. Never raises."""
    try:
        help_text = element.element_info.element.CurrentHelpText  # type: ignore[attr-defined]
        return str(help_text) if help_text else ""
    except Exception:
        return ""


def _supports_selection_item_pattern(element) -> bool:
    """Best-effort, read-only check for UIA SelectionItemPattern support.
    Mirrors _supports_invoke_pattern's discipline (poc_search_invoice.py) --
    reporting/logging only, never gates which method PASO 3 actually tries
    (that is always decided empirically, see click_documento_origen_once())."""
    try:
        return getattr(element, "iface_selection_item", None) is not None
    except Exception:
        return False


def _supports_expand_collapse_pattern(element) -> bool:
    """Return whether a live element exposes UIA ExpandCollapsePattern."""
    try:
        element.iface_expand_collapse.CurrentExpandCollapseState
        return True
    except Exception:
        return False


def _supports_value_pattern(element) -> bool:
    """Return whether a live element exposes UIA ValuePattern."""
    try:
        element.iface_value.CurrentValue
        return True
    except Exception:
        return False


def _is_visible_enabled(element) -> bool:
    try:
        return bool(element.is_visible()) and bool(element.is_enabled())
    except Exception:
        return False


def _live_element_process_id(element) -> int | None:
    try:
        return int(element.element_info.process_id)
    except Exception:
        return None


def choose_single_action_method(element) -> str:
    """Choose an action before mutating UI; never fall through after failure."""
    return "invoke_pattern" if _supports_invoke_pattern(element) else "click_input"


def activate_once(element, method: str) -> None:
    """Perform exactly one preselected activation attempt."""
    if method == "invoke_pattern":
        element.invoke()
        return
    if method == "click_input":
        element.click_input()
        return
    raise ValueError(f"Metodo de activacion no soportado: {method}")


# --------------------------------------------------------------------------
# Generic, read-only live-element recursive walk. Used by PASO 2 (Documento
# Origen value), PASO 5 (Export Document button), PASO 7/8/10 (dialog
# buttons). NEVER invokes/clicks anything itself -- it only reads
# control_type/name/automation_id/rectangle on live elements and collects
# matches; every actual interaction happens later, from a single dedicated
# call site in execute().
# --------------------------------------------------------------------------


def _walk_live(element, visit, max_depth: int, deadline: float, depth: int = 0) -> None:
    if deadline is not None and time.monotonic() > deadline:
        return
    visit(element, depth)
    if depth >= max_depth:
        return
    try:
        children = element.children()
    except Exception:
        return
    for child in children:
        if deadline is not None and time.monotonic() > deadline:
            return
        _walk_live(child, visit, max_depth, deadline, depth + 1)


# --------------------------------------------------------------------------
# PASO 1: validate the invoice result is still active (read-only, never
# re-searches/re-writes/re-presses Enter).
# --------------------------------------------------------------------------


def validate_invoice_result_active(
    controls: list[ControlInfo],
) -> tuple[bool, int]:
    """Read-only: count how many of the 16 result-detail fields
    (FIELD_LABEL_VARIANTS) are currently populated. Returns
    (active_enough, populated_count). Never re-searches/re-writes anything.
    """
    field_states = read_field_states(controls)
    populated_count = sum(1 for v in field_states.values() if v.populated)
    logger.info(
        "PASO 1: {} de {} campos de resultado siguen poblados (umbral minimo: {})",
        populated_count,
        len(FIELD_LABEL_VARIANTS),
        MIN_ACTIVE_FIELDS_THRESHOLD,
    )
    return populated_count >= MIN_ACTIVE_FIELDS_THRESHOLD, populated_count


# --------------------------------------------------------------------------
# PASO 2: identify "Documento Origen" + its value control (live element,
# needed so PASO 3 can invoke()/click_input() the SAME object).
# --------------------------------------------------------------------------


def find_documento_origen_label_candidates(controls: list[ControlInfo]) -> list[ControlInfo]:
    return [
        c for c in controls if _matches_any(f"{c.name} {c.automation_id}", DOCUMENTO_ORIGEN_LABEL_VARIANTS)
    ]


def find_documento_origen_value_live_candidates(
    window, label_rect, max_depth: int, timeout_seconds: float
) -> list:
    """Bounded, read-only recursive walk of LIVE elements looking for a
    control whose name starts with "Factura - " (structural prefix, never
    the full literal invoice number) AND is spatially associated with
    label_rect (_is_right_and_aligned -- covers both the classic
    label-then-value layout and this app's DataItem-row containment layout,
    see inspect_trazabilidad_form.py). Only ever READS name/rectangle here;
    never invokes/clicks anything -- that happens later, from PASO 3's
    single call site."""
    deadline = time.monotonic() + timeout_seconds
    results: list = []

    def _visit(element, depth):
        name = _live_element_name(element)
        if not name.startswith(DOCUMENTO_ORIGEN_VALUE_PREFIX):
            return
        rect = _rect_from_live_element(element)
        if rect is not None and _is_right_and_aligned(label_rect, rect):
            results.append(element)

    _walk_live(window, _visit, max_depth, deadline)
    return results


def save_post_search_selectors(
    element, output_path: Path = SELECTORS_OUTPUT_PATH
) -> Path:
    """Persist a structural (never handle-only) selector for the "Documento
    Origen" value control, schema mirroring F0.3C's trazabilidad_selectors.json.
    Never persists the invoice number itself. session_handle is explicitly
    labeled as session-only diagnostic data, never treated as a stable
    selector on its own (Regla 1)."""
    automation_id = _live_element_automation_id(element)
    class_name = _live_element_class_name(element)
    try:
        session_handle = element.handle or None
    except Exception:
        session_handle = None

    payload = {
        "documento_origen_value": {
            "backend": "uia",
            "automation_id": automation_id or None,
            "control_id": None,
            "class_name": class_name or None,
            "relation": "value_right_of_label_documento_origen",
            "session_handle": session_handle,
            "identified_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def _invoice_fingerprint(invoice: str) -> str:
    return hashlib.sha256(f"clinica-rpa:f05:{invoice}".encode("utf-8")).hexdigest()


def save_viewer_receipt(
    invoice: str,
    process_id: int,
    viewer_handle: int,
    output_path: Path = VIEWER_RECEIPT_PATH,
) -> Path:
    """Persist a session receipt without storing the invoice value."""
    import psutil

    payload = {
        "invoice_fingerprint": _invoice_fingerprint(invoice),
        "process_id": process_id,
        "process_create_time": psutil.Process(process_id).create_time(),
        "viewer_handle": viewer_handle,
        "viewer_automation_id": REPORT_VIEWER_AUTOMATION_ID_HINTS[0],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output_path


def viewer_receipt_matches(
    invoice: str,
    process_id: int,
    viewer_handle: int,
    receipt_path: Path = VIEWER_RECEIPT_PATH,
) -> bool:
    """Validate that a pre-existing viewer was opened by this F0.5 session."""
    import psutil

    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        return (
            payload.get("invoice_fingerprint") == _invoice_fingerprint(invoice)
            and int(payload.get("process_id")) == process_id
            and float(payload.get("process_create_time")) == psutil.Process(process_id).create_time()
            and int(payload.get("viewer_handle")) == viewer_handle
            and payload.get("viewer_automation_id") == REPORT_VIEWER_AUTOMATION_ID_HINTS[0]
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError, psutil.Error):
        return False


# --------------------------------------------------------------------------
# PASO 3: click Documento Origen's value, ONCE. Also PASO 6/7/8/10 share
# this same generic invoke-then-click_input cascade (each called from its
# own single, distinct call site in execute() -- see module docstring).
# --------------------------------------------------------------------------


def activate_preselected_once(element) -> str:
    """Select one method read-only, then perform exactly one UI action.

    An exception is ambiguous: the application may already have processed
    the action. Therefore this function aborts through its caller and NEVER
    tries a second mechanism after a failed mutation.
    """
    method = choose_single_action_method(element)
    activate_once(element, method)
    return method


# --------------------------------------------------------------------------
# Generic light window-wait helper -- reused for PASO 4 (Visor de Reportes),
# PASO 6 (PDF Options), PASO 8 (Guardar como), PASO 10 (Exportar). Each poll
# is ONE top-level window enumeration (via _snapshot_target_windows() /
# enumerate_all_windows(), both light, no deep UIA tree walk) -- never a
# continuous deep UIA scan of GO (mission's own explicit instruction).
# --------------------------------------------------------------------------


def _light_wait_for_window(
    window_source_fn,
    before_handles: set[int],
    predicate,
    timeout_seconds: float,
    poll_interval: float,
    require_new: bool = False,
) -> WindowDiagnostic | None:
    start = time.monotonic()
    while True:
        windows = window_source_fn()
        for w in windows:
            if (not require_new or w.handle not in before_handles) and predicate(w):
                return w
        if time.monotonic() - start > timeout_seconds:
            return None
        time.sleep(poll_interval)


def find_mdi_child_live(
    container, automation_id_hints: tuple[str, ...], title_hints: tuple[str, ...],
    max_depth: int, timeout_seconds: float,
):
    """Search GO's own live UIA tree for a Window-type descendant matching
    by automation_id (preferred, exact) or name (word-boundary-safe
    substring via _matches_any). Real-world finding (F0.5, live GO
    session): "Visor de Reportes" opens as an MDI CHILD element inside the
    SAME top-level GO handle (automation_id="FrmReportViewer"), not as a
    new top-level OS window -- exactly the same MDI pattern already
    observed for "Trazabilidad de Factura" itself in F0.3B/F0.3C. Detecting
    only NEW TOP-LEVEL windows (as PASO 3b/4 originally did) structurally
    cannot see this. Returns the live element or None -- read-only, never
    interacts with anything found."""
    deadline = time.monotonic() + timeout_seconds
    found: list = []

    def _visit(element, depth):
        ctype = _live_element_control_type(element)
        if "window" not in ctype:
            return
        aid = _live_element_automation_id(element)
        if aid and aid in automation_id_hints:
            found.append(element)
            return
        name = _live_element_name(element)
        if _matches_any(name, title_hints):
            found.append(element)

    _walk_live(container, _visit, max_depth, deadline)
    return found[0] if found else None


def _confirm_window_stable(
    window_source_fn, handle: int, predicate, checks: int = 2, interval: float = 1.0
) -> bool:
    """Light "still there + roughly same shape" confirmation: `checks`
    consecutive light top-level enumerations, `interval` apart, all still
    showing the same handle with a matching title/class. Deliberately NOT a
    full UIA control-tree stability check (mission's own explicit
    instruction: a light touch is sufficient here)."""
    for _ in range(checks):
        time.sleep(interval)
        windows = window_source_fn()
        still_present = any(w.handle == handle and predicate(w) for w in windows)
        if not still_present:
            return False
    return True


# --------------------------------------------------------------------------
# PASO 5: identify "Export Document..." inside the Visor de Reportes window.
# --------------------------------------------------------------------------


def find_export_button_candidates(
    viewer_window, max_depth: int, timeout_seconds: float
) -> tuple[list, list]:
    """Bounded, read-only live-tree walk of the Visor de Reportes window,
    collecting button-like elements whose name/automation_id/tooltip match
    the strong or weak Export variants. Returns (strong, weak) -- caller
    decides which set to use and how to handle ambiguity."""
    deadline = time.monotonic() + timeout_seconds
    strong: list = []
    weak: list = []

    def _visit(element, depth):
        ctype = _live_element_control_type(element)
        if "button" not in ctype and "menuitem" not in ctype and "custom" not in ctype:
            return
        name = _live_element_name(element)
        automation_id = _live_element_automation_id(element)
        tooltip = _best_effort_tooltip_live(element)
        haystack = f"{name} {automation_id} {tooltip}"
        if _matches_any(haystack, EXPORT_BUTTON_STRONG_VARIANTS):
            strong.append(element)
        elif _matches_any(haystack, EXPORT_BUTTON_WEAK_VARIANTS):
            weak.append(element)

    _walk_live(viewer_window, _visit, max_depth, deadline)
    return strong, weak


def _live_element_key(element) -> tuple:
    try:
        runtime_id = tuple(element.element_info.runtime_id or ())
    except Exception:
        runtime_id = ()
    rect = _rect_from_live_element(element)
    return (
        runtime_id,
        _live_element_control_type(element),
        _live_element_automation_id(element),
        rect.as_tuple() if rect else None,
    )


def _dedupe_live_elements(elements: list) -> list:
    unique: list = []
    seen: set[tuple] = set()
    for element in elements:
        key = _live_element_key(element)
        if key in seen:
            continue
        seen.add(key)
        unique.append(element)
    return unique


def resolve_export_arrow_target(viewer, export_element) -> ExportArrowTarget | None:
    """Resolve only the dropdown half of Export Document.

    Semantic ExpandCollapse is preferred. A separately exposed arrow button
    is next. The final fallback is a point relative to the right edge of the
    already identified split-button rectangle; callers must require a prior
    human move-only confirmation before clicking that point.
    """
    if _supports_expand_collapse_pattern(export_element):
        return ExportArrowTarget(method="expand_pattern", element=export_element)

    export_rect = _rect_from_live_element(export_element)
    if export_rect is None or export_rect.width < 16 or export_rect.height < 10:
        return None

    named: list = []
    geometric: list = []
    deadline = time.monotonic() + 5.0

    def _visit(element, depth):
        if element is export_element or not _is_visible_enabled(element):
            return
        ctype = _live_element_control_type(element)
        if not any(kind in ctype for kind in ("button", "splitbutton", "custom")):
            return
        rect = _rect_from_live_element(element)
        if rect is None or rect.width <= 0 or rect.height <= 0:
            return

        vertical_overlap = max(0, min(export_rect.bottom, rect.bottom) - max(export_rect.top, rect.top))
        min_height = max(1, min(export_rect.height, rect.height))
        adjacent_right = -2 <= rect.left - export_rect.right <= 6
        inside_right = (
            rect.left >= export_rect.left + int(export_rect.width * 0.65)
            and rect.right <= export_rect.right + 2
        )
        narrow = rect.width <= max(32, int(export_rect.width * 0.45))
        structurally_related = (
            (adjacent_right or inside_right)
            and narrow
            and vertical_overlap / min_height >= 0.70
        )
        if not structurally_related:
            return

        name_hint = f"{_live_element_name(element)} {_live_element_automation_id(element)} {_best_effort_tooltip_live(element)}"
        if _matches_any(name_hint, EXPORT_ARROW_NAME_VARIANTS):
            named.append(element)
        else:
            geometric.append(element)

    _walk_live(viewer, _visit, LIVE_SEARCH_MAX_DEPTH, deadline)
    named = _dedupe_live_elements(named)
    if len(named) == 1:
        return ExportArrowTarget(method="arrow_control", element=named[0])

    geometric = _dedupe_live_elements(geometric)
    if len(geometric) == 1:
        rect = _rect_from_live_element(geometric[0])
        if rect is not None:
            return ExportArrowTarget(
                method="relative_confirmed_control",
                element=geometric[0],
                screen_point=(rect.left + rect.width // 2, rect.top + rect.height // 2),
            )

    relative_x = max(1, export_rect.width - min(7, max(3, export_rect.width // 8)))
    relative_y = max(1, export_rect.height // 2)
    return ExportArrowTarget(
        method="relative_export_rect",
        element=export_element,
        relative_coords=(relative_x, relative_y),
        screen_point=(export_rect.left + relative_x, export_rect.top + relative_y),
    )


def open_export_dropdown_once(target: ExportArrowTarget) -> None:
    """Open the dropdown with exactly one preselected UI action."""
    if target.method == "expand_pattern":
        target.element.expand()
        return
    if target.method in {"arrow_control", "relative_confirmed_control"}:
        target.element.click_input()
        return
    if target.method == "relative_export_rect" and target.relative_coords is not None:
        target.element.click_input(coords=target.relative_coords)
        return
    raise ValueError(f"Target de flecha no soportado: {target.method}")


def snapshot_visible_uia_root_keys(expected_pid: int) -> set[tuple]:
    from pywinauto import Desktop

    try:
        roots = Desktop(backend="uia").windows()
    except Exception:
        return set()
    return {
        _live_element_key(root)
        for root in roots
        if _live_element_process_id(root) == expected_pid and _is_visible_enabled(root)
    }


def find_pdf_file_menu_candidates(
    expected_pid: int,
    viewer,
    timeout_seconds: float,
    before_root_keys: set[tuple] | None = None,
) -> tuple[list, list]:
    """Find exact visible/enabled `PDF File` menu items in popup roots.

    Only UIA top-level roots owned by the authenticated GO PID are scanned,
    plus the already known viewer as a fallback for in-tree popups. Names of
    unrelated controls are never persisted.
    """
    from pywinauto import Desktop

    start = time.monotonic()
    delay = 0.25
    last_popup_roots: list = []
    while time.monotonic() - start <= timeout_seconds:
        try:
            desktop_roots = Desktop(backend="uia").windows()
        except Exception:
            desktop_roots = []

        popup_roots: list = []
        for root in desktop_roots:
            if _live_element_process_id(root) != expected_pid:
                continue
            ctype = _live_element_control_type(root)
            class_name = _live_element_class_name(root).casefold()
            rect = _rect_from_live_element(root)
            popup_like = (
                "menu" in ctype
                or "popup" in class_name
                or "dropdown" in class_name
                or class_name == "#32768"
                or (rect is not None and 0 < rect.width <= 700 and 0 < rect.height <= 900)
            )
            is_new_root = before_root_keys is None or _live_element_key(root) not in before_root_keys
            if popup_like and is_new_root and _is_visible_enabled(root):
                popup_roots.append(root)

        roots = _dedupe_live_elements([*popup_roots, viewer])
        matches: list = []
        deadline = time.monotonic() + min(3.0, max(0.25, timeout_seconds))

        def _visit(element, depth):
            if not _is_visible_enabled(element):
                return
            if (
                _live_element_control_type(element) == "menuitem"
                and _normalize_for_match(_live_element_name(element)) == PDF_FILE_MENU_NAME
            ):
                matches.append(element)

        for root in roots:
            _walk_live(root, _visit, 10, deadline)
            if time.monotonic() > deadline:
                break

        matches = _dedupe_live_elements(matches)
        if matches:
            return matches, popup_roots
        last_popup_roots = popup_roots
        time.sleep(delay)
        delay = min(delay + 0.25, 1.5)

    return [], last_popup_roots


def capture_small_popup_bmp(popup_roots: list, output_path: Path) -> None:
    """Capture only a small popup, never the clinical report viewer."""
    candidates: list[tuple[int, int]] = []
    for root in popup_roots:
        rect = _rect_from_live_element(root)
        try:
            handle = int(root.handle)
        except Exception:
            handle = 0
        if handle and rect and 0 < rect.width <= 700 and 0 < rect.height <= 900:
            candidates.append((rect.width * rect.height, handle))
    if candidates:
        _, handle = min(candidates)
        capture_window_bmp(handle, output_path)


# --------------------------------------------------------------------------
# Generic dialog-button finder (Aceptar / Guardar), with an explicit reject
# list so a strong/weak accept match can never coincide with Cancelar.
# --------------------------------------------------------------------------


def find_button_candidates_live(
    container, accept_variants: tuple[str, ...], reject_variants: tuple[str, ...], max_depth: int, timeout_seconds: float
) -> list:
    deadline = time.monotonic() + timeout_seconds
    results: list = []
    accepted = {_normalize_for_match(v).replace("&", "").rstrip(".") for v in accept_variants}
    rejected = {_normalize_for_match(v).replace("&", "").rstrip(".") for v in reject_variants}

    def _visit(element, depth):
        ctype = _live_element_control_type(element)
        if "button" not in ctype and "custom" not in ctype:
            return
        if not _is_visible_enabled(element):
            return
        name = _normalize_for_match(_live_element_name(element)).replace("&", "").rstrip(".")
        if name in accepted and name not in rejected:
            results.append(element)

    _walk_live(container, _visit, max_depth, deadline)
    return results


def find_no_button_candidates_live(container, max_depth: int, timeout_seconds: float) -> list:
    """STRICT exact-token match: the normalized name must equal EXACTLY
    "no". Deliberately NEVER uses _matches_any()/substring matching here --
    _matches_any only checks that no word-character immediately PRECEDES a
    match, it does NOT require the match to end at a word boundary, so a
    substring check could spuriously match inside an unrelated word. This
    is the single most safety-critical control in this entire script: a
    wrong match could leave the downloaded PDF unconfirmed-closed or, far
    worse, could be confused with "Si"/"Sí". Exact normalized-string
    equality is the only mechanism used, by design."""
    deadline = time.monotonic() + timeout_seconds
    results: list = []

    def _visit(element, depth):
        ctype = _live_element_control_type(element)
        if "button" not in ctype and "custom" not in ctype:
            return
        normalized = _normalize_for_match(_live_element_name(element)).replace("&", "").rstrip(".")
        if normalized == "no" and _is_visible_enabled(element):
            results.append(element)

    _walk_live(container, _visit, max_depth, deadline)
    return results


def find_edit_candidates_live(container, max_depth: int, timeout_seconds: float) -> list:
    deadline = time.monotonic() + timeout_seconds
    results: list = []

    def _visit(element, depth):
        ctype = _live_element_control_type(element)
        if "edit" in ctype:
            results.append(element)

    _walk_live(container, _visit, max_depth, deadline)
    return results


def find_filename_edit_candidates_live(container, max_depth: int, timeout_seconds: float) -> list:
    """Resolve only the standard Save As filename edit, never by width."""
    edits = find_edit_candidates_live(container, max_depth, timeout_seconds)
    stable: list = []
    normalized_names = {_normalize_for_match(v) for v in SAVE_FILENAME_NAME_VARIANTS}
    for element in edits:
        if not _is_visible_enabled(element):
            continue
        automation_id = _live_element_automation_id(element)
        name = _normalize_for_match(_live_element_name(element))
        if automation_id in SAVE_FILENAME_AUTOMATION_IDS or name in normalized_names:
            stable.append(element)
    return _dedupe_live_elements(stable)


# --------------------------------------------------------------------------
# PASO 9: filesystem-first file-creation wait -- deliberately NOT a UIA
# polling loop (mission's explicit instruction; note the total absence of
# walk_tree/_scan_go_controls/any UIA read in this function).
# --------------------------------------------------------------------------


def wait_for_file_stable(
    path: Path, poll_interval: float, timeout_seconds: float, stable_reads_required: int
) -> FileWaitResult:
    start = time.monotonic()
    last_size: int | None = None
    stable_streak = 0
    elapsed_first_seen: float | None = None
    elapsed_stabilized: float | None = None
    appeared = False
    final_size = 0

    while True:
        elapsed = time.monotonic() - start
        if path.exists():
            appeared = True
            if elapsed_first_seen is None:
                elapsed_first_seen = elapsed
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            final_size = size if size >= 0 else final_size
            if size == last_size:
                stable_streak += 1
            else:
                stable_streak = 1
            last_size = size
            if stable_streak >= stable_reads_required:
                elapsed_stabilized = elapsed
                return FileWaitResult(
                    appeared=True,
                    stabilized=True,
                    size_bytes=final_size,
                    elapsed_first_seen=elapsed_first_seen,
                    elapsed_stabilized=elapsed_stabilized,
                )

        if elapsed > timeout_seconds:
            return FileWaitResult(
                appeared=appeared,
                stabilized=False,
                size_bytes=final_size,
                elapsed_first_seen=elapsed_first_seen,
                elapsed_stabilized=None,
            )

        time.sleep(poll_interval)


# --------------------------------------------------------------------------
# PASO 11: PDF validation -- header plus required pypdf page count.
# --------------------------------------------------------------------------


def validate_pdf_file(path: Path) -> tuple[bool, bool, str]:
    """Return (header_valid, pages_valid, page_count_str).

    F0.5 succeeds on structural validation alone: file exists, size > 0,
    ".pdf" extension, header starts with b"%PDF" (header_valid). pypdf is
    an OPTIONAL, best-effort dependency and is NOT installed for this PoC
    (see instruction not to install it yet); when unavailable or unable to
    parse, pages_valid stays informational only and never gates success --
    the caller (PASO 11) must key DOWNLOAD_COMPLETED off header_valid alone.
    File contents are never logged or sent externally.
    """
    if not path.exists():
        return False, False, "N/D"
    try:
        size = path.stat().st_size
    except OSError:
        return False, False, "N/D"
    if size <= 0:
        return False, False, "N/D"
    if path.suffix.lower() != ".pdf":
        return False, False, "N/D"

    try:
        with path.open("rb") as fh:
            prefix = fh.read(5)
    except OSError:
        return False, False, "N/D"

    header_valid = prefix[:4] == PDF_HEADER_BYTES
    if not header_valid:
        return False, False, "N/D"

    try:
        import pypdf
    except ImportError:
        logger.info(
            "pypdf no esta instalado; se omite el conteo de paginas (opcional, "
            "no bloquea el resultado)."
        )
        return True, True, "N/D"

    try:
        reader = pypdf.PdfReader(str(path))
        page_count = len(reader.pages)
    except Exception as exc:
        logger.warning(
            "pypdf no pudo leer el archivo local (informativo, no bloquea "
            "header_valid): {}",
            exc,
        )
        return True, True, "N/D"

    return True, page_count >= 1, str(page_count)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def render_report(state: ReportState) -> str:
    lines = [
        "DOCUMENTO ORIGEN:",
        f"- identificado: {state.doc_origen_identificado}",
        f"- backend: {state.doc_origen_backend}",
        f"- metodo usado: {state.doc_origen_metodo}",
        f"- accion ejecutada: {state.doc_origen_accion_ejecutada}",
        "",
        "VISOR DE REPORTES:",
        f"- abierto: {state.viewer_abierto}",
        f"- handle: {state.viewer_handle}",
        f"- class: {state.viewer_class}",
        f"- tiempo apertura: {state.viewer_tiempo_apertura}",
        "",
        "EXPORT DOCUMENT:",
        f"- identificado: {state.export_identificado}",
        f"- backend: {state.export_backend}",
        f"- metodo flecha: {state.export_metodo}",
        f"- menu abierto: {state.export_menu_abierto}",
        f"- PDF File identificado: {state.export_pdf_file_identificado}",
        f"- metodo PDF File: {state.export_pdf_file_metodo}",
        "",
        "PDF OPTIONS:",
        f"- detectado: {state.pdf_options_detectado}",
        f"- Aceptar ejecutado: {state.pdf_options_aceptar_ejecutado}",
        "",
        "SAVE AS:",
        f"- detectado: {state.save_detectado}",
        f"- ruta destino: {state.save_ruta_destino}",
        f"- Guardar ejecutado: {state.save_guardar_ejecutado}",
        "",
        "ARCHIVO:",
        f"- creado: {state.archivo_creado}",
        f"- tamano bytes: {state.archivo_tamano_bytes}",
        f"- estable: {state.archivo_estable}",
        f"- PDF header valido: {state.archivo_pdf_header_valido}",
        f"- paginas: {state.archivo_paginas}",
        f"- ruta: {state.archivo_ruta}",
        "",
        "FINAL DIALOG:",
        f'- "Desea abrir este archivo" detectado: {state.final_dialog_detectado}',
        f"- boton No ejecutado: {state.final_dialog_no_ejecutado}",
        "",
        "RESULTADO:",
        f"- {state.resultado}",
    ]
    return "\n".join(lines)


def save_report(text: str, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return output_path


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def execute(
    invoice: str,
    max_depth: int,
    scan_timeout: float,
    documento_origen_no_effect_timeout: float,
    report_viewer_timeout: float,
    export_menu_timeout: float,
    export_arrow_confirmed: bool,
    pdf_options_timeout: float,
    save_dialog_timeout: float,
    file_wait_timeout: float,
    final_dialog_timeout: float,
    poll_interval: float,
    log_path: Path,
    stop_after: str = "none",
) -> tuple[ReportState, int]:
    """Fail-fast orchestration of PASO 1 - PASO 11. No step is attempted if
    the previous one did not cleanly succeed. Never raises for an expected
    condition (Regla 14).

    stop_after (one of STOP_AFTER_CHOICES) lets a caller halt the
    orchestration at an intermediate read-only-confirmed checkpoint instead
    of running the full flow through PASO 11: "report_viewer" returns right
    after PASO 4 confirms 'Visor de Reportes' (RESULT_REPORT_VIEWER_READY,
    before PASO 5 ever looks at the Export button); "pdf_options" returns
    right after PASO 6C confirms 'Opciones de Exportacion PDF' is open
    (RESULT_PDF_OPTIONS_READY, before PASO 7 ever touches Aceptar); "save_as"
    returns right after PASO 8 clicks Guardar (RESULT_SAVE_AS_SUBMITTED,
    before PASO 9's file-wait, PASO 10's final dialog, or PASO 11's
    validation ever run). No checkpoint skips or duplicates any
    single-action call site -- everything already executed up to the
    checkpoint stays exactly as it was; only the steps AFTER the checkpoint
    are not attempted this run."""
    state = ReportState()
    masked_invoice = _mask_invoice(invoice)
    logger.info("F0.5: iniciando para factura (enmascarada)={}", masked_invoice)

    # === PASO 1: validar que el resultado de la factura siga activo ===
    logger.info("PASO 1: localizando ventana autenticada de GO y validando resultado activo (solo lectura)...")
    try:
        pids = find_target_process_ids(TARGET_PROCESS_NAME)
    except Exception as exc:
        _classify_and_log(exc, "fallo al resolver PIDs de GO")
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1

    if not pids:
        logger.warning("TRAZABILIDAD_NOT_ACTIVE: no hay ningun proceso '{}' en ejecucion", TARGET_PROCESS_NAME)
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1

    try:
        all_windows = enumerate_all_windows()
    except (WindowNotFoundError, Exception) as exc:
        _classify_and_log(exc, "fallo enumerando ventanas")
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1

    try:
        go_window = find_authenticated_go_window(pids, all_windows, require_favoritos_signals=False)
    except (WindowNotFoundError, GoAmbiguousError) as exc:
        logger.warning("TRAZABILIDAD_NOT_ACTIVE: {}", exc)
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado localizando la ventana autenticada de GO")
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1

    logger.info("GO localizada: handle={} pid={}", go_window.handle, go_window.process_id)

    try:
        validate_screen_active(go_window.handle, max_depth, scan_timeout)
    except TrazabilidadNotActiveError as exc:
        logger.warning("TRAZABILIDAD_NOT_ACTIVE: {}", exc)
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1

    try:
        verify_go_foreground(go_window.handle, expected_pid=go_window.process_id)
    except GoNotForegroundError as exc:
        logger.error("GO_NOT_FOREGROUND: {}", exc)
        state.resultado = RESULT_GO_NOT_FOREGROUND
        return state, 1
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado verificando primer plano")
        state.resultado = RESULT_GO_NOT_FOREGROUND
        return state, 1

    try:
        window = _connect_uia(go_window.handle)
    except Exception as exc:
        _classify_and_log(exc, f"fallo al reconectar via UIA a la ventana GO {go_window.handle}")
        state.resultado = RESULT_INVOICE_RESULT_NOT_ACTIVE
        return state, 1

    try:
        controls = _scan_go_controls(go_window.handle, max_depth, scan_timeout)
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado escaneando controles de GO")
        state.resultado = RESULT_INVOICE_RESULT_NOT_ACTIVE
        return state, 1

    active_enough, populated_count = validate_invoice_result_active(controls)
    if not active_enough:
        logger.error(
            "INVOICE_RESULT_NOT_ACTIVE: solo {} de {} campos de resultado poblados "
            "(umbral minimo: {}) -- no se re-busca, no se corrige.",
            populated_count,
            len(FIELD_LABEL_VARIANTS),
            MIN_ACTIVE_FIELDS_THRESHOLD,
        )
        state.resultado = RESULT_INVOICE_RESULT_NOT_ACTIVE
        return state, 1

    logger.info("PASO 1: resultado de factura confirmado activo ({} campos poblados).", populated_count)

    # === PASO 2: identificar "Documento Origen" + su control de valor (vivo) ===
    logger.info("PASO 2: localizando label 'Documento Origen' y su control de valor (vivo)...")
    label_candidates = find_documento_origen_label_candidates(controls)
    logger.info("Candidatos LABEL 'Documento Origen': {}", len(label_candidates))
    if not label_candidates:
        logger.error("DOCUMENT_ORIGIN_NOT_FOUND: label 'Documento Origen' no encontrado.")
        state.doc_origen_identificado = "NO"
        state.resultado = RESULT_DOCUMENT_ORIGIN_NOT_FOUND
        return state, 1

    resolved_value_element = None
    for label in label_candidates:
        label_rect = _control_info_rect(label)
        if label_rect is None:
            continue
        try:
            value_candidates = find_documento_origen_value_live_candidates(
                window, label_rect, max_depth, scan_timeout
            )
        except Exception as exc:
            _classify_and_log(exc, "fallo inesperado buscando el control de valor de Documento Origen")
            continue
        visible_enabled = [c for c in value_candidates if _is_visible_enabled(c)]
        stable_candidates = [
            c
            for c in visible_enabled
            if _live_element_automation_id(c) == DOCUMENTO_ORIGEN_AUTOMATION_ID
        ]
        logger.info(
            "Candidatos Documento Origen para un label: crudos={} visible+enabled={} "
            "automation_id_estable={}",
            len(value_candidates),
            len(visible_enabled),
            len(stable_candidates),
        )
        if len(stable_candidates) == 1:
            resolved_value_element = stable_candidates[0]
            break

    if resolved_value_element is None:
        logger.error(
            "DOCUMENT_ORIGIN_NOT_FOUND: ningun label 'Documento Origen' ancla a exactamente "
            "un control de valor con prefijo 'Factura - '."
        )
        state.doc_origen_identificado = "NO"
        state.resultado = RESULT_DOCUMENT_ORIGIN_NOT_FOUND
        return state, 1

    value_name = _live_element_name(resolved_value_element)
    plausible = value_name.startswith(DOCUMENTO_ORIGEN_VALUE_PREFIX) and (
        value_name[len(DOCUMENTO_ORIGEN_VALUE_PREFIX):] == invoice
    )
    logger.info(
        "PASO 2: control de valor de Documento Origen identificado (backend=uia, "
        "control_type={}, automation_id={!r}, coincide_con_factura_esperada={})",
        _live_element_control_type(resolved_value_element),
        _live_element_automation_id(resolved_value_element),
        plausible,
    )
    if not plausible:
        logger.error(
            "DOCUMENT_ORIGIN_NOT_FOUND: el control estable existe, pero no corresponde "
            "a la factura solicitada; no se interactua."
        )
        state.doc_origen_identificado = "NO"
        state.resultado = RESULT_DOCUMENT_ORIGIN_NOT_FOUND
        return state, 1
    logger.info(
        "PASO 2: patrones soportados -- Invoke={} SelectionItem={}",
        _supports_invoke_pattern(resolved_value_element),
        _supports_selection_item_pattern(resolved_value_element),
    )

    try:
        selectors_path = save_post_search_selectors(resolved_value_element)
        logger.info("Selectores post-busqueda guardados en {}", selectors_path)
    except Exception as exc:
        _classify_and_log(exc, "fallo (no fatal) guardando selectores post-busqueda")

    state.doc_origen_identificado = "SI"
    state.doc_origen_backend = "uia"

    # === PASO 3: LA UNICA ACCION AUTORIZADA de este paso -- un unico call site.
    # Real-world finding (F0.5, live GO session): "Visor de Reportes" opens as
    # an MDI child WITHIN GO's own top-level window (automation_id=
    # "FrmReportViewer"), not a new top-level OS window -- the original
    # top-level-only detection could never see it. Fixed by also checking
    # find_mdi_child_live() below. Additionally: if the viewer is ALREADY
    # open (e.g. a prior run's click already succeeded but this run is a
    # fresh invocation), skip the click entirely rather than clicking a
    # second time -- Regla 1/14: verify current state before acting, never
    # assume a fresh click is needed. ===
    logger.info("PASO 3 (pre-check): verificando si 'Visor de Reportes' ya esta abierto (solo lectura)...")
    viewer_live = find_mdi_child_live(
        window, REPORT_VIEWER_AUTOMATION_ID_HINTS, REPORT_VIEWER_TITLE_HINTS,
        max_depth=min(LIVE_SEARCH_MAX_DEPTH, 14),
        timeout_seconds=5.0,
    )
    viewer_open_elapsed = time.monotonic()

    if viewer_live is not None:
        try:
            preexisting_viewer_handle = int(viewer_live.handle)
        except Exception:
            preexisting_viewer_handle = 0
        if not preexisting_viewer_handle or not viewer_receipt_matches(
            invoice,
            go_window.process_id,
            preexisting_viewer_handle,
        ):
            logger.error(
                "REPORT_VIEWER_UNVERIFIED: hay un Visor de Reportes abierto, pero no existe "
                "un recibo de sesion que lo vincule con la factura solicitada."
            )
            state.resultado = RESULT_REPORT_VIEWER_UNVERIFIED
            return state, 1
        logger.info(
            "'Visor de Reportes' ya estaba abierto y su recibo de sesion coincide -- se "
            "omite el clic en Documento Origen."
        )
        state.doc_origen_accion_ejecutada = "NO (ya estaba abierto)"
        state.doc_origen_metodo = "N/D (omitido, ya abierto)"
    else:
        logger.info("PASO 3: ejecutando la UNICA interaccion autorizada con Documento Origen...")
        before_click_handles = {w.handle for w in _snapshot_target_windows(pids)}
        try:
            verify_go_foreground(
                go_window.handle,
                expected_pid=go_window.process_id,
                allow_same_pid=False,
            )
            metodo = activate_preselected_once(resolved_value_element)
        except Exception as exc:
            code = _classify_and_log(exc, "fallo inesperado ejecutando el clic autorizado en Documento Origen")
            state.resultado = f"{RESULT_ABORTED_PREFIX} (DOCUMENT_ORIGIN_CLICK_FAILED: {code})"
            return state, 1

        state.doc_origen_metodo = metodo
        state.doc_origen_accion_ejecutada = "SI"
        logger.info("Clic en Documento Origen ejecutado (1 vez). Metodo: {}", metodo)

        # === A partir de aqui: SOLO deteccion pasiva. Ningun otro
        # invoke()/click_input() sobre Documento Origen, para ningun caso. ===

        logger.info(
            "PASO 3b: verificacion corta ({}s) de que el clic tuvo algun efecto observable "
            "(hijo MDI 'Visor de Reportes' o cualquier ventana top-level nueva)...",
            documento_origen_no_effect_timeout,
        )
        deadline = time.monotonic() + documento_origen_no_effect_timeout
        while viewer_live is None and time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            viewer_live = find_mdi_child_live(
                window, REPORT_VIEWER_AUTOMATION_ID_HINTS, REPORT_VIEWER_TITLE_HINTS,
                max_depth=min(LIVE_SEARCH_MAX_DEPTH, 14),
                timeout_seconds=min(2.0, remaining),
            )
            if viewer_live is None:
                time.sleep(poll_interval)

        any_new_top_level = None
        if viewer_live is None:
            any_new_top_level = _light_wait_for_window(
                lambda: _snapshot_target_windows(pids),
                before_click_handles,
                lambda w: w.process_id == go_window.process_id,
                1.0,
                poll_interval,
                require_new=True,
            )

        if viewer_live is None and any_new_top_level is None:
            logger.error(
                "DOCUMENT_ORIGIN_NO_EFFECT: ni el hijo MDI 'Visor de Reportes' ni ninguna ventana "
                "top-level nueva aparecieron en {}s tras el clic.",
                documento_origen_no_effect_timeout,
            )
            state.resultado = RESULT_DOCUMENT_ORIGIN_NO_EFFECT
            return state, 1

        viewer_open_elapsed = time.monotonic()

    # === PASO 4: confirmar "Visor de Reportes" (hijo MDI, detectado arriba;
    # con espera adicional liviana si aun no aparecio) ===
    if viewer_live is None:
        logger.info(
            "PASO 4: esperando hijo MDI 'Visor de Reportes' (polling liviano, hasta {}s)...",
            report_viewer_timeout,
        )
        deadline = time.monotonic() + report_viewer_timeout
        while viewer_live is None and time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            viewer_live = find_mdi_child_live(
                window, REPORT_VIEWER_AUTOMATION_ID_HINTS, REPORT_VIEWER_TITLE_HINTS,
                max_depth=min(LIVE_SEARCH_MAX_DEPTH, 14),
                timeout_seconds=min(2.0, remaining),
            )
            if viewer_live is None:
                time.sleep(poll_interval)

    if viewer_live is None:
        logger.error("REPORT_VIEWER_TIMEOUT: 'Visor de Reportes' no aparecio en {}s.", report_viewer_timeout)
        state.viewer_abierto = "NO"
        state.resultado = RESULT_REPORT_VIEWER_TIMEOUT
        return state, 1

    try:
        viewer_handle = viewer_live.handle
    except Exception:
        viewer_handle = None
    viewer_window = SimpleNamespace(
        handle=viewer_handle,
        process_id=go_window.process_id,
        class_name=_live_element_class_name(viewer_live),
        title=_live_element_name(viewer_live),
        rectangle=str(_rect_from_live_element(viewer_live)),
    )

    if state.doc_origen_accion_ejecutada == "SI":
        try:
            save_viewer_receipt(invoice, go_window.process_id, int(viewer_window.handle))
        except Exception as exc:
            code = _classify_and_log(exc, "fallo guardando recibo de sesion del Visor de Reportes")
            state.resultado = f"{RESULT_ABORTED_PREFIX} (VIEWER_RECEIPT_FAILED: {code})"
            return state, 1

    # Light stability confirmation: re-check the MDI child is still resolvable
    # a moment later (mission's own light-touch instruction -- not a full UIA
    # control-tree stability check).
    stable = True
    time.sleep(1.0)
    recheck = find_mdi_child_live(
        window, REPORT_VIEWER_AUTOMATION_ID_HINTS, REPORT_VIEWER_TITLE_HINTS,
        max_depth=min(LIVE_SEARCH_MAX_DEPTH, 14),
        timeout_seconds=5.0,
    )
    if recheck is None:
        stable = False
    if not stable:
        logger.warning(
            "Visor de Reportes detectado pero no se confirmo estable en la verificacion "
            "liviana de seguimiento; se continua igualmente (evidencia registrada)."
        )

    state.viewer_abierto = "SI"
    state.viewer_handle = str(viewer_window.handle)
    state.viewer_class = viewer_window.class_name
    logger.info(
        "REPORT_VIEWER_OPENED: handle={} pid={} class={} title={!r} rect={}",
        viewer_window.handle,
        viewer_window.process_id,
        viewer_window.class_name,
        viewer_window.title,
        viewer_window.rectangle,
    )

    logger.info(
        "Captura del Visor de Reportes omitida deliberadamente: puede contener datos "
        "clinicos visibles."
    )

    if stop_after == "report_viewer":
        logger.info(
            "REPORT_VIEWER_READY: deteniendo por --stop-after=report_viewer antes de "
            "tocar el boton Export Document (PASO 5 no se ejecuta en esta corrida)."
        )
        state.resultado = RESULT_REPORT_VIEWER_READY
        return state, 0

    # === PASO 5: identificar "Export Document..." dentro del Visor de Reportes ===
    logger.info("PASO 5: localizando boton 'Export Document...' dentro del Visor de Reportes...")
    try:
        viewer_live = _connect_uia(viewer_window.handle)
    except Exception as exc:
        code = _classify_and_log(exc, f"fallo al conectar UIA al Visor de Reportes {viewer_window.handle}")
        state.export_identificado = "NO"
        state.resultado = RESULT_EXPORT_BUTTON_NOT_FOUND
        return state, 1

    try:
        strong, weak = find_export_button_candidates(viewer_live, LIVE_SEARCH_MAX_DEPTH, LIVE_SEARCH_TIMEOUT_SECONDS)
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado buscando el boton Export Document")
        state.export_identificado = "NO"
        state.resultado = RESULT_EXPORT_BUTTON_NOT_FOUND
        return state, 1

    export_candidates = _dedupe_live_elements(
        [c for c in (strong if strong else weak) if _is_visible_enabled(c)]
    )
    logger.info(
        "Candidatos Export Document: {} fuertes, {} debiles -> usando {} (fuertes={})",
        len(strong),
        len(weak),
        len(export_candidates),
        bool(strong),
    )
    if len(export_candidates) != 1:
        logger.error(
            "EXPORT_BUTTON_NOT_FOUND: {} candidatos (se requiere exactamente 1); sin fallback "
            "posicional confiable disponible -- se aborta en vez de adivinar (Regla 14).",
            len(export_candidates),
        )
        state.export_identificado = "NO"
        state.resultado = RESULT_EXPORT_BUTTON_NOT_FOUND
        return state, 1

    export_element = export_candidates[0]
    state.export_identificado = "SI"
    state.export_backend = "uia"
    logger.info(
        "EXPORT_BUTTON_IDENTIFIED: control_type={} automation_id={!r} class={!r}",
        _live_element_control_type(export_element),
        _live_element_automation_id(export_element),
        _live_element_class_name(export_element),
    )
    state.viewer_tiempo_apertura = f"{time.monotonic() - viewer_open_elapsed:.1f}s (desde deteccion hasta fin de PASO 5)"

    # If the exact menu item is already open, do not toggle the split button.
    pdf_file_candidates, popup_roots = find_pdf_file_menu_candidates(
        go_window.process_id,
        viewer_live,
        timeout_seconds=0.75,
    )
    if len(pdf_file_candidates) > 1:
        logger.error("EXPORT_PDF_FILE_NOT_FOUND: el menu ya abierto contiene candidatos ambiguos.")
        state.resultado = RESULT_EXPORT_PDF_FILE_NOT_FOUND
        return state, 1

    if len(pdf_file_candidates) == 1:
        state.export_menu_abierto = "SI (ya estaba abierto)"
        state.export_metodo = "N/D (menu ya abierto)"
    else:
        # === PASO 6A: abrir SOLO la flecha del split button. Nunca se invoca
        # ni se hace clic en el centro de Export Document. ===
        arrow_target = resolve_export_arrow_target(viewer_live, export_element)
        if arrow_target is None:
            logger.error("EXPORT_BUTTON_NOT_FOUND: no se pudo resolver la flecha del split button.")
            state.resultado = RESULT_EXPORT_BUTTON_NOT_FOUND
            return state, 1

        state.export_metodo = arrow_target.method
        requires_confirmation = arrow_target.method in {
            "relative_confirmed_control",
            "relative_export_rect",
        }
        if requires_confirmation and not export_arrow_confirmed:
            if arrow_target.screen_point is None:
                state.resultado = RESULT_EXPORT_BUTTON_NOT_FOUND
                return state, 1
            try:
                verify_go_foreground(
                    viewer_window.handle,
                    expected_pid=go_window.process_id,
                    allow_same_pid=False,
                )
                animate_cursor_to(arrow_target.screen_point)
            except Exception as exc:
                code = _classify_and_log(exc, "fallo en MOVE-ONLY hacia la flecha de Export Document")
                state.resultado = f"{RESULT_ABORTED_PREFIX} (EXPORT_ARROW_MOVE_FAILED: {code})"
                return state, 1
            logger.warning(
                "EXPORT_ARROW_CONFIRMATION_REQUIRED: cursor movido sin clic a {} mediante {}. "
                "Confirme visualmente la flecha antes de repetir con --export-arrow-confirmed.",
                arrow_target.screen_point,
                arrow_target.method,
            )
            state.resultado = RESULT_EXPORT_ARROW_CONFIRMATION_REQUIRED
            return state, 2

        before_popup_keys = snapshot_visible_uia_root_keys(go_window.process_id)
        logger.info(
            "PASO 6A: abriendo exclusivamente la flecha de Export Document mediante {} (una accion)...",
            arrow_target.method,
        )
        try:
            verify_go_foreground(
                viewer_window.handle,
                expected_pid=go_window.process_id,
                allow_same_pid=False,
            )
            open_export_dropdown_once(arrow_target)
        except Exception as exc:
            code = _classify_and_log(exc, "fallo abriendo la flecha del split button Export Document")
            state.resultado = f"{RESULT_ABORTED_PREFIX} (EXPORT_ARROW_FAILED: {code})"
            return state, 1
        state.export_menu_abierto = "SI"

        # === PASO 6B: localizar exactamente 'PDF File' en el popup nuevo. ===
        logger.info("PASO 6B: buscando MenuItem exacto 'PDF File' (hasta {}s)...", export_menu_timeout)
        pdf_file_candidates, popup_roots = find_pdf_file_menu_candidates(
            go_window.process_id,
            viewer_live,
            export_menu_timeout,
            before_root_keys=before_popup_keys,
        )

    capture_small_popup_bmp(popup_roots, SCREENSHOT_EXPORT_MENU_PATH)
    if len(pdf_file_candidates) != 1:
        logger.error(
            "EXPORT_PDF_FILE_NOT_FOUND: se encontraron {} candidatos visible+enabled con "
            "nombre exacto 'PDF File'; se detiene sin segundo clic.",
            len(pdf_file_candidates),
        )
        state.export_pdf_file_identificado = "NO"
        state.resultado = RESULT_EXPORT_PDF_FILE_NOT_FOUND
        return state, 1

    pdf_file_element = pdf_file_candidates[0]
    state.export_pdf_file_identificado = "SI"
    pdf_file_method = choose_single_action_method(pdf_file_element)
    state.export_pdf_file_metodo = pdf_file_method
    before_pdf_options_handles = {w.handle for w in _snapshot_target_windows(pids)}
    logger.info("PASO 6B: activando 'PDF File' mediante {} (una accion)...", pdf_file_method)
    try:
        verify_go_foreground(
            viewer_window.handle,
            expected_pid=go_window.process_id,
            allow_same_pid=False,
        )
        activate_once(pdf_file_element, pdf_file_method)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo activando el MenuItem exacto 'PDF File'")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (EXPORT_PDF_FILE_ACTION_FAILED: {code})"
        return state, 1

    logger.info("PASO 6C: esperando dialogo 'Opciones de Exportacion PDF' (hasta {}s)...", pdf_options_timeout)
    pdf_options_window = _light_wait_for_window(
        lambda: _snapshot_target_windows(pids),
        before_pdf_options_handles,
        lambda w: w.process_id == go_window.process_id and _matches_any(w.title, PDF_OPTIONS_TITLE_HINTS),
        pdf_options_timeout,
        poll_interval,
        require_new=True,
    )
    if pdf_options_window is None:
        logger.error("PDF_OPTIONS_TIMEOUT: dialogo de opciones PDF no aparecio en {}s.", pdf_options_timeout)
        state.pdf_options_detectado = "NO"
        state.resultado = RESULT_PDF_OPTIONS_TIMEOUT
        return state, 1

    state.pdf_options_detectado = "SI"
    logger.info(
        "Dialogo de opciones PDF detectado: handle={} title={!r}",
        pdf_options_window.handle,
        pdf_options_window.title,
    )

    try:
        capture_window_bmp(pdf_options_window.handle, SCREENSHOT_PDF_OPTIONS_PATH)
    except Exception as exc:
        _classify_and_log(exc, "fallo (no fatal) capturando pantalla del dialogo de opciones PDF")

    if stop_after == "pdf_options":
        logger.info(
            "PDF_OPTIONS_READY: deteniendo por --stop-after=pdf_options antes de tocar "
            "Aceptar (PASO 7 no se ejecuta en esta corrida)."
        )
        state.resultado = RESULT_PDF_OPTIONS_READY
        return state, 0

    # === PASO 7: NUNCA tocar ninguna otra opcion -- solo lectura de sanity-check + Aceptar ===
    try:
        pdf_options_live = _connect_uia(pdf_options_window.handle)
    except Exception as exc:
        code = _classify_and_log(exc, f"fallo al conectar UIA al dialogo de opciones PDF {pdf_options_window.handle}")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (PDF_OPTIONS_CONNECT_FAILED: {code})"
        return state, 1

    try:
        deadline = time.monotonic() + LIVE_SEARCH_TIMEOUT_SECONDS
        pdf_options_controls = walk_tree(pdf_options_live, max_depth=LIVE_SEARCH_MAX_DEPTH, deadline=deadline)
        haystack = " ".join(f"{c.name} {c.automation_id}" for c in pdf_options_controls)
        found_keywords = [k for k in PDF_OPTIONS_SANITY_KEYWORDS if _matches_any(haystack, (k,))]
        logger.info(
            "PASO 7: sanity-check informativo del dialogo de opciones PDF -- palabras clave "
            "encontradas: {} (best-effort, nunca bloquea el flujo)",
            found_keywords,
        )
    except Exception as exc:
        _classify_and_log(exc, "fallo (no fatal) en el sanity-check informativo del dialogo de opciones PDF")

    try:
        accept_candidates = find_button_candidates_live(
            pdf_options_live,
            ACCEPT_BUTTON_STRONG_VARIANTS,
            REJECT_BUTTON_VARIANTS,
            LIVE_SEARCH_MAX_DEPTH,
            LIVE_SEARCH_TIMEOUT_SECONDS,
        )
        if not accept_candidates:
            accept_candidates = find_button_candidates_live(
                pdf_options_live,
                ACCEPT_BUTTON_WEAK_VARIANTS,
                REJECT_BUTTON_VARIANTS,
                LIVE_SEARCH_MAX_DEPTH,
                LIVE_SEARCH_TIMEOUT_SECONDS,
            )
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado buscando el boton Aceptar del dialogo de opciones PDF")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (PDF_OPTIONS_ACCEPT_SEARCH_FAILED: {code})"
        return state, 1

    if len(accept_candidates) != 1:
        logger.error(
            "{} candidatos 'Aceptar' en el dialogo de opciones PDF (se requiere exactamente 1).",
            len(accept_candidates),
        )
        state.resultado = f"{RESULT_ABORTED_PREFIX} (PDF_OPTIONS_ACCEPT_AMBIGUOUS)"
        return state, 1

    accept_element = accept_candidates[0]
    before_save_handles = {w.handle for w in enumerate_all_windows()}

    # === LA UNICA ACCION AUTORIZADA de este paso -- un unico call site.
    # NUNCA se toca ningun otro control de este dialogo (ver grep de
    # seguridad: sin set_text/toggle/select aqui). ===
    logger.info("PASO 7: ejecutando la UNICA interaccion autorizada -- clic en Aceptar...")
    try:
        verify_go_foreground(
            pdf_options_window.handle,
            expected_pid=pdf_options_window.process_id,
            allow_same_pid=False,
        )
        activate_preselected_once(accept_element)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado ejecutando el clic autorizado en Aceptar")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (PDF_OPTIONS_ACCEPT_CLICK_FAILED: {code})"
        return state, 1

    state.pdf_options_aceptar_ejecutado = "SI"
    logger.info("Clic en Aceptar ejecutado (1 vez). Ninguna otra opcion del dialogo fue tocada.")

    # === PASO 8: dialogo estandar 'Guardar como' ===
    logger.info("PASO 8: esperando dialogo 'Guardar como' (hasta {}s)...", save_dialog_timeout)
    save_window = _light_wait_for_window(
        enumerate_all_windows,
        before_save_handles,
        lambda w: (
            w.process_id == go_window.process_id
            and SAVE_DIALOG_CLASS_HINT in w.class_name
            and _matches_any(w.title, SAVE_DIALOG_TITLE_HINTS)
        ),
        save_dialog_timeout,
        poll_interval,
        require_new=True,
    )
    if save_window is None:
        logger.error("SAVE_DIALOG_TIMEOUT: dialogo 'Guardar como' no aparecio en {}s.", save_dialog_timeout)
        state.save_detectado = "NO"
        state.resultado = RESULT_SAVE_DIALOG_TIMEOUT
        return state, 1

    state.save_detectado = "SI"
    logger.info("Dialogo 'Guardar como' detectado: handle={} title={!r}", save_window.handle, save_window.title)

    try:
        capture_window_bmp(save_window.handle, SCREENSHOT_SAVE_DIALOG_PATH)
    except Exception as exc:
        _classify_and_log(exc, "fallo (no fatal) capturando pantalla del dialogo Guardar como")

    # Destination path: computed BEFORE touching the dialog, non-colliding.
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    destination = DOWNLOADS_DIR / f"factura_{invoice}_poc.pdf"
    if destination.exists():
        destination = DOWNLOADS_DIR / f"factura_{invoice}_poc_{datetime.now():%Y%m%d_%H%M%S}.pdf"
    masked_destination = str(destination).replace(invoice, masked_invoice)
    state.save_ruta_destino = masked_destination
    logger.info("Ruta destino resuelta (enmascarada): {}", masked_destination)

    try:
        save_live = _connect_uia(save_window.handle)
    except Exception as exc:
        code = _classify_and_log(exc, f"fallo al conectar UIA al dialogo Guardar como {save_window.handle}")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (SAVE_DIALOG_CONNECT_FAILED: {code})"
        return state, 1

    try:
        edit_candidates = find_filename_edit_candidates_live(
            save_live,
            LIVE_SEARCH_MAX_DEPTH,
            LIVE_SEARCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado buscando el campo de nombre de archivo")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (SAVE_DIALOG_FIELD_SEARCH_FAILED: {code})"
        return state, 1

    if len(edit_candidates) != 1:
        logger.error(
            "Se esperaba exactamente un campo de nombre de archivo con selector estructural "
            "estable; encontrados={}. No se usa heuristica por ancho.",
            len(edit_candidates),
        )
        state.resultado = f"{RESULT_ABORTED_PREFIX} (SAVE_DIALOG_FIELD_NOT_FOUND)"
        return state, 1
    filename_edit = edit_candidates[0]

    # Only the computed destination PATH is ever written here -- never the
    # invoice number in isolation, never any other data.
    logger.info("PASO 8: escribiendo la ruta destino en el campo de nombre de archivo (una unica vez)...")
    if not _supports_value_pattern(filename_edit):
        logger.error(
            "SAVE_DIALOG_FIELD_NOT_FOUND: el campo elegido no soporta ValuePattern; "
            "se aborta sin intentar teclado como segunda mutacion."
        )
        state.resultado = f"{RESULT_ABORTED_PREFIX} (SAVE_DIALOG_FIELD_NOT_FOUND)"
        return state, 1
    try:
        filename_edit.set_text(str(destination))
    except Exception as exc:
        code = _classify_and_log(exc, "fallo escribiendo la ruta destino mediante ValuePattern")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (SAVE_DIALOG_WRITE_FAILED: {code})"
        return state, 1

    # Best-effort, read-only "Tipo" confirmation -- never interacted with.
    # Reasoning: the destination filename already carries a literal ".pdf"
    # extension, which is what drives GO's exporter regardless of the
    # dropdown's own displayed state; forcing an extra interaction with
    # that control is not authorized and not needed.
    try:
        combo_hits = [
            c
            for c in walk_tree(save_live, max_depth=LIVE_SEARCH_MAX_DEPTH, deadline=time.monotonic() + 5.0)
            if "combo" in c.control_type.lower() and _matches_any(f"{c.name} {c.automation_id}", ("tipo", "type"))
        ]
        logger.info(
            "PASO 8: chequeo informativo de solo lectura de 'Tipo' -- {} control(es) tipo-combo "
            "relacionados encontrados (no se interactua con ninguno).",
            len(combo_hits),
        )
    except Exception as exc:
        _classify_and_log(exc, "fallo (no fatal) en el chequeo informativo de 'Tipo'")

    try:
        save_button_candidates = find_button_candidates_live(
            save_live, SAVE_BUTTON_STRONG_VARIANTS, REJECT_BUTTON_VARIANTS, LIVE_SEARCH_MAX_DEPTH, LIVE_SEARCH_TIMEOUT_SECONDS
        )
        if not save_button_candidates:
            save_button_candidates = find_button_candidates_live(
                save_live, SAVE_BUTTON_WEAK_VARIANTS, REJECT_BUTTON_VARIANTS, LIVE_SEARCH_MAX_DEPTH, LIVE_SEARCH_TIMEOUT_SECONDS
            )
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado buscando el boton Guardar")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (SAVE_DIALOG_BUTTON_SEARCH_FAILED: {code})"
        return state, 1

    if len(save_button_candidates) != 1:
        logger.error("{} candidatos 'Guardar' encontrados (se requiere exactamente 1).", len(save_button_candidates))
        state.resultado = f"{RESULT_ABORTED_PREFIX} (SAVE_DIALOG_BUTTON_AMBIGUOUS)"
        return state, 1

    save_button = save_button_candidates[0]
    before_final_handles = {w.handle for w in _snapshot_target_windows(pids)}

    # === LA UNICA ACCION AUTORIZADA de este paso -- un unico call site. ===
    logger.info("PASO 8: ejecutando la UNICA interaccion autorizada -- clic en Guardar...")
    try:
        verify_go_foreground(
            save_window.handle,
            expected_pid=save_window.process_id,
            allow_same_pid=False,
        )
        activate_preselected_once(save_button)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado ejecutando el clic autorizado en Guardar")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (SAVE_DIALOG_CLICK_FAILED: {code})"
        return state, 1

    state.save_guardar_ejecutado = "SI"
    logger.info("Clic en Guardar ejecutado (1 vez).")

    if stop_after == "save_as":
        logger.info(
            "SAVE_AS_SUBMITTED: deteniendo por --stop-after=save_as antes de esperar el "
            "archivo y del dialogo final (PASO 9/10/11 no se ejecutan en esta corrida)."
        )
        state.resultado = RESULT_SAVE_AS_SUBMITTED
        return state, 0

    # === PASO 9: espera de creacion de archivo -- SOLO sistema de archivos,
    # nunca UIA (ver docstring de wait_for_file_stable). ===
    logger.info(
        "PASO 9: esperando creacion/estabilizacion del archivo en el sistema de archivos "
        "(polling cada {}s, hasta {}s, estable tras {} lecturas identicas)...",
        FILE_WAIT_POLL_INTERVAL_SECONDS,
        file_wait_timeout,
        FILE_STABLE_READS_REQUIRED,
    )
    file_result = wait_for_file_stable(
        destination, FILE_WAIT_POLL_INTERVAL_SECONDS, file_wait_timeout, FILE_STABLE_READS_REQUIRED
    )
    state.archivo_ruta = masked_destination
    state.archivo_tamano_bytes = str(file_result.size_bytes)

    if not file_result.appeared:
        logger.error("FILE_NOT_CREATED: el archivo nunca aparecio en {}s.", file_wait_timeout)
        state.archivo_creado = "NO"
        state.resultado = RESULT_FILE_NOT_CREATED
        return state, 1

    state.archivo_creado = "SI"
    state.archivo_estable = "SI" if file_result.stabilized else "NO"

    if file_result.size_bytes <= 0:
        logger.error("FILE_EMPTY: el archivo aparecio pero permanecio en 0 bytes durante toda la espera.")
        state.resultado = RESULT_FILE_EMPTY
        return state, 1

    if not file_result.stabilized:
        logger.error(
            "FILE_NOT_STABLE: el archivo aparecio y no esta vacio, pero no alcanzo {} "
            "lecturas consecutivas de tamano estable dentro de {}s.",
            FILE_STABLE_READS_REQUIRED,
            file_wait_timeout,
        )
        state.resultado = RESULT_FILE_NOT_STABLE
        return state, 1

    logger.info(
        "Archivo creado: ruta={} tamano={} bytes estable={} primer_avistamiento={:.1f}s estabilizado={}",
        masked_destination,
        file_result.size_bytes,
        file_result.stabilized,
        file_result.elapsed_first_seen or 0.0,
        f"{file_result.elapsed_stabilized:.1f}s" if file_result.elapsed_stabilized is not None else "N/D",
    )

    # === PASO 10: dialogo final 'Exportar' -- clic en 'No', NUNCA en 'Si' ===
    logger.info("PASO 10: esperando dialogo final 'Exportar' (hasta {}s)...", final_dialog_timeout)
    final_window = _light_wait_for_window(
        lambda: _snapshot_target_windows(pids),
        before_final_handles,
        lambda w: w.process_id == go_window.process_id and _matches_any(w.title, FINAL_DIALOG_TITLE_HINTS),
        final_dialog_timeout,
        poll_interval,
        require_new=True,
    )
    if final_window is None:
        logger.error("FINAL_DIALOG_TIMEOUT: dialogo final 'Exportar' no aparecio en {}s.", final_dialog_timeout)
        state.final_dialog_detectado = "NO"
        state.resultado = RESULT_FINAL_DIALOG_TIMEOUT
        return state, 1

    try:
        final_live = _connect_uia(final_window.handle)
        deadline = time.monotonic() + 5.0
        final_controls = walk_tree(final_live, max_depth=LIVE_SEARCH_MAX_DEPTH, deadline=deadline)
        content_haystack = " ".join(f"{c.name}" for c in final_controls)
        content_confirmed = _matches_any(content_haystack, FINAL_DIALOG_CONTENT_HINTS)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado confirmando contenido del dialogo final")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (FINAL_DIALOG_CONTENT_CHECK_FAILED: {code})"
        return state, 1

    state.final_dialog_detectado = "SI" if content_confirmed else "NO (solo coincidio el titulo)"
    logger.info(
        "Dialogo final detectado: handle={} title={!r} contenido_confirmado={}",
        final_window.handle,
        final_window.title,
        content_confirmed,
    )
    if not content_confirmed:
        logger.error(
            "FINAL_DIALOG_TIMEOUT: se encontro una ventana titulada Exportar, pero no se "
            "confirmo el texto 'Desea abrir'; no se pulsa ningun boton."
        )
        state.resultado = RESULT_FINAL_DIALOG_TIMEOUT
        return state, 1

    try:
        no_candidates = find_no_button_candidates_live(final_live, LIVE_SEARCH_MAX_DEPTH, LIVE_SEARCH_TIMEOUT_SECONDS)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado buscando el boton No")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (FINAL_DIALOG_NO_SEARCH_FAILED: {code})"
        return state, 1

    if len(no_candidates) != 1:
        logger.error(
            "{} candidatos 'No' encontrados (comparacion EXACTA, nunca subcadena) -- se requiere "
            "exactamente 1; nunca se hace clic en 'Si'/'Yes' bajo ningun camino de codigo.",
            len(no_candidates),
        )
        state.resultado = f"{RESULT_ABORTED_PREFIX} (FINAL_DIALOG_NO_AMBIGUOUS)"
        return state, 1

    no_button = no_candidates[0]

    # === LA UNICA ACCION AUTORIZADA de este paso -- un unico call site.
    # Esto NUNCA hace clic en "Si"/"Yes" -- no_button proviene exclusivamente
    # de find_no_button_candidates_live(), que exige igualdad EXACTA con
    # "no" tras normalizar (ver esa funcion para el razonamiento completo). ===
    logger.info("PASO 10: ejecutando la UNICA interaccion autorizada -- clic en No...")
    try:
        verify_go_foreground(
            final_window.handle,
            expected_pid=final_window.process_id,
            allow_same_pid=False,
        )
        activate_preselected_once(no_button)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado ejecutando el clic autorizado en No")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (FINAL_DIALOG_NO_CLICK_FAILED: {code})"
        return state, 1

    state.final_dialog_no_ejecutado = "SI"
    logger.info("Clic en No ejecutado (1 vez).")

    # === PASO 11: validacion del PDF (solo lectura estructural) ===
    logger.info("PASO 11: validando el PDF descargado (solo estructura, nunca contenido)...")
    header_valid, pages_valid, page_count = validate_pdf_file(destination)
    state.archivo_pdf_header_valido = "SI" if header_valid else "NO"
    state.archivo_paginas = page_count

    if not header_valid or not pages_valid:
        logger.error(
            "PDF_INVALID: header_valido={} paginas={} (se requiere paginas >= 1).",
            header_valid,
            page_count,
        )
        state.resultado = RESULT_PDF_INVALID
        return state, 1

    logger.info("PDF valido: header={} paginas={}", header_valid, page_count)
    state.resultado = RESULT_DOWNLOAD_COMPLETED
    return state, 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "F0.5: descarga controlada del documento origen de una factura ya cargada en "
            "'Trazabilidad de Factura' (GO/Indigo) -- Documento Origen -> Visor de Reportes -> "
            "Export Document -> Opciones de Exportacion PDF -> Guardar como -> espera de "
            "archivo -> dialogo final 'No'. Ejecuta el flujo completo UNA UNICA vez."
        )
    )
    parser.add_argument(
        "--invoice",
        required=True,
        help="Numero de factura ya cargado (F0.4A/F0.4C) -- usado solo para verificacion/nombre de archivo.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=TRAZABILIDAD_GATE_MAX_DEPTH,
        help=f"Profundidad maxima de los recorridos UIA acotados (default: {TRAZABILIDAD_GATE_MAX_DEPTH}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=TRAZABILIDAD_GATE_TIMEOUT_SECONDS,
        help=f"Presupuesto de tiempo (s) por recorrido UIA acotado (default: {TRAZABILIDAD_GATE_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--documento-origen-timeout",
        type=float,
        default=DEFAULT_DOCUMENTO_ORIGEN_NO_EFFECT_TIMEOUT_SECONDS,
        help=(
            "Limite (s) para la verificacion corta de PASO 3 ('el clic tuvo algun efecto') "
            f"(default: {DEFAULT_DOCUMENTO_ORIGEN_NO_EFFECT_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--report-viewer-timeout",
        type=float,
        default=DEFAULT_REPORT_VIEWER_TIMEOUT_SECONDS,
        help=f"Limite (s) de espera de 'Visor de Reportes' (default: {DEFAULT_REPORT_VIEWER_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--export-menu-timeout",
        type=float,
        default=DEFAULT_EXPORT_MENU_TIMEOUT_SECONDS,
        help=f"Limite (s) para localizar exactamente 'PDF File' (default: {DEFAULT_EXPORT_MENU_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--export-arrow-confirmed",
        action="store_true",
        help=(
            "Autoriza el unico clic relativo sobre la flecha del split button DESPUES de "
            "una ejecucion MOVE-ONLY confirmada visualmente. Sin este flag, cualquier "
            "fallback geometrico solo mueve el cursor y termina."
        ),
    )
    parser.add_argument(
        "--pdf-options-timeout",
        type=float,
        default=DEFAULT_PDF_OPTIONS_TIMEOUT_SECONDS,
        help=f"Limite (s) de espera del dialogo de opciones PDF (default: {DEFAULT_PDF_OPTIONS_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--save-dialog-timeout",
        type=float,
        default=DEFAULT_SAVE_DIALOG_TIMEOUT_SECONDS,
        help=(
            "Limite (s) de espera del dialogo 'Guardar como' -- no especificado explicitamente "
            f"por la mision; eleccion documentada (default: {DEFAULT_SAVE_DIALOG_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--file-wait-timeout",
        type=float,
        default=DEFAULT_FILE_WAIT_TIMEOUT_SECONDS,
        help=f"Limite (s) de espera de creacion/estabilizacion del archivo (default: {DEFAULT_FILE_WAIT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--final-dialog-timeout",
        type=float,
        default=DEFAULT_FINAL_DIALOG_TIMEOUT_SECONDS,
        help=(
            "Limite (s) de espera del dialogo final 'Exportar' -- no especificado explicitamente "
            f"por la mision; eleccion documentada (default: {DEFAULT_FINAL_DIALOG_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_LIGHT_POLL_INTERVAL_SECONDS,
        help=f"Intervalo (s) entre polls livianos de ventanas (default: {DEFAULT_LIGHT_POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=DEFAULT_LOG_PATH,
        help=f"Ruta del reporte de texto final (default: {DEFAULT_LOG_PATH}).",
    )
    parser.add_argument(
        "--stop-after",
        choices=STOP_AFTER_CHOICES,
        default="none",
        help=(
            "Detiene la orquestacion en un checkpoint intermedio en vez de correr el flujo "
            "completo: 'report_viewer' se detiene justo despues de confirmar 'Visor de "
            "Reportes' (PASO 4), sin tocar el boton Export Document; 'pdf_options' se "
            "detiene justo despues de confirmar 'Opciones de Exportacion PDF' (PASO 6C), "
            "sin pulsar Aceptar; 'save_as' se detiene justo despues de pulsar Guardar "
            "(PASO 8), sin esperar el archivo ni tocar el dialogo final. 'none' (default) "
            "ejecuta el flujo completo."
        ),
    )
    args = parser.parse_args(argv)

    try:
        _validate_invoice_value(args.invoice)
    except InvalidInvoiceValueError as exc:
        parser.error(str(exc))

    return args


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    args = parse_args(argv)

    logger.warning(
        "F0.5: iniciando descarga controlada de documento origen (stop_after={}). "
        "max_depth={}, timeout={}s, report_viewer_timeout={}s, "
        "export_menu_timeout={}s, export_arrow_confirmed={}, pdf_options_timeout={}s, "
        "save_dialog_timeout={}s, file_wait_timeout={}s, final_dialog_timeout={}s",
        args.stop_after,
        args.max_depth,
        args.timeout,
        args.report_viewer_timeout,
        args.export_menu_timeout,
        args.export_arrow_confirmed,
        args.pdf_options_timeout,
        args.save_dialog_timeout,
        args.file_wait_timeout,
        args.final_dialog_timeout,
    )

    try:
        state, exit_code = execute(
            invoice=args.invoice,
            max_depth=args.max_depth,
            scan_timeout=args.timeout,
            documento_origen_no_effect_timeout=args.documento_origen_timeout,
            report_viewer_timeout=args.report_viewer_timeout,
            export_menu_timeout=args.export_menu_timeout,
            export_arrow_confirmed=args.export_arrow_confirmed,
            pdf_options_timeout=args.pdf_options_timeout,
            save_dialog_timeout=args.save_dialog_timeout,
            file_wait_timeout=args.file_wait_timeout,
            final_dialog_timeout=args.final_dialog_timeout,
            poll_interval=args.poll_interval,
            log_path=args.log_path,
            stop_after=args.stop_after,
        )
    except Exception as exc:
        # Defensive last-resort net: execute() is written to catch and
        # classify every expected failure mode internally, and critically
        # never calls any single-action function more than once on any
        # path. This only guards against a genuinely unexpected error that
        # escaped every try/except above.
        code = _classify_and_log(exc, "fallo inesperado no clasificado en el flujo del PoC")
        state = ReportState()
        state.resultado = f"{RESULT_ABORTED_PREFIX} ({code})"
        exit_code = 1

    report = render_report(state)
    save_report(report, args.log_path)
    print(report)
    logger.info("Reporte final:\n{}", report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
