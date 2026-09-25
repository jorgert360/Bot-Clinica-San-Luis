"""GO/Indigo session discovery, foreground verification, and shared UIA
element utilities.

Ported (never re-run against a live GO instance by this module itself) from
the audited PoC scripts, preserving every safety pattern documented in
``scripts/poc_download_invoice_document.py`` and its dependencies:

- ``scripts/diagnose_go_windows.py`` (window enumeration, PID resolution,
  error taxonomy).
- ``scripts/poc_move_to_trazabilidad.py`` (authenticated-window discovery,
  foreground verification, calibrated cursor animation).
- ``scripts/inspect_go.py`` (``ControlInfo``/``walk_tree``: bounded,
  read-only UIA tree walking).
- ``scripts/poc_download_invoice_document.py`` (live-element read-only
  helpers and the ``choose_single_action_method``/``activate_once``/
  ``activate_preselected_once`` single-action dispatch pattern).

Regla 1 (never hardcode a PID/handle/absolute coordinate): every PID/handle
here is resolved fresh on every call. The only coordinate-based primitive
(:func:`click_once`) always takes a point computed by the caller from a
freshly read live rectangle -- this module never stores or reuses a
previous point.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from clinica_rpa.domain.errors import ClinicaRpaError, ErrorCode

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

TARGET_PROCESS_NAME = "Vie Cloud Platform.exe"

STRONG_SIGNAL_TEXTS: tuple[str, ...] = (
    "Mis Favoritos",
    "Trazabilidad de Factura",
    "Consulta historias",
    "Vie Finance",
    "Vie Clinical",
)

DEFAULT_CALIBRATION_PATH = Path("runtime") / "state" / "ui_calibration.json"
MIN_PANEL_WIDTH_PX = 500
MIN_PANEL_HEIGHT_PX = 500
PANEL_SIZE_MATCH_TOLERANCE_RATIO = 0.30
DEFAULT_MOVE_STEPS = 20
DEFAULT_MOVE_DURATION_SECONDS = 0.75

# Bounded budgets for the small, targeted live-element walks used across the
# automation package (never a full untargeted tree walk of the whole GO
# window -- always scoped to a specific dialog/panel's own live subtree).
LIVE_SEARCH_MAX_DEPTH = 20
LIVE_SEARCH_TIMEOUT_SECONDS = 15.0


# --------------------------------------------------------------------------
# Exceptions (internal control flow; services map these to ClinicaRpaError)
# --------------------------------------------------------------------------


class WindowNotFoundError(Exception):
    """Raised when a target window cannot be found/resolved at all."""


class GoAmbiguousError(Exception):
    """Raised when more than one authenticated GO window is found."""


class GoNotForegroundError(Exception):
    """Raised when GO does not own the current foreground window."""


class CalibrationMissingError(Exception):
    """Raised when ``runtime/state/ui_calibration.json`` does not exist."""


class CalibrationInvalidError(Exception):
    """Raised when the calibration file is unparsable/out of bounds."""


class PanelNotFoundError(Exception):
    """Raised when no qualifying WindowsForms content panel is found."""


def _classify_and_log(exc: Exception, context: str) -> str:
    """Classify an unexpected exception into a small technical taxonomy.

    This is a purely technical/diagnostic classification (logged via
    loguru), distinct from :class:`clinica_rpa.domain.errors.ErrorCode` --
    callers still decide the domain-level error_code themselves.
    """
    message = str(exc)
    lowered = message.lower()
    if "access is denied" in lowered or "permission" in lowered:
        code = "SECURITY_OR_AUTHORIZATION_BLOCK"
    elif "timed out" in lowered or "timeout" in lowered or isinstance(exc, TimeoutError):
        code = "UIA_TIMEOUT"
    else:
        code = "UNKNOWN_ERROR"
    logger.warning("{}: {} ({})", code, context, message)
    return code


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    """A simple (left, top, right, bottom) screen-coordinate rectangle."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def vertical_center(self) -> int:
        return (self.top + self.bottom) // 2

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


