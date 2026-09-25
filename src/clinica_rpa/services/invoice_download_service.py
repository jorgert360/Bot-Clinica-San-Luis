"""Application service: download one invoice's origin document as a PDF.

Orchestrates the ``automation.*`` modules end to end, following the exact
16-step flow already proven live and audited in
``scripts/poc_download_invoice_document.py`` (plus its dependency PoCs for
opening Trazabilidad, writing the invoice, and triggering the search):

locate authenticated GO session -> open Trazabilidad if on Mis Favoritos ->
write invoice number -> trigger search (Enter) -> validate result loaded ->
open Documento Origen (skip if a report viewer is already open for this
invoice) -> wait for Visor de Reportes -> open the Export Document dropdown
(arrow only) -> select PDF File -> wait for PDF Options -> click Aceptar ->
wait for Guardar como -> compute a safe destination filename -> write it ->
click Guardar -> wait for the file (filesystem) -> wait for the final
dialog -> click No -> validate the PDF -> return
:class:`~clinica_rpa.domain.models.InvoiceDownloadResult`.

Every mutating action funnels through exactly one call site inside the
``automation.*`` module that owns it (Regla 2); this service only sequences
them and translates :class:`~clinica_rpa.domain.errors.ClinicaRpaError`
into the returned result -- it never catches an ambiguous
post-mutation exception and retries.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from loguru import logger

from clinica_rpa.automation import dialogs, go_session, report_viewer, trazabilidad, waits
from clinica_rpa.domain.errors import ClinicaRpaError, ErrorCode
from clinica_rpa.domain.models import InvoiceDownloadResult, StageTiming
from clinica_rpa.pdf.validation import validate_pdf_file


class _StageRecorder:
    """Phase 1B instrumentation: monotonic per-stage timing.

    Never carries clinical data or a raw invoice number -- only stage
    name, elapsed milliseconds, and a status token ("OK"/"SKIPPED"/"FAIL").
    Logs one ``PERF <STAGE> <ms>ms <STATUS>`` line per stage (the exact
    format requested), and accumulates :class:`StageTiming` records so the
    caller can attach the full breakdown to the returned
    :class:`InvoiceDownloadResult`.
    """

    def __init__(self) -> None:
        self.records: list[StageTiming] = []

    def run(self, stage: str, fn, *args, **kwargs):
        start = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            self.records.append(StageTiming(stage, elapsed_ms, "FAIL"))
            logger.info("PERF {} {:.0f}ms FAIL", stage, elapsed_ms)
            raise
        elapsed_ms = (time.monotonic() - start) * 1000.0
        self.records.append(StageTiming(stage, elapsed_ms, "OK"))
        logger.info("PERF {} {:.0f}ms OK", stage, elapsed_ms)
        return result

    def skip(self, stage: str) -> None:
        self.records.append(StageTiming(stage, 0.0, "SKIPPED"))
        logger.info("PERF {} 0ms SKIPPED", stage)

DEFAULT_MAX_DEPTH = trazabilidad.TRAZABILIDAD_GATE_MAX_DEPTH
DEFAULT_SCAN_TIMEOUT_SECONDS = trazabilidad.TRAZABILIDAD_GATE_TIMEOUT_SECONDS
DEFAULT_SEARCH_TIMEOUT_SECONDS = trazabilidad.DEFAULT_SEARCH_POLL_TIMEOUT_SECONDS
DEFAULT_FILE_WAIT_TIMEOUT_SECONDS = 30.0
# Read-only retry budget for confirming Trazabilidad opened after the single
# calibrated click (see _resolve_go_state) -- never a second click.
DEFAULT_TRAZABILIDAD_OPEN_VERIFY_TIMEOUT_SECONDS = 10.0


def _error_result(
    invoice_masked: str, started_at: float, error: ClinicaRpaError, stage: "_StageRecorder | None" = None
) -> InvoiceDownloadResult:
    return InvoiceDownloadResult(
        invoice_number_masked=invoice_masked,
        status=error.error_code,
        pdf_created=False,
        pdf_path=None,
        pdf_size_bytes=None,
        pdf_pages=None,
        elapsed_seconds=time.monotonic() - started_at,
        error_code=error.error_code,
        error_message_safe=error.message_safe,
        stage_timings=list(stage.records) if stage is not None else [],
    )


def _go_discovery(pids: list[int]) -> tuple[go_session.WindowDiagnostic, object, list, list]:
    """GO_DISCOVERY stage: locate the authenticated GO window, verify
    foreground, connect UIA, and take a first read-only scan. Returns
    (go_window, live_window, controls, all_windows) -- all_windows is
    reused by the OPEN_TRAZABILIDAD stage to avoid a second enumeration.
    """
    try:
        all_windows = go_session.enumerate_all_windows()
    except go_session.WindowNotFoundError as exc:
        raise ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "No se pudo enumerar las ventanas del escritorio.") from exc

    try:
        # Phase 1B.1 optimization: request the SAME depth/timeout _go_discovery
        # would otherwise use for its own separate scan, and reuse the winning
        # candidate's already-scanned controls -- avoids a measured-live
        # duplicate full tree walk of the same window (~0.6-1.4s).
        go_window, controls = go_session.find_authenticated_go_window(
            pids,
            all_windows,
            require_favoritos_signals=False,
            scan_max_depth=DEFAULT_MAX_DEPTH,
            scan_timeout_seconds=DEFAULT_SCAN_TIMEOUT_SECONDS,
            return_controls=True,
        )
    except go_session.WindowNotFoundError as exc:
        raise ClinicaRpaError(ErrorCode.GO_NOT_AUTHENTICATED, "No se encontro una ventana de GO autenticada.") from exc
    except go_session.GoAmbiguousError as exc:
        raise ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "Mas de una ventana de GO autenticada simultanea.") from exc

    try:
        go_session.verify_go_foreground(go_window.handle, expected_pid=go_window.process_id)
    except go_session.GoNotForegroundError as exc:
        raise ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "GO no esta en primer plano.") from exc

    try:
        window = go_session.connect_uia(go_window.handle)
    except Exception as exc:
        go_session._classify_and_log(exc, "fallo reconectando via UIA a la ventana GO")
        raise ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "No se pudo conectar via UIA a la ventana GO.") from exc

    return go_window, window, controls, all_windows


def _open_trazabilidad_if_needed(
    go_window: go_session.WindowDiagnostic,
    pids: list[int],
    all_windows: list,
    max_depth: int,
    scan_timeout: float,
    trazabilidad_open_verify_timeout: float,
) -> list:
    """OPEN_TRAZABILIDAD stage body (only invoked when NOT already active --
    the caller records a SKIPPED stage instead when it is). Not on
    Trazabilidad yet -- only a Mis Favoritos state is recoverable (Phase 1A
    performs no aggressive state recovery beyond that one calibrated
    click)."""
    try:
        go_session.find_authenticated_go_window(pids, all_windows, require_favoritos_signals=True)
    except (go_session.WindowNotFoundError, go_session.GoAmbiguousError) as exc:
        raise ClinicaRpaError(
            ErrorCode.GO_STATE_UNKNOWN, "GO no esta en 'Trazabilidad de Factura' ni en 'Mis Favoritos'."
        ) from exc

    trazabilidad.open_trazabilidad(go_window)

    # Read-only verification retry (never a second click): a single scan
    # right after the click can transiently fail while GO is busy
    # rendering the new screen (observed live, 2026-09-24 -- same COM-busy
    # pattern documented throughout this project). Progressive backoff,
    # bounded by trazabilidad_open_verify_timeout; the click itself is
    # never repeated here.
    from clinica_rpa.automation.waits import _next_backoff_interval

    deadline = time.monotonic() + trazabilidad_open_verify_timeout
    interval = 0.5
    controls: list = []
    active = False
    while True:
        controls = go_session.scan_window_controls(go_window.handle, max_depth, scan_timeout)
        active = bool(controls) and trazabilidad.is_trazabilidad_active(controls)
        if active or time.monotonic() >= deadline:
            break
        time.sleep(interval)
        interval = _next_backoff_interval(interval)

    if not active:
        raise ClinicaRpaError(
            ErrorCode.TRAZABILIDAD_OPEN_FAILED, "El clic en el tile Trazabilidad de Factura no abrio la pantalla esperada."
        )
    return controls


def _write_invoice(go_window, window, invoice: str):
    """INVOICE_WRITE stage: find the field, verify it's empty (precondition,
    not a mutation), write once (ValuePattern), readback-verify. Returns
    the live invoice-input element for the next stage to reuse.

    Bug found and fixed during Phase 1B review (native review finding
    R3-foreground-guard-removed-before-write): the stage-split refactor had
    dropped the foreground check that, in the pre-1B code, ran immediately
    before this exact mutating write -- restored here, right before
    ``write_invoice_once``, matching every other mutating call site's
    discipline in this codebase.
    """
    _t = time.monotonic()
    elements = trazabilidad.find_invoice_input_elements(window)
    visible = trazabilidad.filter_visible_enabled(elements)
    logger.info("PERF INVOICE_WRITE_FIND_CONTROL {:.0f}ms", (time.monotonic() - _t) * 1000.0)
    if len(visible) != 1:
        raise ClinicaRpaError(
            ErrorCode.INVOICE_INPUT_NOT_FOUND, f"{len(visible)} candidatos para el campo de factura (se requiere exactamente 1)."
        )
    element = visible[0]

    current = trazabilidad.read_current_value(element)
    if current.strip():
        # Precondition failure, not a mutation attempt -- Regla 14: never
        # guess/clear a field that already has content.
        raise ClinicaRpaError(ErrorCode.INVOICE_WRITE_FAILED, "El campo de factura ya contiene un valor; no se sobrescribe a ciegas.")

    _t = time.monotonic()
    try:
        go_session.verify_go_foreground(go_window.handle, expected_pid=go_window.process_id, allow_same_pid=False)
    except go_session.GoNotForegroundError as exc:
        raise ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "GO no esta en primer plano antes de escribir la factura.") from exc
    logger.info("PERF INVOICE_WRITE_FOREGROUND_CHECK {:.0f}ms", (time.monotonic() - _t) * 1000.0)

    _t = time.monotonic()
    try:
        trazabilidad.write_invoice_once(element, invoice)
    except RuntimeError as exc:
        # write_invoice_once's own precondition check (ValuePattern
        # unsupported) -- raised BEFORE any mutation was attempted.
        go_session._classify_and_log(exc, "precondicion de escritura de factura no satisfecha")
        raise ClinicaRpaError(ErrorCode.INVOICE_WRITE_FAILED, "El campo de factura no soporta escritura via ValuePattern.") from exc
    except Exception as exc:
        # The mutating set_text() call itself raised -- ambiguous, never
        # retried (Regla 2).
        go_session._classify_and_log(exc, "fallo ejecutando la escritura autorizada del numero de factura")
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "La escritura del numero de factura fallo de forma ambigua.") from exc
    logger.info("PERF INVOICE_WRITE_SET_VALUE {:.0f}ms", (time.monotonic() - _t) * 1000.0)

    _t = time.monotonic()
    readback = trazabilidad.read_current_value(element)
    logger.info("PERF INVOICE_WRITE_READBACK {:.0f}ms", (time.monotonic() - _t) * 1000.0)
    if readback != invoice:
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "La relectura del campo de factura no coincide con el valor escrito.")

    return element


def _trigger_search(go_window, element) -> None:
    """INVOICE_SEARCH stage: foreground check + the single authorized
    Enter keystroke."""
    try:
        go_session.verify_go_foreground(go_window.handle, expected_pid=go_window.process_id, allow_same_pid=False)
        trazabilidad.send_enter_once(element)
        logger.info("ACTION ENTER SENT")
    except go_session.GoNotForegroundError as exc:
        raise ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "GO no esta en primer plano antes de disparar la busqueda.") from exc
    except Exception as exc:
        go_session._classify_and_log(exc, "fallo enviando el Enter autorizado para disparar la busqueda")
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "El disparo de la busqueda (Enter) fallo de forma ambigua.") from exc


def _wait_invoice_result(go_window, max_depth: int, scan_timeout: float) -> None:
    """WAIT_INVOICE_RESULT stage: read-only progressive poll, never a
    second mutation."""

    def _scan() -> list:
        return go_session.scan_window_controls(go_window.handle, max_depth, scan_timeout)

    active_enough, not_found, _populated_count = trazabilidad.wait_for_search_result(_scan, timeout_seconds=DEFAULT_SEARCH_TIMEOUT_SECONDS)
    if active_enough:
        logger.info("INVOICE RESULT DETECTED")
    if not active_enough:
        if not_found:
            raise ClinicaRpaError(ErrorCode.INVOICE_NOT_FOUND, "GO reporto evidencia explicita de 'no encontrado'.")
        raise ClinicaRpaError(ErrorCode.INVOICE_SEARCH_FAILED, "La busqueda no produjo resultados dentro del tiempo esperado.")


def _open_export_menu_arrow(go_window, viewer_window, viewer_live) -> set:
    """OPEN_EXPORT_MENU stage: find Export Document, resolve ONLY its
    dropdown arrow, click it once. Returns the pre-click popup-root
    snapshot for the SELECT_PDF_FILE stage to use as its "new window"
    baseline."""
    strong, weak = report_viewer.find_export_button_candidates(viewer_live, go_session.LIVE_SEARCH_MAX_DEPTH, go_session.LIVE_SEARCH_TIMEOUT_SECONDS)
    export_candidates = go_session.dedupe_live_elements([c for c in (strong if strong else weak) if go_session.is_visible_enabled(c)])
    if len(export_candidates) != 1:
        raise ClinicaRpaError(ErrorCode.EXPORT_ARROW_NOT_FOUND, f"{len(export_candidates)} candidatos 'Export Document' (se requiere exactamente 1).")

    export_element = export_candidates[0]
    arrow_target = report_viewer.resolve_export_arrow_target(viewer_live, export_element)
    if arrow_target is None:
        raise ClinicaRpaError(ErrorCode.EXPORT_ARROW_NOT_FOUND, "No se pudo resolver la flecha del split button Export Document.")

    before_popup_keys = go_session.snapshot_visible_uia_root_keys(go_window.process_id)
    try:
        go_session.verify_go_foreground(viewer_window.handle, expected_pid=go_window.process_id, allow_same_pid=False)
        report_viewer.open_export_dropdown_once(arrow_target)
    except Exception as exc:
        go_session._classify_and_log(exc, "fallo abriendo la flecha del split button Export Document")
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "La apertura de la flecha Export Document fallo de forma ambigua.") from exc
    return before_popup_keys


def _select_pdf_file(go_window, viewer_window, before_popup_keys: set) -> None:
    """SELECT_PDF_FILE stage: detect the export-format popup's rectangle
    (never the individual menu item -- proven to have no accessible
    identity, see ``report_viewer`` module docstring) and click the
    proportionally-computed point for "PDF File" exactly once."""
    report_viewer._select_pdf_file_once(go_window, viewer_window, before_popup_keys, report_viewer.DEFAULT_EXPORT_MENU_TIMEOUT_SECONDS)


def process_invoice(invoice_number: str, destination_directory: Path) -> InvoiceDownloadResult:
    """Download one invoice's origin document as a PDF into
    ``destination_directory``.

    Never logs or returns the raw invoice number (Regla 6) -- only
    :func:`clinica_rpa.automation.trazabilidad.mask_invoice`'d values ever
    leave this function.
    """
    started_at = time.monotonic()
    raw_invoice = invoice_number.strip()
    masked = trazabilidad.mask_invoice(raw_invoice)

    try:
        trazabilidad.validate_invoice_value(raw_invoice)
    except trazabilidad.InvalidInvoiceValueError as exc:
        # Nothing was attempted against GO yet -- closest closed-vocabulary
        # code is INVOICE_WRITE_FAILED (we will never reach the write step).
        return _error_result(masked, started_at, ClinicaRpaError(ErrorCode.INVOICE_WRITE_FAILED, str(exc)))

    invoice = raw_invoice
    logger.info("process_invoice: iniciando (factura enmascarada={})", masked)
    stage = _StageRecorder()

    try:
        pids = go_session.find_target_process_ids(go_session.TARGET_PROCESS_NAME)
        if not pids:
            raise ClinicaRpaError(ErrorCode.GO_NOT_RUNNING, "GO/Indigo no esta en ejecucion.")

        go_window, window, controls, all_windows = stage.run("GO_DISCOVERY", _go_discovery, pids)

        if trazabilidad.is_trazabilidad_active(controls):
            stage.skip("OPEN_TRAZABILIDAD")
        else:
            controls = stage.run(
                "OPEN_TRAZABILIDAD",
                _open_trazabilidad_if_needed,
                go_window,
                pids,
                all_windows,
                DEFAULT_MAX_DEPTH,
                DEFAULT_SCAN_TIMEOUT_SECONDS,
                DEFAULT_TRAZABILIDAD_OPEN_VERIFY_TIMEOUT_SECONDS,
            )

        element = stage.run("INVOICE_WRITE", _write_invoice, go_window, window, invoice)
        stage.run("INVOICE_SEARCH", _trigger_search, go_window, element)
        stage.run("WAIT_INVOICE_RESULT", _wait_invoice_result, go_window, DEFAULT_MAX_DEPTH, DEFAULT_SCAN_TIMEOUT_SECONDS)

        controls = go_session.scan_window_controls(go_window.handle, DEFAULT_MAX_DEPTH, DEFAULT_SCAN_TIMEOUT_SECONDS)
        viewer_handle, click_executed = stage.run(
            "DOCUMENT_ORIGIN",
            report_viewer.open_documento_origen,
            go_window, window, controls, invoice, pids, DEFAULT_MAX_DEPTH, DEFAULT_SCAN_TIMEOUT_SECONDS,
        )
        # Phase 1B.2: open_documento_origen already waited for the viewer's
        # hwnd via pure Win32 detection (no separate WAIT_REPORT_VIEWER stage
        # needed anymore). UIA attaches directly to that already-known hwnd
        # only now, because the export-menu controls below need a live
        # element (Win32 finds windows, UIA only ever attaches to one).
        viewer_live = stage.run("WAIT_REPORT_VIEWER", go_session.connect_uia, viewer_handle)
        viewer_window = SimpleNamespace(handle=viewer_handle, process_id=go_window.process_id)

        if click_executed:
            try:
                report_viewer.save_viewer_receipt(invoice, go_window.process_id, viewer_handle)
            except Exception as exc:
                # Best-effort: a failed receipt write must not fail an
                # otherwise-successful download.
                go_session._classify_and_log(exc, "fallo (no fatal) guardando el recibo de sesion del Visor de Reportes")

        # Captured BEFORE OPEN_EXPORT_MENU/SELECT_PDF_FILE -- the SELECT_PDF_FILE
        # click is what opens "Opciones de Exportacion PDF"; if this snapshot
        # were taken after that click (as a prior revision of this stage split
        # briefly did -- caught by review finding R3-pdf-options-baseline-race),
        # a fast-appearing dialog's handle would already be in the "before" set
        # and wait_for_pdf_options_dialog's require_new=True check would never
        # see it as new, causing an intermittent timeout.
        before_pdf_options_handles = go_session.win32_pure_snapshot_hwnds(go_window.process_id)

        before_popup_keys = stage.run("OPEN_EXPORT_MENU", _open_export_menu_arrow, go_window, viewer_window, viewer_live)
        stage.run("SELECT_PDF_FILE", _select_pdf_file, go_window, viewer_window, before_popup_keys)

        pdf_options_window = stage.run(
            "WAIT_PDF_OPTIONS", report_viewer.wait_for_pdf_options_dialog, pids, before_pdf_options_handles, go_window.process_id
        )
        stage.run("ACCEPT_PDF_OPTIONS", report_viewer.click_aceptar_once, pdf_options_window)

        before_save_handles = go_session.win32_pure_snapshot_hwnds(go_window.process_id)
        save_window = stage.run("WAIT_SAVE_DIALOG", dialogs.wait_for_save_dialog, pids, before_save_handles, go_window.process_id)

        destination = dialogs.compute_destination_path(destination_directory, invoice)
        before_final_handles = go_session.win32_pure_snapshot_hwnds(go_window.process_id)
        save_live = stage.run("WRITE_DESTINATION", dialogs.write_destination_path_once, save_window, destination)
        stage.run("SAVE", dialogs.click_guardar_once, save_window, save_live)

        file_result = stage.run(
            "WAIT_FILE",
            waits.wait_for_file_stable,
            destination,
            poll_interval=waits.FILE_WAIT_POLL_INTERVAL_SECONDS,
            timeout_seconds=DEFAULT_FILE_WAIT_TIMEOUT_SECONDS,
        )
        if not file_result.appeared or not file_result.stabilized or file_result.size_bytes <= 0:
            raise ClinicaRpaError(ErrorCode.FILE_NOT_CREATED, "El archivo PDF no aparecio/estabilizo en el sistema de archivos a tiempo.")

        def _final_no():
            final_window = dialogs.wait_for_final_export_dialog(pids, before_final_handles, go_window.process_id)
            dialogs.click_final_no_once(final_window)

        stage.run("FINAL_NO", _final_no)

        pdf_result = stage.run("PDF_VALIDATION", validate_pdf_file, destination)
        if not pdf_result.success:
            raise ClinicaRpaError(ErrorCode.PDF_INVALID, "El PDF descargado no paso la validacion estructural.")

        elapsed = time.monotonic() - started_at
        logger.info("process_invoice: COMPLETED (factura enmascarada={}, elapsed={:.1f}s)", masked, elapsed)
        return InvoiceDownloadResult(
            invoice_number_masked=masked,
            status="COMPLETED",
            pdf_created=True,
            pdf_path=str(destination),
            pdf_size_bytes=pdf_result.size_bytes,
            pdf_pages=pdf_result.pages,
            elapsed_seconds=elapsed,
            error_code=None,
            error_message_safe=None,
            stage_timings=list(stage.records),
        )

    except ClinicaRpaError as exc:
        logger.error("process_invoice: abortado ({}): {}", exc.error_code, exc.message_safe)
        return _error_result(masked, started_at, exc, stage)
    except Exception as exc:
        # Defensive last-resort net: every expected failure mode above is
        # already classified into a ClinicaRpaError. This only guards
        # against a genuinely unexpected error that escaped every
        # try/except above -- mirrors the PoC's own outer main() net.
        go_session._classify_and_log(exc, "fallo inesperado no clasificado en process_invoice")
        fallback = ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "Fallo inesperado no clasificado durante la descarga.")
        return _error_result(masked, started_at, fallback, stage)
