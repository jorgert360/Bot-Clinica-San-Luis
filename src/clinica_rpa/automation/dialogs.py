""""Guardar como" (write destination path, click Guardar) and the final
"Exportar" dialog (click "No", NEVER "Si").

Ported from ``scripts/poc_download_invoice_document.py`` PASO 8/10.

Safety-critical (mission-mandated): :func:`find_no_button_candidates_live`
matches the "No" button by STRICT exact normalized-name equality, never a
substring/``_matches_any`` check -- this is the single most safety-critical
control in the whole engine, guaranteeing "Si"/"Yes" can never be matched by
accident.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from clinica_rpa.automation.go_session import (
    LIVE_SEARCH_MAX_DEPTH,
    LIVE_SEARCH_TIMEOUT_SECONDS,
    WindowDiagnostic,
    _classify_and_log,
    _matches_any as matches_any,
    _normalize_for_match as normalize_for_match,
    activate_preselected_once,
    connect_uia,
    is_visible_enabled,
    live_element_automation_id,
    live_element_control_type,
    live_element_name,
    supports_value_pattern,
    verify_go_foreground,
    walk_live,
    walk_tree,
)
from clinica_rpa.automation.waits import light_wait_for_window, win32_wait_for_window
from clinica_rpa.domain.errors import ClinicaRpaError, ErrorCode

SAVE_DIALOG_TITLE_HINTS: tuple[str, ...] = ("guardar como", "save as")
SAVE_DIALOG_CLASS_HINT = "#32770"
SAVE_FILENAME_AUTOMATION_IDS: tuple[str, ...] = ("1001", "FileNameControlHost")
SAVE_FILENAME_NAME_VARIANTS: tuple[str, ...] = ("nombre de archivo", "nombre de archivo:", "file name", "file name:")
SAVE_BUTTON_STRONG_VARIANTS: tuple[str, ...] = ("guardar",)
SAVE_BUTTON_WEAK_VARIANTS: tuple[str, ...] = ("save",)
REJECT_BUTTON_VARIANTS: tuple[str, ...] = ("cancelar", "cancel")

FINAL_DIALOG_TITLE_HINTS: tuple[str, ...] = ("exportar",)
FINAL_DIALOG_CONTENT_HINTS: tuple[str, ...] = ("desea abrir",)

# Widened alongside report_viewer's dialog timeouts (Phase 1A live
# regression, 2026-09-24): GO demonstrated significant end-to-end backend
# slowness this session; these two dialogs haven't been exercised yet at
# time of writing but get the same generous allowance preemptively.
DEFAULT_SAVE_DIALOG_TIMEOUT_SECONDS = 30.0
DEFAULT_FINAL_DIALOG_TIMEOUT_SECONDS = 30.0
DEFAULT_LIGHT_POLL_INTERVAL_SECONDS = 0.5


# --------------------------------------------------------------------------
# Generic dialog-button finder, reused by report_viewer (Aceptar) and here
# (Guardar / No).
# --------------------------------------------------------------------------


def find_button_candidates_live(container, accept_variants: tuple[str, ...], reject_variants: tuple[str, ...], max_depth: int, timeout_seconds: float) -> list:
    deadline = time.monotonic() + timeout_seconds
    results: list = []
    accepted = {normalize_for_match(v).replace("&", "").rstrip(".") for v in accept_variants}
    rejected = {normalize_for_match(v).replace("&", "").rstrip(".") for v in reject_variants}

    def _visit(element, _depth):
        ctype = live_element_control_type(element)
        if "button" not in ctype and "custom" not in ctype:
            return
        if not is_visible_enabled(element):
            return
        name = normalize_for_match(live_element_name(element)).replace("&", "").rstrip(".")
        if name in accepted and name not in rejected:
            results.append(element)

    walk_live(container, _visit, max_depth, deadline)
    return results


def find_no_button_candidates_live(container, max_depth: int, timeout_seconds: float) -> list:
    """STRICT exact-token match: the normalized name must equal EXACTLY
    "no". Deliberately never uses word-boundary/substring matching here."""
    deadline = time.monotonic() + timeout_seconds
    results: list = []

    def _visit(element, _depth):
        ctype = live_element_control_type(element)
        if "button" not in ctype and "custom" not in ctype:
            return
        normalized = normalize_for_match(live_element_name(element)).replace("&", "").rstrip(".")
        if normalized == "no" and is_visible_enabled(element):
            results.append(element)

    walk_live(container, _visit, max_depth, deadline)
    return results


def _find_edit_candidates_live(container, max_depth: int, timeout_seconds: float) -> list:
    deadline = time.monotonic() + timeout_seconds
    results: list = []

    def _visit(element, _depth):
        if "edit" in live_element_control_type(element):
            results.append(element)

    walk_live(container, _visit, max_depth, deadline)
    return results


def find_filename_edit_candidates_live(container, max_depth: int, timeout_seconds: float) -> list:
    """Resolve only the standard Save As filename edit, never by width."""
    edits = _find_edit_candidates_live(container, max_depth, timeout_seconds)
    stable: list = []
    normalized_names = {normalize_for_match(v) for v in SAVE_FILENAME_NAME_VARIANTS}
    for element in edits:
        if not is_visible_enabled(element):
            continue
        automation_id = live_element_automation_id(element)
        name = normalize_for_match(live_element_name(element))
        if automation_id in SAVE_FILENAME_AUTOMATION_IDS or name in normalized_names:
            stable.append(element)
    return stable


# --------------------------------------------------------------------------
# "Guardar como": wait, compute destination, write path, click Guardar
# --------------------------------------------------------------------------


def wait_for_save_dialog(
    pids: list[int],
    before_handles: set[int],
    go_process_id: int,
    timeout_seconds: float = DEFAULT_SAVE_DIALOG_TIMEOUT_SECONDS,
):
    """Phase 1B.2: pure Win32 detection (see module docstring reference in
    ``waits.win32_wait_for_window``) -- live-measured ~922ms, previously
    misattributed to GO being slow because the old UIA-enumeration-based
    wait never saw the same dialog within 60+ seconds. ``pids`` is kept in
    the signature for call-site compatibility but is no longer used --
    detection is now scoped to the single already-resolved
    ``go_process_id`` (this session's own authenticated GO process).
    """
    before_hwnds = {h for h in before_handles}
    hwnd = win32_wait_for_window(
        go_process_id,
        SAVE_DIALOG_TITLE_HINTS,
        class_equals=SAVE_DIALOG_CLASS_HINT,
        timeout_seconds=timeout_seconds,
        before_hwnds=before_hwnds,
    )
    if hwnd is None:
        raise ClinicaRpaError(ErrorCode.SAVE_DIALOG_TIMEOUT, "El dialogo 'Guardar como' no aparecio a tiempo.")
    return SimpleNamespace(handle=hwnd, process_id=go_process_id)


def compute_destination_path(destination_directory: Path, invoice: str) -> Path:
    """Compute a non-colliding destination path BEFORE touching the dialog.

    Pattern: ``factura_<invoice>_<timestamp>.pdf``, inside
    ``destination_directory`` (created if missing).

    Bug found and fixed during Phase 1A live regression (2026-09-24): a
    RELATIVE ``destination_directory`` written verbatim into the native
    "Guardar como" filename field gets resolved by Windows against GO's
    OWN process working directory, not this script's -- it silently landed
    in an unrelated folder (observed: the dialog re-anchored itself to
    "Descargas"/Downloads). Always resolve to an absolute path before it
    is ever written into the dialog.
    """
    destination_directory = destination_directory.resolve()
    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = destination_directory / f"factura_{invoice}_{datetime.now():%Y%m%d_%H%M%S}.pdf"
    if destination.exists():
        destination = destination_directory / f"factura_{invoice}_{datetime.now():%Y%m%d_%H%M%S_%f}.pdf"
    return destination


def write_destination_path_once(save_window: WindowDiagnostic, destination: Path):
    """Write the destination path into the filename field (ValuePattern
    only, exactly once). Returns the connected save-dialog live element
    (``save_live``) so the caller can pass it to
    :func:`click_guardar_once` without re-resolving the dialog (Phase 1B:
    split from the combined write+click so each has its own timing stage).
    """
    try:
        save_live = connect_uia(save_window.handle)
    except Exception as exc:
        _classify_and_log(exc, "fallo al conectar UIA al dialogo Guardar como")
        raise ClinicaRpaError(ErrorCode.SAVE_DIALOG_TIMEOUT, "No se pudo conectar al dialogo 'Guardar como'.") from exc

    edit_candidates = find_filename_edit_candidates_live(save_live, LIVE_SEARCH_MAX_DEPTH, LIVE_SEARCH_TIMEOUT_SECONDS)
    if len(edit_candidates) != 1:
        raise ClinicaRpaError(ErrorCode.SAVE_DIALOG_TIMEOUT, f"{len(edit_candidates)} campos de nombre de archivo encontrados (se requiere exactamente 1).")
    filename_edit = edit_candidates[0]

    if not supports_value_pattern(filename_edit):
        raise ClinicaRpaError(ErrorCode.SAVE_DIALOG_TIMEOUT, "El campo de nombre de archivo no soporta ValuePattern.")
    try:
        filename_edit.set_text(str(destination))
    except Exception as exc:
        _classify_and_log(exc, "fallo escribiendo la ruta destino mediante ValuePattern")
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "La escritura de la ruta destino fallo de forma ambigua.") from exc

    return save_live


def click_guardar_once(save_window: WindowDiagnostic, save_live) -> None:
    """Click Guardar exactly once, on the SAME live dialog element already
    connected by :func:`write_destination_path_once` (never re-searches
    the filename field -- only the Guardar button)."""
    save_button_candidates = find_button_candidates_live(save_live, SAVE_BUTTON_STRONG_VARIANTS, REJECT_BUTTON_VARIANTS, LIVE_SEARCH_MAX_DEPTH, LIVE_SEARCH_TIMEOUT_SECONDS)
    if not save_button_candidates:
        save_button_candidates = find_button_candidates_live(save_live, SAVE_BUTTON_WEAK_VARIANTS, REJECT_BUTTON_VARIANTS, LIVE_SEARCH_MAX_DEPTH, LIVE_SEARCH_TIMEOUT_SECONDS)
    if len(save_button_candidates) != 1:
        raise ClinicaRpaError(ErrorCode.SAVE_DIALOG_TIMEOUT, f"{len(save_button_candidates)} candidatos 'Guardar' (se requiere exactamente 1).")

    save_button = save_button_candidates[0]
    try:
        verify_go_foreground(save_window.handle, expected_pid=save_window.process_id, allow_same_pid=False)
        activate_preselected_once(save_button)
    except Exception as exc:
        _classify_and_log(exc, "fallo ejecutando el clic autorizado en Guardar")
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "El clic en Guardar fallo de forma ambigua.") from exc


def write_save_path_once_and_click_guardar(save_window: WindowDiagnostic, destination: Path) -> None:
    """Convenience wrapper preserved for callers that don't need per-stage
    timing (e.g. ad-hoc diagnostics) -- write the path, then click Guardar,
    as two calls under the hood."""
    save_live = write_destination_path_once(save_window, destination)
    click_guardar_once(save_window, save_live)


# --------------------------------------------------------------------------
# Final "Exportar" dialog: click "No", NEVER "Si"
# --------------------------------------------------------------------------


def wait_for_final_export_dialog(
    pids: list[int],
    before_handles: set[int],
    go_process_id: int,
    timeout_seconds: float = DEFAULT_FINAL_DIALOG_TIMEOUT_SECONDS,
):
    """Phase 1B.2: pure Win32 detection -- live-measured ~2.7s. ``pids``
    kept for call-site compatibility, unused (see :func:`wait_for_save_dialog`)."""
    hwnd = win32_wait_for_window(
        go_process_id,
        FINAL_DIALOG_TITLE_HINTS,
        class_equals=None,
        timeout_seconds=timeout_seconds,
        before_hwnds=set(before_handles),
    )
    if hwnd is None:
        raise ClinicaRpaError(ErrorCode.FINAL_DIALOG_TIMEOUT, "El dialogo final 'Exportar' no aparecio a tiempo.")
    return SimpleNamespace(handle=hwnd, process_id=go_process_id)


def click_final_no_once(final_window: WindowDiagnostic) -> None:
    """The ONE authorized interaction with this dialog: click "No". NEVER
    "Si"/"Yes" under any code path -- see :func:`find_no_button_candidates_live`.
    """
    try:
        final_live = connect_uia(final_window.handle)
        deadline = time.monotonic() + 5.0
        final_controls = walk_tree(final_live, max_depth=LIVE_SEARCH_MAX_DEPTH, deadline=deadline)
        content_haystack = " ".join(f"{c.name}" for c in final_controls)
        content_confirmed = matches_any(content_haystack, FINAL_DIALOG_CONTENT_HINTS)
    except Exception as exc:
        _classify_and_log(exc, "fallo confirmando el contenido del dialogo final")
        raise ClinicaRpaError(ErrorCode.FINAL_DIALOG_TIMEOUT, "No se pudo confirmar el contenido del dialogo final.") from exc

    if not content_confirmed:
        raise ClinicaRpaError(ErrorCode.FINAL_DIALOG_TIMEOUT, "Se encontro una ventana 'Exportar' pero no se confirmo su contenido esperado.")

    no_candidates = find_no_button_candidates_live(final_live, LIVE_SEARCH_MAX_DEPTH, LIVE_SEARCH_TIMEOUT_SECONDS)
    if len(no_candidates) != 1:
        raise ClinicaRpaError(ErrorCode.FINAL_DIALOG_TIMEOUT, f"{len(no_candidates)} candidatos 'No' encontrados (se requiere exactamente 1; comparacion EXACTA, nunca subcadena).")

    no_button = no_candidates[0]
    try:
        verify_go_foreground(final_window.handle, expected_pid=final_window.process_id, allow_same_pid=False)
        activate_preselected_once(no_button)
    except Exception as exc:
        _classify_and_log(exc, "fallo ejecutando el clic autorizado en No")
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "El clic en No fallo de forma ambigua.") from exc