@dataclass(frozen=True)
class ControlInfo:
    """Snapshot of one UI Automation control discovered during a walk."""

    depth: int
    control_type: str
    name: str
    automation_id: str
    class_name: str
    rectangle: str


@dataclass(frozen=True)
class WindowDiagnostic:
    """Read-only snapshot of one top-level desktop window."""

    title: str
    handle: int
    process_id: int | None
    visible: bool
    minimized: bool
    class_name: str
    rectangle: str
    width: int
    height: int


@dataclass(frozen=True)
class CalibrationData:
    """Parsed, validated contents of ``ui_calibration.json``."""

    target: str
    reference_panel_width: int
    reference_panel_height: int
    normalized_x: float
    normalized_y: float


@dataclass(frozen=True)
class PanelCandidate:
    handle: int
    class_name: str
    rect: tuple[int, int, int, int]
    width: int
    height: int

    @property
    def area(self) -> int:
        return self.width * self.height


# --------------------------------------------------------------------------
# Small local helpers
# --------------------------------------------------------------------------


def _safe_str(getter) -> str:
    try:
        value = getter()
        return str(value) if value is not None else ""
    except Exception:
        return "?"


def _normalize_for_match(text: str | None) -> str:
    """Whitespace-normalize and casefold text for tolerant matching."""
    if not text:
        return ""
    return " ".join(text.split()).casefold()


def _matches_any(haystack: str, needles: tuple[str, ...]) -> bool:
    """True if any needle appears in haystack at a word boundary.

    Deliberately word-boundary-aware (never a bare substring check) -- see
    ``scripts/inspect_trazabilidad_form.py``'s docstring for the real-world
    false-positive this guards against ("n factura" matching inside
    "informacion factura").

    Both haystack AND needle are casefolded before comparing (bug found and
    fixed during Phase 1A verification: the original single-sided casefold
    silently never matched any Title-Case needle, e.g.
    ``STRONG_SIGNAL_TEXTS = ("Mis Favoritos", ...)`` -- it only happened to
    work in the PoC because every OTHER needle tuple there was already
    hand-written lowercase; this generalizes the fix at the source).
    """
    normalized = _normalize_for_match(haystack)
    for needle in needles:
        pattern = r"(?<!\w)" + re.escape(_normalize_for_match(needle))
        if re.search(pattern, normalized):
            return True
    return False


# --------------------------------------------------------------------------
# Window enumeration / PID resolution
# --------------------------------------------------------------------------


def enumerate_all_windows() -> list[WindowDiagnostic]:
    """Enumerate all significant top-level windows, read-only.

    Raises:
        WindowNotFoundError: the desktop window list itself could not be
            obtained.
    """
    import win32gui
    from pywinauto import Desktop

    try:
        raw_windows = Desktop(backend="uia").windows()
    except Exception as exc:
        _classify_and_log(exc, "fallo al enumerar ventanas via UIA")
        raise WindowNotFoundError(str(exc)) from exc

    results: list[WindowDiagnostic] = []
    for element in raw_windows:
        try:
            handle = element.handle
            title = element.window_text() or ""
            try:
                rect = element.rectangle()
                width, height = rect.width(), rect.height()
                rectangle = str(rect)
            except Exception:
                width = height = 0
                rectangle = "?"
            if not title.strip() and (width < 50 or height < 50):
                continue
            try:
                visible = bool(element.is_visible())
            except Exception:
                visible = False
            try:
                minimized = bool(win32gui.IsIconic(handle))
            except Exception:
                minimized = False
            try:
                class_name = element.element_info.class_name or "?"
            except Exception:
                class_name = "?"
            pid: int | None = None
            try:
                pid = element.process_id()
            except Exception:
                pid = None

            results.append(
                WindowDiagnostic(
                    title=title,
                    handle=handle,
                    process_id=pid,
                    visible=visible,
                    minimized=minimized,
                    class_name=class_name,
                    rectangle=rectangle,
                    width=width,
                    height=height,
                )
            )
        except Exception as exc:
            _classify_and_log(exc, "ventana omitida durante enumeracion")
            continue

    return results


