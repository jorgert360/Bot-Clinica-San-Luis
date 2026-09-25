"""F0.4C -- Busqueda real via Enter + medicion de tiempo de carga en GO/Indigo
("Trazabilidad de Factura").

F0.4B (poc_search_invoice.py) invoco el pequeno boton de accion embebido al
borde derecho del campo de factura (InvokePattern/click_input()) -- se
ejecuto sin error pero produjo SEARCH_NO_EFFECT (0 de 17 campos observados
cambiaron). El usuario real, observando en vivo, reporto que el mecanismo de
busqueda real de GO es presionar Enter en el campo de factura despues de
escribir el numero -- no ese boton (que probablemente es una funcion
distinta y no relacionada -- limpiar/calendario/ayuda -- sin confirmar). El
usuario autorizo explicitamente, como excepcion puntual y acotada a la regla
general de F0.4B de "nunca presionar Enter", el envio de EXACTAMENTE UNA
tecla Enter al campo de entrada de factura como el disparador real de
busqueda. El usuario tambien pidio explicitamente: medir con precision
cuantos segundos tarda GO en cargar la respuesta despues del Enter, ya que
esa duracion se necesitara mas adelante como tiempo de espera antes de pasar
a "la siguiente pestana" cuando se construya la automatizacion real.

Este script:

  1. Reutiliza integramente (import directo, nunca reimplementado) los PASOS
     1-4 y 7-9 de poc_search_invoice.py (F0.4B), que a su vez reutiliza:
     - find_target_process_ids()/enumerate_all_windows()/_classify_and_log()/
       WindowNotFoundError/TARGET_PROCESS_NAME (diagnose_go_windows.py).
     - find_authenticated_go_window()/verify_go_foreground()/
       GoAmbiguousError/GoNotForegroundError (poc_move_to_trazabilidad.py).
     - validate_screen_active()/TrazabilidadNotActiveError
       (inspect_trazabilidad_form.py).
     - find_invoice_input_elements()/_filter_visible_enabled()/
       _read_current_value()/_validate_invoice_value()/
       InvalidInvoiceValueError/TRAZABILIDAD_GATE_MAX_DEPTH/
       TRAZABILIDAD_GATE_TIMEOUT_SECONDS (poc_write_invoice.py, F0.4A).
     - ControlInfo (inspect_go.py).
     Directamente de poc_search_invoice.py (F0.4B) importa ademas:
     FieldSnapshot/AfterSnapshot, read_field_states(), _field_change_label(),
     _snapshot_target_windows()/_scan_go_controls()/_check_not_found_evidence()/
     _looks_like_error_dialog(), poll_after_search() (los mismos helpers de
     lectura, ver poll_after_enter() abajo), classify_result(),
     deep_map_relevant_controls(), STABLE_POLLS_TO_STOP_EARLY, y el
     vocabulario RESULT_* completo -- NUNCA reimplementados.
  2. NUNCA vuelve a escribir/tipear el numero de factura -- solo VERIFICA
     (solo lectura) que el campo ya contiene el valor esperado (longitud 9,
     igual a --invoice), reutilizando exactamente la misma logica de F0.4B.
     Si no esta listo, aborta INVOICE_VALUE_NOT_READY -- nunca lo corrige.
  3. NUNCA localiza ni referencia el boton de busqueda de F0.4B -- la premisa
     completa de este script es que Enter reemplaza ese mecanismo.
  4. Ejecuta LA UNICA interaccion autorizada -- send_enter_once() -- desde
     EXACTAMENTE UN sitio en todo el archivo (ver execute(), PASO 5): EXACTA-
     MENTE una tecla Enter ("{ENTER}"), enviada UNICAMENTE al elemento vivo
     del campo de factura (nunca un evento de teclado global/no acotado,
     nunca a ningun otro control, nunca a la ventana completa). Ninguna otra
     tecla especial (Tab/Escape/flechas/etc.) aparece en ningun lugar de este
     archivo. Este es el UNICO type_keys() de todo el script -- con el UNICO
     argumento literal "{ENTER}".
  5. Antes y despues: SOLO inspeccion de solo lectura (polling acotado,
     configurable via --poll-interval, hasta --post-enter-timeout), con
     medicion PRECISA (time.monotonic()) de cuando aparece el primer cambio
     real y de cuando el estado se estabiliza (3 lecturas consecutivas
     identicas) -- ver poll_after_enter(). Nunca se envia una segunda
     interaccion a ningun control, incluyendo los recien aparecidos.
  6. Clasifica el resultado (INVOICE_FOUND / INVOICE_NOT_FOUND /
     SEARCH_NO_EFFECT / APPLICATION_ERROR), reutilizando exactamente la
     logica de decision de F0.4B (classify_result()), y guarda evidencia
     (BMP + reporte de texto) -- nunca persiste el valor real de ningun
     campo, solo booleano poblado/no-poblado y longitud (Regla 6, misma
     disciplina que F0.4B -- ver PRIVACIDAD abajo).

PRIVACIDAD (mandatorio, Regla 6): identica disciplina a F0.4B -- para los 17
campos de valor y para el numero de factura mismo, este script SOLO
reporta/persiste poblado-o-no (booleano) y longitud, nunca el valor real.
Los nombres/captions de controles de navegacion no se consideran datos
sensibles (mismo criterio ya usado en los scripts hermanos).

Regla 1 (03_CLAUDE_RULES.md): PID/handle de GO y el campo de factura nunca se
hardcodean -- resueltos en vivo cada corrida, reutilizando exactamente la
misma cadena de busqueda de F0.4B/F0.4A (find_authenticated_go_window(),
validate_screen_active(), find_invoice_input_elements()).

Regla 14: cualquier estado ambiguo/inesperado aborta limpiamente con la razon
especifica de la mision -- nunca se adivina, nunca se reintenta.

IMPORTANTE -- estructura de una sola accion: send_enter_once() se llama desde
EXACTAMENTE UN sitio en todo este archivo (ver execute(), PASO 5). No hay
reintentos, no hay bucles, no hay ninguna otra funcion de accion en ningun
otro lugar de este script -- en particular, el mapeo profundo de PASO 8
(deep mapping, reutilizado de F0.4B) SOLO lee propiedades via ControlInfo,
nunca invoca nada sobre ningun control que encuentra.

Uso:
    python scripts/poc_search_invoice_enter.py --invoice FHC000000
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
from inspect_go import ControlInfo  # noqa: E402
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
from poc_search_invoice import (  # noqa: E402
    # capture_window_bmp() is IMPORTED here rather than re-copied a second
    # time: it is pure, read-only (pywin32 GetWindowDC/BitBlt only), has no
    # interaction-surface implications (it never touches UIA/keyboard/mouse),
    # and poc_search_invoice.py itself already documents it as a copy from
    # poc_click_trazabilidad.py -- re-copying it a third time would just add
    # a second stale-copy risk with zero safety benefit. Every function that
    # DOES touch the live interaction surface (send_enter_once()) is written
    # fresh in this file, never imported.
    # Result vocabulary (F0.4B) -- reused verbatim, never redefined.
    RESULT_ABORTED_PREFIX,
    RESULT_APPLICATION_ERROR,
    RESULT_GO_NOT_FOREGROUND,
    RESULT_INVOICE_FOUND,
    RESULT_INVOICE_NOT_FOUND,
    RESULT_INVOICE_VALUE_NOT_READY,
    RESULT_SEARCH_NO_EFFECT,
    RESULT_TRAZABILIDAD_NOT_ACTIVE,
    # Field taxonomy + best-effort readers (F0.4B) -- reused verbatim.
    FIELD_LABEL_VARIANTS,
    REPORT_EXPLICIT_FIELDS,
    STABLE_POLLS_TO_STOP_EARLY,
    AfterSnapshot,
    FieldSnapshot,
    _check_not_found_evidence,
    _field_change_label,
    _looks_like_error_dialog,
    _scan_go_controls,
    _snapshot_target_windows,
    capture_window_bmp,
    classify_result,
    deep_map_relevant_controls,
    read_field_states,
)
from poc_write_invoice import (  # noqa: E402
    TRAZABILIDAD_GATE_MAX_DEPTH,
    TRAZABILIDAD_GATE_TIMEOUT_SECONDS,
    InvalidInvoiceValueError,
    _filter_visible_enabled,
    _read_current_value,
    _validate_invoice_value,
    find_invoice_input_elements,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# F0.4B's own click_input()/invoke() attempt took ~22s just for the invoke()
# call itself to return before any post-action polling even started --
# unusual, but real, observed evidence. The user explicitly wants an
# ACCURATE measured duration, not a truncated one. 20s (F0.4B's own
# post-search default) risks cutting off exactly the kind of slow response
# already observed once. Default raised to 30.0s here (documented choice,
# not the F0.4B default) to give real margin for an accurate measurement;
# --post-enter-timeout remains fully overridable from the CLI.
DEFAULT_POST_ENTER_TIMEOUT_SECONDS = 30.0

# Finer granularity than F0.4B's 0.5s default -- the whole point of this
# script is an accurate elapsed-seconds figure, not a coarse bucket.
DEFAULT_POLL_INTERVAL_SECONDS = 0.3

DEFAULT_SCREENSHOT_PATH = Path("runtime") / "screenshots" / "f04c_invoice_search_enter_result.bmp"
DEFAULT_LOG_PATH = Path("runtime") / "logs" / "f04c_search_enter_result.txt"


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass
class ReportState:
    """Accumulates whatever has been resolved so far. Fields default to
    'N/D' so an early abort still prints a complete, well-formed report
    instead of a partial/crashed one (Regla 14)."""

    trazabilidad_activa: str = "N/D"
    invoice_input_encontrado: str = "N/D"
    valor_preparado: str = "N/D"
    foreground_validado: str = "N/D"

    enter_ejecutado: str = "NO"
    enter_cantidad: str = "0"
    enter_timestamp: str = "N/D"

    primer_cambio_elapsed: str = "N/D"
    estabilizado_elapsed: str = "N/D"
    limite_polling: str = "N/D"

    ui_estable: str = "N/D"
    controles_antes: str = "N/D"
    controles_despues: str = "N/D"
    field_changes: dict[str, str] = field(default_factory=dict)

    nuevos_controles: list[str] = field(default_factory=list)

    resultado: str = "N/D"

    screenshot_path: str = "N/D"
    log_path: str = "N/D"


# --------------------------------------------------------------------------
# PASO 5: the one authorized action -- called from EXACTLY ONE call site
# (execute(), PASO 5 below). No retry, no loop. This is the ONLY
# type_keys() call in the entire file, with the ONLY literal argument
# "{ENTER}" -- no other special key, no digit/letter payload (this script
# never types the invoice number itself; only F0.4A did that). No
# accDoDefaultAction, no .invoke()/.click_input() anywhere in this file --
# this script does not locate or reference F0.4B's search button at all.
# --------------------------------------------------------------------------


def send_enter_once(element) -> None:
    """Send EXACTLY ONE Enter keystroke to `element` (the invoice input),
    and ONLY to this element -- never a global/unscoped key event, never any
    other key. This is the sole, explicitly user-authorized exception to
    this project's general "never press Enter" rule (03_CLAUDE_RULES.md
    F0.4B/F0.4C context), scoped to exactly this one action. Called from
    exactly ONE call site, never retried.
    """
    element.type_keys("{ENTER}")


# --------------------------------------------------------------------------
# PASO 6: precisely-timed AFTER polling -- the core new requirement of this
# mission. Closely mirrors poc_search_invoice.py's poll_after_search() (same
# read-only reads via the SAME imported helpers, same
# STABLE_POLLS_TO_STOP_EARLY-consecutive-polls-to-stop-early rule, same hard
# timeout, same immediate stop on a detected error dialog) -- but with an
# ADDITIONAL responsibility: precisely timing (time.monotonic()) when GO's
# response first appears and when it stabilizes. NEVER sends any
# interaction -- only enumerates windows and walks the UIA tree (read-only).
# --------------------------------------------------------------------------


def poll_after_enter(
    pids: list[int],
    go_handle: int,
    before_handles: set[int],
    before_field_states: dict[str, FieldSnapshot],
    max_depth: int,
    scan_timeout: float,
    post_enter_timeout: float,
    poll_interval: float,
) -> tuple[list[AfterSnapshot], bool, float | None, float | None]:
    """Read-only polling loop after the single authorized Enter keystroke.

    Returns (polls, stable_reached, first_change_elapsed_seconds,
    stable_elapsed_seconds):

      - first_change_elapsed_seconds: elapsed time (seconds, time.monotonic()
        based) of the FIRST poll where at least one field that was NOT
        populated in before_field_states becomes populated. None if no such
        poll ever occurs across the whole run (mirrors SEARCH_NO_EFFECT --
        never fabricated/guessed).
      - stable_elapsed_seconds: elapsed time of the poll at which
        STABLE_POLLS_TO_STOP_EARLY consecutive polls agreed on the
        field-population signature -- ONLY reported (non-None) when a real
        change was actually observed first (first_change_elapsed_seconds is
        not None); a signature that is trivially "stable from poll 1"
        because nothing ever differed from BEFORE is never reported as a
        meaningful load time (would fabricate a duration for a no-op).
    """
    start = time.monotonic()
    polls: list[AfterSnapshot] = []
    stable_streak = 0
    previous_signature: tuple | None = None
    stable_reached = False
    first_change_elapsed_seconds: float | None = None
    stable_elapsed_seconds: float | None = None

    while True:
        elapsed = time.monotonic() - start
        if elapsed > post_enter_timeout:
            logger.info("Limite de {}s de polling post-Enter alcanzado; deteniendo.", post_enter_timeout)
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

        newly_populated = [
            name
            for name, after in field_states.items()
            if after.populated and not before_field_states.get(name, FieldSnapshot(False, 0)).populated
        ]
        if first_change_elapsed_seconds is None and newly_populated:
            first_change_elapsed_seconds = elapsed
            logger.info(
                "Primer cambio real detectado @{:.2f}s post-Enter: campos={}",
                elapsed,
                newly_populated,
            )

        logger.info(
            "Poll post-Enter @{:.2f}s: ventanas={} nuevas={} controles_uia={} "
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
                post_enter_timeout,
            )
            stable_reached = True
            if first_change_elapsed_seconds is not None:
                stable_elapsed_seconds = elapsed
            break

        time.sleep(poll_interval)

    return polls, stable_reached, first_change_elapsed_seconds, stable_elapsed_seconds


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def render_report(state: ReportState) -> str:
    lines = [
        "PRE-SEARCH:",
        f"- trazabilidad activa: {state.trazabilidad_activa}",
        f"- invoice input encontrado: {state.invoice_input_encontrado}",
        f"- valor preparado: {state.valor_preparado}",
        f"- foreground validado: {state.foreground_validado}",
        "",
        "ENTER:",
        f"- ejecutado: {state.enter_ejecutado}",
        f"- cantidad de teclas enviadas: {state.enter_cantidad}",
        f"- timestamp: {state.enter_timestamp}",
        "",
        "TIEMPO DE CARGA MEDIDO:",
        f"- primer cambio detectado a los: {state.primer_cambio_elapsed}",
        f"- estabilizado (3 lecturas consecutivas) a los: {state.estabilizado_elapsed}",
        f"- limite de polling usado: {state.limite_polling}",
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
    post_enter_timeout: float,
    poll_interval: float,
    screenshot_path: Path,
    log_path: Path,
) -> tuple[ReportState, int]:
    """Run the full validation trail and, if every pre-search check passes,
    the single authorized Enter keystroke + precisely-timed read-only
    post-Enter inspection. Never raises for an expected condition
    (Regla 14)."""
    state = ReportState()

    # === PASO 1: localizar GO autenticada + confirmar pantalla activa ===
    # (identico a poc_search_invoice.py / F0.4B)
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

    # === PASO 1e: verificar (SOLO LECTURA) que el valor ya este preparado.
    # NUNCA se escribe/corrige aqui -- set_text()/type_keys() del valor no
    # existen en este archivo. ===
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

    # === PASO 4: snapshot ANTES (solo lectura) ===
    # Nota de numeracion: este script no tiene PASO 2/3 propios (F0.4B
    # localizaba y evaluaba el boton de busqueda alli -- este script no lo
    # necesita, no lo busca, no lo referencia). La numeracion PASO 4+ se
    # conserva identica a F0.4B por trazabilidad de mision.
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

    # === PASO 5: LA UNICA ACCION AUTORIZADA -- exactamente una tecla Enter,
    # un unico call site, sin bucle, sin reintento. ===
    logger.info("PASO 5: ejecutando la UNICA interaccion autorizada (Enter) sobre el campo de factura...")
    try:
        verify_go_foreground(go_window.handle, expected_pid=go_window.process_id)
        send_enter_once(invoice_element)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado enviando la tecla Enter autorizada")
        state.enter_ejecutado = "NO"
        state.resultado = f"{RESULT_ABORTED_PREFIX} (ENTER_FAILED: {code})"
        return state, 1

    enter_timestamp = datetime.now()
    state.enter_ejecutado = "SI"
    state.enter_cantidad = "1"
    state.enter_timestamp = enter_timestamp.isoformat()
    logger.info("Enter enviado (1 vez, exclusivamente al campo de factura) a las {}", state.enter_timestamp)

    # === A partir de aqui: SOLO inspeccion de solo lectura.
    # send_enter_once() NUNCA se llama de nuevo, para ningun caso,
    # incluyendo manejo de errores. ===

    # === PASO 6: polling AFTER (solo lectura, acotado, con medicion precisa
    # de tiempo de carga) ===
    logger.info("PASO 6: polling post-Enter de solo lectura (hasta {}s)...", post_enter_timeout)
    stable_reached = False
    first_change_elapsed: float | None = None
    stable_elapsed: float | None = None
    try:
        polls, stable_reached, first_change_elapsed, stable_elapsed = poll_after_enter(
            pids=pids,
            go_handle=go_window.handle,
            before_handles=before_handles,
            before_field_states=before_field_states,
            max_depth=max_depth,
            scan_timeout=scan_timeout,
            post_enter_timeout=post_enter_timeout,
            poll_interval=poll_interval,
        )
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado durante el polling post-Enter (solo lectura)")
        polls = []
        logger.warning("Polling post-Enter incompleto por error: {}", code)

    state.limite_polling = f"{post_enter_timeout:.1f}s"
    state.primer_cambio_elapsed = f"{first_change_elapsed:.1f}s" if first_change_elapsed is not None else "N/A"
    state.estabilizado_elapsed = f"{stable_elapsed:.1f}s" if stable_elapsed is not None else "N/A"
    logger.warning(
        "TIEMPO DE CARGA MEDIDO -- primer cambio: {} | estabilizado: {} | limite usado: {}",
        state.primer_cambio_elapsed,
        state.estabilizado_elapsed,
        state.limite_polling,
    )

    final = polls[-1] if polls else None
    state.controles_despues = str(final.control_count) if final else "N/D"
    state.ui_estable = "SI" if stable_reached else "NO"

    final_field_states = final.field_states if final else {name: FieldSnapshot(False, 0) for name in FIELD_LABEL_VARIANTS}
    for name in FIELD_LABEL_VARIANTS:
        before_snap = before_field_states.get(name, FieldSnapshot(False, 0))
        after_snap = final_field_states.get(name, FieldSnapshot(False, 0))
        state.field_changes[name] = _field_change_label(before_snap, after_snap)

    # === PASO 7: clasificacion (reutilizada verbatim de F0.4B) ===
    logger.info("PASO 7: clasificando el resultado final...")
    result_code = classify_result(before_field_states, polls)
    state.resultado = result_code
    logger.info("Clasificacion final: {}", result_code)

    # === PASO 8: mapeo profundo de solo lectura (solo si hubo cambio real,
    # reutilizado verbatim de F0.4B) ===
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
            "F0.4C: envia UNA UNICA vez la tecla Enter (autorizada explicita y puntualmente "
            "por el usuario) al campo de factura de 'Trazabilidad de Factura' en GO/Indigo, "
            "mide con precision el tiempo de carga de la respuesta, y luego inspecciona (solo "
            "lectura) lo que se cargo. Nunca escribe en el campo de factura ni toca el boton "
            "de busqueda de F0.4B."
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
        "--post-enter-timeout",
        type=float,
        default=DEFAULT_POST_ENTER_TIMEOUT_SECONDS,
        help=(
            "Limite duro en segundos para el polling de solo lectura post-Enter. Elevado por "
            "encima del default de F0.4B (20s->30s) porque F0.4B ya observo en vivo una "
            "respuesta de ~22s solo para que invoke() retornara -- este limite debe dar margen "
            f"real para una medicion precisa, no truncada (default: {DEFAULT_POST_ENTER_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=(
            "Intervalo en segundos entre polls post-Enter -- mas fino que F0.4B (0.3s) para "
            f"reportar un tiempo de carga preciso, no en buckets gruesos (default: {DEFAULT_POLL_INTERVAL_SECONDS})."
        ),
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
    """Entry point: run the F0.4C one-shot Enter-search+measure+inspect PoC."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    args = parse_args(argv)

    logger.warning(
        "F0.4C: iniciando PoC de UNA UNICA tecla Enter autorizada sobre el campo de factura, "
        "con medicion precisa de tiempo de carga. max_depth={}, timeout={}s, "
        "post_enter_timeout={}s, poll_interval={}s",
        args.max_depth,
        args.timeout,
        args.post_enter_timeout,
        args.poll_interval,
    )

    try:
        state, exit_code = execute(
            invoice=args.invoice,
            max_depth=args.max_depth,
            scan_timeout=args.timeout,
            post_enter_timeout=args.post_enter_timeout,
            poll_interval=args.poll_interval,
            screenshot_path=args.screenshot_path,
            log_path=args.log_path,
        )
    except Exception as exc:
        # Defensive last-resort net: execute() is written to catch and
        # classify every expected failure mode internally, and critically
        # never calls send_enter_once() more than once on any path. This
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
