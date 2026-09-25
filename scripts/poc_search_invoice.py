"""F0.4B -- Primera consulta real de factura en GO/Indigo ("Trazabilidad de Factura").

F0.3C identifico (solo lectura, evidencia UIA/Win32 en vivo) el campo de
numero de factura -- automation_id="INDbteInvoiceNumber" -- y un boton de
accion pequeno embebido al borde derecho de ese mismo campo (sin
automation_id propio, localizado por relacion espacial via
_is_embedded_at_right_edge(), ver inspect_trazabilidad_form.py). F0.4A
escribio, por unica vez, un numero de factura de prueba autorizado en ese
campo (ValuePattern.SetValue(), con readback de verificacion).

El usuario autorizo explicitamente, por unica vez, la SIGUIENTE accion: uno
y solo un clic/invoke sobre ese boton de busqueda/consulta, seguido de
inspeccion de solo lectura de lo que GO cargo en respuesta, y luego
detencion total. Este script:

  1. Reutiliza integramente (import directo, nunca reimplementado):
     - find_target_process_ids()/enumerate_all_windows()/_classify_and_log()/
       WindowNotFoundError/TARGET_PROCESS_NAME (diagnose_go_windows.py).
     - find_authenticated_go_window()/verify_go_foreground()/
       GoAmbiguousError/GoNotForegroundError (poc_move_to_trazabilidad.py).
     - validate_screen_active()/TrazabilidadNotActiveError, Rect,
       _is_embedded_at_right_edge(), _is_right_and_aligned(),
       _control_info_rect(), _matches_any()/_normalize_for_match(),
       ACTION_LIKE_UIA_TYPES/ACTION_MAX_WIDTH_PX/
       EMBEDDED_RIGHT_EDGE_TOLERANCE_PX/VERTICAL_CENTER_TOLERANCE_PX/
       EDIT_LIKE_UIA_TYPES (inspect_trazabilidad_form.py).
     - find_invoice_input_elements()/_filter_visible_enabled()/
       _read_current_value()/_validate_invoice_value()/
       InvalidInvoiceValueError/TARGET_AUTOMATION_ID/
       TRAZABILIDAD_GATE_MAX_DEPTH/TRAZABILIDAD_GATE_TIMEOUT_SECONDS/
       _safe_str() (poc_write_invoice.py, F0.4A).
     - ControlInfo/walk_tree() (inspect_go.py).
  2. NUNCA vuelve a escribir/tipear en el campo de factura -- solo VERIFICA
     (solo lectura) que ya contiene el valor esperado (longitud 9, igual a
     --invoice). Si no esta listo, aborta INVOICE_VALUE_NOT_READY -- nunca
     lo corrige escribiendo.
  3. Localiza el boton de busqueda dinamicamente cada corrida, por relacion
     espacial (embebido al borde derecho del campo), como un ELEMENTO VIVO
     de pywinauto (nunca solo metadata ControlInfo), recorriendo
     element.children() reales -- necesario para poder invocar .invoke()/
     .click_input() sobre el mismo objeto identificado.
  4. Ejecuta LA UNICA interaccion autorizada -- invoke_search_once() --
     desde EXACTAMENTE UN sitio en todo el archivo (ver execute(), PASO 5):
     prefiere InvokePattern (element.invoke()), cae a click_input() SOLO si
     InvokePattern no esta soportado por este control especifico. Ninguna
     otra funcion de interaccion (accDoDefaultAction, SendKeys, teclado,
     pyautogui, mouse_event crudo, SetForegroundWindow/ShowWindow) aparece
     en ningun lugar de este archivo.
  5. Antes y despues: SOLO inspeccion de solo lectura (polling acotado,
     ~500ms, hasta --post-search-timeout). Nunca se envia una segunda
     interaccion a ningun control, incluyendo los recien aparecidos.
  6. Clasifica el resultado (INVOICE_FOUND / INVOICE_NOT_FOUND /
     SEARCH_NO_EFFECT / SEARCH_CONTROL_AMBIGUOUS / APPLICATION_ERROR) y
     guarda evidencia (BMP + reporte de texto) -- nunca persiste el valor
     real de ningun campo, solo booleano poblado/no-poblado y longitud
     (Regla 6: eleccion documentada -- ver PRIVACIDAD abajo).

PRIVACIDAD (mandatorio, Regla 6): para los 17 campos de valor listados en la
mision (Informacion Factura: N Factura, Cliente, Fecha Factura, Facturador,
Codigo Contrato, Estado Cartera, Moneda, Valor Total Factura, Valor
Paciente; Saldos Actuales: Total Factura, Saldo Actual en Cartera,
Retenciones, Total Glosado, Pago Parcial, Saldo por Conciliar, Total
Aceptado IPS, Total Aceptado EAPB) y para el numero de factura mismo, este
script SOLO reporta/persiste: poblado-o-no (booleano) y longitud -- nunca el
valor real, nunca un valor enmascarado parcial. Eleccion documentada: se
opto por longitud-only (mas simple y mas segura que reconstruir un helper de
enmascarado generico) porque la mision explicitamente permite "longitud o
tipo de valor" como suficiente. Los nombres/captions de controles de
navegacion (botones, titulos de ventana, "Documento origen", etc.) NO se
consideran datos sensibles -- son texto estatico de la aplicacion, mismo
criterio ya usado en inspect_trazabilidad_form.py/poc_click_trazabilidad.py.

Regla 1 (03_CLAUDE_RULES.md): PID/handle de GO, el campo de factura y el
boton de busqueda nunca se hardcodean -- resueltos en vivo cada corrida.

Regla 14: cualquier estado ambiguo/inesperado aborta limpiamente con la
razon especifica de la mision -- nunca se adivina, nunca se reintenta.

IMPORTANTE -- estructura de una sola accion: invoke_search_once() se llama
desde EXACTAMENTE UN sitio en todo este archivo (ver execute(), PASO 5). No
hay reintentos, no hay bucles, no hay ninguna otra funcion de accion en
ningun otro lugar de este script -- en particular, el mapeo profundo de
PASO 8 (deep mapping) SOLO lee propiedades (control_type/name/
automation_id/rectangle) via ControlInfo, nunca invoca nada sobre ningun
control que encuentra.

Uso:
    python scripts/poc_search_invoice.py --invoice FHC000000
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
    ACTION_LIKE_UIA_TYPES,
    ACTION_MAX_WIDTH_PX,
    EDIT_LIKE_UIA_TYPES,
    EMBEDDED_RIGHT_EDGE_TOLERANCE_PX,
    VERTICAL_CENTER_TOLERANCE_PX,
    Rect,
    TrazabilidadNotActiveError,
    _control_info_rect,
    _is_embedded_at_right_edge,
    _is_right_and_aligned,
    _matches_any,
    validate_screen_active,
)
from poc_move_to_trazabilidad import (  # noqa: E402
    GoAmbiguousError,
    GoNotForegroundError,
    find_authenticated_go_window,
    verify_go_foreground,
)
from poc_write_invoice import (  # noqa: E402
    TRAZABILIDAD_GATE_MAX_DEPTH,
    TRAZABILIDAD_GATE_TIMEOUT_SECONDS,
    InvalidInvoiceValueError,
    _filter_visible_enabled,
    _read_current_value,
    _safe_str,
    _validate_invoice_value,
    find_invoice_input_elements,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_POST_SEARCH_TIMEOUT_SECONDS = 15.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.5

# "A couple of consecutive stable polls is sufficient evidence" -- this
# mission's own spec calls for 3 (poc_click_trazabilidad.py's sibling
# constant uses 2 for a different, simpler before/after comparison).
STABLE_POLLS_TO_STOP_EARLY = 3

DEFAULT_SCREENSHOT_PATH = Path("runtime") / "screenshots" / "f04b_invoice_search_result.bmp"
DEFAULT_LOG_PATH = Path("runtime") / "logs" / "f04b_search_result.txt"

# Result vocabulary (Regla 14). SEARCH_CONTROL_AMBIGUOUS is reused for BOTH
# "zero candidates" and ">1 candidates" (the mission's closed vocabulary has
# no separate "not found" code for this step) -- the internal log always
# distinguishes the two cases explicitly (see find_search_button()).
RESULT_INVOICE_FOUND = "INVOICE_FOUND"
RESULT_INVOICE_NOT_FOUND = "INVOICE_NOT_FOUND"
RESULT_SEARCH_NO_EFFECT = "SEARCH_NO_EFFECT"
RESULT_SEARCH_CONTROL_AMBIGUOUS = "SEARCH_CONTROL_AMBIGUOUS"
RESULT_APPLICATION_ERROR = "APPLICATION_ERROR"
RESULT_GO_NOT_FOREGROUND = "GO_NOT_FOREGROUND"
RESULT_INVOICE_VALUE_NOT_READY = "INVOICE_VALUE_NOT_READY"
RESULT_TRAZABILIDAD_NOT_ACTIVE = "TRAZABILIDAD_NOT_ACTIVE"
# Not part of the mission's closed RESULTADO vocabulary: reused convention
# from sibling scripts for a genuinely unclassified failure.
RESULT_ABORTED_PREFIX = "ABORTED"

# Standard Win32 dialog class, same evidence hint used in
# poc_click_trazabilidad.py -- rewritten fresh here per mission instruction
# (do not import poc_click_trazabilidad.py).
ERROR_DIALOG_CLASS_HINTS: tuple[str, ...] = ("#32770",)
ERROR_TEXT_HINTS: tuple[str, ...] = (
    "error",
    "excepcion",
    "excepción",
    "no se pudo",
    "ha ocurrido un error",
    "unhandled exception",
)

# Case-insensitive, word-boundary-matched (via _matches_any) "not found"
# evidence. Evaluated only against generic app message text -- never against
# any of the 17 protected value fields themselves.
NOT_FOUND_TEXT_HINTS: tuple[str, ...] = (
    "no encontrado",
    "no encontrada",
    "no existe",
    "sin resultados",
)

# The 17 value fields named by the mission (Informacion Factura: 9,
# Saldos Actuales: 8). "N Factura" is tracked separately (it is the same
# field already verified in PASO 1 via the live INVOICE_INPUT element) --
# these 16 are located generically by label text, best-effort (see
# read_field_states()). Order matters only for the report's explicit
# 7-field subset + "otros" bucket (see render_report()).
FIELD_LABEL_VARIANTS: dict[str, tuple[str, ...]] = {
    "Cliente": ("cliente",),
    "Fecha Factura": ("fecha factura",),
    "Facturador": ("facturador",),
    "Codigo Contrato": ("codigo contrato", "código contrato"),
    "Estado Cartera": ("estado cartera",),
    "Moneda": ("moneda",),
    "Valor Total Factura": ("valor total factura",),
    "Valor Paciente": ("valor paciente",),
    "Total Factura": ("total factura",),
    "Saldo Actual en Cartera": ("saldo actual en cartera",),
    "Retenciones": ("retenciones",),
    "Total Glosado": ("total glosado",),
    "Pago Parcial": ("pago parcial",),
    "Saldo por Conciliar": ("saldo por conciliar",),
    "Total Aceptado IPS": ("total aceptado ips",),
    "Total Aceptado EAPB": ("total aceptado eapb",),
}
# Explicit 7-field subset shown by name in the mandated report format;
# everything else in FIELD_LABEL_VARIANTS (plus N Factura) goes in "otros".
REPORT_EXPLICIT_FIELDS: tuple[str, ...] = (
    "Cliente",
    "Fecha Factura",
    "Facturador",
    "Codigo Contrato",
    "Estado Cartera",
    "Moneda",
    "Valor Total Factura",
)
# Minimum coherent evidence for INVOICE_FOUND (mission's own example: "at
# minimum" these three).
KEY_INVOICE_FOUND_FIELDS: tuple[str, ...] = ("Cliente", "Fecha Factura", "Facturador")

# Step 8: keywords searched case-insensitively across the AFTER tree.
DEEP_MAP_KEYWORDS: tuple[str, ...] = (
    "documento origen",
    "documentos",
    "detalle",
    "impresion",
    "impresión",
    "imprimir",
    "factura",
    "trazabilidad",
    "movimientos",
    "auditoria",
    "auditoría",
)
# Step 8: NEW control_types of interest (UIA may report "Table" or
# "DataGrid" for a grid depending on the concrete grid implementation).
DEEP_MAP_CONTROL_TYPES: tuple[str, ...] = (
    "button",
    "datagrid",
    "table",
    "list",
    "tree",
    "custom",
    "link",
)


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSnapshot:
    """Boolean populated-or-not + length ONLY -- never the real value
    (Regla 6, see module docstring PRIVACIDAD)."""

    populated: bool
    length: int


@dataclass(frozen=True)
class ButtonCandidate:
    """One live-element candidate for the search/query button."""

    element: object  # live pywinauto UIA wrapper -- deliberately untyped here
    control_type: str
    automation_id: str
    class_name: str
    rect: Rect
    parent_class: str
    supports_invoke: bool


@dataclass
class AfterSnapshot:
    """One post-search read-only polling observation."""

    elapsed_seconds: float
    windows: list[WindowDiagnostic]
    new_handles: set[int]
    controls: list[ControlInfo]
    control_count: int
    field_states: dict[str, FieldSnapshot]
    not_found_evidence: bool
    error_window: WindowDiagnostic | None


@dataclass
class ReportState:
    """Accumulates whatever has been resolved so far. Fields default to
    'N/D' so an early abort still prints a complete, well-formed report
    instead of a partial/crashed one (Regla 14)."""

    trazabilidad_activa: str = "N/D"
    invoice_input_encontrado: str = "N/D"
    valor_preparado: str = "N/D"
    control_consulta_identificado: str = "N/D"
    metodo_seleccionado_pre: str = "N/D"

    search_ejecutado: str = "NO"
    search_cantidad: str = "0"
    search_metodo: str = "N/D"
    search_timestamp: str = "N/D"

    ui_estable: str = "N/D"
    controles_antes: str = "N/D"
    controles_despues: str = "N/D"
    field_changes: dict[str, str] = field(default_factory=dict)

    nuevos_controles: list[str] = field(default_factory=list)

    resultado: str = "N/D"

    screenshot_path: str = "N/D"
    log_path: str = "N/D"


# --------------------------------------------------------------------------
# Small local helpers
# --------------------------------------------------------------------------


def _rect_from_live_element(element) -> Rect | None:
    """Read-only rectangle read of a LIVE pywinauto element -- never mutates."""
    try:
        r = element.rectangle()
        return Rect(int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception as exc:
        logger.debug("No se pudo leer rectangle de elemento vivo: {}", exc)
        return None


def _parent_class_name(element) -> str:
    """Best-effort, read-only parent class name for logging/transparency."""
    try:
        parent = element.parent()
        return _safe_str(lambda: parent.element_info.class_name)
    except Exception:
        return "?"


def _supports_invoke_pattern(element) -> bool:
    """Best-effort, read-only check for UIA InvokePattern support.

    Uses pywinauto's `iface_invoke` (a cached property that resolves the
    underlying IUIAutomationInvokePattern COM interface, or None if this
    control does not support it) -- never calls Invoke() itself. This is
    ONLY used for the PASO 3 read-only report/log; the actual method used
    is always decided empirically by invoke_search_once()'s own try/except
    at the single authorized call site (Regla 14: never guess when the real
    outcome can be observed directly).
    """
    try:
        return getattr(element, "iface_invoke", None) is not None
    except Exception as exc:
        logger.debug("No se pudo verificar soporte de InvokePattern: {}", exc)
        return False


# --------------------------------------------------------------------------
# PASO 2: locate the search button -- LIVE elements, bounded recursive walk
# --------------------------------------------------------------------------


def _recursive_find_action_candidates(
    element,
    input_rect: Rect,
    results: list,
    max_depth: int,
    deadline: float,
    depth: int = 0,
) -> None:
    """Bounded recursive walk of LIVE elements (never ControlInfo metadata),
    collecting every descendant whose control_type looks button/custom-like
    AND satisfies _is_embedded_at_right_edge(input_rect, candidate_rect)
    (reused directly from inspect_trazabilidad_form.py -- the exact
    relationship F0.3C proved live for this app's DevExpress ButtonEdit
    layout). A live element is required (not flattened ControlInfo) because
    the caller must be able to call .invoke()/.click_input() on the SAME
    object later. Read-only: only ever reads control_type/rectangle here,
    never calls any action method.
    """
    if deadline is not None and time.monotonic() > deadline:
        return

    try:
        control_type = element.element_info.control_type or ""
    except Exception as exc:
        logger.debug("No se pudo leer control_type en profundidad {}: {}", depth, exc)
        control_type = ""

    if any(t in control_type.lower() for t in ACTION_LIKE_UIA_TYPES):
        rect = _rect_from_live_element(element)
        if rect is not None and _is_embedded_at_right_edge(input_rect, rect):
            results.append(element)

    if depth >= max_depth:
        return

    try:
        children = element.children()
    except Exception as exc:
        logger.debug("No se pudieron obtener hijos en profundidad {}: {}", depth, exc)
        return

    for child in children:
        if deadline is not None and time.monotonic() > deadline:
            return
        _recursive_find_action_candidates(child, input_rect, results, max_depth, deadline, depth + 1)


def find_search_button_candidates(
    window, input_rect: Rect, max_depth: int, timeout_seconds: float
) -> list[ButtonCandidate]:
    """Return every LIVE candidate button embedded at input_rect's right edge,
    with full transparency metadata (Regla 1: every candidate logged by the
    caller, nothing hidden)."""
    results: list = []
    deadline = time.monotonic() + timeout_seconds
    _recursive_find_action_candidates(window, input_rect, results, max_depth, deadline)

    candidates: list[ButtonCandidate] = []
    for el in results:
        rect = _rect_from_live_element(el)
        if rect is None:
            continue
        candidates.append(
            ButtonCandidate(
                element=el,
                control_type=_safe_str(lambda el=el: el.element_info.control_type),
                automation_id=_safe_str(lambda el=el: el.element_info.automation_id),
                class_name=_safe_str(lambda el=el: el.element_info.class_name),
                rect=rect,
                parent_class=_parent_class_name(el),
                supports_invoke=_supports_invoke_pattern(el),
            )
        )
    return candidates


# --------------------------------------------------------------------------
# PASO 4 / PASO 6: best-effort field-population reads (17 named fields)
#
# Rigor level (documented, per mission's own explicit allowance): this is a
# BEST-EFFORT read, deliberately less rigorous than F0.3C's single-field
# deep-dive disambiguation for INVOICE_INPUT. For each field label, the
# first Edit-like control satisfying _is_right_and_aligned() (which already
# covers BOTH this app's classic label-then-textbox layout AND its
# DevExpress DataItem-row containment layout -- see
# inspect_trazabilidad_form.py) is accepted, with no further tie-breaking
# among multiple candidates. This mirrors the precedent already established
# in this codebase (find_invoice_input_candidates_uia/_win32's `.name`
# holding the live value for a UIA-exposed Edit-like control -- see
# find_invoice_input_candidates_win32's own comment "never store an Edit's
# actual text as name") -- `.name` is read ONLY to compute a boolean
# has-content + length, NEVER logged/persisted itself (Regla 6).
# --------------------------------------------------------------------------


def _find_label_control(controls: list[ControlInfo], variants: tuple[str, ...]) -> ControlInfo | None:
    for c in controls:
        if _matches_any(f"{c.name} {c.automation_id}", variants):
            return c
    return None


def read_field_states(controls: list[ControlInfo]) -> dict[str, FieldSnapshot]:
    """Best-effort populated/length read for every field in FIELD_LABEL_VARIANTS.

    Never raises: any field whose label or paired Edit-like control cannot
    be resolved is simply reported as not-populated (a clean, conservative
    default -- never a crash, Regla 14).
    """
    states: dict[str, FieldSnapshot] = {}
    for field_name, variants in FIELD_LABEL_VARIANTS.items():
        label = _find_label_control(controls, variants)
        if label is None:
            states[field_name] = FieldSnapshot(False, 0)
            continue
        label_rect = _control_info_rect(label)
        if label_rect is None:
            states[field_name] = FieldSnapshot(False, 0)
            continue

        matched: ControlInfo | None = None
        for c in controls:
            if c is label:
                continue
            if not any(t in c.control_type.lower() for t in EDIT_LIKE_UIA_TYPES):
                continue
            crect = _control_info_rect(c)
            if crect is None:
                continue
            if _is_right_and_aligned(label_rect, crect):
                matched = c
                break

        if matched is not None and matched.name.strip():
            states[field_name] = FieldSnapshot(True, len(matched.name))
        else:
            states[field_name] = FieldSnapshot(False, 0)
    return states


def _field_change_label(before: FieldSnapshot, after: FieldSnapshot) -> str:
    """Render one field's before/after delta -- boolean + length only."""
    if before.populated == after.populated:
        if after.populated:
            return f"NO_CAMBIO (longitud {after.length})"
        return "NO_CAMBIO"
    if after.populated:
        return f"AHORA_POBLADO (longitud {after.length})"
    return "AHORA_VACIO"