def find_target_process_ids(process_name: str = TARGET_PROCESS_NAME) -> list[int]:
    """Fresh-resolve every PID currently matching process_name via psutil.

    Never assumes a previously-seen PID is still valid (Regla 1).
    """
    import psutil

    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == process_name.lower():
                pids.append(proc.info["pid"])
        except Exception:
            continue
    return pids


def _snapshot_target_windows(pids: list[int]) -> list[WindowDiagnostic]:
    """Enumerate current top-level windows belonging to pids -- read-only."""
    return [w for w in enumerate_all_windows() if w.process_id in pids]


# Public alias -- used across automation/services modules (the leading
# underscore above only signals "ported 1:1 from the PoC's own private
# helper name", not a real intra-package access restriction).
snapshot_target_windows = _snapshot_target_windows


def _search_content(controls: list[ControlInfo]) -> list[str]:
    haystack_parts = [f"{c.name} {c.automation_id} {c.class_name}" for c in controls]
    haystack = " ".join(haystack_parts)
    return [text for text in STRONG_SIGNAL_TEXTS if _matches_any(haystack, (text,))]


def find_authenticated_go_window(
    pids: list[int],
    all_windows: list[WindowDiagnostic],
    require_favoritos_signals: bool = True,
) -> WindowDiagnostic:
    """Find the single top-level GO window proven to be authenticated content.

    Mirrors ``poc_move_to_trazabilidad.find_authenticated_go_window``: by
    default (``require_favoritos_signals=True``) requires "Mis Favoritos"
    AND ("Vie Finance" OR "Vie Clinical"). Passing False accepts any single
    STRONG_SIGNAL_TEXTS match -- appropriate once GO has already navigated
    away from Favoritos (e.g. to Trazabilidad de Factura).

    Raises:
        WindowNotFoundError: no candidate matches.
        GoAmbiguousError: more than one candidate matches.
    """
    top_level = [w for w in all_windows if w.process_id in pids and w.visible and not w.minimized]

    matches: list[WindowDiagnostic] = []
    for w in top_level:
        try:
            controls = _scan_window_controls(w.handle, max_depth=10, timeout_seconds=10.0)
        except Exception as exc:
            _classify_and_log(exc, f"fallo inspeccionando ventana candidata {w.handle}")
            continue
        matched_strong = _search_content(controls)
        if require_favoritos_signals:
            has_favoritos = "Mis Favoritos" in matched_strong
            has_finance_or_clinical = "Vie Finance" in matched_strong or "Vie Clinical" in matched_strong
            is_match = has_favoritos and has_finance_or_clinical
        else:
            is_match = bool(matched_strong)
        if is_match:
            matches.append(w)

    if not matches:
        raise WindowNotFoundError(
            "Ninguna ventana top-level de GO contiene evidencia de sesion autenticada."
        )
    if len(matches) > 1:
        raise GoAmbiguousError(
            f"{len(matches)} ventanas autenticadas de GO encontradas simultaneamente."
        )
    return matches[0]


# --------------------------------------------------------------------------
# UIA connection + bounded tree walk
# --------------------------------------------------------------------------


def connect_uia(handle: int):
    """Connect a live UIA wrapper to `handle`. Raises on failure."""
    from pywinauto import Desktop

    element = Desktop(backend="uia").window(handle=handle)
    element.wait("exists", timeout=5)
    return element


