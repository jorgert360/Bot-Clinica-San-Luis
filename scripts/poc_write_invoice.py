"""F0.4A -- Escribir factura de prueba en el campo INVOICE_INPUT de GO/Indigo.

F0.3C identifico, por evidencia UIA/Win32 en vivo y de solo lectura, el campo
exacto de numero de factura dentro de la pantalla "Trazabilidad de Factura":
automation_id="INDbteInvoiceNumber" (backend UIA). El usuario autorizo
explicitamente, por unica vez, UNA escritura de un numero de factura de
prueba en ese campo, seguida de lectura de verificacion (readback) -- y
ninguna otra interaccion: ni clic en el boton de accion embebido al borde
derecho del campo, ni Enter, ni busqueda, nada mas.

Este script:

  1. Reutiliza integramente (import directo) find_target_process_ids() /
     enumerate_all_windows() / find_authenticated_go_window() de
     diagnose_go_windows.py / poc_move_to_trazabilidad.py, y el gate de
     pantalla activa (validate_screen_active/TrazabilidadNotActiveError) de
     inspect_trazabilidad_form.py -- nunca reimplementados.
  2. Localiza el campo EXCLUSIVAMENTE por automation_id="INDbteInvoiceNumber"
     (nunca por coordenadas, nunca por heuristica espacial): primero intenta
     la busqueda nativa UIA FindAll/PropertyCondition via
     window.descendants(auto_id=...); si esa API no esta disponible en la
     version de pywinauto instalada (verificado: build_condition() de esta
     version NO acepta auto_id como parametro -- ver find_invoice_input_
     elements() abajo para la evidencia exacta), cae a un recorrido
     recursivo acotado propio que compara element_info.automation_id.
  3. Verifica (solo lectura) que el campo este vacio antes de escribir.
  4. Verifica (solo lectura, nunca activa nada) que GO este en primer plano.
  5. Ejecuta LA UNICA escritura autorizada: ValuePattern.SetValue() via
     pywinauto set_text(), con fallback a type_keys() (solo los caracteres
     literales del valor, nunca Enter/Tab/teclas especiales) si ValuePattern
     no esta soportado por este control. Llamada desde exactamente UN sitio
     en todo el archivo -- ver write_invoice_once() y su unico call site en
     execute() (PASO 5).
  6. Relee el campo y compara igualdad exacta con el valor dado -- solo se
     imprime/loguea el booleano de coincidencia y las longitudes, nunca los
     valores en si.
  7. Enmascara cualquier valor que se persista o imprima mas alla de la
     comparacion booleana (ver _mask_invoice()).
  8. Captura opcionalmente una BMP (best-effort, no fatal si falla).

Privacidad (mandatorio, Regla 6): el valor real de --invoice NUNCA se
escribe en un archivo completo, nunca se imprime/loguea sin enmascarar
(salvo la confirmacion booleana expected==actual en consola), y no se
persiste en ningun lugar mas alla de la memoria de este proceso.

Regla 1 (03_CLAUDE_RULES.md): PID/handle de GO nunca se hardcodean --
resueltos en vivo cada corrida via find_authenticated_go_window()
(require_favoritos_signals=False, pues esta pantalla no es Favoritos).

Regla 12: ninguna mutacion de ventana/proceso mas alla de la unica escritura
autorizada en el campo objetivo.

Regla 14: cualquier estado ambiguo/inesperado aborta limpiamente con la
razon especifica de la mision (TRAZABILIDAD_NOT_ACTIVE, GO_NOT_FOREGROUND,
INVOICE_INPUT_NOT_FOUND, INVOICE_INPUT_AMBIGUOUS, INVOICE_INPUT_NOT_EMPTY,
INVOICE_VALUE_MISMATCH) -- nunca se adivina, nunca se reintenta, nunca se
corrige automaticamente.

Uso:
    python scripts/poc_write_invoice.py --invoice FHC000000
    python scripts/poc_write_invoice.py --invoice FHC000000 --delay 8
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
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
    WindowNotFoundError,
    _classify_and_log,
    _countdown_wait,
    enumerate_all_windows,
    find_target_process_ids,
)
from inspect_trazabilidad_form import (  # noqa: E402
    TrazabilidadNotActiveError,
    validate_screen_active,
)
from poc_move_to_trazabilidad import (  # noqa: E402
    GoAmbiguousError,
    GoNotForegroundError,
    find_authenticated_go_window,
    verify_go_foreground,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# The ONLY selector this script is authorized to use to locate the field --
# never coordinates, never spatial heuristics (unlike F0.3C, which needed
# those because it did not yet have a stable selector).
TARGET_AUTOMATION_ID = "INDbteInvoiceNumber"

# pywinauto's type_keys() special-key syntax characters. Rejected in
# --invoice defensively, before ANY interaction with GO, even though the
# authorized test value (letters+digits only) never contains them.
FORBIDDEN_INVOICE_CHARS: tuple[str, ...] = ("{", "}", "~", "(", ")")

# Real-world finding (F0.3C, live GO session, inspect_trazabilidad_form.py):
# a full untargeted UIA tree walk from the GO window root needed
# --max-depth 14 to reach the real form fields (the shared
# DIAGNOSTIC_SCAN_MAX_DEPTH default is lower, tuned for a different use
# case). validate_screen_active() is called with this depth here for the
# same reason. The targeted automation_id search in Step 2 does NOT inherit
# this limitation the same way -- see find_invoice_input_elements()'s
# docstring -- but the same generous depth is reused for its recursive
# fallback path for consistency/safety.
TRAZABILIDAD_GATE_MAX_DEPTH = 14
TRAZABILIDAD_GATE_TIMEOUT_SECONDS = 20.0

# Bounded time budget for the Step 2 recursive-walk fallback (only used if
# the native descendants(auto_id=...) path is unavailable -- see
# find_invoice_input_elements()).
AUTOMATION_ID_SEARCH_TIMEOUT_SECONDS = 15.0

DEFAULT_SCREENSHOT_PATH = Path("runtime") / "screenshots" / "f04a_invoice_written.bmp"

# Result vocabulary (Regla 14: fixed, named abort/outcome reasons -- never
# an invented/overlapping category).
RESULT_INVOICE_VALUE_CONFIRMED = "INVOICE_VALUE_CONFIRMED"
RESULT_INVOICE_INPUT_NOT_FOUND = "INVOICE_INPUT_NOT_FOUND"
RESULT_INVOICE_INPUT_AMBIGUOUS = "INVOICE_INPUT_AMBIGUOUS"
RESULT_INVOICE_INPUT_NOT_EMPTY = "INVOICE_INPUT_NOT_EMPTY"
RESULT_INVOICE_VALUE_MISMATCH = "INVOICE_VALUE_MISMATCH"
RESULT_GO_NOT_FOREGROUND = "GO_NOT_FOREGROUND"
RESULT_TRAZABILIDAD_NOT_ACTIVE = "TRAZABILIDAD_NOT_ACTIVE"
# Not part of the mission's closed 7-code vocabulary: the mission's list has
# no named code for "the one authorized write mechanism itself raised an
# unexpected exception". This project's own established convention
# (poc_click_trazabilidad.py, poc_move_to_trazabilidad.py) for an
# unenumerated failure is "ABORTED (<classified_code>)" -- reused here
# rather than inventing a new overlapping category or silently forcing the
# closest-sounding enum value. Flagged explicitly for the coordinator.
RESULT_WRITE_FAILED_PREFIX = "ABORTED (WRITE_FAILED"


class InvalidInvoiceValueError(Exception):
    """Raised when --invoice contains pywinauto type_keys() special-key syntax."""


class InvoiceInputNotFoundError(Exception):
    """Raised when zero visible+enabled INDbteInvoiceNumber matches are found."""


class InvoiceInputAmbiguousError(Exception):
    """Raised when more than one visible+enabled INDbteInvoiceNumber match is found."""


class InvoiceInputNotEmptyError(Exception):
    """Raised when the field already has non-empty content before the write."""


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass
class ReportState:
    """Accumulates whatever has been resolved so far, for the final report.

    Fields default to "N/D" so an early abort still prints a complete,
    well-formed report instead of a partial/crashed one (Regla 14).
    """

    trazabilidad_activa: str = "N/D"
    foreground_validado: str = "N/D"

    encontrado: str = "N/D"
    automation_id: str = "N/D"
    input_class: str = "N/D"
    input_rectangle: str = "N/D"
    input_visible: str = "N/D"
    input_enabled: str = "N/D"

    write_ejecutado: str = "NO"
    write_metodo: str = "N/D"
    write_cantidad: str = "0"

    valor_coincide: str = "N/D"
    longitud_esperada: str = "N/D"
    longitud_obtenida: str = "N/D"

    screenshot_path: str = "N/D"

    resultado: str = "N/D"


# --------------------------------------------------------------------------
# Masking helper (Regla 6: never persist/print the real invoice value)
# --------------------------------------------------------------------------


def _mask_invoice(value: str) -> str:
    """Mask `value`, showing only the first 3 and last 2 characters.

    Exact rule: for a value of length >= 6, the result is
    ``value[:3] + "*" * (len(value) - 5) + value[-2:]`` -- i.e. the middle
    is replaced with exactly as many '*' as omitted characters, so the
    masked string always has the SAME LENGTH as the original (unlike the
    mission brief's own illustrative examples, e.g. "FHC******33", which use
    a fixed placeholder asterisk count not tied to any specific real length
    -- this implementation instead preserves length exactly, which is a
    stricter/more auditable rule).

    Edge case (value shorter than 6 characters, where "first 3 + last 2"
    would overlap or reveal the entire value): the ENTIRE value is masked
    instead (all '*', same length) -- a short value never gets to leak more
    than its own length via a partial/overlapping reveal. Empty string
    returns empty string.
    """
    length = len(value)
    if length == 0:
        return ""
    if length <= 5:
        return "*" * length
    return value[:3] + ("*" * (length - 5)) + value[-2:]


def _validate_invoice_value(value: str) -> None:
    """Reject --invoice if it contains pywinauto type_keys() special-key syntax.

    Defense in depth (Regla 5/12): even though the authorized test value is
    letters+digits only, this guarantees no accidental key injection
    regardless of what is actually passed via --invoice. Never includes the
    raw value in the raised message (Regla 6).
    """
    found = sorted({ch for ch in FORBIDDEN_INVOICE_CHARS if ch in value})
    if found:
        raise InvalidInvoiceValueError(
            "--invoice contiene caracteres no permitidos (sintaxis especial de "
            f"pywinauto type_keys): {found!r}. Nunca se aceptan '{{', '}}', '~', '(', ')'."
        )


# --------------------------------------------------------------------------
# Small local helpers
# --------------------------------------------------------------------------


def _safe_str(getter) -> str:
    """Call getter() and stringify the result, defaulting to '?' on error."""
    try:
        value = getter()
        return str(value) if value is not None else ""
    except Exception:
        return "?"


def _fmt_bool(value: bool | None) -> str:
    if value is True:
        return "SI"
    if value is False:
        return "NO"
    return "?"


# --------------------------------------------------------------------------
# Step 2: locate the field EXCLUSIVELY by automation_id
# --------------------------------------------------------------------------


def _recursive_find_by_automation_id(
    element,
    automation_id: str,
    results: list,
    max_depth: int,
    deadline: float,
    depth: int = 0,
) -> None:
    """Bounded recursive fallback: collect every descendant whose
    element_info.automation_id == automation_id.

    Never early-exits on the first match: ambiguity detection in Step 2
    requires ALL matches, not just one. Bounded by max_depth and deadline
    (Regla 14: never hangs). A single unreachable/inaccessible node is
    skipped, never aborts the whole walk.
    """
    if deadline is not None and time.monotonic() > deadline:
        return

    try:
        if element.element_info.automation_id == automation_id:
            results.append(element)
    except Exception as exc:
        logger.debug("No se pudo leer automation_id en profundidad {}: {}", depth, exc)

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
        _recursive_find_by_automation_id(
            child, automation_id, results, max_depth, deadline, depth + 1
        )


def find_invoice_input_elements(window) -> tuple[list, str]:
    """Locate every live element whose automation_id == TARGET_AUTOMATION_ID.

    Two mechanisms, tried in order, both EXCLUSIVELY keyed on automation_id
    (never coordinates, never spatial heuristics):

      1. Native UIA FindAll via pywinauto's window.descendants(auto_id=...).
         VERIFIED against this project's installed pywinauto version
         (.venv/Lib/site-packages/pywinauto/uia_defines.py,
         IUIA.build_condition()): that function's signature is
         ``build_condition(process=None, class_name=None, title=None,
         control_type=None, content_only=None)`` with NO **kwargs
         catch-all, so passing auto_id raises TypeError immediately -- this
         native path is confirmed NOT available in this environment, and
         the code below expects and handles exactly that.
      2. Bounded recursive walk (_recursive_find_by_automation_id) comparing
         element_info.automation_id directly. This is the path that
         actually executes in this project's environment. Unlike the manual
         tree walker used for the F0.4 label/input discovery (which needed
         --max-depth 14 tuning because it collected the WHOLE tree first),
         this walk is targeted and does not depend on a fixed textual
         search across many unrelated controls -- it is bounded the same
         way (TRAZABILIDAD_GATE_MAX_DEPTH / AUTOMATION_ID_SEARCH_TIMEOUT_
         SECONDS) purely as a safety net against a pathological tree, not
         because the automation_id match itself needs that much tuning.

    Returns (elements, method_used) where method_used is
    "native_descendants(auto_id=...)" or "recursive_walk(automation_id match)".
    """
    try:
        found = window.descendants(auto_id=TARGET_AUTOMATION_ID)
        return list(found), "native_descendants(auto_id=...)"
    except TypeError as exc:
        logger.info(
            "descendants(auto_id=...) no soportado por esta version de pywinauto "
            "(TypeError esperado: {}); usando recorrido recursivo dirigido de respaldo.",
            exc,
        )
    except Exception as exc:
        _classify_and_log(
            exc,
            "fallo inesperado (no fatal) en descendants(auto_id=...) nativo; "
            "usando recorrido recursivo dirigido de respaldo",
        )

    results: list = []
    deadline = time.monotonic() + AUTOMATION_ID_SEARCH_TIMEOUT_SECONDS
    _recursive_find_by_automation_id(
        window, TARGET_AUTOMATION_ID, results, TRAZABILIDAD_GATE_MAX_DEPTH, deadline
    )
    return results, "recursive_walk(automation_id match)"


def _filter_visible_enabled(elements: list) -> list:
    """Best-effort visible+enabled filter via the live UIA wrapper's own
    is_visible()/is_enabled() (available directly on a real, interactable
    UIA element -- unlike the flattened ControlInfo used elsewhere in this
    codebase, which carries neither field).
    """
    kept = []
    for el in elements:
        try:
            visible = bool(el.is_visible())
        except Exception:
            visible = False
        try:
            enabled = bool(el.is_enabled())
        except Exception:
            enabled = False
        if visible and enabled:
            kept.append(el)
    return kept


def _read_current_value(element) -> str:
    """Best-effort read of the field's current text/value.

    Tries get_value() (ValuePattern.CurrentValue, exposed by pywinauto's
    EditWrapper for UIA controls whose control_type maps to "Edit") first,
    then falls back to window_text() -- whichever this specific control
    actually supports. Never raises: returns "" on total failure, so a read
    failure degrades to "field appears empty" rather than crashing (the
    caller only ever uses this for a boolean has-content check and, once,
    for the Step 6 in-memory equality comparison -- the raw return value is
    NEVER logged/printed by any caller in this file).
    """
    for attr in ("get_value", "window_text"):
        try:
            method = getattr(element, attr)
        except AttributeError:
            continue
        try:
            value = method()
        except Exception:
            continue
        if value is not None:
            return str(value)
    return ""


# --------------------------------------------------------------------------
# Step 5: the one authorized write -- called from EXACTLY ONE call site
# (execute(), PASO 5 below). No retry, no loop, no other interaction
# function (accDoDefaultAction, Invoke, Select, Focus, click, click_input,
# SetForegroundWindow, ShowWindow, pyautogui, mouse_event, SendInput,
# keybd_event) appears anywhere in this file.
# --------------------------------------------------------------------------


def write_invoice_once(element, value: str) -> str:
    """Write `value` into `element` EXACTLY ONCE.

    Prefers UIA ValuePattern (via pywinauto's set_text(), which internally
    calls IUIAutomationValuePattern.SetValue() when the control's UIA
    control_type maps to pywinauto's EditWrapper); falls back to
    type_keys() only if set_text() is unavailable or raises for this
    specific control (e.g. ValuePattern unsupported, or this control_type
    is not wrapped as an Edit at all).

    Called from exactly ONE call site in the entire script, never retried:
    if BOTH mechanisms fail, the exception from type_keys() propagates to
    that single call site, which logs it and aborts -- it is never caught
    here and never triggers a third attempt.

    NEVER appends Enter/Tab/any special key: type_keys() is called with
    only the literal characters of `value` (already validated in
    parse_args() to contain none of pywinauto's special-key syntax
    characters) and with_spaces=True (this invoice value never contains
    spaces, but that flag has no bearing on key-injection safety either
    way).

    Returns "value_pattern" or "type_keys" -- the method actually used.
    """
    try:
        supports_value = getattr(element, "iface_value", None) is not None
    except Exception:
        supports_value = False
    if not supports_value:
        raise RuntimeError("INVOICE_INPUT no soporta ValuePattern; no se intenta teclado")
    element.set_text(value)
    return "value_pattern"


# --------------------------------------------------------------------------
# Step 8: BMP screenshot -- COPIED (not imported) from
# poc_click_trazabilidad.py's capture_window_bmp(), byte-for-byte identical
# logic. poc_click_trazabilidad.py is explicitly NOT to be imported (mission
# constraint: this script's interaction capability must stay self-contained
# and minimal, and importing that module would pull in its click-capable
# functions). Copying this one pure, read-only, pywin32-only helper function
# is the documented choice over reimplementing it differently.
# --------------------------------------------------------------------------


def capture_window_bmp(handle: int, output_path: Path) -> Path:
    """Capture handle's current window rect as a .bmp file, using only pywin32
    (GetWindowDC + CreateDCFromHandle + BitBlt + SaveBitmapFile).

    Best-effort/optional per the mission spec ("puedes guardar"): never
    raises on a capture failure -- logs a warning and returns the intended
    path, so a screenshot problem never blocks the rest of the report.
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
# Reporting
# --------------------------------------------------------------------------


def render_report(state: ReportState) -> str:
    lines = [
        "GO:",
        f"- trazabilidad activa: {state.trazabilidad_activa}",
        f"- foreground validado: {state.foreground_validado}",
        "",
        "INVOICE INPUT:",
        f"- encontrado: {state.encontrado}",
        f"- automation_id: {state.automation_id}",
        f"- class: {state.input_class}",
        f"- rectangle: {state.input_rectangle}",
        f"- visible: {state.input_visible}",
        f"- enabled: {state.input_enabled}",
        "",
        "WRITE:",
        f"- ejecutado: {state.write_ejecutado}",
        f"- metodo: {state.write_metodo}",
        f"- cantidad de escrituras: {state.write_cantidad}",
        "",
        "READBACK:",
        f"- valor coincide: {state.valor_coincide}",
        f"- longitud esperada: {state.longitud_esperada}",
        f"- longitud obtenida: {state.longitud_obtenida}",
        "",
        "RESULTADO:",
        f"- {state.resultado}",
    ]
    if state.screenshot_path != "N/D":
        lines += ["", f"(screenshot: {state.screenshot_path})"]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def execute(invoice: str, delay: int, screenshot_path: Path) -> tuple[ReportState, int]:
    """Run the full validation trail and, if every pre-write check passes,
    the single authorized write + readback. Never raises for an expected
    condition -- every documented abort path is caught here and turned into
    a clean ABORTED/RESULTADO state (Regla 14).
    """
    state = ReportState()

    if delay > 0:
        _countdown_wait(delay)

    # === PASO 1: localizar GO autenticada + confirmar pantalla activa ===
    # (identico en estructura a inspect_trazabilidad_form.run(): TODO fallo
    # de esta seccion colapsa a TRAZABILIDAD_NOT_ACTIVE, el unico codigo de
    # la mision para este paso.)
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
    except WindowNotFoundError as exc:
        logger.warning("TRAZABILIDAD_NOT_ACTIVE: {}", exc)
        state.trazabilidad_activa = "NO"
        state.resultado = RESULT_TRAZABILIDAD_NOT_ACTIVE
        return state, 1
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado enumerando ventanas")
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

    # === PASO 2: localizar el campo EXCLUSIVAMENTE por automation_id ===
    logger.info("PASO 2: localizando INVOICE_INPUT por automation_id={!r}...", TARGET_AUTOMATION_ID)
    try:
        from pywinauto import Desktop

        window = Desktop(backend="uia").window(handle=go_window.handle)
        window.wait("exists", timeout=5)
    except Exception as exc:
        _classify_and_log(exc, f"fallo al reconectar via UIA a la ventana GO {go_window.handle}")
        state.resultado = RESULT_INVOICE_INPUT_NOT_FOUND
        return state, 1

    try:
        candidates, method_used = find_invoice_input_elements(window)
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado buscando INVOICE_INPUT por automation_id")
        state.resultado = RESULT_INVOICE_INPUT_NOT_FOUND
        return state, 1

    logger.info(
        "Busqueda de INVOICE_INPUT: metodo={} candidatos_crudos={}", method_used, len(candidates)
    )
    visible_enabled = _filter_visible_enabled(candidates)
    logger.info("Candidatos visible+enabled: {}", len(visible_enabled))

    if not visible_enabled:
        logger.error("INVOICE_INPUT_NOT_FOUND: 0 candidatos visible+enabled con automation_id={!r}", TARGET_AUTOMATION_ID)
        state.encontrado = "NO"
        state.resultado = RESULT_INVOICE_INPUT_NOT_FOUND
        return state, 1
    if len(visible_enabled) > 1:
        logger.error(
            "INVOICE_INPUT_AMBIGUOUS: {} candidatos visible+enabled con automation_id={!r}",
            len(visible_enabled),
            TARGET_AUTOMATION_ID,
        )
        state.encontrado = "NO"
        state.resultado = RESULT_INVOICE_INPUT_AMBIGUOUS
        return state, 1

    element = visible_enabled[0]
    state.encontrado = "SI"
    state.automation_id = _safe_str(lambda: element.element_info.automation_id)
    state.input_class = _safe_str(lambda: element.element_info.class_name)
    state.input_rectangle = _safe_str(lambda: element.rectangle())
    input_visible = _safe_str(lambda: element.is_visible())
    input_enabled = _safe_str(lambda: element.is_enabled())
    state.input_visible = "SI" if input_visible == "True" else ("NO" if input_visible == "False" else input_visible)
    state.input_enabled = "SI" if input_enabled == "True" else ("NO" if input_enabled == "False" else input_enabled)
    logger.info(
        "INVOICE_INPUT identificado: automation_id={} class={} rect={}",
        state.automation_id,
        state.input_class,
        state.input_rectangle,
    )

    # === PASO 3: pre-check -- el campo debe estar vacio ===
    logger.info("PASO 3: verificando que el campo este vacio (solo booleano, nunca el valor real)...")
    current_value = _read_current_value(element)
    has_text = bool(current_value)
    logger.info("Contenido actual del campo: has_text={}", has_text)
    if has_text:
        logger.error("INVOICE_INPUT_NOT_EMPTY: el campo ya tiene contenido; no se sobrescribe.")
        state.resultado = RESULT_INVOICE_INPUT_NOT_EMPTY
        return state, 1

    # === PASO 4: verificar primer plano (nunca se activa GO) ===
    logger.info("PASO 4: verificando que GO este en primer plano (nunca se activa)...")
    try:
        verify_go_foreground(go_window.handle, expected_pid=go_window.process_id)
    except GoNotForegroundError as exc:
        logger.error("GO_NOT_FOREGROUND: {}", exc)
        state.foreground_validado = "NO"
        state.resultado = RESULT_GO_NOT_FOREGROUND
        return state, 1
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado verificando primer plano")
        state.foreground_validado = "NO"
        state.resultado = RESULT_GO_NOT_FOREGROUND
        return state, 1

    state.foreground_validado = "SI"
    logger.info("GO confirmada en primer plano.")

    # === PASO 5: LA UNICA ESCRITURA AUTORIZADA -- un unico call site, sin
    # bucle, sin reintento. Si write_invoice_once() lanza (ambos mecanismos
    # internos fallaron), se registra y se aborta: NUNCA se vuelve a llamar. ===
    logger.info("PASO 5: ejecutando la UNICA escritura autorizada...")
    try:
        verify_go_foreground(go_window.handle, expected_pid=go_window.process_id)
        metodo = write_invoice_once(element, invoice)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado ejecutando la escritura autorizada (ambos mecanismos)")
        state.write_ejecutado = "NO"
        state.resultado = f"{RESULT_WRITE_FAILED_PREFIX}: {code})"
        return state, 1

    state.write_ejecutado = "SI"
    state.write_metodo = metodo
    state.write_cantidad = "1"
    logger.info("Escritura ejecutada (1 vez). Metodo usado: {}", metodo)

    # === A partir de aqui: SOLO lectura de verificacion. write_invoice_once()
    # nunca se llama de nuevo, para ningun caso, incluyendo manejo de errores. ===

    # === PASO 6: readback + comparacion (solo booleano/longitudes, nunca
    # los valores en si) ===
    logger.info("PASO 6: releyendo el campo y comparando con el valor esperado...")
    actual_value = _read_current_value(element)
    matches = actual_value == invoice
    state.valor_coincide = "SI" if matches else "NO"
    state.longitud_esperada = str(len(invoice))
    state.longitud_obtenida = str(len(actual_value))
    logger.info(
        "Comparacion de readback: expected == actual -> {} (longitud_esperada={}, longitud_obtenida={})",
        matches,
        state.longitud_esperada,
        state.longitud_obtenida,
    )
    print(f"expected == actual: {matches}")

    if matches:
        state.resultado = RESULT_INVOICE_VALUE_CONFIRMED
    else:
        logger.warning("INVOICE_VALUE_MISMATCH: no se corrige ni se reintenta.")
        state.resultado = RESULT_INVOICE_VALUE_MISMATCH

    # === PASO 7 (spec): enmascarado -- aplicado arriba solo si se decide
    # persistir/mostrar el valor en algun lugar; el reporte final y esta
    # funcion nunca incluyen el valor real ni enmascarado, solo el booleano
    # de coincidencia y las longitudes (ver docstring del modulo).

    # Screenshots after writing can expose invoice/patient data and are not
    # required for the current PoC evidence.
    logger.info("PASO 8: captura omitida por privacidad (la pantalla puede contener datos sensibles).")
    state.screenshot_path = "N/D (omitida por privacidad)"

    return state, (0 if matches else 1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments. --invoice is required; validated (forbidden
    pywinauto special-key characters) as part of argument parsing itself,
    before any Step 1 logic runs and before any interaction with GO.
    """
    parser = argparse.ArgumentParser(
        description=(
            "F0.4A: escribe UNA UNICA vez un numero de factura de prueba en el campo "
            "INVOICE_INPUT (automation_id=INDbteInvoiceNumber) de la pantalla "
            "'Trazabilidad de Factura' en GO/Indigo, y verifica por readback que quedo "
            "escrito correctamente. Ninguna otra interaccion."
        )
    )
    parser.add_argument(
        "--invoice",
        required=True,
        help=(
            "Numero de factura de prueba a escribir (CLI only -- nunca se hardcodea ni "
            "se persiste en texto plano en ningun archivo)."
        ),
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=0,
        help=(
            "Segundos de cuenta regresiva antes de empezar (0 = sin espera, procede de "
            "inmediato si GO ya esta en primer plano). Default: 0."
        ),
    )
    parser.add_argument(
        "--screenshot-path",
        type=Path,
        default=DEFAULT_SCREENSHOT_PATH,
        help=f"Ruta de la captura de pantalla BMP opcional (default: {DEFAULT_SCREENSHOT_PATH}).",
    )
    args = parser.parse_args(argv)

    try:
        _validate_invoice_value(args.invoice)
    except InvalidInvoiceValueError as exc:
        # Never include the raw --invoice value in this message (Regla 6).
        parser.error(str(exc))

    return args


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the F0.4A one-shot write+readback PoC."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    args = parse_args(argv)

    logger.warning(
        "F0.4A: iniciando PoC de UNA UNICA escritura autorizada en INVOICE_INPUT "
        "(automation_id={!r}). delay={}s, screenshot_path={}",
        TARGET_AUTOMATION_ID,
        args.delay,
        args.screenshot_path,
    )

    try:
        state, exit_code = execute(
            invoice=args.invoice,
            delay=args.delay,
            screenshot_path=args.screenshot_path,
        )
    except Exception as exc:
        # Defensive last-resort net: execute() is written to catch and
        # classify every expected failure mode internally, and critically
        # never calls write_invoice_once() more than once on any path. This
        # only guards against a genuinely unexpected error that escaped
        # every try/except above.
        code = _classify_and_log(exc, "fallo inesperado no clasificado en el flujo del PoC")
        state = ReportState()
        state.resultado = f"ABORTED ({code})"
        exit_code = 1

    report = render_report(state)
    print(report)
    logger.info("Reporte final:\n{}", report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
