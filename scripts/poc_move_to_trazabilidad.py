"""F0.3A — Calibración y move-only hacia "Trazabilidad de Factura" en GO/Índigo.

"Trazabilidad de Factura" y "Consulta historias" son tiles CUSTOM_DRAWN_CONTROL
sin objeto UIA/MSAA/Win32 propio (confirmado por barridos previos de
diagnose_go_windows.py e inspect_go_msaa.py): viven dentro de un panel WinForms
genérico grande. Como no se puede apuntar directamente por UIA/MSAA, y el clic
aún no está autorizado, este script:

  1. Localiza dinámicamente la ventana autenticada de GO (nunca un PID/handle
     recordado -- Regla 1, 03_CLAUDE_RULES.md).
  2. Localiza el panel de contenido WinForms grande dentro de esa ventana,
     usando la calibración (si existe) para desambiguar entre varios paneles
     casi idénticos superpuestos (patrón ya observado en vivo en esta app).
  3. Carga runtime/state/ui_calibration.json (coordenadas NORMALIZADAS
     relativas al panel, nunca píxeles absolutos hardcodeados -- Regla 5:
     fallback temporal documentado y autorizado).
  4. Calcula el punto actual = panel_rect + normalizado_x/y.
  5. Verifica que GO ya esté en primer plano SIN activarla (nunca
     SetForegroundWindow/ShowWindow/etc. -- Regla 12).
  6. Mueve el cursor de forma gradual y visible con win32api.SetCursorPos
     ÚNICAMENTE -- nunca clic, nunca mouse down/up, nunca accDoDefaultAction,
     nunca Invoke/Select/Focus, nunca entrada de teclado.

Regla 14: cualquier estado ambiguo o no reconocido aborta limpiamente con una
razón específica -- nunca se adivina, nunca se cae a clic, nunca se mejora
la situación por cuenta propia.

Uso:
    python scripts/poc_move_to_trazabilidad.py
"""

from __future__ import annotations

import argparse
import json
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
    WindowDiagnostic,
    WindowNotFoundError,
    _classify_and_log,
    _descendant_handles,
    enumerate_all_windows,
    find_target_process_ids,
    inspect_candidate,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_CALIBRATION_PATH = Path("runtime") / "state" / "ui_calibration.json"

# Minimum panel dimensions to be considered a "content panel" candidate at
# all. Today's confirmed reference panel is 1279x764; 500x500 is chosen
# comfortably below that so it still tolerates a smaller/resized GO window,
# while remaining large enough to exclude small toolbar/status-bar WinForms
# panels that also happen to start with "WindowsForms10...". Configurable
# via --min-panel-width/--min-panel-height for future recalibration.
MIN_PANEL_WIDTH_PX = 500
MIN_PANEL_HEIGHT_PX = 500

# When calibration is available, a panel candidate is treated as "reasonably
# close" in size to the reference panel when the sum of its absolute
# width/height deltas is within 30% of the reference width+height. This is a
# deliberately generous band (a moved/resized-but-not-radically-different
# window can easily differ by a hundred pixels) while still being tight
# enough to reject a clearly different panel (e.g. a narrow sidebar).
PANEL_SIZE_MATCH_TOLERANCE_RATIO = 0.30

# Move-only cursor animation: a short, bounded, visually-observable glide
# from the current cursor position to the target -- never a synchronization
# primitive, never used to wait on application state (Regla 5/12).
DEFAULT_MOVE_STEPS = 20
DEFAULT_MOVE_DURATION_SECONDS = 0.75


# --------------------------------------------------------------------------
# Error taxonomy (03_CLAUDE_RULES.md, Regla 14): every ambiguous/unrecognized
# state gets its own specific, named abort reason -- never a generic bucket.
# --------------------------------------------------------------------------


class GoAmbiguousError(Exception):
    """Raised when more than one authenticated GO window is found simultaneously."""


class PanelNotFoundError(Exception):
    """Raised when no qualifying WindowsForms content panel can be located under GO."""


class CalibrationMissingError(Exception):
    """Raised when the calibration file does not exist at the expected path."""


class CalibrationInvalidError(Exception):
    """Raised when the calibration file exists but is unparsable, has missing/invalid
    fields, or the resolved target point falls outside the located panel's bounds.
    """


