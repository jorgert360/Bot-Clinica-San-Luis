"""Bot-San-Francisco -- Tk desktop GUI (Phase 1D).

Pure presentation layer: this module only ever calls
:func:`clinica_rpa.gui.worker.start_invoice_worker`, which itself only ever
calls ``clinica_rpa.services.invoice_download_service.process_invoice``. No
UIA/Win32/GO automation happens in this file.

Threading discipline (Phase 1D spec, section 7): ``process_invoice`` runs on
a background thread; this module (the Tk main thread) never blocks on it,
and the worker thread never touches a Tk widget. The two communicate only
through a ``queue.Queue``, drained here via ``root.after(...)``.
"""

from __future__ import annotations

import os
import queue
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from loguru import logger

from clinica_rpa.gui import state
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

        self.busy = False
        self.destination_dir: Path | None = None
        self.result_queue: "queue.Queue" = queue.Queue()
        self.worker_thread = None
        self.start_time: float | None = None
        self._timer_after_id: str | None = None
        self._poll_after_id: str | None = None
        self.last_pdf_path: str | None = None

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
    # Folder selection
    # ------------------------------------------------------------------

    def _select_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Seleccionar carpeta de destino")
        if not chosen:
            return
        self.destination_dir = Path(chosen).resolve()
        self.folder_var.set(str(self.destination_dir))

    # ------------------------------------------------------------------
    # Validation + start
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
    # Queue polling (the only place worker results reach Tk widgets)
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
    # Result rendering
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
    # Controls enable/disable
    # ------------------------------------------------------------------

    def _set_controls_enabled(self, enabled: bool) -> None:
        widget_state = "normal" if enabled else "disabled"
        self.invoice_entry.config(state=widget_state)
        self.select_folder_btn.config(state=widget_state)
        self.process_btn.config(state=widget_state)

    # ------------------------------------------------------------------
    # Open folder / PDF -- native Windows mechanism only, never a shell
    # command built from user input.
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

    # ------------------------------------------------------------------
    # Close protocol
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        if self.busy:
            messagebox.showwarning(_WINDOW_TITLE, state.CLOSE_WHILE_BUSY_MESSAGE)
            return
        if self._timer_after_id is not None:
            self.root.after_cancel(self._timer_after_id)
        if self._poll_after_id is not None:
            self.root.after_cancel(self._poll_after_id)
        self.root.destroy()


def run() -> None:
    root = tk.Tk()
    BotSanFranciscoApp(root)
    root.mainloop()


if __name__ == "__main__":
    run()
