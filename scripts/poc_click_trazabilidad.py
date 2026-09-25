"""F0.3B -- Primer clic automatico controlado sobre "Trazabilidad de Factura" en GO/Indigo.

Diagnosticos previos, exhaustivos y de solo lectura (UIA, MSAA/IAccessible,
Win32 hit-testing -- diagnose_go_windows.py / inspect_go.py / inspect_go_msaa.py)
probaron de forma concluyente que el tile "Trazabilidad de Factura" es un
CUSTOM_DRAWN_CONTROL: no tiene objeto UIA/MSAA/Win32 propio y vive dentro de
un panel WinForms generico grande. F0.3A (poc_move_to_trazabilidad.py) ya
valido en vivo -- con confirmacion humana -- que el punto calculado a partir
de la calibracion normalizada cae exactamente sobre ese tile.

El usuario autorizo explicitamente, por unica vez, UN clic izquierdo real en
ese mismo punto, seguido solo de inspeccion de solo lectura para ver que se
abrio, y luego detencion total (hard-stop). Este script:

  1. Reutiliza integramente (import directo, nunca reimplementado) la
     localizacion dinamica de la ventana GO autenticada, la localizacion del
     panel de contenido, la carga/validacion de calibracion, el calculo del
     punto objetivo y la verificacion de primer plano de
     poc_move_to_trazabilidad.py (F0.3A) -- Regla 1, 03_CLAUDE_RULES.md.
  2. Captura un snapshot ANTES (solo lectura): ventanas top-level del/los
     PID(s) de "Vie Cloud Platform.exe", handle raiz de GO, handle/rect del
     panel, y un barrido UIA acotado del arbol de GO (reutilizando
     walk_tree()/ControlInfo de inspect_go.py) buscando una lista fija de
     textos objetivo.
  3. Anima el cursor (reutilizando animate_cursor_to(), ya probado en vivo)
     hasta el punto, espera una pausa fija de estabilizacion visual, y
     ejecuta EXACTAMENTE UN clic izquierdo (mouseDown + mouseUp) via
     win32api.mouse_event -- la unica interaccion mutante de todo el script,
     documentada explicitamente como TEMPORARY_FALLBACK (Regla 5).
  4. Despues del clic: SOLO inspeccion de solo lectura, en polling acotado
     (hasta 10s, cada 500ms), reutilizando los mismos patrones de
     enumerate_all_windows()/walk_tree() para detectar cualquier ventana
     nueva y releer los mismos textos objetivo.
  5. Clasifica el resultado (TRAZABILIDAD_OPENED / CLICK_NO_EFFECT /
     WRONG_SCREEN_OPENED / APPLICATION_ERROR), guarda un reporte de texto y
     una captura de pantalla BMP (solo pywin32 -- no se agrega Pillow como
     dependencia nueva solo para codificar PNG), e imprime/loguea el
     reporte final.

Regla 12 (03_CLAUDE_RULES.md): nunca se cierra/mata ninguna ventana; el clic
es la unica excepcion explicita y autorizada a "nunca interactuar", para
esta accion especifica.

Regla 14: cualquier estado ambiguo o no reconocido ANTES del clic aborta
limpiamente con una razon especifica, reutilizando la taxonomia de errores
ya existente en poc_move_to_trazabilidad.py -- nunca se inventan categorias
nuevas superpuestas.

IMPORTANTE -- estructura de un solo clic: click_once() se llama desde
EXACTAMENTE UN sitio en todo este archivo (ver la funcion execute(),
seccion "PASO 7"). No hay reintentos, no hay bucles, no hay ninguna otra
funcion de interaccion (teclado, accDoDefaultAction, Invoke, Select, Focus,
SetForegroundWindow/ShowWindow) en ningun lugar de este script.

Uso:
    python scripts/poc_click_trazabilidad.py
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    _descendant_handles,
    enumerate_all_windows,
    find_target_process_ids,
)
from inspect_go import ControlInfo, walk_tree  # noqa: E402
from poc_move_to_trazabilidad import (  # noqa: E402
    DEFAULT_CALIBRATION_PATH,
    DEFAULT_MOVE_DURATION_SECONDS,
    DEFAULT_MOVE_STEPS,
    MIN_PANEL_HEIGHT_PX,
    MIN_PANEL_WIDTH_PX,
    CalibrationInvalidError,
    CalibrationMissingError,
    GoAmbiguousError,
    GoNotForegroundError,
    PanelCandidate,
    PanelNotFoundError,
    animate_cursor_to,
    find_authenticated_go_window,
    load_calibration,
    locate_content_panel,
    resolve_target_point,
    verify_go_foreground,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Controlled visual-stabilization pause after the cursor animation finishes
# and before the click, NOT a general synchronization primitive -- a fixed
# value in [0.5s, 0.8s] per the mission spec, so a human observer (and any
# screen-refresh/rendering the target app needs) has a moment of visible
# stillness before the single authorized click is dispatched.
POST_MOVE_STABILIZATION_SECONDS = 0.65

# Read-only post-click polling: bounded to 10s total, one poll every 500ms,
# per the mission spec. Never used to wait on application state beyond
# observing it -- purely an inspection cadence.
DEFAULT_POST_CLICK_TIMEOUT_SECONDS = 10.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.5

# Reused UIA scan bounds for the BEFORE/AFTER tree walks -- same style of
# constant as diagnose_go_windows.py's DIAGNOSTIC_SCAN_MAX_DEPTH /
# DIAGNOSTIC_SCAN_TIMEOUT_SECONDS (kept local here since importing those
# private-ish module constants would couple this script to
# diagnose_go_windows.py's internal DIAGNOSTIC_SCAN_MAX_DEPTH definition,
# which is itself derived from inspect_go.DEFAULT_MAX_DEPTH - 2).
CLICK_SCAN_MAX_DEPTH = 6
CLICK_SCAN_TIMEOUT_SECONDS = 15.0

# Fixed list of target texts to search for before/after the click, per the
# mission spec (superset of diagnose_go_windows.STRONG_SIGNAL_TEXTS, with
# invoice-tracking-specific additions).
TARGET_TEXTS: tuple[str, ...] = (
    "Trazabilidad de Factura",
    "Factura",
    "Numero de factura",
    "Número de factura",
    "Buscar",
    "Consultar",
    "Documento origen",
    "Imprimir",
    "Estado",
    "Fecha",
    "Ingreso",
)

DEFAULT_REPORT_PATH = Path("runtime") / "logs" / "f03b_trazabilidad_open.txt"
DEFAULT_SCREENSHOT_PATH = Path("runtime") / "screenshots" / "f03b_after_click.bmp"

# Consecutive stable polls (same window-handle set AND same matched-text
# set as the immediately preceding poll) treated as sufficient evidence of
# a settled state, so polling can stop before the 10s hard cap once the UI
# has clearly stopped changing. Documented per mission spec: "a couple of
# consecutive consistent polls is reasonable evidence of stability".
STABLE_POLLS_TO_STOP_EARLY = 2

# Result classification vocabulary (Phase 4). Kept as plain string
# constants (not an Enum) to match this project's existing style of
# string-tagged ABORTED/RESULTADO codes elsewhere in scripts/.
RESULT_TRAZABILIDAD_OPENED = "TRAZABILIDAD_OPENED"
RESULT_CLICK_NO_EFFECT = "CLICK_NO_EFFECT"
RESULT_WRONG_SCREEN_OPENED = "WRONG_SCREEN_OPENED"
RESULT_APPLICATION_ERROR = "APPLICATION_ERROR"

# Class-name substrings (case-insensitive) that, together with explicit
# error-looking text, are treated as evidence of a standard Windows/WinForms
# error dialog. Used only as corroborating evidence alongside text content
# -- never on their own -- to avoid over-classifying APPLICATION_ERROR
# (Phase 4 spec: "do not over-interpret").
ERROR_DIALOG_CLASS_HINTS: tuple[str, ...] = ("#32770",)  # #32770 = standard Win32 dialog class
ERROR_TEXT_HINTS: tuple[str, ...] = (
    "error",
    "excepcion",
    "excepción",
    "no se pudo",
    "ha ocurrido un error",
    "unhandled exception",
)


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowSnapshotEntry:
    """One top-level window belonging to the target process(es), for
    before/after comparison."""

    handle: int
    title: str
    class_name: str
    pid: int | None


@dataclass(frozen=True)
class ContentSnapshot:
    """Result of a single UIA+Win32 content scan of one window, at one point
    in time (used for both BEFORE and each AFTER poll)."""

    window_handle: int
    uia_control_count: int
    win32_descendant_count: int
    matched_texts: tuple[str, ...]


@dataclass
class BeforeState:
    """Everything captured read-only before the click."""

    go_handle: int
    go_pid: int
    panel_handle: int
    panel_rect: tuple[int, int, int, int]
    target_x: int
    target_y: int
    windows: list[WindowSnapshotEntry] = field(default_factory=list)
    go_content: ContentSnapshot | None = None


@dataclass
class AfterPoll:
    """One post-click polling observation."""

    elapsed_seconds: float
    windows: list[WindowSnapshotEntry]
    new_handles: set[int]
    relevant_window: WindowSnapshotEntry | None
    content: ContentSnapshot | None


@dataclass
class ReportState:
    """Accumulates everything resolved so far, for the final report. Fields
    default to 'N/D' so an early abort (before the click) still prints a
    complete, well-formed report instead of a partial/crashed one."""

    go_handle: str = "N/D"
    go_pid: str = "N/D"
    panel_info: str = "N/D"
    target_point: str = "N/D"
    go_foreground: str = "N/D"

    click_ejecutado: str = "NO"
    click_cantidad: str = "0"
    click_timestamp: str = "N/D"

    nueva_ventana: str = "N/D"
    post_handle: str = "N/D"
    post_title: str = "N/D"
    post_class: str = "N/D"
    post_uia_controls: str = "N/D"
    post_win32_controls: str = "N/D"

    texto_trazabilidad: str = "N/D"
    texto_factura: str = "N/D"
    texto_buscar_consultar: str = "N/D"
    texto_documento_origen: str = "N/D"
    texto_otros: str = "N/D"

    resultado: str = "N/D"
    riesgos: list[str] = field(default_factory=list)

    screenshot_path: str = "N/D"
    report_path: str = "N/D"


# --------------------------------------------------------------------------
# Window/content snapshot helpers (read-only, reused across BEFORE and every
# AFTER poll)
# --------------------------------------------------------------------------


def snapshot_target_windows(pids: list[int]) -> list[WindowSnapshotEntry]:
    """Enumerate all current top-level windows belonging to pids -- read-only.

    Reuses enumerate_all_windows() (diagnose_go_windows.py) rather than
    reimplementing window enumeration.
    """
    all_windows = enumerate_all_windows()
    return [
        WindowSnapshotEntry(
            handle=w.handle, title=w.title, class_name=w.class_name, pid=w.process_id
        )
        for w in all_windows
        if w.process_id in pids
    ]


def scan_window_content(handle: int) -> ContentSnapshot:
    """Read-only UIA + Win32 content scan of one window handle.

    UIA: walks the tree via walk_tree() (inspect_go.py), bounded by
    CLICK_SCAN_MAX_DEPTH/CLICK_SCAN_TIMEOUT_SECONDS, and searches
    name/automation_id/class_name of every control for TARGET_TEXTS.

    Win32: reuses _descendant_handles() (diagnose_go_windows.py, itself
    win32gui.EnumChildWindows) purely for a best-effort descendant count
    comparison point, independent of the UIA tree.

    Never raises: any failure to connect/walk is logged and treated as an
    empty scan (0 controls, no matches) rather than aborting -- this
    function only runs after the click, where no further interaction of any
    kind is permitted, so a scan failure must degrade gracefully, not crash.
    """
    uia_controls: list[ControlInfo] = []
    try:
        from pywinauto import Desktop

        element = Desktop(backend="uia").window(handle=handle)
        element.wait("exists", timeout=5)
        deadline = time.monotonic() + CLICK_SCAN_TIMEOUT_SECONDS
        uia_controls = walk_tree(element, max_depth=CLICK_SCAN_MAX_DEPTH, deadline=deadline)
    except Exception as exc:
        _classify_and_log(exc, f"fallo (no fatal) escaneando UIA de ventana {handle}")
        uia_controls = []

    win32_count = 0
    try:
        win32_count = len(_descendant_handles(handle))
    except Exception as exc:
        _classify_and_log(exc, f"fallo (no fatal) escaneando descendientes Win32 de ventana {handle}")
        win32_count = 0

    haystacks = [f"{c.name} {c.automation_id} {c.class_name}".lower() for c in uia_controls]
    matched = tuple(t for t in TARGET_TEXTS if any(t.lower() in h for h in haystacks))

    return ContentSnapshot(
        window_handle=handle,
        uia_control_count=len(uia_controls),
        win32_descendant_count=win32_count,
        matched_texts=matched,
    )


def _looks_like_error_dialog(entry: WindowSnapshotEntry, matched_texts: tuple[str, ...]) -> bool:
    """Best-effort, conservative check for a standard error dialog.

    Only true when BOTH the class name matches a known Win32 dialog class
    AND the window title contains explicit error-looking text -- avoids
    flagging every generic dialog (e.g. a normal WinForms child form also
    uses class patterns that can resemble a dialog) as an application
    error. matched_texts is accepted for symmetry/future use but title is
    the primary signal here since error dialogs are typically short-lived
    and may not be fully UIA-walkable before they close.
    """
    class_hit = any(hint in entry.class_name.lower() for hint in ERROR_DIALOG_CLASS_HINTS)
    title_hit = any(hint in entry.title.lower() for hint in ERROR_TEXT_HINTS)
    return class_hit and title_hit


# --------------------------------------------------------------------------
# Phase 2: the one authorized click
# --------------------------------------------------------------------------


def click_once(x: int, y: int) -> datetime:
    """Perform exactly one left mouseDown+mouseUp at (x, y). TEMPORARY_FALLBACK
    (Regla 5, 03_CLAUDE_RULES.md): coordinate-based click, used only because
    this control has no UIA/MSAA/Win32 selector (proven CUSTOM_DRAWN_CONTROL).
    Called exactly once per script invocation -- never retried, never looped.
    """
    import win32api
    import win32con

    timestamp = datetime.now(timezone.utc).astimezone()
    logger.warning(
        "TEMPORARY_FALLBACK (Regla 5, 03_CLAUDE_RULES.md): ejecutando el UNICO clic "
        "izquierdo autorizado por coordenadas en ({}, {}) -- sin selector UIA/MSAA/Win32 "
        "disponible (CUSTOM_DRAWN_CONTROL confirmado). timestamp={}",
        x,
        y,
        timestamp.isoformat(),
    )
    win32api.SetCursorPos((x, y))
    if tuple(win32api.GetCursorPos()) != (x, y):
        raise RuntimeError("El cursor no quedo en el punto objetivo; clic cancelado")
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    logger.info("Clic ejecutado (mouseDown+mouseUp) en ({}, {}) a las {}", x, y, timestamp.isoformat())
    return timestamp


# --------------------------------------------------------------------------
# Phase 5b: BMP screenshot via pywin32 only (no Pillow dependency)
# --------------------------------------------------------------------------


def capture_window_bmp(handle: int, output_path: Path) -> Path:
    """Capture handle's current window rect as a .bmp file, using only pywin32
    (GetWindowDC + CreateDCFromHandle + BitBlt + SaveBitmapFile).

    Deviation from the mission's literal ".png" filename: this project's
    pyproject.toml deliberately does not include an imaging library (e.g.
    Pillow), and PNG encoding is not achievable with pywin32 alone. BMP is
    used instead -- flagged explicitly in the final report/console output
    for the coordinator to sanity-check.

    Captures the GO window's own client rect (not the full desktop): it is
    more useful for this specific before/after comparison (avoids capturing
    unrelated desktop content) and produces a smaller file. Read-only:
    never raises on a capture failure -- returns the intended path but logs
    a warning, so a screenshot problem never blocks the rest of the
    read-only inspection/report.
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
# Phase 3: AFTER-state read-only polling
# --------------------------------------------------------------------------