class GoNotForegroundError(Exception):
    """Raised when GO (or a descendant of its root window) is not the current
    foreground window. This script never tries to fix that itself.
    """


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationData:
    """Parsed, validated contents of runtime/state/ui_calibration.json."""

    target: str
    reference_panel_width: int
    reference_panel_height: int
    relative_x_px: int
    relative_y_px: int
    normalized_x: float
    normalized_y: float
    calibrated_at: str
    source: str


@dataclass(frozen=True)
class PanelCandidate:
    """One qualifying WindowsForms panel found under the GO window."""

    handle: int
    class_name: str
    rect: tuple[int, int, int, int]  # (left, top, right, bottom), screen coords
    width: int
    height: int

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass
class ReportState:
    """Accumulates whatever has been resolved so far, for the final report.

    Fields default to "N/D" (no disponible) so an early abort still prints a
    complete, well-formed report block instead of a partial/crashed one.
    """

    go_handle: str = "N/D"
    go_pid: str = "N/D"
    go_foreground: str = "N/D"
    go_rectangle: str = "N/D"

    panel_handle: str = "N/D"
    panel_rectangle: str = "N/D"
    panel_width: str = "N/D"
    panel_height: str = "N/D"

    cal_ref_width: str = "N/D"
    cal_ref_height: str = "N/D"
    cal_rel_x: str = "N/D"
    cal_rel_y: str = "N/D"
    cal_norm_x: str = "N/D"
    cal_norm_y: str = "N/D"

    point_x: str = "N/D"
    point_y: str = "N/D"
    point_inside_panel: str = "N/D"

    resultado: str = "N/D"


# --------------------------------------------------------------------------
# Step 1: locate the authenticated GO window dynamically
# --------------------------------------------------------------------------


def find_authenticated_go_window(
    pids: list[int],
    all_windows: list[WindowDiagnostic],
    require_favoritos_signals: bool = True,
) -> WindowDiagnostic:
    """Find the single top-level GO window whose UIA content proves it is the
    authenticated main screen (never the stale Chromium login window).

    Criteria (visible, not minimized, belongs to one of pids) plus content
    evidence, reusing search_content()'s STRONG_SIGNAL_TEXTS matching via
    inspect_candidate() (diagnose_go_windows.py) rather than reimplementing
    tree-walking/content-matching here.

    By default (require_favoritos_signals=True, preserves F0.3A/F0.3B
    behavior exactly) evidence must be "Mis Favoritos" AND ("Vie Finance" OR
    "Vie Clinical") -- appropriate while GO is showing the favorites screen.
    Callers that run once GO has already navigated to a *different* known
    screen (e.g. F0.3C on "Trazabilidad de Factura", after the favorites
    tiles are no longer in the tree) should pass False: any single
    STRONG_SIGNAL_TEXTS match is then accepted as authenticated-content
    evidence, since STRONG_SIGNAL_TEXTS already includes every known
    authenticated screen's anchor text (Regla 1: still evidence-based, just
    a broader accepted-evidence set for a broader calling context -- never a
    silent fallback to "any window").

    Raises:
        WindowNotFoundError: no candidate matches the authenticated-content
            criteria.
        GoAmbiguousError: more than one candidate matches -- a genuinely
            confusing state (Regla 1/14: never guess which one).
    """
    window_lookup = {w.handle: w for w in all_windows}
    top_level = [w for w in all_windows if w.process_id in pids and w.visible and not w.minimized]
    logger.info(
        "Ventanas top-level visibles/no-minimizadas de '{}' (PIDs={}): {}",
        TARGET_PROCESS_NAME,
        pids,
        [(w.handle, w.title) for w in top_level],
    )

    matches: list[WindowDiagnostic] = []
    for w in top_level:
        try:
            insp = inspect_candidate(w.handle, w.class_name, window_lookup)
        except Exception as exc:
            _classify_and_log(exc, f"fallo inspeccionando ventana top-level candidata {w.handle}")
            continue
        if insp is None:
            continue
        logger.info(
            "Ventana {} (title={!r}): señales fuertes encontradas={}",
            w.handle,
            w.title,
            insp.matched_strong,
        )
        if require_favoritos_signals:
            has_favoritos = "Mis Favoritos" in insp.matched_strong
            has_finance_or_clinical = (
                "Vie Finance" in insp.matched_strong or "Vie Clinical" in insp.matched_strong
            )
            is_match = has_favoritos and has_finance_or_clinical
        else:
            is_match = bool(insp.matched_strong)
        if is_match:
            matches.append(w)

    if not matches:
        criteria_desc = (
            "'Mis Favoritos' + 'Vie Finance'/'Vie Clinical'"
            if require_favoritos_signals
            else "cualquier señal fuerte conocida (STRONG_SIGNAL_TEXTS)"
        )
        raise WindowNotFoundError(
            f"Ninguna ventana top-level de GO contiene evidencia de sesión autenticada ({criteria_desc})."
        )
    if len(matches) > 1:
        raise GoAmbiguousError(
            f"{len(matches)} ventanas autenticadas de GO encontradas simultáneamente: "
            f"{[w.handle for w in matches]}"
        )
    return matches[0]


