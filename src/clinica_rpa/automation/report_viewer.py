"""Open "Documento Origen", wait for the "Visor de Reportes" MDI child,
open the Export Document dropdown, select "PDF File", wait for the PDF
Options dialog, and click "Aceptar".

Ported from ``scripts/poc_download_invoice_document.py`` PASO 2-7.

PDF File selection (Regla 5, mandatory design note): live testing proved
"PDF File" inside the export-format popup has ZERO UIA/MSAA accessible
identity -- ``control_type`` does not expose it as ``MenuItem`` and MSAA's
``accChild()`` fails entirely on its children. The only proven-working
approach is a single coordinate click computed proportionally from the
popup window's own freshly-read rectangle (never a hardcoded point): the
export-format list has 9 known entries (PDF File, HTML File, MHT File, RTF
File, XLS File, XLSX File, CSV File, Text File, Image File), so PDF File
(item 1 of 9) is targeted at
``target_y = rect_top + (rect_height / 9) / 2``,
``target_x = rect_left + rect_width / 2``. This module therefore detects
only the POPUP WINDOW's existence/rectangle via UIA (never the individual
menu item), then performs one coordinate click via
:func:`clinica_rpa.automation.go_session.click_once`.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from clinica_rpa.automation.go_session import (
    LIVE_SEARCH_MAX_DEPTH,
    LIVE_SEARCH_TIMEOUT_SECONDS,
    ControlInfo,
    Rect,
    WindowDiagnostic,
    _classify_and_log,
    _matches_any as matches_any,
    activate_preselected_once,
    best_effort_tooltip_live,
    click_once,
    connect_uia,
    dedupe_live_elements,
    is_visible_enabled,
    live_element_automation_id,
    live_element_class_name,
    live_element_control_type,
    live_element_key,
    live_element_name,
    live_element_process_id,
    rect_from_live_element,
    snapshot_visible_uia_root_keys,
    supports_expand_collapse_pattern,
    verify_go_foreground,
    walk_live,
)
from clinica_rpa.automation.waits import find_mdi_child_live, light_wait_for_window
from clinica_rpa.automation.trazabilidad import mask_invoice
from clinica_rpa.domain.errors import ClinicaRpaError, ErrorCode

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DOCUMENTO_ORIGEN_LABEL_VARIANTS: tuple[str, ...] = ("documento origen",)
DOCUMENTO_ORIGEN_VALUE_PREFIX = "Factura - "
DOCUMENTO_ORIGEN_AUTOMATION_ID = "INDHleDocument"

REPORT_VIEWER_TITLE_HINTS: tuple[str, ...] = ("visor de reportes",)
REPORT_VIEWER_AUTOMATION_ID_HINTS: tuple[str, ...] = ("FrmReportViewer",)

EXPORT_BUTTON_STRONG_VARIANTS: tuple[str, ...] = ("export document", "exportar documento")
EXPORT_BUTTON_WEAK_VARIANTS: tuple[str, ...] = ("export", "exportar")
EXPORT_ARROW_NAME_VARIANTS: tuple[str, ...] = ("drop down", "dropdown", "open menu", "more options", "flecha")

# The 9 known export formats, in the order they appear in the popup. "PDF
# File" is item index 0 (first of 9) -- see module docstring for the
# proportional-click formula this drives.
EXPORT_FORMAT_ITEM_COUNT = 9
PDF_FILE_ITEM_INDEX = 0

PDF_OPTIONS_TITLE_HINTS: tuple[str, ...] = ("opciones de exportacion pdf", "opciones de exportación pdf")
ACCEPT_BUTTON_STRONG_VARIANTS: tuple[str, ...] = ("aceptar",)
ACCEPT_BUTTON_WEAK_VARIANTS: tuple[str, ...] = ("accept", "ok")
REJECT_BUTTON_VARIANTS: tuple[str, ...] = ("cancelar", "cancel")

VIEWER_RECEIPT_PATH = Path("runtime") / "state" / "invoice_download_viewer_receipt.json"

# Widened from the PoC's own 7.0s default (Phase 1A live regression,
# 2026-09-24, third observation): the Documento Origen click itself always
# succeeded, but "Visor de Reportes" repeatedly took well over 7s+1s to
# become observable this session -- confirmed present and correct every
# time this fired, just detected too late. 30s matches the generosity
# already applied to the Guardar como/final-dialog waits.
DEFAULT_DOCUMENTO_ORIGEN_NO_EFFECT_TIMEOUT_SECONDS = 30.0
DEFAULT_REPORT_VIEWER_TIMEOUT_SECONDS = 20.0
# Widened from the PoC's own 10.0s default (Phase 1A live regression,
# 2026-09-24, second observation): GO's overall backend responsiveness was
# demonstrably slow during this session (56s end-to-end from Documento
# Origen click to PDF Options actually appearing, confirmed present on
# screen despite two prior timeouts) -- every click/selection in the
# pipeline had already succeeded, only the read-only waits were too tight.
DEFAULT_EXPORT_MENU_TIMEOUT_SECONDS = 20.0
DEFAULT_PDF_OPTIONS_TIMEOUT_SECONDS = 45.0
DEFAULT_LIGHT_POLL_INTERVAL_SECONDS = 0.5


@dataclass
class ExportArrowTarget:
    """One preselected way to open only the split-button dropdown."""

    method: str
    element: object
    relative_coords: tuple[int, int] | None = None
    screen_point: tuple[int, int] | None = None


# --------------------------------------------------------------------------
# Session-receipt pattern (Regla 9): avoid re-clicking Documento Origen if a
# report viewer belonging to THIS invoice+process is already open.
# --------------------------------------------------------------------------


def _invoice_fingerprint(invoice: str) -> str:
    return hashlib.sha256(f"clinica-rpa:invoice-download:{invoice}".encode("utf-8")).hexdigest()


def save_viewer_receipt(invoice: str, process_id: int, viewer_handle: int, output_path: Path = VIEWER_RECEIPT_PATH) -> None:
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


def viewer_receipt_matches(invoice: str, process_id: int, viewer_handle: int, receipt_path: Path = VIEWER_RECEIPT_PATH) -> bool:
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
# Documento Origen: identify (read-only) + single authorized click
# --------------------------------------------------------------------------


def find_documento_origen_value_live_candidates(window, label_rect, max_depth: int, timeout_seconds: float) -> list:
    deadline = time.monotonic() + timeout_seconds
    results: list = []

    def _visit(element, _depth):
        name = live_element_name(element)
        if not name.startswith(DOCUMENTO_ORIGEN_VALUE_PREFIX):
            return
        rect = rect_from_live_element(element)
        if rect is not None and _is_right_and_aligned(label_rect, rect):
            results.append(element)

    walk_live(window, _visit, max_depth, deadline)
    return results


def _is_right_and_aligned(label_rect: Rect, candidate_rect: Rect) -> bool:
    from clinica_rpa.automation.trazabilidad import is_right_and_aligned

    return is_right_and_aligned(label_rect, candidate_rect)


def open_documento_origen(
    go_window: WindowDiagnostic,
    window,
    controls: list[ControlInfo],
    invoice: str,
    pids: list[int],
    max_depth: int,
    scan_timeout: float,
    no_effect_timeout: float = DEFAULT_DOCUMENTO_ORIGEN_NO_EFFECT_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_LIGHT_POLL_INTERVAL_SECONDS,
):
    """Locate and (if not already open for this invoice) click "Documento
    Origen"'s value exactly once. Returns
    ``(viewer_live_or_None, click_executed)`` -- ``viewer_live`` is the live
    "Visor de Reportes" MDI child element if it already appeared during this
    call, else ``None`` (the caller must still wait via
    :func:`wait_for_report_viewer`); ``click_executed`` tells the caller
    whether a fresh click happened (and therefore whether a new session
    receipt should be saved).

    Raises:
        ClinicaRpaError(DOCUMENT_ORIGIN_NOT_FOUND): the value control could
            not be resolved, or did not match the requested invoice.
        ClinicaRpaError(UI_ACTION_AMBIGUOUS): the click raised.
        ClinicaRpaError(REPORT_VIEWER_TIMEOUT): no MDI child/new window
            appeared after the click within ``no_effect_timeout``.
    """
    from clinica_rpa.automation.trazabilidad import _control_info_rect
    from clinica_rpa.automation.go_session import _snapshot_target_windows

    label_candidates = [c for c in controls if matches_any(f"{c.name} {c.automation_id}", DOCUMENTO_ORIGEN_LABEL_VARIANTS)]
    if not label_candidates:
        raise ClinicaRpaError(ErrorCode.DOCUMENT_ORIGIN_NOT_FOUND, "No se encontro el label 'Documento Origen'.")

    resolved_value_element = None
    for label in label_candidates:
        label_rect = _control_info_rect(label)
        if label_rect is None:
            continue
        try:
            value_candidates = find_documento_origen_value_live_candidates(window, label_rect, max_depth, scan_timeout)
        except Exception as exc:
            _classify_and_log(exc, "fallo buscando el control de valor de Documento Origen")
            continue
        stable = [
            c
            for c in value_candidates
            if is_visible_enabled(c) and live_element_automation_id(c) == DOCUMENTO_ORIGEN_AUTOMATION_ID
        ]
        if len(stable) == 1:
            resolved_value_element = stable[0]
            break

    if resolved_value_element is None:
        raise ClinicaRpaError(
            ErrorCode.DOCUMENT_ORIGIN_NOT_FOUND, "No se pudo resolver el control de valor de 'Documento Origen'."
        )

    value_name = live_element_name(resolved_value_element)
    plausible = value_name.startswith(DOCUMENTO_ORIGEN_VALUE_PREFIX) and value_name[len(DOCUMENTO_ORIGEN_VALUE_PREFIX):] == invoice
    if not plausible:
        raise ClinicaRpaError(
            ErrorCode.DOCUMENT_ORIGIN_NOT_FOUND, "El control de 'Documento Origen' no corresponde a la factura solicitada."
        )

    # Pre-check: is a viewer already open for THIS invoice+process? Skip the
    # click entirely if so (Regla 1/14: verify state before acting).
    viewer_live = find_mdi_child_live(
        window, REPORT_VIEWER_AUTOMATION_ID_HINTS, REPORT_VIEWER_TITLE_HINTS, max_depth=min(LIVE_SEARCH_MAX_DEPTH, 14), timeout_seconds=5.0
    )
    if viewer_live is not None:
        try:
            preexisting_handle = int(viewer_live.handle)
        except Exception:
            preexisting_handle = 0
        if preexisting_handle and viewer_receipt_matches(invoice, go_window.process_id, preexisting_handle):
            logger.info("Visor de Reportes ya abierto y recibo coincide; se omite el clic en Documento Origen.")
            return viewer_live, False
        raise ClinicaRpaError(
            ErrorCode.GO_STATE_UNKNOWN,
            "Hay un Visor de Reportes abierto sin recibo de sesion que lo vincule con esta descarga.",
        )

    before_click_handles = {w.handle for w in _snapshot_target_windows(pids)}
    try:
        verify_go_foreground(go_window.handle, expected_pid=go_window.process_id, allow_same_pid=False)
        activate_preselected_once(resolved_value_element)
    except Exception as exc:
        _classify_and_log(exc, "fallo ejecutando el clic autorizado en Documento Origen")
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "El clic en Documento Origen fallo de forma ambigua.") from exc

    logger.info("Documento Origen: clic ejecutado (factura enmascarada={}).", mask_invoice(invoice))

    deadline = time.monotonic() + no_effect_timeout
    while viewer_live is None and time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        viewer_live = find_mdi_child_live(
            window, REPORT_VIEWER_AUTOMATION_ID_HINTS, REPORT_VIEWER_TITLE_HINTS, max_depth=min(LIVE_SEARCH_MAX_DEPTH, 14), timeout_seconds=min(2.0, remaining)
        )
        if viewer_live is None:
            time.sleep(poll_interval)

    any_new_top_level = None
    if viewer_live is None:
        any_new_top_level = light_wait_for_window(
            lambda: _snapshot_target_windows(pids), before_click_handles, lambda w: w.process_id == go_window.process_id, 1.0, poll_interval, require_new=True
        )
    if viewer_live is None and any_new_top_level is None:
        raise ClinicaRpaError(
            ErrorCode.REPORT_VIEWER_TIMEOUT, "Documento Origen no produjo ningun efecto observable."
        )

    return viewer_live, True


def wait_for_report_viewer(window, viewer_live, timeout_seconds: float = DEFAULT_REPORT_VIEWER_TIMEOUT_SECONDS, poll_interval: float = DEFAULT_LIGHT_POLL_INTERVAL_SECONDS):
    """Wait (light polling) for the "Visor de Reportes" MDI child if it did
    not already appear during the pre-check/no-effect window."""
    if viewer_live is not None:
        return viewer_live
    deadline = time.monotonic() + timeout_seconds
    while viewer_live is None and time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        viewer_live = find_mdi_child_live(
            window, REPORT_VIEWER_AUTOMATION_ID_HINTS, REPORT_VIEWER_TITLE_HINTS, max_depth=min(LIVE_SEARCH_MAX_DEPTH, 14), timeout_seconds=min(2.0, remaining)
        )
        if viewer_live is None:
            time.sleep(poll_interval)
    if viewer_live is None:
        raise ClinicaRpaError(ErrorCode.REPORT_VIEWER_TIMEOUT, "'Visor de Reportes' no aparecio a tiempo.")
    return viewer_live


# --------------------------------------------------------------------------
# Export Document dropdown arrow (never the split-button's center)
# --------------------------------------------------------------------------


def find_export_button_candidates(viewer_window, max_depth: int, timeout_seconds: float) -> tuple[list, list]:
    deadline = time.monotonic() + timeout_seconds
    strong: list = []
    weak: list = []

    def _visit(element, _depth):
        ctype = live_element_control_type(element)
        if "button" not in ctype and "menuitem" not in ctype and "custom" not in ctype:
            return
        name = live_element_name(element)
        automation_id = live_element_automation_id(element)
        tooltip = best_effort_tooltip_live(element)
        haystack = f"{name} {automation_id} {tooltip}"
        if matches_any(haystack, EXPORT_BUTTON_STRONG_VARIANTS):
            strong.append(element)
        elif matches_any(haystack, EXPORT_BUTTON_WEAK_VARIANTS):
            weak.append(element)

    walk_live(viewer_window, _visit, max_depth, deadline)
    return strong, weak


def resolve_export_arrow_target(viewer, export_element) -> ExportArrowTarget | None:
    """Resolve only the dropdown half of Export Document.

    Semantic ExpandCollapse is preferred; a separately exposed arrow button
    is next; the final fallback is a point relative to the split-button's
    OWN rectangle (right edge), never its center.
    """
    if supports_expand_collapse_pattern(export_element):
        return ExportArrowTarget(method="expand_pattern", element=export_element)

    export_rect = rect_from_live_element(export_element)
    if export_rect is None or export_rect.width < 16 or export_rect.height < 10:
        return None

    named: list = []
    geometric: list = []
    deadline = time.monotonic() + 5.0

    def _visit(element, _depth):
        if element is export_element or not is_visible_enabled(element):
            return
        ctype = live_element_control_type(element)
        if not any(kind in ctype for kind in ("button", "splitbutton", "custom")):
            return
        rect = rect_from_live_element(element)
        if rect is None or rect.width <= 0 or rect.height <= 0:
            return
        vertical_overlap = max(0, min(export_rect.bottom, rect.bottom) - max(export_rect.top, rect.top))
        min_height = max(1, min(export_rect.height, rect.height))
        adjacent_right = -2 <= rect.left - export_rect.right <= 6
        inside_right = rect.left >= export_rect.left + int(export_rect.width * 0.65) and rect.right <= export_rect.right + 2
        narrow = rect.width <= max(32, int(export_rect.width * 0.45))
        structurally_related = (adjacent_right or inside_right) and narrow and vertical_overlap / min_height >= 0.70
        if not structurally_related:
            return
        name_hint = f"{live_element_name(element)} {live_element_automation_id(element)} {best_effort_tooltip_live(element)}"
        if matches_any(name_hint, EXPORT_ARROW_NAME_VARIANTS):
            named.append(element)
        else:
            geometric.append(element)

    walk_live(viewer, _visit, LIVE_SEARCH_MAX_DEPTH, deadline)
    named = dedupe_live_elements(named)
    if len(named) == 1:
        return ExportArrowTarget(method="arrow_control", element=named[0])

    geometric = dedupe_live_elements(geometric)
    if len(geometric) == 1:
        rect = rect_from_live_element(geometric[0])
        if rect is not None:
            return ExportArrowTarget(method="relative_confirmed_control", element=geometric[0], screen_point=(rect.left + rect.width // 2, rect.top + rect.height // 2))

    relative_x = max(1, export_rect.width - min(7, max(3, export_rect.width // 8)))
    relative_y = max(1, export_rect.height // 2)
    return ExportArrowTarget(
        method="relative_export_rect", element=export_element, relative_coords=(relative_x, relative_y), screen_point=(export_rect.left + relative_x, export_rect.top + relative_y)
    )


def open_export_dropdown_once(target: ExportArrowTarget) -> None:
    """Open the dropdown with exactly one preselected UI action.

    Design note (deviation from the interactive PoC, explicitly authorized
    for this unattended service): ``poc_download_invoice_document.py``
    gated any coordinate-fallback method
    (``relative_confirmed_control``/``relative_export_rect``) behind a
    prior human-confirmed MOVE-ONLY rehearsal (``--export-arrow-confirmed``).
    Phase 1A runs unattended, so this function performs the click directly
    when that fallback is resolved -- it always reuses the EXACT SAME
    relative-to-the-split-button's-own-live-rect computation from
    :func:`resolve_export_arrow_target` above (never a hardcoded point,
    never the button's center).
    """
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


def open_export_menu(
    go_window: WindowDiagnostic, viewer_window, viewer_live, export_menu_timeout: float = DEFAULT_EXPORT_MENU_TIMEOUT_SECONDS
) -> None:
    """Find Export Document, open ONLY its dropdown arrow (single click),
    then select "PDF File" via a single proportional coordinate click.

    Raises:
        ClinicaRpaError(EXPORT_ARROW_NOT_FOUND): the button/arrow could not
            be resolved unambiguously.
        ClinicaRpaError(UI_ACTION_AMBIGUOUS): the arrow click raised.
        ClinicaRpaError(PDF_FILE_NOT_FOUND): the export-format popup never
            appeared, or its rectangle could not be resolved.
    """
    strong, weak = find_export_button_candidates(viewer_live, LIVE_SEARCH_MAX_DEPTH, LIVE_SEARCH_TIMEOUT_SECONDS)
    export_candidates = dedupe_live_elements([c for c in (strong if strong else weak) if is_visible_enabled(c)])
    if len(export_candidates) != 1:
        raise ClinicaRpaError(ErrorCode.EXPORT_ARROW_NOT_FOUND, f"{len(export_candidates)} candidatos 'Export Document' (se requiere exactamente 1).")

    export_element = export_candidates[0]
    arrow_target = resolve_export_arrow_target(viewer_live, export_element)
    if arrow_target is None:
        raise ClinicaRpaError(ErrorCode.EXPORT_ARROW_NOT_FOUND, "No se pudo resolver la flecha del split button Export Document.")

    before_popup_keys = snapshot_visible_uia_root_keys(go_window.process_id)
    try:
        verify_go_foreground(viewer_window.handle, expected_pid=go_window.process_id, allow_same_pid=False)
        open_export_dropdown_once(arrow_target)
    except Exception as exc:
        _classify_and_log(exc, "fallo abriendo la flecha del split button Export Document")
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "La apertura de la flecha Export Document fallo de forma ambigua.") from exc

    _select_pdf_file_once(go_window, viewer_window, before_popup_keys, export_menu_timeout)


def _find_export_popup_rect(expected_pid: int, before_root_keys: set[tuple], timeout_seconds: float) -> Rect | None:
    """Detect ONLY the existence/rectangle of the new export-format popup
    window -- never the individual "PDF File" menu item (proven to have no
    accessible identity, see module docstring)."""
    from pywinauto import Desktop

    start = time.monotonic()
    delay = 0.25
    while time.monotonic() - start <= timeout_seconds:
        try:
            desktop_roots = Desktop(backend="uia").windows()
        except Exception:
            desktop_roots = []

        for root in desktop_roots:
            if live_element_process_id(root) != expected_pid:
                continue
            if live_element_key(root) in before_root_keys:
                continue
            ctype = live_element_control_type(root)
            class_name = live_element_class_name(root).casefold()
            rect = rect_from_live_element(root)
            popup_like = (
                "menu" in ctype
                or "popup" in class_name
                or "dropdown" in class_name
                or class_name == "#32768"
                or (rect is not None and 0 < rect.width <= 700 and 0 < rect.height <= 900)
            )
            if popup_like and rect is not None and is_visible_enabled(root):
                return rect

        time.sleep(delay)
        delay = min(delay + 0.25, 1.5)

    return None


def _select_pdf_file_once(go_window: WindowDiagnostic, viewer_window, before_popup_keys: set[tuple], timeout_seconds: float) -> None:
    popup_rect = _find_export_popup_rect(go_window.process_id, before_popup_keys, timeout_seconds)
    if popup_rect is None:
        raise ClinicaRpaError(ErrorCode.PDF_FILE_NOT_FOUND, "El popup de formatos de exportacion no aparecio.")

    # Proportional point for item PDF_FILE_ITEM_INDEX of EXPORT_FORMAT_ITEM_COUNT,
    # computed fresh from the popup's OWN just-read rectangle -- never a
    # hardcoded/invented screen point (Regla 1/5).
    item_height = popup_rect.height / EXPORT_FORMAT_ITEM_COUNT
    target_x = popup_rect.left + popup_rect.width // 2
    target_y = round(popup_rect.top + item_height * PDF_FILE_ITEM_INDEX + item_height / 2)

    try:
        verify_go_foreground(viewer_window.handle, expected_pid=go_window.process_id, allow_same_pid=False)
        click_once(target_x, target_y)
    except Exception as exc:
        _classify_and_log(exc, "fallo seleccionando 'PDF File' por coordenada proporcional")
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "La seleccion de 'PDF File' fallo de forma ambigua.") from exc


# --------------------------------------------------------------------------
# PDF Options dialog: wait + click Aceptar (never touches any other option)
# --------------------------------------------------------------------------


def wait_for_pdf_options_dialog(pids: list[int], before_handles: set[int], go_process_id: int, timeout_seconds: float = DEFAULT_PDF_OPTIONS_TIMEOUT_SECONDS, poll_interval: float = DEFAULT_LIGHT_POLL_INTERVAL_SECONDS):
    from clinica_rpa.automation.go_session import _snapshot_target_windows

    window = light_wait_for_window(
        lambda: _snapshot_target_windows(pids), before_handles, lambda w: w.process_id == go_process_id and matches_any(w.title, PDF_OPTIONS_TITLE_HINTS), timeout_seconds, poll_interval, require_new=True
    )
    if window is None:
        raise ClinicaRpaError(ErrorCode.PDF_OPTIONS_TIMEOUT, "El dialogo 'Opciones de Exportacion PDF' no aparecio a tiempo.")
    return window


def click_aceptar_once(pdf_options_window) -> None:
    """The ONE authorized interaction with this dialog: click Aceptar.

    Never touches any other option (no set_text/toggle/select anywhere in
    this function).
    """
    from clinica_rpa.automation.dialogs import find_button_candidates_live

    try:
        pdf_options_live = connect_uia(pdf_options_window.handle)
    except Exception as exc:
        _classify_and_log(exc, "fallo al conectar UIA al dialogo de opciones PDF")
        raise ClinicaRpaError(ErrorCode.PDF_OPTIONS_TIMEOUT, "No se pudo conectar al dialogo de opciones PDF.") from exc

    accept_candidates = find_button_candidates_live(pdf_options_live, ACCEPT_BUTTON_STRONG_VARIANTS, REJECT_BUTTON_VARIANTS, LIVE_SEARCH_MAX_DEPTH, LIVE_SEARCH_TIMEOUT_SECONDS)
    if not accept_candidates:
        accept_candidates = find_button_candidates_live(pdf_options_live, ACCEPT_BUTTON_WEAK_VARIANTS, REJECT_BUTTON_VARIANTS, LIVE_SEARCH_MAX_DEPTH, LIVE_SEARCH_TIMEOUT_SECONDS)
    if len(accept_candidates) != 1:
        raise ClinicaRpaError(ErrorCode.PDF_OPTIONS_TIMEOUT, f"{len(accept_candidates)} candidatos 'Aceptar' (se requiere exactamente 1).")

    accept_element = accept_candidates[0]
    try:
        verify_go_foreground(pdf_options_window.handle, expected_pid=pdf_options_window.process_id, allow_same_pid=False)
        activate_preselected_once(accept_element)
    except Exception as exc:
        _classify_and_log(exc, "fallo ejecutando el clic autorizado en Aceptar")
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "El clic en Aceptar fallo de forma ambigua.") from exc