def poll_after_click(
    pids: list[int],
    go_handle: int,
    before_handles: set[int],
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> list[AfterPoll]:
    """Read-only polling loop, bounded to timeout_seconds total, one poll
    every poll_interval_seconds. Never sends any interaction -- only
    enumerates windows and walks UIA/Win32 trees (Phase 3 of the mission
    spec).

    Stops early once STABLE_POLLS_TO_STOP_EARLY consecutive polls agree on
    both the window-handle set and the matched-text set, but never exceeds
    timeout_seconds regardless.
    """
    start = time.monotonic()
    polls: list[AfterPoll] = []
    stable_streak = 0
    previous_signature: tuple[frozenset[int], tuple[str, ...]] | None = None

    while True:
        elapsed = time.monotonic() - start
        if elapsed > timeout_seconds:
            logger.info("Limite de 10s de polling post-clic alcanzado; deteniendo.")
            break

        windows = snapshot_target_windows(pids)
        current_handles = {w.handle for w in windows}
        new_handles = current_handles - before_handles

        # Pick the "most relevant" window to content-scan this poll: a new
        # top-level window if one appeared (most recently discovered one,
        # i.e. not yet seen in a prior poll's new_handles union), else the
        # original GO window.
        relevant_window: WindowSnapshotEntry | None = None
        if new_handles:
            candidate_handle = sorted(new_handles)[-1]
            relevant_window = next((w for w in windows if w.handle == candidate_handle), None)
        if relevant_window is None:
            relevant_window = next((w for w in windows if w.handle == go_handle), None)

        content: ContentSnapshot | None = None
        if relevant_window is not None:
            content = scan_window_content(relevant_window.handle)

        poll = AfterPoll(
            elapsed_seconds=elapsed,
            windows=windows,
            new_handles=new_handles,
            relevant_window=relevant_window,
            content=content,
        )
        polls.append(poll)
        logger.info(
            "Poll post-clic @{:.2f}s: ventanas={} nuevas={} relevante={} controles_uia={} textos={}",
            elapsed,
            len(windows),
            sorted(new_handles),
            relevant_window.handle if relevant_window else None,
            content.uia_control_count if content else "N/D",
            content.matched_texts if content else (),
        )

        signature = (frozenset(current_handles), content.matched_texts if content else ())
        if previous_signature is not None and signature == previous_signature:
            stable_streak += 1
        else:
            stable_streak = 1
        previous_signature = signature

        if stable_streak >= STABLE_POLLS_TO_STOP_EARLY:
            logger.info(
                "{} polls consecutivos estables (mismas ventanas/textos); se detiene el "
                "polling antes del limite de {}s.",
                stable_streak,
                timeout_seconds,
            )
            break

        time.sleep(poll_interval_seconds)

    return polls


# --------------------------------------------------------------------------
# Phase 4: classification
# --------------------------------------------------------------------------

INVOICE_RELATED_TEXTS = frozenset(
    {"Factura", "Numero de factura", "Número de factura", "Buscar", "Consultar", "Documento origen"}
)


def classify_result(
    before: BeforeState, polls: list[AfterPoll]
) -> tuple[str, list[str]]:
    """Compare BEFORE vs the final AFTER poll and classify exactly one of the
    four Phase 4 outcomes. Returns (result_code, observed_risks).

    Decision logic (documented inline, since this is a judgment call over
    real/messy UI evidence -- kept legible/auditable rather than a black
    box):

      1. APPLICATION_ERROR: ONLY when a genuinely new top-level window
         appeared AND it looks like a standard error dialog (class hint +
         explicit error-looking title text, see _looks_like_error_dialog).
         Real, specific evidence only -- never inferred from "nothing else
         matched".

      2. CLICK_NO_EFFECT: no new top-level window appeared for this
         process, AND the final content scan's matched-text set and UIA
         control count are essentially unchanged from BEFORE (control count
         within a small tolerance, same matched texts) -- i.e. still just
         looking at the Favoritos screen with the tile unopened.

      3. TRAZABILIDAD_OPENED: a new window appeared (or the existing GO
         window's content changed substantially), its content contains at
         least one invoice-tracking-related text (Factura/
         Numero de factura/Buscar/Consultar/Documento origen), AND it does
         NOT still show "Trazabilidad de Factura" as an unopened favorites
         tile alongside nothing else new -- i.e. there is evidence of
         having navigated INTO something, not just still looking at the
         tile.

      4. WRONG_SCREEN_OPENED: a new window/screen DID appear (control count
         or window set changed materially) but its matched content does NOT
         contain any invoice-tracking-related text -- some other module
         opened instead.
    """
    if not polls:
        return RESULT_CLICK_NO_EFFECT, ["No se obtuvo ningun poll post-clic (inesperado)."]

    final = polls[-1]
    risks: list[str] = []

    # --- 1. APPLICATION_ERROR check (evaluated across ALL polls, not just
    # the final one, since an error dialog can appear and be dismissed by
    # the application itself before the last poll) ---
    for poll in polls:
        for w in poll.windows:
            if w.handle in poll.new_handles and _looks_like_error_dialog(w, ()):
                risks.append(
                    f"Ventana nueva con apariencia de dialogo de error detectada: "
                    f"handle={w.handle} title={w.title!r} class={w.class_name!r}"
                )
                return RESULT_APPLICATION_ERROR, risks

    new_window_appeared = bool(final.new_handles)

    before_matched = set(before.go_content.matched_texts) if before.go_content else set()
    final_matched = set(final.content.matched_texts) if final.content else set()
    before_count = before.go_content.uia_control_count if before.go_content else 0
    final_count = final.content.uia_control_count if final.content else 0
    # A small, fixed tolerance for "essentially unchanged" control count --
    # UIA scans of a live app can vary by a handful of controls run to run
    # even with nothing meaningfully changed (timing/rendering jitter).
    count_delta = abs(final_count - before_count)
    count_essentially_unchanged = count_delta <= 5

    content_changed = (final_matched != before_matched) or not count_essentially_unchanged

    if not new_window_appeared and not content_changed:
        return RESULT_CLICK_NO_EFFECT, risks

    invoice_evidence = bool(final_matched & INVOICE_RELATED_TEXTS)

    if invoice_evidence:
        # Guard against "still just looking at the tile": if the ONLY
        # matched text is "Trazabilidad de Factura" itself (the tile label)
        # with no other invoice-related evidence and no new window, treat
        # as no real navigation happened.
        only_tile_label = final_matched == {"Trazabilidad de Factura"} and not new_window_appeared
        if not only_tile_label:
            return RESULT_TRAZABILIDAD_OPENED, risks

    if new_window_appeared or content_changed:
        return RESULT_WRONG_SCREEN_OPENED, risks

    # Defensive fallback (should be unreachable given the branches above):
    # never silently misclassify -- surface it as CLICK_NO_EFFECT with an
    # explicit risk note rather than guessing.
    risks.append(
        "Estado de clasificacion inesperado (ninguna rama coincidio con certeza); "
        "se reporta CLICK_NO_EFFECT de forma conservadora."
    )
    return RESULT_CLICK_NO_EFFECT, risks


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _fmt_matched(before_text: str, before: BeforeState, final: AfterPoll | None) -> str:
    before_has = before_text in (before.go_content.matched_texts if before.go_content else ())
    after_has = before_text in (final.content.matched_texts if final and final.content else ())
    return f"antes={'SI' if before_has else 'NO'} / despues={'SI' if after_has else 'NO'}"


def render_report(state: ReportState) -> str:
    lines = [
        "PRE-CLICK:",
        f"- GO handle: {state.go_handle}",
        f"- PID: {state.go_pid}",
        f"- panel: {state.panel_info}",
        f"- target: {state.target_point}",
        f"- foreground validado: {state.go_foreground}",
        "",
        "CLICK:",
        f"- ejecutado: {state.click_ejecutado}",
        f"- cantidad de clicks: {state.click_cantidad}",
        f"- timestamp: {state.click_timestamp}",
        "",
        "POST-CLICK:",
        f"- nueva ventana/formulario: {state.nueva_ventana}",
        f"- handle: {state.post_handle}",
        f"- title: {state.post_title}",
        f"- class: {state.post_class}",
        f"- controles UIA: {state.post_uia_controls}",
        f"- controles Win32: {state.post_win32_controls}",
        "",
        "TEXTOS ENCONTRADOS:",
        f"- Trazabilidad de Factura: {state.texto_trazabilidad}",
        f"- Factura: {state.texto_factura}",
        f"- Buscar/Consultar: {state.texto_buscar_consultar}",
        f"- Documento origen: {state.texto_documento_origen}",
        f"- otros relevantes: {state.texto_otros}",
        "",
        "RESULTADO:",
        f"- {state.resultado}",
        "",
        "RIESGOS OBSERVADOS:",
    ]
    if state.riesgos:
        lines.extend(f"- {r}" for r in state.riesgos)
    else:
        lines.append("- Ninguno observado.")
    lines += [
        "",
        f"(screenshot: {state.screenshot_path})",
        f"(reporte guardado en: {state.report_path})",
    ]
    return "\n".join(lines)


def save_report(text: str, output_path: Path = DEFAULT_REPORT_PATH) -> Path:
    """Write the rendered report to output_path, creating parent dirs as needed."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return output_path


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def execute(
    calibration_path: Path = DEFAULT_CALIBRATION_PATH,
    min_panel_width: int = MIN_PANEL_WIDTH_PX,
    min_panel_height: int = MIN_PANEL_HEIGHT_PX,
    move_steps: int = DEFAULT_MOVE_STEPS,
    move_duration: float = DEFAULT_MOVE_DURATION_SECONDS,
    post_click_timeout: float = DEFAULT_POST_CLICK_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    report_path: Path = DEFAULT_REPORT_PATH,
    screenshot_path: Path = DEFAULT_SCREENSHOT_PATH,
) -> tuple[ReportState, int]:
    """Run the full validation trail, the single authorized click (if every
    pre-click check passes), and the read-only post-click inspection.

    Never raises -- every expected failure mode before the click is caught
    and turned into a clean ABORTED result (Regla 14). Once execution
    reaches PASO 7 (the click), the click function is called from exactly
    one call site with no retry/loop of any kind, per the mission's
    absolute constraints.
    """
    state = ReportState()

    # === PASOS 1-6: identical validation trail to F0.3A
    # (poc_move_to_trazabilidad.py), reused directly via import -- never
    # reimplemented here. ===

    logger.info("PASO 1: localizando ventana autenticada de GO (sin PID/handle recordado)...")
    try:
        pids = find_target_process_ids(TARGET_PROCESS_NAME)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo al resolver PIDs de GO")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    if not pids:
        logger.error("WINDOW_NOT_FOUND: no hay ningun proceso '{}' en ejecucion", TARGET_PROCESS_NAME)
        state.resultado = "ABORTED (WINDOW_NOT_FOUND)"
        return state, 1

    try:
        all_windows = enumerate_all_windows()
    except WindowNotFoundError as exc:
        logger.error("WINDOW_NOT_FOUND: {}", exc)
        state.resultado = "ABORTED (WINDOW_NOT_FOUND)"
        return state, 1
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado enumerando ventanas")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    try:
        go_window = find_authenticated_go_window(pids, all_windows)
    except WindowNotFoundError as exc:
        logger.error("WINDOW_NOT_FOUND: {}", exc)
        state.resultado = "ABORTED (WINDOW_NOT_FOUND)"
        return state, 1
    except GoAmbiguousError as exc:
        logger.error("GO_AMBIGUOUS: {}", exc)
        state.resultado = "ABORTED (GO_AMBIGUOUS)"
        return state, 1
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado localizando la ventana autenticada de GO")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    state.go_handle = str(go_window.handle)
    state.go_pid = str(go_window.process_id)
    logger.info("GO localizada: handle={} pid={}", go_window.handle, go_window.process_id)

    logger.info("PASO 2: cargando calibracion desde {}", calibration_path)
    try:
        calibration = load_calibration(calibration_path)
    except CalibrationMissingError as exc:
        logger.error("CALIBRATION_MISSING: {}", exc)
        state.resultado = "ABORTED (CALIBRATION_MISSING)"
        return state, 1
    except CalibrationInvalidError as exc:
        logger.error("CALIBRATION_INVALID: {}", exc)
        state.resultado = "ABORTED (CALIBRATION_INVALID)"
        return state, 1
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado cargando calibracion")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    logger.info("PASO 3: localizando panel de contenido dentro de GO (handle={})", go_window.handle)
    try:
        panel: PanelCandidate = locate_content_panel(
            go_window.handle, min_panel_width, min_panel_height, calibration
        )
    except PanelNotFoundError as exc:
        logger.error("PANEL_NOT_FOUND: {}", exc)
        state.resultado = "ABORTED (PANEL_NOT_FOUND)"
        return state, 1
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado localizando el panel de contenido")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    state.panel_info = f"handle={panel.handle} rect={panel.rect} {panel.width}x{panel.height}"
    logger.info("Panel seleccionado: {}", state.panel_info)

    logger.info("PASO 4: resolviendo punto actual a partir de coordenadas normalizadas")
    try:
        target_x, target_y = resolve_target_point(panel.rect, calibration.normalized_x, calibration.normalized_y)
    except CalibrationInvalidError as exc:
        logger.error("CALIBRATION_INVALID: {}", exc)
        state.resultado = "ABORTED (CALIBRATION_INVALID)"
        return state, 1
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado resolviendo el punto objetivo")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    state.target_point = f"({target_x}, {target_y})"
    logger.info("Punto objetivo resuelto: {}", state.target_point)

    logger.info("PASO 5: verificando que GO este en primer plano (nunca se activa)")
    try:
        verify_go_foreground(go_window.handle, expected_pid=go_window.process_id)
    except GoNotForegroundError as exc:
        logger.error("GO_NOT_FOREGROUND: {}", exc)
        state.go_foreground = "NO"
        state.resultado = "ABORTED (GO_NOT_FOREGROUND)"
        return state, 1
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado verificando primer plano")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    state.go_foreground = "SI"
    logger.info("GO confirmada en primer plano.")

    # === PASO 6: BEFORE snapshot (read-only) ===
    logger.info("PASO 6: capturando snapshot ANTES (solo lectura)")
    try:
        before_windows = snapshot_target_windows(pids)
        go_content_before = scan_window_content(go_window.handle)
        before = BeforeState(
            go_handle=go_window.handle,
            go_pid=go_window.process_id,
            panel_handle=panel.handle,
            panel_rect=panel.rect,
            target_x=target_x,
            target_y=target_y,
            windows=before_windows,
            go_content=go_content_before,
        )
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado capturando snapshot ANTES")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    before_handles = {w.handle for w in before.windows}
    logger.info(
        "Snapshot ANTES: {} ventanas top-level, {} controles UIA en GO, textos encontrados={}",
        len(before.windows),
        go_content_before.uia_control_count,
        go_content_before.matched_texts,
    )

    # === PASO 7: mover cursor + UNICO clic autorizado ===
    logger.info("PASO 7: moviendo cursor y ejecutando el UNICO clic autorizado")
    try:
        animate_cursor_to((target_x, target_y), steps=move_steps, duration_seconds=move_duration)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado moviendo el cursor (antes del clic)")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    logger.info(
        "Pausa de estabilizacion visual controlada de {}s (no es una primitiva de "
        "sincronizacion general, ver POST_MOVE_STABILIZATION_SECONDS)",
        POST_MOVE_STABILIZATION_SECONDS,
    )
    time.sleep(POST_MOVE_STABILIZATION_SECONDS)

    try:
        verify_go_foreground(go_window.handle, expected_pid=go_window.process_id)
        # SINGLE AUTHORIZED CLICK -- called exactly once, never retried.
        click_timestamp = click_once(target_x, target_y)
    except Exception as exc:
        # Regla del mandato: si el mecanismo de clic lanza una excepcion,
        # se registra y se DETIENE -- nunca se reintenta el clic en ningun
        # camino de codigo, por ningun motivo.
        code = _classify_and_log(exc, "fallo inesperado ejecutando el clic autorizado")
        state.click_ejecutado = "NO"
        state.resultado = f"ABORTED (CLICK_FAILED: {code})"
        return state, 1

    state.click_ejecutado = "SI"
    state.click_cantidad = "1"
    state.click_timestamp = click_timestamp.isoformat()

    # === A partir de aqui: SOLO inspeccion de solo lectura. Nunca se llama
    # click_once() de nuevo, ni ninguna otra funcion de interaccion, para
    # ningun caso, incluyendo manejo de errores. ===

    logger.info("PASO 8: polling post-clic de solo lectura (hasta {}s)", post_click_timeout)
    try:
        polls = poll_after_click(
            pids=pids,
            go_handle=go_window.handle,
            before_handles=before_handles,
            timeout_seconds=post_click_timeout,
            poll_interval_seconds=poll_interval,
        )
    except Exception as exc:
        # Un fallo del polling de solo lectura NUNCA reintenta el clic; se
        # registra y el reporte final refleja la inspeccion incompleta.
        code = _classify_and_log(exc, "fallo inesperado durante el polling post-clic (solo lectura)")
        polls = []
        state.riesgos.append(f"Polling post-clic incompleto por error: {code}")

    final = polls[-1] if polls else None

    state.nueva_ventana = "SI" if (final and final.new_handles) else "NO"
    if final and final.relevant_window:
        state.post_handle = str(final.relevant_window.handle)
        state.post_title = final.relevant_window.title or "(sin titulo)"
        state.post_class = final.relevant_window.class_name
    if final and final.content:
        state.post_uia_controls = str(final.content.uia_control_count)
        state.post_win32_controls = str(final.content.win32_descendant_count)

    state.texto_trazabilidad = _fmt_matched("Trazabilidad de Factura", before, final)
    state.texto_factura = _fmt_matched("Factura", before, final)
    buscar_before = bool(
        {"Buscar", "Consultar"} & set(before.go_content.matched_texts if before.go_content else ())
    )
    buscar_after = bool({"Buscar", "Consultar"} & set(final.content.matched_texts if final and final.content else ()))
    state.texto_buscar_consultar = f"antes={'SI' if buscar_before else 'NO'} / despues={'SI' if buscar_after else 'NO'}"
    state.texto_documento_origen = _fmt_matched("Documento origen", before, final)

    otros_before = set(before.go_content.matched_texts if before.go_content else ()) - {
        "Trazabilidad de Factura",
        "Factura",
        "Buscar",
        "Consultar",
        "Documento origen",
    }
    otros_after = set(final.content.matched_texts if final and final.content else ()) - {
        "Trazabilidad de Factura",
        "Factura",
        "Buscar",
        "Consultar",
        "Documento origen",
    }
    state.texto_otros = f"antes={sorted(otros_before) or '[]'} / despues={sorted(otros_after) or '[]'}"

    # === PASO 9: clasificacion ===
    logger.info("PASO 9: clasificando el resultado final")
    result_code, risks = classify_result(before, polls)
    state.resultado = result_code
    state.riesgos.extend(risks)
    logger.info("Clasificacion final: {}", result_code)

    # === PASO 10: captura de pantalla (best-effort, solo lectura) ===
    logger.info("PASO 10: capturando pantalla (BMP, solo pywin32)")
    screenshot_target = final.relevant_window.handle if (final and final.relevant_window) else go_window.handle
    try:
        saved_path = capture_window_bmp(screenshot_target, screenshot_path)
        state.screenshot_path = str(saved_path)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado (no fatal) durante captura de pantalla")
        state.screenshot_path = f"N/D ({code})"
        state.riesgos.append(f"No se pudo guardar la captura de pantalla: {code}")

    state.report_path = str(report_path)
    return state, 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments. Default invocation is argument-free."""
    parser = argparse.ArgumentParser(
        description=(
            "F0.3B: mueve el cursor y ejecuta UN UNICO clic autorizado sobre "
            "'Trazabilidad de Factura' en GO/Indigo, luego inspecciona (solo lectura) "
            "que se abrio."
        )
    )
    parser.add_argument(
        "--calibration-path",
        type=Path,
        default=DEFAULT_CALIBRATION_PATH,
        help=f"Ruta al archivo de calibracion (default: {DEFAULT_CALIBRATION_PATH}).",
    )
    parser.add_argument(
        "--min-panel-width",
        type=int,
        default=MIN_PANEL_WIDTH_PX,
        help=f"Ancho minimo para considerar un panel candidato (default: {MIN_PANEL_WIDTH_PX}).",
    )
    parser.add_argument(
        "--min-panel-height",
        type=int,
        default=MIN_PANEL_HEIGHT_PX,
        help=f"Alto minimo para considerar un panel candidato (default: {MIN_PANEL_HEIGHT_PX}).",
    )
    parser.add_argument(
        "--move-steps",
        type=int,
        default=DEFAULT_MOVE_STEPS,
        help=f"Cantidad de pasos discretos de la animacion del cursor (default: {DEFAULT_MOVE_STEPS}).",
    )
    parser.add_argument(
        "--move-duration",
        type=float,
        default=DEFAULT_MOVE_DURATION_SECONDS,
        help=f"Duracion total en segundos de la animacion del cursor (default: {DEFAULT_MOVE_DURATION_SECONDS}).",
    )
    parser.add_argument(
        "--post-click-timeout",
        type=float,
        default=DEFAULT_POST_CLICK_TIMEOUT_SECONDS,
        help=(
            "Limite duro en segundos para el polling de solo lectura post-clic "
            f"(default: {DEFAULT_POST_CLICK_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Intervalo en segundos entre polls post-clic (default: {DEFAULT_POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"Ruta del reporte de texto final (default: {DEFAULT_REPORT_PATH}).",
    )
    parser.add_argument(
        "--screenshot-path",
        type=Path,
        default=DEFAULT_SCREENSHOT_PATH,
        help=f"Ruta de la captura de pantalla BMP (default: {DEFAULT_SCREENSHOT_PATH}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the F0.3B one-shot move+click+inspect PoC."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    args = parse_args(argv)

    logger.warning(
        "F0.3B: iniciando PoC de UN UNICO clic autorizado (TEMPORARY_FALLBACK, Regla 5) "
        "sobre 'Trazabilidad de Factura'. calibration_path={}, post_click_timeout={}s, "
        "poll_interval={}s",
        args.calibration_path,
        args.post_click_timeout,
        args.poll_interval,
    )

    try:
        state, exit_code = execute(
            calibration_path=args.calibration_path,
            min_panel_width=args.min_panel_width,
            min_panel_height=args.min_panel_height,
            move_steps=args.move_steps,
            move_duration=args.move_duration,
            post_click_timeout=args.post_click_timeout,
            poll_interval=args.poll_interval,
            report_path=args.report_path,
            screenshot_path=args.screenshot_path,
        )
    except Exception as exc:
        # Defensive last-resort net: execute() is written to catch and
        # classify every expected failure mode internally. This only
        # guards against a genuinely unexpected error that escaped every
        # try/except above -- and critically, execute() never calls
        # click_once() again on any exception path, so this net cannot
        # trigger a second click either.
        code = _classify_and_log(exc, "fallo inesperado no clasificado en el flujo del PoC")
        state = ReportState()
        state.resultado = f"ABORTED ({code})"
        exit_code = 1

    report = render_report(state)
    save_report(report, args.report_path)
    print(report)
    logger.info("Reporte final:\n{}", report)

    if state.screenshot_path.startswith("N/D"):
        print(
            "\nNOTA (desviacion de la mision): no se genero .png -- pywin32 no puede "
            "codificar PNG sin agregar Pillow como dependencia nueva, y este proyecto "
            "deliberadamente no incluye una libreria de imagenes en pyproject.toml. "
            "Ver runtime/screenshots/f03b_after_click.bmp (o el path configurado)."
        )
    else:
        print(
            f"\nNOTA (desviacion de la mision): la captura se guardo como .bmp, no .png "
            f"({state.screenshot_path}) -- pywin32 no puede codificar PNG sin agregar "
            f"Pillow como dependencia nueva. Marcado para que el coordinador decida si "
            f"agregar Pillow es necesario."
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