# --------------------------------------------------------------------------
# Step 2: locate the content panel
# --------------------------------------------------------------------------


def discover_panel_candidates(
    go_handle: int, min_width: int, min_height: int
) -> list[PanelCandidate]:
    """Collect every visible+enabled WindowsForms-classed descendant of
    go_handle whose current rectangle is at least min_width x min_height.

    Reuses _descendant_handles() (diagnose_go_windows.py, itself
    win32gui.EnumChildWindows over the full descendant subtree) rather than
    reimplementing descendant enumeration.
    """
    import win32gui

    candidates: list[PanelCandidate] = []
    for h in _descendant_handles(go_handle):
        try:
            class_name = win32gui.GetClassName(h)
        except Exception as exc:
            logger.debug("No se pudo leer class_name de descendiente {}: {}", h, exc)
            continue
        if not class_name.lower().startswith("windowsforms"):
            continue
        try:
            visible = bool(win32gui.IsWindowVisible(h))
            enabled = bool(win32gui.IsWindowEnabled(h))
        except Exception as exc:
            logger.debug("No se pudo leer visible/enabled de {}: {}", h, exc)
            continue
        if not visible or not enabled:
            continue
        try:
            left, top, right, bottom = win32gui.GetWindowRect(h)
        except Exception as exc:
            logger.debug("No se pudo leer rectangle de {}: {}", h, exc)
            continue
        width, height = right - left, bottom - top
        if width < min_width or height < min_height:
            continue
        candidates.append(
            PanelCandidate(
                handle=h, class_name=class_name, rect=(left, top, right, bottom), width=width, height=height
            )
        )
    return candidates


def select_panel(
    candidates: list[PanelCandidate], calibration: CalibrationData | None
) -> PanelCandidate:
    """Deterministically choose one panel among possibly-several near-identical
    stacked candidates (a documented, observed real pattern in this app).

    1. If calibration exists, prefer the candidate whose current width/height
       are closest to the calibrated reference dimensions, PROVIDED that
       closeness is within PANEL_SIZE_MATCH_TOLERANCE_RATIO of the reference
       width+height (a legitimate similarity heuristic, not a blind guess:
       today's calibration was captured against a panel of that exact size).
    2. Otherwise (no calibration, or no candidate close enough), fall back to
       the largest qualifying candidate by area.

    Every candidate considered is logged (handle, class, rect, area) so the
    choice is transparent and auditable (Regla 1).
    """
    for c in candidates:
        logger.info(
            "Panel candidato: handle={} class={} rect={} {}x{} area={}",
            c.handle,
            c.class_name,
            c.rect,
            c.width,
            c.height,
            c.area,
        )

    if calibration is not None:
        def size_diff(c: PanelCandidate) -> int:
            return abs(c.width - calibration.reference_panel_width) + abs(
                c.height - calibration.reference_panel_height
            )

        best = min(candidates, key=size_diff)
        diff = size_diff(best)
        threshold = PANEL_SIZE_MATCH_TOLERANCE_RATIO * (
            calibration.reference_panel_width + calibration.reference_panel_height
        )
        logger.info(
            "Mejor candidato por similitud a calibración ({}x{}): handle={} diff={} umbral={}",
            calibration.reference_panel_width,
            calibration.reference_panel_height,
            best.handle,
            diff,
            threshold,
        )
        if diff <= threshold:
            logger.info("Panel seleccionado por similitud a calibración: handle={}", best.handle)
            return best
        logger.info(
            "Ningún candidato suficientemente cercano a la calibración (diff={} > umbral={}); "
            "se usa el fallback de mayor área",
            diff,
            threshold,
        )

    largest = max(candidates, key=lambda c: c.area)
    logger.info("Panel seleccionado por área máxima: handle={} area={}", largest.handle, largest.area)
    return largest


