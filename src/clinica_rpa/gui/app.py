"""Bot-San-Francisco -- Tk desktop GUI (Phase 1D, extended Phase 1E).

Pure presentation layer: this module only ever calls
:func:`clinica_rpa.gui.worker.start_invoice_worker` (single invoice) and
:func:`clinica_rpa.gui.batch_worker.start_batch_worker` (Excel batch), which
themselves only ever call
``clinica_rpa.services.invoice_download_service.process_invoice`` (directly,
or via ``clinica_rpa.services.batch_invoice_service.process_batch`` for the
per-NIT-folder batch case). No UIA/Win32/GO automation happens in this file.

Threading discipline (Phase 1D spec, section 7 -- unchanged for Phase 1E's
batch tab): both `process_invoice` and the batch loop run on a background
thread; this module (the Tk main thread) never blocks on them, and neither
worker thread ever touches a Tk widget. Each communicates only through its
own ``queue.Queue``, drained here via ``root.after(...)``.

Phase 1E adds a second notebook tab ("Lote desde Excel") alongside the
original single-invoice tab (Phase 1D, already live-tested against real GO)
-- the single-invoice tab's own widgets/logic are unchanged. Both tabs share
one ``busy`` flag so a batch and a single-invoice run can never overlap.
"""

from __future__ import annotations

import os
import queue
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from loguru import logger

from clinica_rpa.batch.excel_loader import BatchFormatInvalidError, BatchExcelSummary, load_invoice_batch
from clinica_rpa.domain.models import BatchInvoiceItemResult, InvoiceBatchItem
from clinica_rpa.gui import batch_worker, state
from clinica_rpa.gui.worker import start_invoice_worker

_POLL_INTERVAL_MS = 150
_TIMER_INTERVAL_MS = 1000

_WINDOW_TITLE = "Bot-San-Francisco"
_WINDOW_SIZE = "650x600"


def _best_effort_focus_go() -> None:
    """Bring GO's own window to the foreground -- best-effort UX helper
    only, never a safety precondition.

    Operator feedback (live test, 2026-09-25): minimizing our own window
    does not reliably return focus to GO on Windows. This calls
    ``win32gui.SetForegroundWindow`` on GO's window, the same OS-level
    focus switch a human does with Alt+Tab -- it touches no control
    inside GO, sends no click/keystroke to GO, and is read-only discovery
    otherwise (fresh PID/window lookup every time, per Regla 1). The
    engine's own ``verify_go_foreground`` check inside ``process_invoice``
    remains the real, unchanged safety gate -- if this best-effort call
    fails or finds nothing, that check still correctly fails the run
    rather than proceeding blind.
    """
    try:
        import win32gui

        from clinica_rpa.automation import go_session

        pids = go_session.find_target_process_ids()
        if not pids:
            return
        for window in go_session.snapshot_target_windows(pids):
            if window.visible and not window.minimized:
                win32gui.SetForegroundWindow(window.handle)
                return
    except Exception as exc:  # best-effort only, never raised to the caller
        logger.debug("No se pudo enfocar GO automaticamente (tolerado): {!r}", exc)