def walk_tree(
    element,
    max_depth: int = LIVE_SEARCH_MAX_DEPTH,
    deadline: float | None = None,
    depth: int = 0,
) -> list[ControlInfo]:
    """Recursively walk a UI Automation subtree, collecting ControlInfo.

    A single unreachable control is logged and skipped rather than aborting
    the whole walk (mirrors ``inspect_go.walk_tree``).
    """
    if deadline is not None and time.monotonic() > deadline:
        return []

    info = ControlInfo(
        depth=depth,
        control_type=_safe_str(lambda: element.element_info.control_type),
        name=_safe_str(lambda: element.element_info.name),
        automation_id=_safe_str(lambda: element.element_info.automation_id),
        class_name=_safe_str(lambda: element.element_info.class_name),
        rectangle=_safe_str(lambda: element.rectangle()),
    )
    collected = [info]

    if depth >= max_depth:
        return collected

    try:
        children = element.children()
    except Exception:
        return collected

    for child in children:
        if deadline is not None and time.monotonic() > deadline:
            break
        try:
            collected.extend(walk_tree(child, max_depth=max_depth, deadline=deadline, depth=depth + 1))
        except Exception:
            continue

    return collected


def _scan_window_controls(handle: int, max_depth: int, timeout_seconds: float) -> list[ControlInfo]:
    """Bounded, read-only UIA scan. Never raises: degrades to []."""
    try:
        element = connect_uia(handle)
    except Exception as exc:
        _classify_and_log(exc, f"fallo (no fatal) reconectando via UIA a ventana {handle}")
        return []
    deadline = time.monotonic() + timeout_seconds
    try:
        return walk_tree(element, max_depth=max_depth, deadline=deadline)
    except Exception as exc:
        _classify_and_log(exc, f"fallo (no fatal) escaneando UIA de ventana {handle}")
        return []


# Public alias -- see the note next to `snapshot_target_windows` above.
scan_window_controls = _scan_window_controls


# --------------------------------------------------------------------------
# Live-element read-only helpers (shared across automation/* modules)
# --------------------------------------------------------------------------


