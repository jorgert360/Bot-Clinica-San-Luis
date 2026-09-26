"""Open "Trazabilidad de Factura", write the invoice number, trigger the
search (Enter), and validate the result loaded.

Ported from ``scripts/poc_click_trazabilidad.py``, ``scripts/poc_write_invoice.py``,
``scripts/poc_search_invoice_enter.py``, and ``scripts/poc_search_invoice.py``.

Privacy (Regla 6): the raw invoice number is never logged. Every log call in
this module that could carry it uses :func:`mask_invoice` first. Field
values read from the 16 result-detail fields are recorded only as
populated-or-not + length, exactly like the PoC (never the real value).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from loguru import logger

from clinica_rpa.automation.go_session import (
    LIVE_SEARCH_MAX_DEPTH,
    LIVE_SEARCH_TIMEOUT_SECONDS,
    ControlInfo,
    Rect,
    WindowDiagnostic,
    _matches_any as matches_any,
    activate_preselected_once,
    click_once,
    connect_uia,
    load_calibration,
    locate_content_panel,
    resolve_target_point,
    uia_find_first_by_automation_id,
    uia_raw_element_name,
    verify_go_foreground,
)
from clinica_rpa.domain.errors import ClinicaRpaError, ErrorCode

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

TARGET_AUTOMATION_ID = "INDbteInvoiceNumber"

# pywinauto's type_keys() special-key syntax characters -- rejected
# defensively before ANY interaction with GO.
FORBIDDEN_INVOICE_CHARS: tuple[str, ...] = ("{", "}", "~", "(", ")")

TRAZABILIDAD_GATE_MAX_DEPTH = 14
TRAZABILIDAD_GATE_TIMEOUT_SECONDS = 20.0
AUTOMATION_ID_SEARCH_TIMEOUT_SECONDS = 15.0

GATE_TRAZABILIDAD = "trazabilidad de factura"
GATE_INFORMACION_FACTURA = "informacion factura"
LABEL_VARIANTS: tuple[str, ...] = ("n° factura", "nº factura", "no factura", "n factura")

# Phase 1E.1 (batch, between-items): live-confirmed button (2026-09-25,
# read-only UIA scan of the Trazabilidad result screen) -- named exactly
# "Deshacer", no accents, no reject-variant needed since no other button on
# this screen shares a normalized name with it.
UNDO_BUTTON_VARIANTS: tuple[str, ...] = ("deshacer",)

# The 16 result-detail fields (mirrors poc_search_invoice.FIELD_LABEL_VARIANTS).
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
MIN_ACTIVE_FIELDS_THRESHOLD = 3

EDIT_LIKE_UIA_TYPES: tuple[str, ...] = ("edit", "document", "custom")
EDIT_LEFT_TOLERANCE_PX = -5
VERTICAL_CENTER_TOLERANCE_PX = 10

NOT_FOUND_TEXT_HINTS: tuple[str, ...] = ("no encontrado", "no encontrada", "no existe", "sin resultados")

# Phase 1B close-out (2026-09-25): live-confirmed by the user
# (GO_QUERY_REAL_WAIT_CONFIRMED) that GO's own backend genuinely takes
# ~45-50s to resolve an invoice search -- Fase 1B.3's independent-detector
# diagnostic proved this is GO's UI thread itself being unresponsive to
# ANY UIA call during that window, not an artifact of our detection
# strategy. This can no longer be optimized away from Python; the timeout
# below is a safety ceiling for a genuine failure, never a sleep target.
DEFAULT_SEARCH_POLL_TIMEOUT_SECONDS = 90.0
DEFAULT_SEARCH_POLL_INTERVAL_SECONDS = 0.5

# The single cheap field checked while waiting -- "Estado Cartera", proven
# live (Fase 1B.3) to read empty before search and populated once the
# result loads. A single-field readiness gate is a deliberate tradeoff: it
# can occasionally wait longer than strictly necessary for an invoice where
# this one field happens to stay blank while others populate (the final,
# single full validation below still resolves that case correctly, just
# after the full safety timeout instead of earlier).
RESULT_READY_SENTINEL_AUTOMATION_ID = "INDtxtPortfolioStatus"
SEARCH_INITIAL_DELAY_SECONDS = 2.0
SEARCH_BACKOFF_SECONDS = 8.0


class InvalidInvoiceValueError(Exception):
    """Raised when an invoice value contains forbidden/unsafe characters."""


@dataclass(frozen=True)
class FieldSnapshot:
    """Boolean populated-or-not + length ONLY -- never the real value."""

    populated: bool
    length: int


# --------------------------------------------------------------------------
# Privacy helper (Regla 6)
# --------------------------------------------------------------------------


def mask_invoice(value: str) -> str:
    """Mask `value`, showing only the first 3 and last 2 characters.

    For length <= 5 the ENTIRE value is masked. Result always has the same
    length as the input. Empty string returns empty string.
    """
    length = len(value)
    if length == 0:
        return ""
    if length <= 5:
        return "*" * length
    return value[:3] + ("*" * (length - 5)) + value[-2:]


def validate_invoice_value(value: str) -> None:
    """Reject an invoice value that would be unsafe to pass to type_keys()
    (mission Section: never a fixed prefix assumption -- future invoice
    prefixes other than the current convention must keep working).

    Raises:
        InvalidInvoiceValueError: value is empty, unreasonably long, or
            contains a forbidden character.
    """
    stripped = value.strip()
    if not stripped:
        raise InvalidInvoiceValueError("El numero de factura no puede estar vacio.")
    if len(stripped) > 64:
        raise InvalidInvoiceValueError("El numero de factura excede la longitud maxima razonable (64).")
    found = sorted({ch for ch in FORBIDDEN_INVOICE_CHARS if ch in stripped})
    if found:
        raise InvalidInvoiceValueError(
            f"El numero de factura contiene caracteres no permitidos: {found!r}."
        )


# --------------------------------------------------------------------------
# Gate: is "Trazabilidad de Factura" the active screen? (read-only)
# --------------------------------------------------------------------------


def is_trazabilidad_active(controls: list[ControlInfo]) -> bool:
    """Confirm the UIA tree contains "Trazabilidad de Factura",
    "Informacion Factura" (accent-insensitive), and an "N Factura" variant.
    """
    haystacks = [f"{c.name} {c.automation_id}".casefold() for c in controls]
    found_trazabilidad = any(GATE_TRAZABILIDAD in h for h in haystacks)
    found_informacion = any(("informacion factura" in h) or ("información factura" in h) for h in haystacks)
    found_n_factura = any(any(v in h for v in LABEL_VARIANTS) for h in haystacks)
    return found_trazabilidad and found_informacion and found_n_factura


# --------------------------------------------------------------------------
# open_trazabilidad(): the ONE authorized calibrated click on the
# "Trazabilidad de Factura" tile (CUSTOM_DRAWN_CONTROL, no UIA/MSAA/Win32
# identity -- Regla 5's one authorized coordinate fallback).
# --------------------------------------------------------------------------


def open_trazabilidad(go_window: WindowDiagnostic) -> None:
    """Click the "Trazabilidad de Factura" tile exactly once, via the
    existing calibrated relative-coordinate pattern.

    Design note (deviation from the interactive PoC, explicitly authorized
    for this unattended service): ``scripts/poc_click_trazabilidad.py``
    required a prior, separately-run MOVE-ONLY confirmation step
    (``poc_move_to_trazabilidad.py``) before a human authorized the actual
    click. Phase 1A's ``process_invoice()`` is meant to run unattended for
    regression, so this function performs the calibrated click directly --
    it reuses the EXACT SAME relative-computation logic (panel rect +
    normalized offsets from ``runtime/state/ui_calibration.json``,
    ``resolve_target_point``), never a hardcoded/invented point. This
    mirrors the already-audited-live final click of the PoC pair, just
    without the separate move-only rehearsal call.

    Raises:
        ClinicaRpaError(TRAZABILIDAD_OPEN_FAILED): calibration/panel
            resolution failed, or the single click raised.
    """
    from clinica_rpa.automation.go_session import (
        CalibrationInvalidError,
        CalibrationMissingError,
        PanelNotFoundError,
        _classify_and_log,
    )

    try:
        calibration = load_calibration()
        panel = locate_content_panel(go_window.handle, calibration=calibration)
        target_x, target_y = resolve_target_point(panel.rect, calibration.normalized_x, calibration.normalized_y)
    except (CalibrationMissingError, CalibrationInvalidError, PanelNotFoundError) as exc:
        raise ClinicaRpaError(
            ErrorCode.TRAZABILIDAD_OPEN_FAILED, "No se pudo resolver el punto calibrado del tile Trazabilidad."
        ) from exc

    try:
        verify_go_foreground(go_window.handle, expected_pid=go_window.process_id)
        click_once(target_x, target_y)
    except Exception as exc:
        _classify_and_log(exc, "fallo ejecutando el clic calibrado en el tile Trazabilidad de Factura")
        # Regla 2: an exception right after the single mutating click is
        # ambiguous (it may have already registered server-side) -- never
        # retried, never a second mechanism attempted.
        raise ClinicaRpaError(
            ErrorCode.UI_ACTION_AMBIGUOUS, "El clic en el tile Trazabilidad de Factura fallo de forma ambigua."
        ) from exc


# --------------------------------------------------------------------------
# reset_result_screen_once(): the ONE authorized click on "Deshacer"
# (Phase 1E.1, batch between-items). Live-confirmed 2026-09-25: after
# downloading one invoice, GO's Trazabilidad result screen keeps that
# invoice's data loaded and the invoice-number field refuses to accept a
# new value (INVOICE_WRITE_FAILED, Regla 14: never overwrite blindly) until
# this button clears it.
# --------------------------------------------------------------------------


def reset_result_screen_once(go_window: WindowDiagnostic) -> None:
    """Click "Deshacer" exactly once so the invoice-number field becomes
    writable again for the next batch item.

    Foreground is verified immediately before the single click, exactly
    like every other mutating action in this codebase. Per spec (Fase
    1E.1 section 6/11): the caller must treat ANY failure here as "abort
    the batch, never continue clicking" -- this function never retries a
    second mechanism and never guesses.

    Raises:
        ClinicaRpaError(RESET_SCREEN_FAILED): the button could not be
            resolved (0 or more than 1 candidate), or GO's foreground
            could not be verified before the click.
        ClinicaRpaError(UI_ACTION_AMBIGUOUS): the click itself raised
            (Regla 2 -- may have already registered server-side).
    """
    from clinica_rpa.automation.dialogs import find_button_candidates_live
    from clinica_rpa.automation.go_session import _classify_and_log

    try:
        window = connect_uia(go_window.handle)
    except Exception as exc:
        _classify_and_log(exc, "fallo al reconectar UIA para el clic en Deshacer")
        raise ClinicaRpaError(ErrorCode.RESET_SCREEN_FAILED, "No se pudo conectar a la ventana de GO para el reinicio de pantalla.") from exc

    candidates = find_button_candidates_live(window, UNDO_BUTTON_VARIANTS, (), LIVE_SEARCH_MAX_DEPTH, LIVE_SEARCH_TIMEOUT_SECONDS)
    if len(candidates) != 1:
        raise ClinicaRpaError(
            ErrorCode.RESET_SCREEN_FAILED, f"{len(candidates)} candidatos 'Deshacer' (se requiere exactamente 1)."
        )

    undo_element = candidates[0]
    try:
        verify_go_foreground(go_window.handle, expected_pid=go_window.process_id)
        activate_preselected_once(undo_element)
    except Exception as exc:
        _classify_and_log(exc, "fallo ejecutando el clic autorizado en Deshacer")
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "El clic en Deshacer fallo de forma ambigua.") from exc

    logger.info("ACTION DESHACER CLICKED")


# --------------------------------------------------------------------------
# Write invoice number (ValuePattern only, exactly once)
# --------------------------------------------------------------------------


def _recursive_find_by_automation_id(element, automation_id: str, results: list, max_depth: int, deadline: float, depth: int = 0) -> None:
    if deadline is not None and time.monotonic() > deadline:
        return
    try:
        if element.element_info.automation_id == automation_id:
            results.append(element)
    except Exception:
        pass
    if depth >= max_depth:
        return
    try:
        children = element.children()
    except Exception:
        return
    for child in children:
        if deadline is not None and time.monotonic() > deadline:
            return
        _recursive_find_by_automation_id(child, automation_id, results, max_depth, deadline, depth + 1)


def find_invoice_input_elements(window) -> list:
    """Locate every live element whose automation_id == TARGET_AUTOMATION_ID.

    Never coordinates, never spatial heuristics. Tries the native
    ``descendants(auto_id=...)`` UIA search first, falls back to a bounded
    recursive walk (this project's installed pywinauto does not accept
    ``auto_id`` in ``build_condition`` -- see ``poc_write_invoice.py``).
    """
    try:
        found = window.descendants(auto_id=TARGET_AUTOMATION_ID)
        return list(found)
    except TypeError:
        pass
    except Exception:
        pass

    results: list = []
    deadline = time.monotonic() + AUTOMATION_ID_SEARCH_TIMEOUT_SECONDS
    _recursive_find_by_automation_id(window, TARGET_AUTOMATION_ID, results, TRAZABILIDAD_GATE_MAX_DEPTH, deadline)
    return results


def filter_visible_enabled(elements: list) -> list:
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


def read_current_value(element) -> str:
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


def write_invoice_once(element, value: str) -> None:
    """Write `value` into `element` EXACTLY ONCE, via UIA ValuePattern only.

    Raises RuntimeError (mapped by the caller to INVOICE_WRITE_FAILED,
    since nothing was attempted yet) if ValuePattern is unsupported -- never
    falls back to type_keys() as a second mechanism for this field.
    """
    try:
        supports_value = getattr(element, "iface_value", None) is not None
    except Exception:
        supports_value = False
    if not supports_value:
        raise RuntimeError("INVOICE_INPUT no soporta ValuePattern.")
    element.set_text(value)


# --------------------------------------------------------------------------
# Trigger search (Enter) -- the sole authorized exception to "never press
# Enter", scoped to exactly this element.
# --------------------------------------------------------------------------


def send_enter_once(element) -> None:
    """Send EXACTLY ONE Enter keystroke to `element`, and only to it."""
    element.type_keys("{ENTER}")


# --------------------------------------------------------------------------
# Read-only result-field snapshot + active-result validation
# --------------------------------------------------------------------------


def _control_info_rect(c: ControlInfo) -> Rect | None:
    text = c.rectangle
    try:
        parts = text.strip("()").split(",")
        values = {}
        for part in parts:
            part = part.strip()
            key, value = part[0], part[1:]
            values[key] = int(value)
        return Rect(values["L"], values["T"], values["R"], values["B"])
    except Exception:
        return None


def _is_contained_within(outer: Rect, inner: Rect, margin: int = 3) -> bool:
    return (
        inner.left >= outer.left - margin
        and inner.right <= outer.right + margin
        and inner.top >= outer.top - margin
        and inner.bottom <= outer.bottom + margin
    )


def is_right_and_aligned(label_rect: Rect, candidate_rect: Rect) -> bool:
    if _is_contained_within(label_rect, candidate_rect):
        return True
    if candidate_rect.left < label_rect.right + EDIT_LEFT_TOLERANCE_PX:
        return False
    vcenter_delta = abs(candidate_rect.vertical_center - label_rect.vertical_center)
    return vcenter_delta <= VERTICAL_CENTER_TOLERANCE_PX


def _find_label_control(controls: list[ControlInfo], variants: tuple[str, ...]) -> ControlInfo | None:
    for c in controls:
        if matches_any(f"{c.name} {c.automation_id}", variants):
            return c
    return None


def read_field_states(controls: list[ControlInfo]) -> dict[str, FieldSnapshot]:
    """Best-effort populated/length read for every field in
    FIELD_LABEL_VARIANTS. Never raises."""
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
            if is_right_and_aligned(label_rect, crect):
                matched = c
                break

        if matched is not None and matched.name.strip():
            states[field_name] = FieldSnapshot(True, len(matched.name))
        else:
            states[field_name] = FieldSnapshot(False, 0)
    return states


def check_not_found_evidence(controls: list[ControlInfo]) -> bool:
    """True only if explicit 'not found'-looking text is present."""
    for c in controls:
        haystack = f"{c.name} {c.automation_id}"
        if matches_any(haystack, NOT_FOUND_TEXT_HINTS):
            return True
    return False


def validate_invoice_result_active(controls: list[ControlInfo]) -> tuple[bool, int]:
    """Read-only: count how many of the 16 result-detail fields are
    populated. Returns (active_enough, populated_count)."""
    field_states = read_field_states(controls)
    populated_count = sum(1 for v in field_states.values() if v.populated)
    return populated_count >= MIN_ACTIVE_FIELDS_THRESHOLD, populated_count


def minimal_result_ready_check(window, max_wait_seconds: float) -> bool:
    """Single cheap targeted UIA read -- NEVER a full tree walk.

    Phase 1B close-out (GO_QUERY_REAL_WAIT_CONFIRMED): reads exactly ONE
    result field's current value via
    :func:`clinica_rpa.automation.go_session.uia_find_first_by_automation_id`,
    the cheapest independent detector proven live in Fase 1B.3 (raw
    ``FindFirst`` calls: 562-1969ms, versus 3-5s+ for a full
    ``scan_window_controls`` walk). A transient failure (GO's UI thread
    still busy, or the field not yet in the tree) is tolerated and treated
    as "not ready yet", never raised.

    The raw COM ``FindFirst`` call itself has no native timeout and can
    block for as long as GO's UI thread stays unresponsive (review finding
    R3-unbounded-com-findfirst) -- run it on a daemon thread and
    ``join(timeout=max_wait_seconds)`` so the caller's own safety ceiling
    is actually enforced end-to-end rather than only checked between
    calls. A timed-out check reports "not ready yet"; its thread is
    abandoned (daemon, never joined again), a deliberate, bounded leak
    rather than an unenforceable wait.

    Fase 1C live regression (2026-09-25, TEST A/FHC245333) caught this
    worker thread raising ``COMError(-2146233083, ...)`` ("catastrophic
    failure") -- COM was never initialized on this brand-new thread before
    touching a COM interface pointer created on the caller's thread. Fixed
    generally (not per-symptom) by giving the worker thread its own COM
    apartment via ``pythoncom.CoInitialize()``/``CoUninitialize()``, which
    UI Automation's documented free-threading support allows even though
    the pointer itself was created elsewhere.
    """
    result = {"ready": False}

    def _check() -> None:
        import pythoncom

        # Review finding R3-coinit-outside-try: CoInitialize() itself must
        # never escape this thread uncaught either -- otherwise a failure
        # here (e.g. RPC_E_CHANGED_MODE) would reach threading.excepthook
        # instead of the tolerated "not ready yet" path below, breaking
        # this function's documented never-raises contract. CoUninitialize
        # only runs when CoInitialize actually succeeded.
        try:
            pythoncom.CoInitialize()
        except Exception as exc:
            logger.debug("minimal_result_ready_check CoInitialize failed (tolerated): {!r}", exc)
            return
        try:
            element = uia_find_first_by_automation_id(window, RESULT_READY_SENTINEL_AUTOMATION_ID)
            if not element:
                return
            value = uia_raw_element_name(element)
            result["ready"] = bool(value and value.strip())
        except Exception as exc:
            # Review finding R3-sentinel-com-worker-thread-silent-failure:
            # a bare `pass` here would make a worker-thread-specific COM
            # failure (e.g. cross-apartment marshaling) indistinguishable
            # from "GO just isn't ready yet", silently degrading every
            # search to the full safety ceiling. Logged, never raised --
            # the caller still treats this tick as "not ready".
            logger.debug("minimal_result_ready_check worker thread error (tolerated): {!r}", exc)
        finally:
            pythoncom.CoUninitialize()

    thread = threading.Thread(target=_check, daemon=True)
    thread.start()
    thread.join(timeout=max(0.1, max_wait_seconds))
    return result["ready"]


def wait_for_search_result(
    window,
    scan_fn,
    timeout_seconds: float = DEFAULT_SEARCH_POLL_TIMEOUT_SECONDS,
    initial_delay_seconds: float = SEARCH_INITIAL_DELAY_SECONDS,
    backoff_seconds: float = SEARCH_BACKOFF_SECONDS,
) -> tuple[bool, bool, int]:
    """Cheap read-only wait after the Enter keystroke, then ONE full
    validation.

    Phase 1B close-out: Fase 1B.3's independent-detector diagnostic proved
    GO's own UI thread becomes unresponsive to ANY UIA call for
    ~42-46s while it resolves the query (GO_QUERY_REAL_WAIT_CONFIRMED) --
    this is GO's real time, not a detection-strategy problem, and cannot be
    optimized away from Python. This function therefore no longer repeats
    an expensive ``scan_fn()`` (full tree walk) every tick -- it polls with
    :func:`minimal_result_ready_check` (one cheap targeted field read) on a
    long backoff, then calls ``scan_fn()`` and validates the full 16-field
    result EXACTLY ONCE, either as soon as the sentinel looks ready or once
    ``timeout_seconds`` (a safety ceiling for a genuine failure, never a
    sleep target) is reached. If GO finishes before the sentinel would next
    be checked, the loop still only sleeps up to ``backoff_seconds`` before
    noticing.

    ``scan_fn()`` must return a fresh ``list[ControlInfo]`` snapshot.

    Returns (active_enough, not_found_evidence, populated_count). Logs
    ``GO_QUERY_WAIT_SECONDS`` (time spent waiting for the cheap sentinel)
    separately from ``AUTOMATION_OVERHEAD_SECONDS`` (the one final full
    validation's own cost) -- never mixed.
    """
    start = time.monotonic()
    time.sleep(min(initial_delay_seconds, timeout_seconds))
    ready = False
    while True:
        remaining = timeout_seconds - (time.monotonic() - start)
        if remaining <= 0:
            break
        ready = minimal_result_ready_check(window, max_wait_seconds=remaining)
        if ready:
            break
        remaining = timeout_seconds - (time.monotonic() - start)
        if remaining <= 0:
            break
        time.sleep(min(backoff_seconds, remaining))
    go_query_wait_seconds = time.monotonic() - start

    # Bounded retry (review finding R3-single-shot-validation-after-sentinel):
    # a single transient scan_fn() failure here must not immediately fail
    # the whole search -- by this point GO's freeze should already be over,
    # so a second attempt is cheap and expected to succeed.
    _val_t0 = time.monotonic()
    controls: list = []
    for attempt in range(2):
        try:
            controls = scan_fn()
        except Exception:
            controls = []
        if controls:
            break
        if attempt == 0:
            time.sleep(1.0)
    automation_overhead_seconds = time.monotonic() - _val_t0
    active_enough, populated_count = validate_invoice_result_active(controls) if controls else (False, 0)
    not_found = check_not_found_evidence(controls) if controls else False
    logger.info(
        "PERF GO_QUERY_WAIT_SECONDS {:.1f}s AUTOMATION_OVERHEAD_SECONDS {:.1f}s sentinel_ready={} found={}",
        go_query_wait_seconds, automation_overhead_seconds, ready, active_enough or not_found,
    )
    return active_enough, not_found, populated_count