class BotSanFranciscoApp:
    """Owns the Tk root window and all mutable GUI state."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(_WINDOW_TITLE)
        self.root.geometry(_WINDOW_SIZE)
        self.root.minsize(600, 560)

        # Shared across both tabs -- a single-invoice run and a batch run
        # can never be active at the same time.
        self.busy = False

        # Single-invoice tab state (Phase 1D, unchanged).
        self.destination_dir: Path | None = None
        self.result_queue: "queue.Queue" = queue.Queue()
        self.worker_thread = None
        self.start_time: float | None = None
        self._timer_after_id: str | None = None
        self._poll_after_id: str | None = None
        self.last_pdf_path: str | None = None

        # Batch tab state (Phase 1E).
        self.batch_queue: "queue.Queue" = queue.Queue()
        self.batch_worker_thread = None
        self._batch_poll_after_id: str | None = None
        self.batch_items: list[InvoiceBatchItem] = []
        self.batch_summary: BatchExcelSummary | None = None
        self.batch_excel_path: Path | None = None
        self.batch_root_dir: Path | None = None
        self.batch_total = 0
        self.batch_index = 0
        self.batch_completed_count = 0
        self.batch_error_count = 0
        self.batch_nit_seen: set[str] = set()

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass  # fall back to whatever default ttk theme is available

        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)

        title = ttk.Label(outer, text=_WINDOW_TITLE, font=("Segoe UI", 18, "bold"))
        title.pack(anchor="w")
        subtitle = ttk.Label(outer, text="Automatizacion de descarga de facturas", font=("Segoe UI", 10))
        subtitle.pack(anchor="w", pady=(0, 12))

        ttk.Separator(outer).pack(fill="x", pady=(0, 12))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)

        single_tab = ttk.Frame(notebook, padding=(0, 12, 0, 0))
        notebook.add(single_tab, text="Factura individual")
        self._build_single_invoice_tab(single_tab)

        batch_tab = ttk.Frame(notebook, padding=(0, 12, 0, 0))
        notebook.add(batch_tab, text="Lote desde Excel")
        self._build_batch_tab(batch_tab)

    # ------------------------------------------------------------------
    # Single-invoice tab (Phase 1D, unchanged widgets/logic)
    # ------------------------------------------------------------------

    def _build_single_invoice_tab(self, outer: ttk.Frame) -> None:
        form = ttk.Frame(outer)
        form.pack(fill="x")
        form.columnconfigure(0, weight=1)

        ttk.Label(form, text="Numero de factura").grid(row=0, column=0, sticky="w")
        self.invoice_entry = ttk.Entry(form, font=("Segoe UI", 11))
        self.invoice_entry.grid(row=1, column=0, sticky="ew", pady=(2, 10))

        ttk.Label(form, text="Carpeta de destino").grid(row=2, column=0, sticky="w")
        folder_row = ttk.Frame(form)
        folder_row.grid(row=3, column=0, sticky="ew", pady=(2, 10))
        folder_row.columnconfigure(0, weight=1)
        self.folder_var = tk.StringVar(value="")
        self.folder_entry = ttk.Entry(folder_row, textvariable=self.folder_var, state="readonly")
        self.folder_entry.grid(row=0, column=0, sticky="ew")
        self.select_folder_btn = ttk.Button(folder_row, text="Seleccionar", command=self._select_folder)
        self.select_folder_btn.grid(row=0, column=1, padx=(8, 0))

        self.validation_label = ttk.Label(form, text="", foreground="#c0392b")
        self.validation_label.grid(row=4, column=0, sticky="w")

        self.hint_label = ttk.Label(outer, text=state.BEFORE_START_HINT, font=("Segoe UI", 9, "italic"), foreground="#555555")
        self.hint_label.pack(anchor="w", pady=(4, 10))

        self.process_btn = ttk.Button(outer, text="Procesar factura", command=self._on_process_click)
        self.process_btn.pack(anchor="w")

        ttk.Separator(outer).pack(fill="x", pady=12)

        status_row = ttk.Frame(outer)
        status_row.pack(fill="x")
        ttk.Label(status_row, text="Estado:", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.status_label = ttk.Label(status_row, text="● " + state.STATUS_LABELS[state.GuiState.IDLE], foreground=state.STATUS_COLORS[state.GuiState.IDLE])
        self.status_label.pack(side="left", padx=(6, 0))

        time_row = ttk.Frame(outer)
        time_row.pack(fill="x", pady=(4, 0))
        ttk.Label(time_row, text="Tiempo:", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.time_label = ttk.Label(time_row, text="00:00")
        self.time_label.pack(side="left", padx=(6, 0))

        self.processing_hint_label = ttk.Label(outer, text="", font=("Segoe UI", 9, "italic"), foreground="#b58900")
        self.processing_hint_label.pack(anchor="w", pady=(6, 0))

        ttk.Separator(outer).pack(fill="x", pady=12)

        result_frame = ttk.LabelFrame(outer, text="Resultado", padding=12)
        result_frame.pack(fill="x")
        result_frame.columnconfigure(1, weight=1)

        self._result_rows: dict[str, tk.StringVar] = {}
        for i, key in enumerate(("pdf", "tamano", "paginas", "tiempo_total")):
            label_text = {
                "pdf": "PDF:",
                "tamano": "Tamano:",
                "paginas": "Paginas:",
                "tiempo_total": "Tiempo total:",
            }[key]
            ttk.Label(result_frame, text=label_text).grid(row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value="-")
            self._result_rows[key] = var
            ttk.Label(result_frame, textvariable=var).grid(row=i, column=1, sticky="w", padx=(8, 0), pady=2)

        self.error_label = ttk.Label(result_frame, text="", foreground="#c0392b", wraplength=560, justify="left")
        self.error_label.grid(row=10, column=0, columnspan=2, sticky="w", pady=(8, 0))

        buttons_row = ttk.Frame(outer)
        buttons_row.pack(fill="x", pady=(12, 0))
        self.open_folder_btn = ttk.Button(buttons_row, text="Abrir carpeta", command=self._open_folder, state="disabled")
        self.open_folder_btn.pack(side="left")
        self.open_pdf_btn = ttk.Button(buttons_row, text="Abrir PDF", command=self._open_pdf, state="disabled")
        self.open_pdf_btn.pack(side="left", padx=(8, 0))

    # ------------------------------------------------------------------
    # Folder selection (single-invoice tab)
    # ------------------------------------------------------------------

    def _select_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Seleccionar carpeta de destino")
        if not chosen:
            return
        self.destination_dir = Path(chosen).resolve()
        self.folder_var.set(str(self.destination_dir))

    # ------------------------------------------------------------------
    # Validation + start (single-invoice tab)
    # ------------------------------------------------------------------

    def _validate_inputs(self) -> tuple[str, Path] | None:
        self.validation_label.config(text="")

        invoice = self.invoice_entry.get().strip()
        if not invoice:
            self.validation_label.config(text="Ingrese un numero de factura.")
            return None

        if self.destination_dir is None:
            self.validation_label.config(text="Seleccione una carpeta de destino.")
            return None

        destination = self.destination_dir
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.validation_label.config(text="No se pudo crear/usar la carpeta de destino seleccionada.")
            return None

        return invoice, destination

    def _on_process_click(self) -> None:
        if self.busy:
            return
        validated = self._validate_inputs()
        if validated is None:
            return
        invoice, destination = validated

        self.busy = True
        self.start_time = time.monotonic()
        self.last_pdf_path = None
        self._set_controls_enabled(False)
        self._reset_result_display()
        self._set_status(state.GuiState.PREPARING)
        self.processing_hint_label.config(text=state.PROCESSING_HINT + "\n" + state.WHILE_PROCESSING_HINT)

        logger.info("GUI: iniciando procesamiento (factura enmascarada={})", state.mask_invoice_for_log(invoice))

        self.worker_thread = start_invoice_worker(invoice, destination, self.result_queue)
        self._set_status(state.GuiState.QUERYING_GO)
        self._tick_timer()
        self._poll_queue()

        # UX fix (operator feedback, live test 2026-09-25): the engine
        # requires GO to be the foreground window for every mutating
        # action (never bypassed). Minimize our own window, then
        # best-effort activate GO's window directly -- plain iconify()
        # alone did not reliably return focus to GO.
        self.root.iconify()
        _best_effort_focus_go()

    # ------------------------------------------------------------------
    # Timer (root.after only -- never a sleep on the GUI thread)
    # ------------------------------------------------------------------

    def _tick_timer(self) -> None:
        if self.start_time is not None:
            elapsed = time.monotonic() - self.start_time
            self.time_label.config(text=state.format_elapsed(elapsed))
        if self.busy:
            self._timer_after_id = self.root.after(_TIMER_INTERVAL_MS, self._tick_timer)

    # ------------------------------------------------------------------
    # Queue polling (single-invoice tab -- the only place its worker
    # results reach Tk widgets)
    # ------------------------------------------------------------------

    def _poll_queue(self) -> None:
        try:
            while True:
                _tag, result = self.result_queue.get_nowait()
                self._on_worker_result(result)
        except queue.Empty:
            pass
        if self.busy:
            self._poll_after_id = self.root.after(_POLL_INTERVAL_MS, self._poll_queue)

    def _on_worker_result(self, result) -> None:
        self.busy = False
        if self.start_time is not None:
            self.time_label.config(text=state.format_elapsed(time.monotonic() - self.start_time))
        self.start_time = None
        self.processing_hint_label.config(text="")
        self._set_controls_enabled(True)
        # Complements the auto-minimize on start: bring our own window back
        # once the result is ready, without ever touching GO's window.
        self.root.deiconify()
        self.root.lift()
        self._render_result(result)

    # ------------------------------------------------------------------
    # Result rendering (single-invoice tab)
    # ------------------------------------------------------------------

    def _reset_result_display(self) -> None:
        for var in self._result_rows.values():
            var.set("-")
        self.error_label.config(text="")
        self.open_folder_btn.config(state="disabled")
        self.open_pdf_btn.config(state="disabled")
        self.last_pdf_path = None

    def _render_result(self, result) -> None:
        if result.status == "COMPLETED":
            self._set_status(state.GuiState.COMPLETED)
            pdf_name = Path(result.pdf_path).name if result.pdf_path else "-"
            self._result_rows["pdf"].set(pdf_name)
            self._result_rows["tamano"].set(state.format_size(result.pdf_size_bytes))
            self._result_rows["paginas"].set(state.format_pages(result.pdf_pages))
            self._result_rows["tiempo_total"].set(f"{result.elapsed_seconds:.1f} s")
            self.error_label.config(text="")

            if self.destination_dir is not None:
                self.open_folder_btn.config(state="normal")

            if result.pdf_created and result.pdf_path and Path(result.pdf_path).exists():
                self.last_pdf_path = result.pdf_path
                self.open_pdf_btn.config(state="normal")

            logger.info("GUI: procesamiento COMPLETED (elapsed={:.1f}s)", result.elapsed_seconds)
        else:
            self._set_status(state.GuiState.ERROR)
            label = state.error_label_for(result.error_code)
            code = result.error_code or "DESCONOCIDO"
            detail = result.error_message_safe or ""
            self.error_label.config(text=f"{label} ({code})" + (f"\n{detail}" if detail else ""))
            self._result_rows["tiempo_total"].set(f"{result.elapsed_seconds:.1f} s")

            if self.destination_dir is not None:
                self.open_folder_btn.config(state="normal")

            logger.warning("GUI: procesamiento fallido (error_code={})", result.error_code)

    def _set_status(self, gui_state: str) -> None:
        label = state.STATUS_LABELS.get(gui_state, "Procesando...")
        color = state.STATUS_COLORS.get(gui_state, state.DEFAULT_STATUS_COLOR)
        self.status_label.config(text="● " + label, foreground=color)

    # ------------------------------------------------------------------
    # Controls enable/disable -- shared: toggles BOTH tabs, so a batch and
    # a single-invoice run can never start while the other is busy.
    # ------------------------------------------------------------------

    def _set_controls_enabled(self, enabled: bool) -> None:
        widget_state = "normal" if enabled else "disabled"
        self.invoice_entry.config(state=widget_state)
        self.select_folder_btn.config(state=widget_state)
        self.process_btn.config(state=widget_state)

        self.batch_select_excel_btn.config(state=widget_state)
        self.batch_select_folder_btn.config(state=widget_state)
        if enabled:
            self._update_batch_process_enabled()
        else:
            self.batch_process_btn.config(state="disabled")

    # ------------------------------------------------------------------
    # Open folder / PDF (single-invoice tab) -- native Windows mechanism
    # only, never a shell command built from user input.
    # ------------------------------------------------------------------

    def _open_folder(self) -> None:
        if self.destination_dir is None:
            return
        try:
            os.startfile(str(self.destination_dir))  # noqa: S606 - Windows-native, no shell involved
        except OSError:
            messagebox.showerror(_WINDOW_TITLE, "No se pudo abrir la carpeta de destino.")

    def _open_pdf(self) -> None:
        if not self.last_pdf_path or not Path(self.last_pdf_path).exists():
            return
        try:
            os.startfile(self.last_pdf_path)  # noqa: S606 - Windows-native, no shell involved
        except OSError:
            messagebox.showerror(_WINDOW_TITLE, "No se pudo abrir el PDF.")

    # ==================================================================
    # Batch tab (Phase 1E)
    # ==================================================================

    def _build_batch_tab(self, outer: ttk.Frame) -> None:
        file_frame = ttk.Frame(outer)
        file_frame.pack(fill="x")
        file_frame.columnconfigure(0, weight=1)

        self.batch_select_excel_btn = ttk.Button(file_frame, text="Seleccionar Excel", command=self._on_select_excel)
        self.batch_select_excel_btn.grid(row=0, column=0, sticky="w")

        info_frame = ttk.Frame(outer)
        info_frame.pack(fill="x", pady=(8, 0))
        self.batch_file_var = tk.StringVar(value="-")
        self.batch_records_var = tk.StringVar(value="-")
        self.batch_nit_count_var = tk.StringVar(value="-")
        ttk.Label(info_frame, text="Archivo:").grid(row=0, column=0, sticky="w")
        ttk.Label(info_frame, textvariable=self.batch_file_var).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(info_frame, text="Registros:").grid(row=1, column=0, sticky="w")
        ttk.Label(info_frame, textvariable=self.batch_records_var).grid(row=1, column=1, sticky="w", padx=(8, 0))
        ttk.Label(info_frame, text="NIT diferentes:").grid(row=2, column=0, sticky="w")
        ttk.Label(info_frame, textvariable=self.batch_nit_count_var).grid(row=2, column=1, sticky="w", padx=(8, 0))

        self.batch_validation_label = ttk.Label(outer, text="", foreground="#c0392b", wraplength=560, justify="left")
        self.batch_validation_label.pack(anchor="w", pady=(4, 8))

        ttk.Separator(outer).pack(fill="x", pady=(0, 10))

        root_row_label = ttk.Label(outer, text="Carpeta de destino")
        root_row_label.pack(anchor="w")
        root_row = ttk.Frame(outer)
        root_row.pack(fill="x", pady=(2, 4))
        root_row.columnconfigure(0, weight=1)
        self.batch_root_var = tk.StringVar(value="")
        self.batch_root_entry = ttk.Entry(root_row, textvariable=self.batch_root_var, state="readonly")
        self.batch_root_entry.grid(row=0, column=0, sticky="ew")
        self.batch_select_folder_btn = ttk.Button(root_row, text="Seleccionar", command=self._select_batch_root)
        self.batch_select_folder_btn.grid(row=0, column=1, padx=(8, 0))

        ttk.Label(
            outer,
            text="Las facturas se organizaran automaticamente en subcarpetas por NIT.",
            font=("Segoe UI", 9, "italic"),
            foreground="#555555",
        ).pack(anchor="w")
        ttk.Label(
            outer, text="Organizacion: Destino\\NIT\\Factura.pdf", font=("Segoe UI", 9, "italic"), foreground="#555555"
        ).pack(anchor="w", pady=(0, 10))

        self.batch_process_btn = ttk.Button(outer, text="Procesar lote", command=self._on_batch_process_click, state="disabled")
        self.batch_process_btn.pack(anchor="w")

        ttk.Separator(outer).pack(fill="x", pady=12)

        progress_frame = ttk.Frame(outer)
        progress_frame.pack(fill="x")
        self.batch_current_invoice_var = tk.StringVar(value="-")
        self.batch_current_nit_var = tk.StringVar(value="-")
        self.batch_progress_var = tk.StringVar(value="0 / 0")
        self.batch_status_var = tk.StringVar(value=state.STATUS_LABELS[state.GuiState.IDLE])
        ttk.Label(progress_frame, text="Procesando:").grid(row=0, column=0, sticky="w")
        ttk.Label(progress_frame, textvariable=self.batch_current_invoice_var).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(progress_frame, text="NIT:").grid(row=1, column=0, sticky="w")
        ttk.Label(progress_frame, textvariable=self.batch_current_nit_var).grid(row=1, column=1, sticky="w", padx=(8, 0))
        ttk.Label(progress_frame, text="Factura:").grid(row=2, column=0, sticky="w")
        ttk.Label(progress_frame, textvariable=self.batch_progress_var).grid(row=2, column=1, sticky="w", padx=(8, 0))
        ttk.Label(progress_frame, text="Estado:").grid(row=3, column=0, sticky="w")
        ttk.Label(progress_frame, textvariable=self.batch_status_var).grid(row=3, column=1, sticky="w", padx=(8, 0))

        activity_frame = ttk.LabelFrame(outer, text="Actividad", padding=8)
        activity_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.batch_activity_text = tk.Text(activity_frame, height=6, state="disabled", font=("Consolas", 9), wrap="none")
        self.batch_activity_text.pack(fill="both", expand=True)

        summary_frame = ttk.Frame(outer)
        summary_frame.pack(fill="x", pady=(10, 0))
        self.batch_summary_var = tk.StringVar(value="")
        ttk.Label(summary_frame, textvariable=self.batch_summary_var, wraplength=560, justify="left").pack(anchor="w")
        self.batch_root_label_var = tk.StringVar(value="")
        ttk.Label(summary_frame, textvariable=self.batch_root_label_var, wraplength=560, justify="left").pack(anchor="w")

        self.batch_open_folder_btn = ttk.Button(outer, text="Abrir carpeta", command=self._open_batch_folder, state="disabled")
        self.batch_open_folder_btn.pack(anchor="w", pady=(8, 0))

    # ------------------------------------------------------------------
    # Excel selection + validation
    # ------------------------------------------------------------------

    def _on_select_excel(self) -> None:
        chosen = filedialog.askopenfilename(title="Seleccionar archivo Excel", filetypes=[("Excel", "*.xlsx")])
        if not chosen:
            return
        path = Path(chosen).resolve()
        try:
            items, summary = load_invoice_batch(path)
        except BatchFormatInvalidError as exc:
            self.batch_items = []
            self.batch_summary = None
            self.batch_excel_path = None
            self.batch_file_var.set("-")
            self.batch_records_var.set("-")
            self.batch_nit_count_var.set("-")
            self.batch_validation_label.config(text=exc.message_safe, foreground="#c0392b")
            self._update_batch_process_enabled()
            logger.warning("GUI BATCH: formato de Excel invalido: {}", exc.message_safe)
            return

        self.batch_items = items
        self.batch_summary = summary
        self.batch_excel_path = path
        self.batch_file_var.set(path.name)
        self.batch_records_var.set(str(summary.valid_count))
        self.batch_nit_count_var.set(str(summary.distinct_nit_count))
        if summary.invalid_rows:
            self.batch_validation_label.config(
                text=f"{len(summary.invalid_rows)} fila(s) invalida(s) seran omitidas.", foreground="#b58900"
            )
        else:
            self.batch_validation_label.config(text="", foreground="#c0392b")
        logger.info(
            "GUI BATCH: Excel cargado (registros={}, nit_distintos={}, filas_invalidas={})",
            summary.valid_count, summary.distinct_nit_count, len(summary.invalid_rows),
        )
        self._update_batch_process_enabled()

    def _select_batch_root(self) -> None:
        chosen = filedialog.askdirectory(title="Seleccionar carpeta raiz de destino")
        if not chosen:
            return
        self.batch_root_dir = Path(chosen).resolve()
        self.batch_root_var.set(str(self.batch_root_dir))
        self._update_batch_process_enabled()

    def _update_batch_process_enabled(self) -> None:
        if self.busy:
            self.batch_process_btn.config(state="disabled")
            return
        enabled = bool(self.batch_items) and self.batch_root_dir is not None
        self.batch_process_btn.config(state="normal" if enabled else "disabled")

    # ------------------------------------------------------------------
    # Batch start
    # ------------------------------------------------------------------

    def _reset_batch_progress_display(self) -> None:
        self.batch_activity_text.config(state="normal")
        self.batch_activity_text.delete("1.0", "end")
        self.batch_activity_text.config(state="disabled")
        self.batch_current_invoice_var.set("-")
        self.batch_current_nit_var.set("-")
        self.batch_progress_var.set(f"0 / {self.batch_total}")
        self.batch_summary_var.set("")
        self.batch_root_label_var.set("")
        self.batch_open_folder_btn.config(state="disabled")

    def _on_batch_process_click(self) -> None:
        if self.busy:
            return
        if not self.batch_items or self.batch_root_dir is None:
            return

        try:
            self.batch_root_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.batch_validation_label.config(text="No se pudo crear/usar la carpeta raiz seleccionada.", foreground="#c0392b")
            return

        self.busy = True
        self.batch_total = len(self.batch_items)
        self.batch_index = 0
        self.batch_completed_count = 0
        self.batch_error_count = 0
        self.batch_nit_seen = set()
        self._set_controls_enabled(False)
        self._reset_batch_progress_display()
        self.batch_status_var.set("Preparando...")

        logger.info("GUI BATCH: iniciando lote ({} facturas)", self.batch_total)

        self.batch_worker_thread = batch_worker.start_batch_worker(self.batch_items, self.batch_root_dir, self.batch_queue)
        self.batch_status_var.set("Consultando factura en GO...")
        self._poll_batch_queue()

        # Same UX fix as the single-invoice tab (section 7/16): minimize
        # our window and best-effort activate GO's, never touching GO
        # beyond an OS-level focus switch.
        self.root.iconify()
        _best_effort_focus_go()

    # ------------------------------------------------------------------
    # Batch queue polling
    # ------------------------------------------------------------------

    def _poll_batch_queue(self) -> None:
        try:
            while True:
                tag, payload = self.batch_queue.get_nowait()
                if tag == batch_worker.ITEM_DONE:
                    self._on_batch_item_done(payload)
                elif tag == batch_worker.BATCH_DONE:
                    self._on_batch_finished()
                elif tag == batch_worker.BATCH_ERROR:
                    self._on_batch_worker_error(payload)
        except queue.Empty:
            pass
        if self.busy:
            self._batch_poll_after_id = self.root.after(_POLL_INTERVAL_MS, self._poll_batch_queue)

    def _on_batch_item_done(self, item_result: BatchInvoiceItemResult) -> None:
        self.batch_index += 1
        self.batch_nit_seen.add(item_result.nit)
        if item_result.status == "COMPLETED":
            self.batch_completed_count += 1
            marker = "✓"
            suffix = ""
        else:
            self.batch_error_count += 1
            marker = "✗"
            suffix = f" ({state.error_label_for(item_result.error_code)})"

        self._append_activity_line(f"{marker} {item_result.invoice_number_masked} → {item_result.nit}{suffix}")

        self.batch_progress_var.set(f"{self.batch_index} / {self.batch_total}")
        self.batch_current_invoice_var.set(item_result.invoice_number_masked)
        self.batch_current_nit_var.set(item_result.nit)
        if self.batch_index < self.batch_total:
            self.batch_status_var.set("Consultando factura en GO...")
        else:
            self.batch_status_var.set("Finalizando...")

    def _on_batch_finished(self) -> None:
        self.busy = False
        self._set_controls_enabled(True)
        self.root.deiconify()
        self.root.lift()
        self.batch_status_var.set("Procesamiento finalizado")
        self.batch_summary_var.set(
            f"Facturas: {self.batch_total}   Completadas: {self.batch_completed_count}   "
            f"Errores: {self.batch_error_count}   NIT procesados: {len(self.batch_nit_seen)}"
        )
        if self.batch_root_dir is not None:
            self.batch_root_label_var.set(f"Carpeta raiz: {self.batch_root_dir}")
            self.batch_open_folder_btn.config(state="normal")
        logger.info(
            "GUI BATCH: finalizado (total={}, completadas={}, errores={}, nit_procesados={})",
            self.batch_total, self.batch_completed_count, self.batch_error_count, len(self.batch_nit_seen),
        )

    def _on_batch_worker_error(self, message: str) -> None:
        self.busy = False
        self._set_controls_enabled(True)
        self.root.deiconify()
        self.root.lift()
        self.batch_status_var.set("Error")
        self.batch_validation_label.config(text=message, foreground="#c0392b")
        logger.error("GUI BATCH: error inesperado del worker: {}", message)

    def _append_activity_line(self, line: str) -> None:
        self.batch_activity_text.config(state="normal")
        self.batch_activity_text.insert("end", line + "\n")
        self.batch_activity_text.see("end")
        self.batch_activity_text.config(state="disabled")

    # ------------------------------------------------------------------
    # Open root folder (batch tab)
    # ------------------------------------------------------------------

    def _open_batch_folder(self) -> None:
        if self.batch_root_dir is None:
            return
        try:
            os.startfile(str(self.batch_root_dir))  # noqa: S606 - Windows-native, no shell involved
        except OSError:
            messagebox.showerror(_WINDOW_TITLE, "No se pudo abrir la carpeta raiz.")

    # ------------------------------------------------------------------
    # Close protocol (shared)
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        if self.busy:
            messagebox.showwarning(_WINDOW_TITLE, state.CLOSE_WHILE_BUSY_MESSAGE)
            return
        if self._timer_after_id is not None:
            self.root.after_cancel(self._timer_after_id)
        if self._poll_after_id is not None:
            self.root.after_cancel(self._poll_after_id)
        if self._batch_poll_after_id is not None:
            self.root.after_cancel(self._batch_poll_after_id)
        self.root.destroy()


def run() -> None:
    root = tk.Tk()
    BotSanFranciscoApp(root)
    root.mainloop()


if __name__ == "__main__":
    run()