def rect_from_live_element(element) -> Rect | None:
    try:
        r = element.rectangle()
        return Rect(int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


def live_element_control_type(element) -> str:
    try:
        return (element.element_info.control_type or "").lower()
    except Exception:
        return ""


def live_element_name(element) -> str:
    try:
        return element.element_info.name or ""
    except Exception:
        return ""


def live_element_automation_id(element) -> str:
    try:
        return element.element_info.automation_id or ""
    except Exception:
        return ""


def live_element_class_name(element) -> str:
    try:
        return element.element_info.class_name or ""
    except Exception:
        return ""


def live_element_process_id(element) -> int | None:
    try:
        return int(element.element_info.process_id)
    except Exception:
        return None


def best_effort_tooltip_live(element) -> str:
    try:
        help_text = element.element_info.element.CurrentHelpText  # type: ignore[attr-defined]
        return str(help_text) if help_text else ""
    except Exception:
        return ""


def is_visible_enabled(element) -> bool:
    try:
        return bool(element.is_visible()) and bool(element.is_enabled())
    except Exception:
        return False


def supports_invoke_pattern(element) -> bool:
    try:
        return getattr(element, "iface_invoke", None) is not None
    except Exception:
        return False


def supports_expand_collapse_pattern(element) -> bool:
    try:
        element.iface_expand_collapse.CurrentExpandCollapseState
        return True
    except Exception:
        return False


def supports_value_pattern(element) -> bool:
    try:
        element.iface_value.CurrentValue
        return True
    except Exception:
        return False


def walk_live(element, visit, max_depth: int, deadline: float, depth: int = 0) -> None:
    """Bounded, read-only recursive walk over LIVE pywinauto elements.

    Never invokes/clicks anything -- `visit(element, depth)` is caller-owned
    and must stay read-only too. Every actual mutating action in this
    package happens later, from its own single dedicated call site.
    """
    if deadline is not None and time.monotonic() > deadline:
        return
    visit(element, depth)
    if depth >= max_depth:
        return
    try:
        children = element.children()
    except Exception:
        return
    for child in children:
        if deadline is not None and time.monotonic() > deadline:
            return
        walk_live(child, visit, max_depth, deadline, depth + 1)


def live_element_key(element) -> tuple:
    try:
        runtime_id = tuple(element.element_info.runtime_id or ())
    except Exception:
        runtime_id = ()
    rect = rect_from_live_element(element)
    return (
        runtime_id,
        live_element_control_type(element),
        live_element_automation_id(element),
        rect.as_tuple() if rect else None,
    )


def dedupe_live_elements(elements: list) -> list:
    unique: list = []
    seen: set[tuple] = set()
    for element in elements:
        key = live_element_key(element)
        if key in seen:
            continue
        seen.add(key)
        unique.append(element)
    return unique


def snapshot_visible_uia_root_keys(expected_pid: int) -> set[tuple]:
    from pywinauto import Desktop

    try:
        roots = Desktop(backend="uia").windows()
    except Exception:
        return set()
    return {
        live_element_key(root)
        for root in roots
        if live_element_process_id(root) == expected_pid and is_visible_enabled(root)
    }


# --------------------------------------------------------------------------
# Single-action dispatch primitives (Regla 2: exactly one call site per
# mutating action, no fallback cascade after a failed/ambiguous attempt).
# --------------------------------------------------------------------------


def choose_single_action_method(element) -> str:
    """Choose an action method via read-only inspection, before mutating."""
    return "invoke_pattern" if supports_invoke_pattern(element) else "click_input"


def activate_once(element, method: str) -> None:
    """Perform exactly one preselected activation attempt. Never retried."""
    if method == "invoke_pattern":
        element.invoke()
        return
    if method == "click_input":
        element.click_input()
        return
    raise ValueError(f"Metodo de activacion no soportado: {method}")


def activate_preselected_once(element) -> str:
    """Select one method read-only, then perform exactly one UI action.

    An exception here is ambiguous (the app may already have processed the
    action) -- callers must surface it as UI_ACTION_AMBIGUOUS and never try
    a second mechanism.
    """
    method = choose_single_action_method(element)
    activate_once(element, method)
    return method


def click_once(x: int, y: int) -> None:
    """Perform exactly one left mouseDown+mouseUp at (x, y).

    TEMPORARY_FALLBACK (Regla 5): coordinate-based click, authorized only
    for controls proven to have no UIA/MSAA/Win32 selector (the
    "Trazabilidad de Factura" tile) or a popup menu item proven to have no
    accessible identity (the PDF File export option -- see
    ``automation.report_viewer``). Callers must always compute (x, y) from a
    freshly read live rectangle; this function never stores/reuses a point.
    """
    import win32api
    import win32con

    win32api.SetCursorPos((x, y))
    if tuple(win32api.GetCursorPos()) != (x, y):
        raise RuntimeError("El cursor no quedo en el punto objetivo; clic cancelado")
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


# --------------------------------------------------------------------------
# Foreground verification (never activates/changes focus itself)
# --------------------------------------------------------------------------


def _current_foreground_root() -> tuple[int, int]:
    import win32con
    import win32gui

    fg = win32gui.GetForegroundWindow()
    if not fg:
        return 0, 0
    try:
        root = win32gui.GetAncestor(fg, win32con.GA_ROOT)
    except Exception:
        root = fg
    return fg, (root or fg)


def verify_go_foreground(
    go_handle: int,
    expected_pid: int | None = None,
    allow_same_pid: bool = True,
) -> int:
    """Verify GO owns the current foreground window, WITHOUT changing focus.

    Raises:
        GoNotForegroundError: the foreground window does not belong to GO.

    Returns:
        The raw foreground handle (for logging only).
    """
    fg_handle, fg_root = _current_foreground_root()
    if not fg_handle:
        raise GoNotForegroundError("Windows no reporto ninguna ventana en primer plano")

    import win32process

    try:
        _, go_pid = win32process.GetWindowThreadProcessId(go_handle)
    except Exception as exc:
        raise GoNotForegroundError(f"No se pudo verificar el PID de GO (handle={go_handle})") from exc
    if expected_pid is not None and go_pid != expected_pid:
        raise GoNotForegroundError(
            f"El handle de GO pertenece al PID {go_pid}, no al PID esperado {expected_pid}"
        )

    is_owned = fg_handle == go_handle or fg_root == go_handle
    if not is_owned and expected_pid is not None and allow_same_pid:
        try:
            _, fg_pid = win32process.GetWindowThreadProcessId(fg_handle)
        except Exception:
            fg_pid = None
        if fg_pid == expected_pid:
            is_owned = True

    if not is_owned:
        raise GoNotForegroundError(
            f"La ventana en primer plano (handle={fg_handle}, root={fg_root}) no pertenece a GO (handle={go_handle})"
        )
    return fg_handle


# --------------------------------------------------------------------------
# Calibration-based "Trazabilidad de Factura" tile location (Regla 5's one
# authorized coordinate fallback, reused exactly as calibrated by the PoC).
# --------------------------------------------------------------------------


def load_calibration(path: Path = DEFAULT_CALIBRATION_PATH) -> CalibrationData:
    """Load and validate ``ui_calibration.json``.

    Raises:
        CalibrationMissingError: the file does not exist.
        CalibrationInvalidError: unparsable/out-of-range contents.
    """
    if not path.exists():
        raise CalibrationMissingError(f"No existe el archivo de calibracion: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CalibrationInvalidError(f"No se pudo parsear JSON de calibracion ({path}): {exc}") from exc

    try:
        data = CalibrationData(
            target=str(raw["target"]),
            reference_panel_width=int(raw["reference_panel_width"]),
            reference_panel_height=int(raw["reference_panel_height"]),
            normalized_x=float(raw["normalized_x"]),
            normalized_y=float(raw["normalized_y"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationInvalidError(f"Campos de calibracion faltantes o invalidos en {path}: {exc}") from exc

    if not (0.0 <= data.normalized_x <= 1.0 and 0.0 <= data.normalized_y <= 1.0):
        raise CalibrationInvalidError("normalized_x/normalized_y fuera de rango [0,1]")
    if data.reference_panel_width <= 0 or data.reference_panel_height <= 0:
        raise CalibrationInvalidError("reference_panel_width/height deben ser positivos")
    return data


def resolve_target_point(
    panel_rect: tuple[int, int, int, int], normalized_x: float, normalized_y: float
) -> tuple[int, int]:
    """Compute today's absolute screen point from panel_rect + normalized offsets."""
    left, top, right, bottom = panel_rect
    target_x = left + round(normalized_x * (right - left))
    target_y = top + round(normalized_y * (bottom - top))
    if not (left <= target_x <= right and top <= target_y <= bottom):
        raise CalibrationInvalidError(
            f"Punto calculado ({target_x},{target_y}) cae fuera del panel {panel_rect}"
        )
    return target_x, target_y


def _descendant_handles(handle: int) -> list[int]:
    import win32gui

    handles: list[int] = []

    def _cb(child_handle, _extra):
        handles.append(child_handle)
        return True

    try:
        win32gui.EnumChildWindows(handle, _cb, None)
    except Exception:
        pass
    return handles


def discover_panel_candidates(go_handle: int, min_width: int, min_height: int) -> list[PanelCandidate]:
    """Collect visible+enabled WindowsForms-classed descendants of go_handle
    whose current rectangle is at least min_width x min_height."""
    import win32gui

    candidates: list[PanelCandidate] = []
    for h in _descendant_handles(go_handle):
        try:
            class_name = win32gui.GetClassName(h)
        except Exception:
            continue
        if not class_name.lower().startswith("windowsforms"):
            continue
        try:
            if not (win32gui.IsWindowVisible(h) and win32gui.IsWindowEnabled(h)):
                continue
            left, top, right, bottom = win32gui.GetWindowRect(h)
        except Exception:
            continue
        width, height = right - left, bottom - top
        if width < min_width or height < min_height:
            continue
        candidates.append(
            PanelCandidate(handle=h, class_name=class_name, rect=(left, top, right, bottom), width=width, height=height)
        )
    return candidates


def select_panel(candidates: list[PanelCandidate], calibration: CalibrationData | None) -> PanelCandidate:
    """Deterministically choose one panel among possibly-several near-identical
    stacked candidates (mirrors ``poc_move_to_trazabilidad.select_panel``)."""
    if calibration is not None:

        def size_diff(c: PanelCandidate) -> int:
            return abs(c.width - calibration.reference_panel_width) + abs(c.height - calibration.reference_panel_height)

        best = min(candidates, key=size_diff)
        diff = size_diff(best)
        threshold = PANEL_SIZE_MATCH_TOLERANCE_RATIO * (
            calibration.reference_panel_width + calibration.reference_panel_height
        )
        if diff <= threshold:
            return best

    return max(candidates, key=lambda c: c.area)


def locate_content_panel(
    go_handle: int,
    min_width: int = MIN_PANEL_WIDTH_PX,
    min_height: int = MIN_PANEL_HEIGHT_PX,
    calibration: CalibrationData | None = None,
) -> PanelCandidate:
    candidates = discover_panel_candidates(go_handle, min_width, min_height)
    if not candidates:
        raise PanelNotFoundError(
            f"Ningun panel WindowsForms calificado (>= {min_width}x{min_height}) bajo la ventana GO handle={go_handle}"
        )
    return select_panel(candidates, calibration)


def animate_cursor_to(
    target: tuple[int, int],
    steps: int = DEFAULT_MOVE_STEPS,
    duration_seconds: float = DEFAULT_MOVE_DURATION_SECONDS,
) -> None:
    """Smoothly glide the cursor from its current position to target.

    ONLY win32api.SetCursorPos()/GetCursorPos() -- pure cursor repositioning,
    no click. Used for calibration/diagnostic purposes; the actual
    authorized Trazabilidad-tile click goes straight through
    :func:`click_once`, mirroring ``poc_click_trazabilidad.py``'s own
    already-confirmed-live click (Phase 1A runs unattended -- see
    ``automation.trazabilidad.open_trazabilidad`` for the documented
    rationale).
    """
    import win32api

    start_x, start_y = win32api.GetCursorPos()
    target_x, target_y = target
    steps = max(int(steps), 1)
    delay = max(duration_seconds, 0.0) / steps
    for i in range(1, steps + 1):
        t = i / steps
        x = round(start_x + (target_x - start_x) * t)
        y = round(start_y + (target_y - start_y) * t)
        win32api.SetCursorPos((x, y))
        time.sleep(delay)
    win32api.SetCursorPos((target_x, target_y))


def locate_authenticated_session(require_favoritos_signals: bool) -> WindowDiagnostic:
    """Resolve the authenticated GO window fresh, raising ClinicaRpaError on
    any failure (never a raw automation exception) -- the one entry point
    every ``services``/``automation`` caller should use to get a session.
    """
    pids = find_target_process_ids(TARGET_PROCESS_NAME)
    if not pids:
        raise ClinicaRpaError(ErrorCode.GO_NOT_RUNNING, "GO/Indigo no esta en ejecucion.")

    try:
        all_windows = enumerate_all_windows()
    except WindowNotFoundError as exc:
        _classify_and_log(exc, "fallo enumerando ventanas de escritorio")
        raise ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "No se pudo enumerar las ventanas del escritorio.") from exc

    try:
        go_window = find_authenticated_go_window(pids, all_windows, require_favoritos_signals=require_favoritos_signals)
    except WindowNotFoundError as exc:
        raise ClinicaRpaError(ErrorCode.GO_NOT_AUTHENTICATED, "No se encontro una ventana de GO autenticada.") from exc
    except GoAmbiguousError as exc:
        raise ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "Mas de una ventana de GO autenticada simultanea.") from exc

    try:
        verify_go_foreground(go_window.handle, expected_pid=go_window.process_id)
    except GoNotForegroundError as exc:
        raise ClinicaRpaError(ErrorCode.GO_STATE_UNKNOWN, "GO no esta en primer plano.") from exc

    return go_window