# --------------------------------------------------------------------------
# PASO 5: the one authorized action -- called from EXACTLY ONE call site
# (execute(), PASO 5 below). No retry, no loop. No SendKeys/keyboard
# anywhere in this file. No accDoDefaultAction, no second .invoke()/
# .click_input() on anything, anywhere, after this single call.
# --------------------------------------------------------------------------


def invoke_search_once(element) -> str:
    """Activate the search button EXACTLY ONCE.

    Prefers UIA InvokePattern (element.invoke()); falls back to
    click_input() ONLY if InvokePattern raises/is unsupported for this
    specific control -- pywinauto resolves the click's screen position
    live from the control itself, never from coordinates passed by this
    script. Called from exactly ONE call site in the entire script, never
    retried: if BOTH mechanisms fail, the exception from click_input()
    propagates to that single call site, which logs it and aborts -- never
    a third attempt, never a fallback to raw coordinates/mouse_event/
    pyautogui.

    Returns "invoke_pattern" or "click_input" -- the method actually used.
    """
    method = "invoke_pattern" if _supports_invoke_pattern(element) else "click_input"
    if method == "invoke_pattern":
        element.invoke()
    else:
        element.click_input()
    return method


# --------------------------------------------------------------------------
# PASO 9: BMP screenshot -- COPIED (not imported) from
# poc_click_trazabilidad.py's capture_window_bmp(), byte-for-byte identical
# logic, per the same self-containment rationale used by poc_write_invoice.py.
# --------------------------------------------------------------------------


