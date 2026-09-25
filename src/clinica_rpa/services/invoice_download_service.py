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
from clinica_rpa.domain.models import InvoiceDownloadResult
from clinica_rpa.pdf.validation import validate_pdf_file

DEFAULT_MAX_DEPTH = trazabilidad.TRAZABILIDAD_GATE_MAX_DEPTH
DEFAULT_SCAN_TIMEOUT_SECONDS = trazabilidad.TRAZABILIDAD_GATE_TIMEOUT_SECONDS
DEFAULT_SEARCH_TIMEOUT_SECONDS = trazabilidad.DEFAULT_SEARCH_POLL_TIMEOUT_SECONDS
DEFAULT_FILE_WAIT_TIMEOUT_SECONDS = 30.0
# Read-only retry budget for confirming Trazabilidad opened after the single
# calibrated click (see _resolve_go_state) -- never a second click.
DEFAULT_TRAZABILIDAD_OPEN_VERIFY_TIMEOUT_SECONDS = 10.0


def _error_result(invoice_masked: str, started_at: float, error: ClinicaRpaError) -> InvoiceDownloadResult:
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
    )


def _resolve_go_state(
    pids: list[int],
    max_depth: int,
    scan_timeout: float,
    trazabilidad_open_verify_timeout: float = DEFAULT_TRAZABILIDAD_OPEN_VERIFY_TIMEOUT_SECONDS,
) -> tuple[go_session.WindowDiagnostic, object, list]:
    """Locate the authenticated GO window, open Trazabilidad if it is
    currently showing Mis Favoritos, and return (go_window, live_window,
    controls) with the Trazabilidad screen confirmed active.

    Phase 1A performs no aggressive state recovery: any state other than
    "already on Trazabilidad" or "on Mis Favoritos" (recoverable by one
    calibrated click) raises ClinicaRpaError(GO_STATE_UNKNOWN).
    """
    try:
        all_windows = go_session.enumerate_all_windows()
    except go_session.WindowNotFoundError as exc:
        raise ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "No se pudo enumerar las ventanas del escritorio.") from exc

    try:
        go_window = go_session.find_authenticated_go_window(pids, all_windows, require_favoritos_signals=False)
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

    controls = go_session.scan_window_controls(go_window.handle, max_depth, scan_timeout)

    if trazabilidad.is_trazabilidad_active(controls):
        return go_window, window, controls

    # Not on Trazabilidad yet -- only a Mis Favoritos state is recoverable.
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
    return go_window, window, controls