def locate_content_panel(
    go_handle: int, min_width: int, min_height: int, calibration: CalibrationData | None
) -> PanelCandidate:
    """discover_panel_candidates() + select_panel(), raising PanelNotFoundError
    if nothing qualifies at all.
    """
    candidates = discover_panel_candidates(go_handle, min_width, min_height)
    if not candidates:
        raise PanelNotFoundError(
            f"Ningún panel WindowsForms calificado (visible+enabled+>= {min_width}x{min_height}) "
            f"encontrado bajo la ventana GO handle={go_handle}"
        )
    return select_panel(candidates, calibration)


# --------------------------------------------------------------------------
# Step 3: load calibration
# --------------------------------------------------------------------------


def load_calibration(path: Path) -> CalibrationData:
    """Load and validate runtime/state/ui_calibration.json.

    Raises:
        CalibrationMissingError: the file does not exist.
        CalibrationInvalidError: the file exists but cannot be parsed, is
            missing expected fields, has fields of the wrong type, or has
            normalized_x/y outside [0, 1].
    """
    if not path.exists():
        raise CalibrationMissingError(f"No existe el archivo de calibración: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CalibrationInvalidError(f"No se pudo parsear JSON de calibración ({path}): {exc}") from exc

    try:
        data = CalibrationData(
            target=str(raw["target"]),
            reference_panel_width=int(raw["reference_panel_width"]),
            reference_panel_height=int(raw["reference_panel_height"]),
            relative_x_px=int(raw["relative_x_px"]),
            relative_y_px=int(raw["relative_y_px"]),
            normalized_x=float(raw["normalized_x"]),
            normalized_y=float(raw["normalized_y"]),
            calibrated_at=str(raw["calibrated_at"]),
            source=str(raw["source"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationInvalidError(
            f"Campos de calibración faltantes o inválidos en {path}: {exc}"
        ) from exc

    if not (0.0 <= data.normalized_x <= 1.0 and 0.0 <= data.normalized_y <= 1.0):
        raise CalibrationInvalidError(
            f"normalized_x/normalized_y fuera de rango [0,1]: "
            f"{data.normalized_x}, {data.normalized_y}"
        )
    if data.reference_panel_width <= 0 or data.reference_panel_height <= 0:
        raise CalibrationInvalidError(
            f"reference_panel_width/height deben ser positivos: "
            f"{data.reference_panel_width}, {data.reference_panel_height}"
        )

    return data


# --------------------------------------------------------------------------
# Step 4: resolve the current target point
# --------------------------------------------------------------------------


def resolve_target_point(
    panel_rect: tuple[int, int, int, int], normalized_x: float, normalized_y: float
) -> tuple[int, int]:
    """Compute today's absolute screen point from panel_rect + normalized offsets.

    panel_rect = (left, top, right, bottom) in screen coordinates.

    Raises:
        CalibrationInvalidError: the computed point falls outside the panel
            bounds. Should always hold by construction for normalized_x/y in
            [0, 1], but validated defensively per the mission's explicit
            instruction -- never clamp/silently correct, abort instead.
    """
    left, top, right, bottom = panel_rect
    target_x = left + round(normalized_x * (right - left))
    target_y = top + round(normalized_y * (bottom - top))

    if not (left <= target_x <= right and top <= target_y <= bottom):
        raise CalibrationInvalidError(
            f"Punto calculado ({target_x},{target_y}) cae fuera de los límites del "
            f"panel {panel_rect}"
        )
    return target_x, target_y


# --------------------------------------------------------------------------
# Step 5: foreground check (protection, before moving anything)
# --------------------------------------------------------------------------


def _current_foreground_root() -> tuple[int, int]:
    """Returns (foreground_handle, foreground_root_handle) -- read-only.

    GA_ROOT walks the owner/parent chain all the way to the root window in a
    single call, which is exactly the "climb GetParent/GetAncestor(GA_ROOT)
    up from the foreground handle" check the mission asks for -- GO's actual
    foreground-most element at any instant could be a specific child HWND,
    not necessarily the exact same HWND enumerated as "the GO window" in
    Step 1, so this compares root-to-root rather than requiring an exact
    handle match.
    """
    import win32con
    import win32gui

    fg = win32gui.GetForegroundWindow()
    if not fg:
        return 0, 0
    try:
        root = win32gui.GetAncestor(fg, win32con.GA_ROOT)
    except Exception as exc:
        logger.debug("No se pudo obtener GA_ROOT de la ventana en primer plano {}: {}", fg, exc)
        root = fg
    return fg, (root or fg)


def verify_go_foreground(
    go_handle: int,
    expected_pid: int | None = None,
    allow_same_pid: bool = True,
) -> int:
    """Verify GO (exact handle), its foreground-most descendant's root, or
    (when expected_pid is given) the same OWNING PROCESS owns the current
    foreground window -- WITHOUT ever changing focus.

    expected_pid is optional and defaults to None, preserving this
    function's original exact-handle-or-GA_ROOT behavior for every existing
    caller that doesn't pass it. Real-world finding (F0.5, live GO
    session): an MDI child like "Visor de Reportes" can legitimately be the
    real foreground window while GetAncestor(fg, GA_ROOT) does NOT chain up
    to GO's own top-level handle (MDI child/MDICLIENT relationships aren't
    always recognized as an "owner" chain by GA_ROOT) -- PID membership is
    unambiguous, additional evidence that the foreground genuinely belongs
    to GO's own process in that case.

    Raises:
        GoNotForegroundError: the foreground window does not belong to GO.

    Returns:
        The raw foreground handle (for reporting only).
    """
    fg_handle, fg_root = _current_foreground_root()
    if not fg_handle:
        raise GoNotForegroundError("Windows no reporto ninguna ventana en primer plano")

    import win32process

    try:
        _, go_pid = win32process.GetWindowThreadProcessId(go_handle)
    except Exception as exc:
        raise GoNotForegroundError(
            f"No se pudo verificar el PID propietario de GO (handle={go_handle})"
        ) from exc
    if expected_pid is not None and go_pid != expected_pid:
        raise GoNotForegroundError(
            f"El handle de GO pertenece al PID {go_pid}, no al PID autenticado esperado {expected_pid}"
        )

    is_owned = fg_handle == go_handle or fg_root == go_handle

    fg_pid: int | None = None
    if not is_owned and expected_pid is not None and allow_same_pid:
        try:
            _, fg_pid = win32process.GetWindowThreadProcessId(fg_handle)
        except Exception as exc:
            logger.debug("No se pudo resolver PID de la ventana en primer plano {}: {}", fg_handle, exc)
        if fg_pid == expected_pid:
            is_owned = True

    logger.info(
        "Verificación de primer plano: foreground_handle={} foreground_root={} "
        "foreground_pid={} go_handle={} expected_pid={} -> pertenece a GO: {}",
        fg_handle,
        fg_root,
        fg_pid,
        go_handle,
        expected_pid,
        is_owned,
    )
    if not is_owned:
        raise GoNotForegroundError(
            f"La ventana en primer plano (handle={fg_handle}, root={fg_root}) no "
            f"pertenece a GO (handle={go_handle})"
        )
    return fg_handle


# --------------------------------------------------------------------------
# Step 6: move-only, slowly
# --------------------------------------------------------------------------


def animate_cursor_to(
    target: tuple[int, int],
    steps: int = DEFAULT_MOVE_STEPS,
    duration_seconds: float = DEFAULT_MOVE_DURATION_SECONDS,
) -> None:
    """Smoothly glide the cursor from its current position to target.

    ONLY win32api.SetCursorPos()/GetCursorPos() are used -- pure cursor
    repositioning, no button state, no mouse_event/SendInput/pyautogui. This
    is an intentional, bounded, visually-observable move (so a human can
    track it), never a general synchronization primitive.
    """
    import win32api

    start_x, start_y = win32api.GetCursorPos()
    target_x, target_y = target
    steps = max(int(steps), 1)
    delay = max(duration_seconds, 0.0) / steps

    logger.info(
        "Animando cursor: ({}, {}) -> ({}, {}) en {} pasos / {}s",
        start_x,
        start_y,
        target_x,
        target_y,
        steps,
        duration_seconds,
    )
    for i in range(1, steps + 1):
        t = i / steps
        x = round(start_x + (target_x - start_x) * t)
        y = round(start_y + (target_y - start_y) * t)
        win32api.SetCursorPos((x, y))
        time.sleep(delay)
    # Guarantee the exact final position regardless of rounding drift.
    win32api.SetCursorPos((target_x, target_y))


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def execute(
    calibration_path: Path = DEFAULT_CALIBRATION_PATH,
    min_panel_width: int = MIN_PANEL_WIDTH_PX,
    min_panel_height: int = MIN_PANEL_HEIGHT_PX,
    move_steps: int = DEFAULT_MOVE_STEPS,
    move_duration: float = DEFAULT_MOVE_DURATION_SECONDS,
) -> tuple[ReportState, int]:
    """Run the full validation trail (Steps 1-6), returning the accumulated
    ReportState and process exit code. Never raises -- every expected
    failure mode is caught here and turned into a clean ABORTED result
    (Regla 14: a clean abort, never a crash).
    """
    state = ReportState()

    # --- Step 1: locate the authenticated GO window ---
    logger.info("PASO 1: localizando ventana autenticada de GO (sin PID/handle recordado)...")
    try:
        pids = find_target_process_ids(TARGET_PROCESS_NAME)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo al resolver PIDs de GO")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    if not pids:
        logger.error("WINDOW_NOT_FOUND: no hay ningún proceso '{}' en ejecución", TARGET_PROCESS_NAME)
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
    state.go_rectangle = go_window.rectangle
    logger.info(
        "GO localizada: handle={} pid={} rect={}", go_window.handle, go_window.process_id, go_window.rectangle
    )

    # --- Load calibration early: Step 3 in the mission brief, but needed
    # here (before Step 2's panel-selection heuristic) since select_panel()
    # prefers the candidate whose size matches the calibrated reference. ---
    logger.info("PASO 2: cargando calibración desde {} (necesaria para la heurística del PASO 3)", calibration_path)
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
        code = _classify_and_log(exc, "fallo inesperado cargando calibración")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    state.cal_ref_width = str(calibration.reference_panel_width)
    state.cal_ref_height = str(calibration.reference_panel_height)
    state.cal_rel_x = str(calibration.relative_x_px)
    state.cal_rel_y = str(calibration.relative_y_px)
    state.cal_norm_x = f"{calibration.normalized_x:.8f}"
    state.cal_norm_y = f"{calibration.normalized_y:.8f}"
    logger.info("Calibración cargada: {}", calibration)

    # --- Step 2 (panel location, using the calibration just loaded) ---
    logger.info("PASO 3: localizando panel de contenido dentro de GO (handle={})", go_window.handle)
    try:
        panel = locate_content_panel(go_window.handle, min_panel_width, min_panel_height, calibration)
    except PanelNotFoundError as exc:
        logger.error("PANEL_NOT_FOUND: {}", exc)
        state.resultado = "ABORTED (PANEL_NOT_FOUND)"
        return state, 1
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado localizando el panel de contenido")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    state.panel_handle = str(panel.handle)
    state.panel_rectangle = str(panel.rect)
    state.panel_width = str(panel.width)
    state.panel_height = str(panel.height)
    logger.info(
        "Panel seleccionado: handle={} rect={} {}x{}", panel.handle, panel.rect, panel.width, panel.height
    )

    # --- Step 4: resolve current target point ---
    logger.info("PASO 4: resolviendo punto actual a partir de coordenadas normalizadas")
    try:
        target_x, target_y = resolve_target_point(panel.rect, calibration.normalized_x, calibration.normalized_y)
    except CalibrationInvalidError as exc:
        logger.error("CALIBRATION_INVALID: {}", exc)
        state.point_inside_panel = "NO"
        state.resultado = "ABORTED (CALIBRATION_INVALID)"
        return state, 1
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado resolviendo el punto objetivo")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    state.point_x = str(target_x)
    state.point_y = str(target_y)
    state.point_inside_panel = "SI"
    logger.info("Punto objetivo resuelto: ({}, {})", target_x, target_y)

    # --- Step 5: foreground check (protection, before moving anything) ---
    logger.info("PASO 5: verificando que GO esté en primer plano (nunca se activa)")
    try:
        fg_handle = verify_go_foreground(go_window.handle, expected_pid=go_window.process_id)
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
    logger.info("GO confirmada en primer plano (foreground_handle={})", fg_handle)

    # --- Step 6: move-only, slowly ---
    logger.info("PASO 6: moviendo el cursor de forma gradual y visible (solo movimiento, sin clic)")
    try:
        animate_cursor_to((target_x, target_y), steps=move_steps, duration_seconds=move_duration)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado moviendo el cursor")
        state.resultado = f"ABORTED ({code})"
        return state, 1

    logger.info("Cursor movido a ({}, {})", target_x, target_y)
    state.resultado = "CURSOR_MOVED"
    return state, 0


def render_report(state: ReportState) -> str:
    """Render the final validation-trail report in the mandated format."""
    lines = [
        "GO:",
        f"- handle: {state.go_handle}",
        f"- PID: {state.go_pid}",
        f"- foreground: {state.go_foreground}",
        f"- rectangle: {state.go_rectangle}",
        "",
        "PANEL:",
        f"- handle: {state.panel_handle}",
        f"- rectangle: {state.panel_rectangle}",
        f"- width: {state.panel_width}",
        f"- height: {state.panel_height}",
        "",
        "CALIBRACION:",
        f"- reference width: {state.cal_ref_width}",
        f"- reference height: {state.cal_ref_height}",
        f"- relative_x: {state.cal_rel_x}",
        f"- relative_y: {state.cal_rel_y}",
        f"- normalized_x: {state.cal_norm_x}",
        f"- normalized_y: {state.cal_norm_y}",
        "",
        "PUNTO ACTUAL:",
        f"- x: {state.point_x}",
        f"- y: {state.point_y}",
        f"- dentro del panel: {state.point_inside_panel}",
        "",
        "RESULTADO:",
        f"- {state.resultado}",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments. Every flag is optional; default invocation is
    argument-free (everything is discovered dynamically + loaded from the
    calibration file).
    """
    parser = argparse.ArgumentParser(
        description=(
            "F0.3A: calibración y move-only hacia 'Trazabilidad de Factura' en GO/Índigo. "
            "Solo mueve el cursor -- nunca hace clic."
        )
    )
    parser.add_argument(
        "--calibration-path",
        type=Path,
        default=DEFAULT_CALIBRATION_PATH,
        help=f"Ruta al archivo de calibración (default: {DEFAULT_CALIBRATION_PATH}).",
    )
    parser.add_argument(
        "--min-panel-width",
        type=int,
        default=MIN_PANEL_WIDTH_PX,
        help=f"Ancho mínimo para considerar un panel candidato (default: {MIN_PANEL_WIDTH_PX}).",
    )
    parser.add_argument(
        "--min-panel-height",
        type=int,
        default=MIN_PANEL_HEIGHT_PX,
        help=f"Alto mínimo para considerar un panel candidato (default: {MIN_PANEL_HEIGHT_PX}).",
    )
    parser.add_argument(
        "--move-steps",
        type=int,
        default=DEFAULT_MOVE_STEPS,
        help=f"Cantidad de pasos discretos de la animación del cursor (default: {DEFAULT_MOVE_STEPS}).",
    )
    parser.add_argument(
        "--move-duration",
        type=float,
        default=DEFAULT_MOVE_DURATION_SECONDS,
        help=(
            "Duración total en segundos de la animación del cursor "
            f"(default: {DEFAULT_MOVE_DURATION_SECONDS})."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the full move-only calibration PoC and print the report."""
    # Windows consoles often default to a legacy codepage (e.g. cp1252) that
    # cannot encode every character a window/control name may contain. Force
    # UTF-8 on stdout/stderr so real content never crashes this script;
    # fall back silently if the stream doesn't support it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    args = parse_args(argv)

    logger.info(
        "F0.3A: iniciando PoC de calibración y move-only (calibration_path={}, "
        "min_panel={}x{}, move_steps={}, move_duration={}s)",
        args.calibration_path,
        args.min_panel_width,
        args.min_panel_height,
        args.move_steps,
        args.move_duration,
    )

    try:
        state, exit_code = execute(
            calibration_path=args.calibration_path,
            min_panel_width=args.min_panel_width,
            min_panel_height=args.min_panel_height,
            move_steps=args.move_steps,
            move_duration=args.move_duration,
        )
    except Exception as exc:
        # Defensive last-resort net: execute() is written to catch and
        # classify every expected failure mode internally and never raise.
        # This only guards against a genuinely unexpected error, so the
        # script still prints a clean ABORTED report instead of an
        # unhandled traceback (Regla 14: never look like a crash).
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