def capture_window_bmp(handle: int, output_path: Path) -> Path:
    """Capture handle's current window rect as a .bmp file, using only pywin32
    (GetWindowDC + CreateDCFromHandle + BitBlt + SaveBitmapFile).

    Best-effort: never raises on a capture failure -- logs a warning and
    returns the intended path, so a screenshot problem never blocks the
    rest of the read-only report.
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
# PASO 4/6: BEFORE/AFTER content scan helpers (read-only)
# --------------------------------------------------------------------------


def _scan_go_controls(go_handle: int, max_depth: int, timeout_seconds: float) -> list[ControlInfo]:
    """Bounded, read-only UIA scan of the GO window. Never raises: any
    failure degrades to an empty control list (Regla 14 -- this only runs
    for inspection, never gates the single authorized action after it has
    already happened)."""
    try:
        from pywinauto import Desktop

        element = Desktop(backend="uia").window(handle=go_handle)
        element.wait("exists", timeout=5)
    except Exception as exc:
        _classify_and_log(exc, f"fallo (no fatal) reconectando via UIA a ventana GO {go_handle}")
        return []

    deadline = time.monotonic() + timeout_seconds
    try:
        return walk_tree(element, max_depth=max_depth, deadline=deadline)
    except Exception as exc:
        _classify_and_log(exc, f"fallo (no fatal) escaneando UIA de ventana {go_handle}")
        return []


def _snapshot_target_windows(pids: list[int]) -> list[WindowDiagnostic]:
    """Enumerate current top-level windows belonging to pids -- read-only."""
    all_windows = enumerate_all_windows()
    return [w for w in all_windows if w.process_id in pids]


def _looks_like_error_dialog(entry: WindowDiagnostic) -> bool:
    """Conservative, evidence-based check: BOTH a known Win32 dialog class
    hint AND explicit error-looking title text must be present. Mirrors
    poc_click_trazabilidad.py's _looks_like_error_dialog() logic, rewritten
    fresh here per mission instruction (never import that module)."""
    class_hit = any(hint in entry.class_name.lower() for hint in ERROR_DIALOG_CLASS_HINTS)
    title_hit = any(hint in entry.title.lower() for hint in ERROR_TEXT_HINTS)
    return class_hit and title_hit


def _check_not_found_evidence(controls: list[ControlInfo]) -> bool:
    """True only if explicit 'not found'-looking text is present -- never
    inferred from silence/absence of other evidence (Regla 14: don't
    over-infer)."""
    for c in controls:
        haystack = f"{c.name} {c.automation_id}"
        if _matches_any(haystack, NOT_FOUND_TEXT_HINTS):
            return True
    return False


# --------------------------------------------------------------------------
# PASO 6: AFTER polling (read-only, bounded)
# --------------------------------------------------------------------------


def poll_after_search(
    pids: list[int],
    go_handle: int,
    before_handles: set[int],
    max_depth: int,
    scan_timeout: float,
    post_search_timeout: float,
    poll_interval: float,
) -> tuple[list[AfterSnapshot], bool]:
    """Read-only polling loop, bounded to post_search_timeout total, one poll
    every poll_interval. NEVER sends any interaction -- only enumerates
    windows and walks the UIA tree.

    Stops early once STABLE_POLLS_TO_STOP_EARLY consecutive polls agree on
    the field-population signature, or as soon as an error dialog is
    detected -- but never exceeds post_search_timeout regardless.

    Returns (polls, stable_reached) -- stable_reached is True only when the
    loop stopped because STABLE_POLLS_TO_STOP_EARLY consecutive polls
    agreed (used for the report's "UI estable" field); False when it
    stopped due to the hard timeout or an error dialog.
    """
    start = time.monotonic()
    polls: list[AfterSnapshot] = []
    stable_streak = 0
    previous_signature: tuple | None = None
    stable_reached = False

    while True:
        elapsed = time.monotonic() - start
        if elapsed > post_search_timeout:
            logger.info("Limite de {}s de polling post-busqueda alcanzado; deteniendo.", post_search_timeout)
            break

        windows = _snapshot_target_windows(pids)
        current_handles = {w.handle for w in windows}
        new_handles = current_handles - before_handles

        controls = _scan_go_controls(go_handle, max_depth, scan_timeout)
        field_states = read_field_states(controls)
        not_found = _check_not_found_evidence(controls)

        error_window: WindowDiagnostic | None = None
        for w in windows:
            if w.handle in new_handles and _looks_like_error_dialog(w):
                error_window = w
                break

        snapshot = AfterSnapshot(
            elapsed_seconds=elapsed,
            windows=windows,
            new_handles=new_handles,
            controls=controls,
            control_count=len(controls),
            field_states=field_states,
            not_found_evidence=not_found,
            error_window=error_window,
        )
        polls.append(snapshot)
        logger.info(
            "Poll post-busqueda @{:.2f}s: ventanas={} nuevas={} controles_uia={} "
            "campos_poblados={} not_found_evidence={} error_dialog={}",
            elapsed,
            len(windows),
            sorted(new_handles),
            len(controls),
            sum(1 for v in field_states.values() if v.populated),
            not_found,
            bool(error_window),
        )

        if error_window is not None:
            logger.warning("Dialogo de error detectado; deteniendo polling de inmediato (evidencia real).")
            break

        signature = tuple(sorted((k, v.populated) for k, v in field_states.items()))
        if previous_signature is not None and signature == previous_signature:
            stable_streak += 1
        else:
            stable_streak = 1
        previous_signature = signature

        if stable_streak >= STABLE_POLLS_TO_STOP_EARLY:
            logger.info(
                "{} polls consecutivos estables (mismo estado de poblacion de campos); "
                "se detiene el polling antes del limite de {}s.",
                stable_streak,
                post_search_timeout,
            )
            stable_reached = True
            break

        time.sleep(poll_interval)

    return polls, stable_reached


# --------------------------------------------------------------------------
# PASO 7: classification
# --------------------------------------------------------------------------


def classify_result(
    before_field_states: dict[str, FieldSnapshot],
    polls: list[AfterSnapshot],
) -> str:
    """Classify exactly one of the four post-search RESULTADO outcomes.

    Decision order (documented, evidence-first -- mirrors
    poc_click_trazabilidad.py's classify_result() discipline):

      1. APPLICATION_ERROR: ONLY when a poll observed a genuinely new
         top-level window that looks like a standard error dialog (checked
         across ALL polls, since a dialog can appear and be dismissed by
         the app before the final poll).
      2. INVOICE_NOT_FOUND: ONLY when explicit "not found"-looking text was
         observed in any poll -- never inferred from silence.
      3. INVOICE_FOUND: at least KEY_INVOICE_FOUND_FIELDS-worth of real
         invoice-detail fields transitioned from not-populated to populated
         (a coherent set, not just the search button's own state changing).
      4. SEARCH_NO_EFFECT: fallback -- nothing above matched, i.e. no error,
         no not-found evidence, and the field-population set stayed the
         same as BEFORE.
    """
    if not polls:
        return RESULT_SEARCH_NO_EFFECT

    for poll in polls:
        if poll.error_window is not None:
            return RESULT_APPLICATION_ERROR

    if any(poll.not_found_evidence for poll in polls):
        return RESULT_INVOICE_NOT_FOUND

    final = polls[-1]
    newly_populated = [
        name
        for name, after in final.field_states.items()
        if after.populated and not before_field_states.get(name, FieldSnapshot(False, 0)).populated
    ]
    key_hits = sum(1 for name in KEY_INVOICE_FOUND_FIELDS if name in newly_populated)

    if key_hits >= 2 and len(newly_populated) >= 3:
        return RESULT_INVOICE_FOUND

    return RESULT_SEARCH_NO_EFFECT


# --------------------------------------------------------------------------
# PASO 8: deep read-only mapping (only when a real change was observed)
# --------------------------------------------------------------------------


def _control_signature(c: ControlInfo) -> tuple:
    return (c.control_type, c.name, c.automation_id, c.rectangle)


def _is_potentially_value_holding(c: ControlInfo) -> bool:
    """True for the exact control_types this app is known (F0.3C/F0.4A live
    evidence) to expose real field VALUES through -- see EDIT_LIKE_UIA_TYPES.
    "custom" deliberately overlaps with ACTION_LIKE_UIA_TYPES too (this
    app's DevExpress controls are ambiguous by control_type alone), so it is
    treated as potentially sensitive here -- never assume a "custom"-typed
    control is "just a button" (Regla 6: fail toward redaction, not leakage).
    """
    return any(t in c.control_type.lower() for t in EDIT_LIKE_UIA_TYPES)


def _safe_search_haystack(c: ControlInfo) -> str:
    """Keyword-search haystack that NEVER includes a value-holding control's
    own text (Regla 6): automation_id is always included (a developer-
    assigned identifier, never a patient/invoice value); c.name is included
    only when this control_type is not one of the value-holding types."""
    parts = [c.automation_id]
    if not _is_potentially_value_holding(c):
        parts.append(c.name)
    return " ".join(parts).lower()


def _safe_display_name(c: ControlInfo) -> str:
    """Render a control's name for the report -- redacted (boolean+length
    only) for any potentially value-holding control_type, raw otherwise
    (navigation/action captions like "Documento origen"/"Imprimir" are not
    sensitive -- same precedent as inspect_trazabilidad_form.py's action
    block and poc_click_trazabilidad.py's matched-text reporting)."""
    if _is_potentially_value_holding(c):
        has_content = bool(c.name.strip())
        return f"<no_expuesto: poblado={'SI' if has_content else 'NO'} longitud={len(c.name) if has_content else 0}>"
    return repr(c.name)


def deep_map_relevant_controls(
    before_controls: list[ControlInfo], after_controls: list[ControlInfo]
) -> list[str]:
    """READ-ONLY: list keyword-matching controls plus NEW controls of
    interesting types (relative to BEFORE). Never interacts with anything
    found -- only reads control_type/name/automation_id/rectangle, and never
    exposes the raw name of a potentially value-holding control (Regla 6;
    see _safe_search_haystack()/_safe_display_name())."""
    before_sigs = {_control_signature(c) for c in before_controls}
    lines: list[str] = []
    seen: set[tuple] = set()

    for c in after_controls:
        sig = _control_signature(c)
        if sig in seen:
            continue
        keyword_hit = any(k in _safe_search_haystack(c) for k in DEEP_MAP_KEYWORDS)
        is_new = sig not in before_sigs
        type_hit = is_new and any(t in c.control_type.lower() for t in DEEP_MAP_CONTROL_TYPES)
        if keyword_hit or type_hit:
            seen.add(sig)
            lines.append(
                f"- control_type={c.control_type} name={_safe_display_name(c)} "
                f"automation_id={c.automation_id!r} rectangle={c.rectangle}"
            )

    return lines


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def render_report(state: ReportState) -> str:
    lines = [
        "PRE-SEARCH:",
        f"- trazabilidad activa: {state.trazabilidad_activa}",
        f"- invoice input encontrado: {state.invoice_input_encontrado}",
        f"- valor preparado: {state.valor_preparado}",
        f"- control consulta identificado: {state.control_consulta_identificado}",
        f"- metodo seleccionado: {state.metodo_seleccionado_pre}",
        "",
        "SEARCH:",
        f"- ejecutado: {state.search_ejecutado}",
        f"- cantidad de acciones: {state.search_cantidad}",
        f"- metodo: {state.search_metodo}",
        f"- timestamp: {state.search_timestamp}",
        "",
        "POST-SEARCH:",
        f"- UI estable: {state.ui_estable}",
        f"- controles UIA antes: {state.controles_antes}",
        f"- controles UIA despues: {state.controles_despues}",
        "- campos que cambiaron:",
    ]
    for name in REPORT_EXPLICIT_FIELDS:
        lines.append(f"  - {name}: {state.field_changes.get(name, 'N/D')}")
    otros = [
        f"{name}={state.field_changes[name]}"
        for name in state.field_changes
        if name not in REPORT_EXPLICIT_FIELDS
    ]
    lines.append(f"  - otros: {', '.join(otros) if otros else 'N/D'}")
    lines += [
        "",
        "NUEVOS CONTROLES RELEVANTES:",
    ]
    if state.nuevos_controles:
        lines.extend(state.nuevos_controles)
    else:
        lines.append("(ninguno / no evaluado)")
    lines += [
        "",
        "RESULTADO:",
        f"- {state.resultado}",
        "",
        f"(screenshot: {state.screenshot_path})",
        f"(reporte guardado en: {state.log_path})",
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
    post_search_timeout: float,
    poll_interval: float,
    screenshot_path: Path,
    log_path: Path,
) -> tuple[ReportState, int]:
    """Run the full validation trail and, if every pre-search check passes,
    the single authorized search action + read-only post-search inspection.
    Never raises for an expected condition (Regla 14)."""
    state = ReportState()

    # === PASO 1: localizar GO autenticada + confirmar pantalla activa ===
    logger.info("PASO 1: localizando ventana autenticada de GO (sin PID/handle recordado)...")
    try:
        pids = find_target_process_ids(TARGET_PROCESS_NAME)
    except Exception as exc:
        _classify_and_log(exc, "fallo al resolver PIDs de GO")
        state.trazabilidad_activa = "NO"
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1

    if not pids:
        logger.warning("TRAZABILIDAD_NOT_ACTIVE: no hay ningun proceso '{}' en ejecucion", TARGET_PROCESS_NAME)
        state.trazabilidad_activa = "NO"
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1

    try:
        all_windows = enumerate_all_windows()
    except (WindowNotFoundError, Exception) as exc:
        _classify_and_log(exc, "fallo enumerando ventanas")
        state.trazabilidad_activa = "NO"
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1

    try:
        go_window = find_authenticated_go_window(pids, all_windows, require_favoritos_signals=False)
    except (WindowNotFoundError, GoAmbiguousError) as exc:
        logger.warning("TRAZABILIDAD_NOT_ACTIVE: {}", exc)
        state.trazabilidad_activa = "NO"
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado localizando la ventana autenticada de GO")
        state.trazabilidad_activa = "NO"
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1

    logger.info("GO localizada: handle={} pid={}", go_window.handle, go_window.process_id)

    logger.info("PASO 1b: validando que la pantalla Trazabilidad de Factura este activa...")
    try:
        validate_screen_active(go_window.handle, TRAZABILIDAD_GATE_MAX_DEPTH, TRAZABILIDAD_GATE_TIMEOUT_SECONDS)
    except TrazabilidadNotActiveError as exc:
        logger.warning("TRAZABILIDAD_NOT_ACTIVE: {}", exc)
        state.trazabilidad_activa = "NO"
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1

    state.trazabilidad_activa = "SI"
    logger.info("Pantalla 'Trazabilidad de Factura' confirmada activa.")

    # === PASO 1c: verificar primer plano (nunca se activa GO) ===
    logger.info("PASO 1c: verificando que GO este en primer plano (nunca se activa)...")
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

    logger.info("GO confirmada en primer plano.")

    # === PASO 1d: localizar INVOICE_INPUT exclusivamente por automation_id ===
    logger.info("PASO 1d: localizando INVOICE_INPUT por automation_id (F0.4A)...")
    try:
        from pywinauto import Desktop

        window = Desktop(backend="uia").window(handle=go_window.handle)
        window.wait("exists", timeout=5)
    except Exception as exc:
        _classify_and_log(exc, f"fallo al reconectar via UIA a la ventana GO {go_window.handle}")
        state.invoice_input_encontrado = "NO"
        state.resultado = RESULT_INVOICE_VALUE_NOT_READY
        return state, 1

    try:
        candidates, method_used = find_invoice_input_elements(window)
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado buscando INVOICE_INPUT por automation_id")
        state.invoice_input_encontrado = "NO"
        state.resultado = RESULT_INVOICE_VALUE_NOT_READY
        return state, 1

    visible_enabled = _filter_visible_enabled(candidates)
    logger.info(
        "Busqueda de INVOICE_INPUT: metodo={} candidatos_crudos={} visible+enabled={}",
        method_used,
        len(candidates),
        len(visible_enabled),
    )
    if len(visible_enabled) != 1:
        logger.error(
            "INVOICE_VALUE_NOT_READY: se requiere exactamente 1 candidato visible+enabled "
            "(encontrados: {}).",
            len(visible_enabled),
        )
        state.invoice_input_encontrado = "NO"
        state.resultado = RESULT_INVOICE_VALUE_NOT_READY
        return state, 1

    invoice_element = visible_enabled[0]
    state.invoice_input_encontrado = "SI"
    input_rect = _rect_from_live_element(invoice_element)
    if input_rect is None:
        logger.error("INVOICE_VALUE_NOT_READY: no se pudo leer el rectangulo del campo.")
        state.resultado = RESULT_INVOICE_VALUE_NOT_READY
        return state, 1

    # === PASO 1e: verificar (SOLO LECTURA) que el valor ya este preparado.
    # NUNCA se escribe/corrige aqui -- set_text()/type_keys() no existen en
    # este archivo. ===
    logger.info("PASO 1e: verificando (solo lectura) que el campo ya contenga el valor esperado...")
    current_value = _read_current_value(invoice_element)
    matches_expected = len(current_value) == 9 and current_value == invoice
    logger.info(
        "Verificacion de valor preparado: longitud_actual={} longitud_esperada=9 coincide={}",
        len(current_value),
        matches_expected,
    )
    if not matches_expected:
        logger.error("INVOICE_VALUE_NOT_READY: el campo no contiene el valor esperado; no se corrige.")
        state.valor_preparado = "NO"
        state.resultado = RESULT_INVOICE_VALUE_NOT_READY
        return state, 1

    state.valor_preparado = "SI"
    logger.info("Valor preparado confirmado (solo booleano/longitud, nunca el valor real).")

    # === PASO 2: localizar el boton de busqueda dinamicamente (elemento vivo) ===
    logger.info("PASO 2: localizando control de busqueda por relacion espacial (borde derecho del input)...")
    try:
        button_candidates = find_search_button_candidates(window, input_rect, max_depth, scan_timeout)
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado buscando el control de busqueda")
        state.control_consulta_identificado = "NO"
        state.resultado = RESULT_SEARCH_CONTROL_AMBIGUOUS
        return state, 1

    for i, cand in enumerate(button_candidates):
        logger.info(
            "Candidato SEARCH_CONTROL [{}]: control_type={} automation_id={!r} class={} "
            "rect={} parent_class={} supports_invoke={}",
            i,
            cand.control_type,
            cand.automation_id,
            cand.class_name,
            cand.rect.as_tuple(),
            cand.parent_class,
            cand.supports_invoke,
        )

    if len(button_candidates) == 0:
        logger.error("SEARCH_CONTROL_AMBIGUOUS: 0 candidatos encontrados (caso 'no encontrado').")
        state.control_consulta_identificado = "NO"
        state.resultado = RESULT_SEARCH_CONTROL_AMBIGUOUS
        return state, 1
    if len(button_candidates) > 1:
        logger.error(
            "SEARCH_CONTROL_AMBIGUOUS: {} candidatos encontrados (caso '>1').", len(button_candidates)
        )
        state.control_consulta_identificado = "NO"
        state.resultado = RESULT_SEARCH_CONTROL_AMBIGUOUS
        return state, 1

    search_button = button_candidates[0]
    state.control_consulta_identificado = "SI"
    logger.info("Control de busqueda identificado (unico candidato).")

    # === PASO 3: determinar metodo de interaccion (solo lectura, sin accion) ===
    state.metodo_seleccionado_pre = "invoke_pattern" if search_button.supports_invoke else "click_input"
    logger.info(
        "PASO 3: metodo preferido (deteccion de solo lectura, NO es necesariamente el "
        "metodo real usado -- ver PASO 5): {}",
        state.metodo_seleccionado_pre,
    )

    # === PASO 4: snapshot ANTES (solo lectura) ===
    logger.info("PASO 4: capturando snapshot ANTES (solo lectura)...")
    try:
        before_windows = _snapshot_target_windows(pids)
        before_controls = _scan_go_controls(go_window.handle, max_depth, scan_timeout)
        before_field_states = read_field_states(before_controls)
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado capturando snapshot ANTES")
        state.resultado = f"{RESULT_ABORTED_PREFIX} (SNAPSHOT_BEFORE_FAILED)"
        return state, 1

    before_handles = {w.handle for w in before_windows}
    state.controles_antes = str(len(before_controls))
    logger.info(
        "Snapshot ANTES: {} ventanas top-level, {} controles UIA, {} campos poblados",
        len(before_windows),
        len(before_controls),
        sum(1 for v in before_field_states.values() if v.populated),
    )

    # === PASO 5: LA UNICA ACCION AUTORIZADA -- un unico call site, sin
    # bucle, sin reintento. ===
    logger.info("PASO 5: ejecutando la UNICA interaccion autorizada con el control de busqueda...")
    try:
        verify_go_foreground(go_window.handle, expected_pid=go_window.process_id)
        metodo = invoke_search_once(search_button.element)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado ejecutando la interaccion autorizada (ambos mecanismos)")
        state.search_ejecutado = "NO"
        state.resultado = f"{RESULT_ABORTED_PREFIX} (SEARCH_FAILED: {code})"
        return state, 1

    search_timestamp = datetime.now()
    state.search_ejecutado = "SI"
    state.search_cantidad = "1"
    state.search_metodo = metodo
    state.search_timestamp = search_timestamp.isoformat()
    logger.info("Interaccion ejecutada (1 vez). Metodo usado: {} a las {}", metodo, state.search_timestamp)

    # === A partir de aqui: SOLO inspeccion de solo lectura.
    # invoke_search_once() NUNCA se llama de nuevo, para ningun caso,
    # incluyendo manejo de errores. ===

    # === PASO 6: polling AFTER (solo lectura, acotado) ===
    logger.info("PASO 6: polling post-busqueda de solo lectura (hasta {}s)...", post_search_timeout)
    stable_reached = False
    try:
        polls, stable_reached = poll_after_search(
            pids=pids,
            go_handle=go_window.handle,
            before_handles=before_handles,
            max_depth=max_depth,
            scan_timeout=scan_timeout,
            post_search_timeout=post_search_timeout,
            poll_interval=poll_interval,
        )
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado durante el polling post-busqueda (solo lectura)")
        polls = []
        logger.warning("Polling post-busqueda incompleto por error: {}", code)

    final = polls[-1] if polls else None
    state.controles_despues = str(final.control_count) if final else "N/D"
    state.ui_estable = "SI" if stable_reached else "NO"

    final_field_states = final.field_states if final else {name: FieldSnapshot(False, 0) for name in FIELD_LABEL_VARIANTS}
    for name in FIELD_LABEL_VARIANTS:
        before_snap = before_field_states.get(name, FieldSnapshot(False, 0))
        after_snap = final_field_states.get(name, FieldSnapshot(False, 0))
        state.field_changes[name] = _field_change_label(before_snap, after_snap)

    # === PASO 7: clasificacion ===
    logger.info("PASO 7: clasificando el resultado final...")
    result_code = classify_result(before_field_states, polls)
    state.resultado = result_code
    logger.info("Clasificacion final: {}", result_code)

    # === PASO 8: mapeo profundo de solo lectura (solo si hubo cambio real) ===
    if result_code not in (RESULT_SEARCH_NO_EFFECT,) and final is not None:
        logger.info("PASO 8: mapeo profundo de solo lectura de controles nuevos/relevantes...")
        try:
            state.nuevos_controles = deep_map_relevant_controls(before_controls, final.controls)
        except Exception as exc:
            code = _classify_and_log(exc, "fallo inesperado (no fatal) durante el mapeo profundo")
            state.nuevos_controles = [f"(mapeo profundo incompleto: {code})"]
    else:
        logger.info("PASO 8: omitido (SEARCH_NO_EFFECT o sin snapshot AFTER -- nada nuevo que mapear).")

    # The populated result can contain patient data; keep text-only technical
    # evidence and do not create a new screenshot.
    logger.info("PASO 9: captura omitida por privacidad (resultado con datos sensibles).")
    state.screenshot_path = "N/D (omitida por privacidad)"

    state.log_path = str(log_path)
    return state, 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments. --invoice is required and validated (forbidden
    pywinauto special-key characters, defense-in-depth per mission spec)
    before any interaction with GO -- even though this script never types
    anything, the value is still compared/handled as a string."""
    parser = argparse.ArgumentParser(
        description=(
            "F0.4B: ejecuta UNA UNICA vez la interaccion autorizada con el control de "
            "busqueda/consulta de 'Trazabilidad de Factura' en GO/Indigo, luego inspecciona "
            "(solo lectura) lo que se cargo. Nunca escribe en el campo de factura."
        )
    )
    parser.add_argument(
        "--invoice",
        required=True,
        help=(
            "Numero de factura ya escrito (F0.4A) que se espera encontrar en el campo -- "
            "usado SOLO para verificacion en memoria, nunca se vuelve a escribir."
        ),
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
        help=(
            "Presupuesto de tiempo en segundos para cada recorrido UIA acotado "
            f"(default: {TRAZABILIDAD_GATE_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--post-search-timeout",
        type=float,
        default=DEFAULT_POST_SEARCH_TIMEOUT_SECONDS,
        help=(
            "Limite duro en segundos para el polling de solo lectura post-busqueda "
            f"(default: {DEFAULT_POST_SEARCH_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Intervalo en segundos entre polls post-busqueda (default: {DEFAULT_POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--screenshot-path",
        type=Path,
        default=DEFAULT_SCREENSHOT_PATH,
        help=f"Ruta de la captura de pantalla BMP (default: {DEFAULT_SCREENSHOT_PATH}).",
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=DEFAULT_LOG_PATH,
        help=f"Ruta del reporte de texto final (default: {DEFAULT_LOG_PATH}).",
    )
    args = parser.parse_args(argv)

    try:
        _validate_invoice_value(args.invoice)
    except InvalidInvoiceValueError as exc:
        # Never include the raw --invoice value in this message (Regla 6).
        parser.error(str(exc))

    return args


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the F0.4B one-shot search+inspect PoC."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    args = parse_args(argv)

    logger.warning(
        "F0.4B: iniciando PoC de UNA UNICA interaccion autorizada con el control de "
        "busqueda de 'Trazabilidad de Factura'. max_depth={}, timeout={}s, "
        "post_search_timeout={}s, poll_interval={}s",
        args.max_depth,
        args.timeout,
        args.post_search_timeout,
        args.poll_interval,
    )

    try:
        state, exit_code = execute(
            invoice=args.invoice,
            max_depth=args.max_depth,
            scan_timeout=args.timeout,
            post_search_timeout=args.post_search_timeout,
            poll_interval=args.poll_interval,
            screenshot_path=args.screenshot_path,
            log_path=args.log_path,
        )
    except Exception as exc:
        # Defensive last-resort net: execute() is written to catch and
        # classify every expected failure mode internally, and critically
        # never calls invoke_search_once() more than once on any path. This
        # only guards against a genuinely unexpected error that escaped
        # every try/except above.
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