def _write_and_search(go_window, window, invoice: str, max_depth: int, scan_timeout: float) -> None:
    elements = trazabilidad.find_invoice_input_elements(window)
    visible = trazabilidad.filter_visible_enabled(elements)
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

    try:
        go_session.verify_go_foreground(go_window.handle, expected_pid=go_window.process_id, allow_same_pid=False)
    except go_session.GoNotForegroundError as exc:
        raise ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "GO no esta en primer plano antes de escribir la factura.") from exc

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

    readback = trazabilidad.read_current_value(element)
    if readback != invoice:
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "La relectura del campo de factura no coincide con el valor escrito.")

    try:
        go_session.verify_go_foreground(go_window.handle, expected_pid=go_window.process_id, allow_same_pid=False)
        trazabilidad.send_enter_once(element)
    except go_session.GoNotForegroundError as exc:
        raise ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "GO no esta en primer plano antes de disparar la busqueda.") from exc
    except Exception as exc:
        go_session._classify_and_log(exc, "fallo enviando el Enter autorizado para disparar la busqueda")
        raise ClinicaRpaError(ErrorCode.UI_ACTION_AMBIGUOUS, "El disparo de la busqueda (Enter) fallo de forma ambigua.") from exc

    def _scan() -> list:
        return go_session.scan_window_controls(go_window.handle, max_depth, scan_timeout)

    active_enough, not_found, _populated_count = trazabilidad.wait_for_search_result(_scan, timeout_seconds=DEFAULT_SEARCH_TIMEOUT_SECONDS)
    if not active_enough:
        if not_found:
            raise ClinicaRpaError(ErrorCode.INVOICE_NOT_FOUND, "GO reporto evidencia explicita de 'no encontrado'.")
        raise ClinicaRpaError(ErrorCode.INVOICE_SEARCH_FAILED, "La busqueda no produjo resultados dentro del tiempo esperado.")


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

    try:
        pids = go_session.find_target_process_ids(go_session.TARGET_PROCESS_NAME)
        if not pids:
            raise ClinicaRpaError(ErrorCode.GO_NOT_RUNNING, "GO/Indigo no esta en ejecucion.")

        go_window, window, controls = _resolve_go_state(pids, DEFAULT_MAX_DEPTH, DEFAULT_SCAN_TIMEOUT_SECONDS)

        _write_and_search(go_window, window, invoice, DEFAULT_MAX_DEPTH, DEFAULT_SCAN_TIMEOUT_SECONDS)

        controls = go_session.scan_window_controls(go_window.handle, DEFAULT_MAX_DEPTH, DEFAULT_SCAN_TIMEOUT_SECONDS)
        viewer_live, click_executed = report_viewer.open_documento_origen(
            go_window, window, controls, invoice, pids, DEFAULT_MAX_DEPTH, DEFAULT_SCAN_TIMEOUT_SECONDS
        )
        viewer_live = report_viewer.wait_for_report_viewer(window, viewer_live)

        try:
            viewer_handle = int(viewer_live.handle)
        except Exception as exc:
            raise ClinicaRpaError(ErrorCode.REPORT_VIEWER_TIMEOUT, "No se pudo resolver el handle del Visor de Reportes.") from exc
        viewer_window = SimpleNamespace(handle=viewer_handle, process_id=go_window.process_id)

        if click_executed:
            try:
                report_viewer.save_viewer_receipt(invoice, go_window.process_id, viewer_handle)
            except Exception as exc:
                # Best-effort: a failed receipt write must not fail an
                # otherwise-successful download.
                go_session._classify_and_log(exc, "fallo (no fatal) guardando el recibo de sesion del Visor de Reportes")

        before_pdf_options_handles = {w.handle for w in go_session.snapshot_target_windows(pids)}
        report_viewer.open_export_menu(go_window, viewer_window, viewer_live)
        pdf_options_window = report_viewer.wait_for_pdf_options_dialog(pids, before_pdf_options_handles, go_window.process_id)
        report_viewer.click_aceptar_once(pdf_options_window)

        before_save_handles = {w.handle for w in go_session.enumerate_all_windows()}
        save_window = dialogs.wait_for_save_dialog(pids, before_save_handles, go_window.process_id)

        destination = dialogs.compute_destination_path(destination_directory, invoice)
        before_final_handles = {w.handle for w in go_session.snapshot_target_windows(pids)}
        dialogs.write_save_path_once_and_click_guardar(save_window, destination)

        file_result = waits.wait_for_file_stable(destination, timeout_seconds=DEFAULT_FILE_WAIT_TIMEOUT_SECONDS)
        if not file_result.appeared or not file_result.stabilized or file_result.size_bytes <= 0:
            raise ClinicaRpaError(ErrorCode.FILE_NOT_CREATED, "El archivo PDF no aparecio/estabilizo en el sistema de archivos a tiempo.")

        final_window = dialogs.wait_for_final_export_dialog(pids, before_final_handles, go_window.process_id)
        dialogs.click_final_no_once(final_window)

        pdf_result = validate_pdf_file(destination)
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
        )

    except ClinicaRpaError as exc:
        logger.error("process_invoice: abortado ({}): {}", exc.error_code, exc.message_safe)
        return _error_result(masked, started_at, exc)
    except Exception as exc:
        # Defensive last-resort net: every expected failure mode above is
        # already classified into a ClinicaRpaError. This only guards
        # against a genuinely unexpected error that escaped every
        # try/except above -- mirrors the PoC's own outer main() net.
        go_session._classify_and_log(exc, "fallo inesperado no clasificado en process_invoice")
        fallback = ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "Fallo inesperado no clasificado durante la descarga.")
        return _error_result(masked, started_at, fallback)
