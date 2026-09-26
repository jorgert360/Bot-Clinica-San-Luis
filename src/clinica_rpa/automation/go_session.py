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


# --------------------------------------------------------------------------
# Pure Win32 window detection (Phase 1B.2): ZERO pywinauto/UIA/COM.
#
# Root cause found and fixed (Fase 1B.1 diagnostic, 2026-09-25): the ~45-90s
# "waits" for PDF Options/Guardar como/the final Exportar dialog were NEVER
# GO being slow to render -- a live race proved each dialog exists within
# ~1-3 seconds of the click, confirmed by a pure Win32 EnumWindows check,
# while the OLD detector (``enumerate_all_windows`` -> pywinauto
# ``Desktop(backend="uia").windows()``, which reads a UIA property on every
# top-level window on the desktop, including GO's own busy windows) did not
# see the SAME dialog even after 60+ seconds. The delay was entirely inside
# our own UIA enumeration, not GO's rendering. These primitives replace that
# enumeration on the hot polling path for every dialog wait in this package;
# UIA is only ever attached AFTER a Win32 hwnd is already known (via
# :func:`connect_uia`), to interact with that one window's own controls --
# never again to discover *which* window newly appeared.
# --------------------------------------------------------------------------


def win32_pure_find_all_windows(
    pid: int, title_contains_any: tuple[str, ...], class_equals: str | None = None
) -> list[int]:
    """Zero UIA/COM. ``EnumWindows`` + ``GetWindowText``/``GetClassName``
    only. Returns EVERY matching VISIBLE top-level hwnd owned by ``pid``, in
    ``EnumWindows`` (Z-order, not creation-order) order. Never touches
    pywinauto/COM in any way.

    Callers doing "require a genuinely NEW window" filtering (see
    :func:`clinica_rpa.automation.waits.win32_wait_for_window`) MUST use
    this -- not the single-result :func:`win32_pure_find_window` -- because
    Z-order is not creation order: a stale already-open match (e.g. a
    lingering dialog from a prior interrupted run) can be enumerated before
    a genuinely new one, silently starving a before/after ``require_new``
    check that only ever looks at one hwnd (review finding
    R3-win32-first-match-masks-new-window).
    """
    import win32gui
    import win32process

    found: list[int] = []

    def _cb(hwnd, _extra):
        try:
            _, wpid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if wpid != pid:
            return True
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
        except Exception:
            return True
        title_ok = any(t.lower() in title.lower() for t in title_contains_any)
        class_ok = class_equals is None or cls == class_equals
        if title_ok and class_ok:
            found.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return found


def win32_pure_find_window(
    pid: int, title_contains_any: tuple[str, ...], class_equals: str | None = None
) -> int | None:
    """Zero UIA/COM. Returns the first matching VISIBLE top-level hwnd
    owned by ``pid``, or None -- single-match convenience wrapper around
    :func:`win32_pure_find_all_windows` for callers that only ever expect
    at most one match (e.g. an "is it already open?" pre-check) and never
    do before/after ``require_new`` filtering."""
    found = win32_pure_find_all_windows(pid, title_contains_any, class_equals)
    return found[0] if found else None


def win32_pure_snapshot_hwnds(pid: int) -> set[int]:
    """Zero UIA/COM. Every currently-visible top-level hwnd owned by
    ``pid`` -- used as a "before" baseline so a subsequent
    :func:`win32_pure_find_window` poll can require a genuinely NEW window,
    the same discipline the UIA-based waits already used."""
    import win32gui
    import win32process

    found: set[int] = set()

    def _cb(hwnd, _extra):
        try:
            _, wpid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if wpid != pid:
            return True
        try:
            if win32gui.IsWindowVisible(hwnd):
                found.add(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return found


def close_window_once(hwnd: int) -> None:
    """Send exactly one WM_CLOSE to ``hwnd`` -- the OS-level equivalent of
    clicking that window's own close control. Never sends any other
    message, never a second attempt.

    Phase 1E.1 (batch, between-items): user-confirmed live (2026-09-25)
    that GO's "Deshacer" button (see
    :func:`clinica_rpa.automation.trazabilidad.reset_result_screen_once`)
    only clears the loaded invoice once the "Visor de Reportes" window for
    that invoice is closed first. Callers are responsible for having
    already resolved ``hwnd`` to a window they intend to close (e.g. via
    :func:`win32_pure_find_window` scoped to GO's own pid) -- this
    function performs no discovery itself. ``PostMessage`` is
    asynchronous: the window may not be gone yet when this call returns,
    see :func:`wait_until_window_closed` in ``waits.py``.
    """
    import win32con
    import win32gui

    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def _search_content(controls: list[ControlInfo]) -> list[str]:
    haystack_parts = [f"{c.name} {c.automation_id} {c.class_name}" for c in controls]
    haystack = " ".join(haystack_parts)
    return [text for text in STRONG_SIGNAL_TEXTS if _matches_any(haystack, (text,))]


def find_authenticated_go_window(
    pids: list[int],
    all_windows: list[WindowDiagnostic],
    require_favoritos_signals: bool = True,
    scan_max_depth: int = 10,
    scan_timeout_seconds: float = 10.0,
    return_controls: bool = False,
):
    """Find the single top-level GO window proven to be authenticated content.

    Mirrors ``poc_move_to_trazabilidad.find_authenticated_go_window``: by
    default (``require_favoritos_signals=True``) requires "Mis Favoritos"
    AND ("Vie Finance" OR "Vie Clinical"). Passing False accepts any single
    STRONG_SIGNAL_TEXTS match -- appropriate once GO has already navigated
    away from Favoritos (e.g. to Trazabilidad de Factura).

    Phase 1B.1 optimization: this already has to fully scan each candidate
    window to check its content. When ``return_controls=True``, it returns
    ``(window, controls)`` instead of just ``window`` -- the winning
    candidate's own already-scanned controls -- so a caller (like
    ``_go_discovery``) that needs BOTH "which window is GO" AND "its
    control tree" no longer has to perform a second full walk of the same
    window right after this one. Measured live (2026-09-25): this exact
    duplicate scan cost ~0.6-1.4s. ``scan_max_depth``/``scan_timeout_seconds``
    let a caller request the SAME depth/timeout it would have used for its
    own separate scan, so reusing this result never loses coverage compared
    to the two-scan version.

    Raises:
        WindowNotFoundError: no candidate matches.
        GoAmbiguousError: more than one candidate matches.
    """
    top_level = [w for w in all_windows if w.process_id in pids and w.visible and not w.minimized]

    matches: list[WindowDiagnostic] = []
    matched_controls: dict[int, list[ControlInfo]] = {}
    for w in top_level:
        try:
            controls = _scan_window_controls(w.handle, max_depth=scan_max_depth, timeout_seconds=scan_timeout_seconds)
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
            matched_controls[w.handle] = controls

    if not matches:
        raise WindowNotFoundError(
            "Ninguna ventana top-level de GO contiene evidencia de sesion autenticada."
        )
    if len(matches) > 1:
        raise GoAmbiguousError(
            f"{len(matches)} ventanas autenticadas de GO encontradas simultaneamente."
        )
    winner = matches[0]
    if return_controls:
        return winner, matched_controls[winner.handle]
    return winner


# --------------------------------------------------------------------------
# UIA connection + bounded tree walk
# --------------------------------------------------------------------------


def connect_uia(handle: int):
    """Connect a live UIA wrapper to `handle`. Raises on failure."""
    from pywinauto import Desktop

    element = Desktop(backend="uia").window(handle=handle)
    element.wait("exists", timeout=5)
    return element


def uia_find_first_by_automation_id(root_element, automation_id: str):
    """Single cheap targeted UIA search -- NEVER a Python-side tree walk.

    Phase 1B close-out: this installed pywinauto's ``descendants(auto_id=)``
    raises ``TypeError`` (its ``build_condition()`` only accepts
    process/class_name/title/control_type -- confirmed live, Fase 1B.3).
    Uses the raw ``IUIAutomation`` COM ``FindFirst`` with a genuine
    ``UIA_AutomationIdPropertyId`` condition instead, letting UIA's own
    provider do the search natively (proven live, Fase 1B.3 diagnostic,
    2026-09-25: 562-1969ms, versus 3-5s+ for a full
    :func:`scan_window_controls` walk). Returns the raw
    ``IUIAutomationElement``, or None. Read-only.

    Like any UIA call against GO's window, this CAN still block for GO's
    own confirmed ~42-46s unresponsive window (Fase 1B.3:
    GO_QUERY_REAL_WAIT_CONFIRMED) if issued while GO's UI thread is busy --
    it is cheap when GO is responsive, not immune to GO's freeze.
    """
    from pywinauto.uia_defines import IUIA

    ui = IUIA()
    condition = ui.iuia.CreatePropertyCondition(ui.UIA_dll.UIA_AutomationIdPropertyId, automation_id)
    raw_root = root_element.element_info._element
    return raw_root.FindFirst(ui.ui_automation_client.TreeScope_Descendants, condition)


def uia_raw_element_name(raw_element) -> str:
    """Read ``CurrentName`` off a raw ``IUIAutomationElement`` (e.g. from
    :func:`uia_find_first_by_automation_id`). Never raises for a None
    name."""
    name = raw_element.CurrentName
    return name if name is not None else ""


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
    """Bounded, read-only UIA scan. Never raises: degrades to [].

    Phase 1B.1 instrumentation: logs ``PERF WALK_TREE`` with element count
    and elapsed time for every call -- this is the single choke point used
    by GO_DISCOVERY, the OPEN_TRAZABILIDAD retry loop, and every
    WAIT_INVOICE_RESULT poll tick, so this one log line answers "how much
    does one full tree walk of this window cost, and how many elements
    does it visit" for all three call sites without touching each of them
    individually.
    """
    t0 = time.monotonic()
    try:
        element = connect_uia(handle)
    except Exception as exc:
        _classify_and_log(exc, f"fallo (no fatal) reconectando via UIA a ventana {handle}")
        logger.info("PERF UIA_CONNECT {:.0f}ms FAIL", (time.monotonic() - t0) * 1000.0)
        return []
    connect_ms = (time.monotonic() - t0) * 1000.0
    deadline = time.monotonic() + timeout_seconds
    t1 = time.monotonic()
    try:
        result = walk_tree(element, max_depth=max_depth, deadline=deadline)
    except Exception as exc:
        _classify_and_log(exc, f"fallo (no fatal) escaneando UIA de ventana {handle}")
        logger.info("PERF WALK_TREE elements=0 {:.0f}ms FAIL (connect={:.0f}ms)", (time.monotonic() - t1) * 1000.0, connect_ms)
        return []
    walk_ms = (time.monotonic() - t1) * 1000.0
    hit_deadline = time.monotonic() > deadline
    logger.info(
        "PERF WALK_TREE elements={} depth<= {} {:.0f}ms hit_deadline={} (connect={:.0f}ms)",
        len(result), max_depth, walk_ms, hit_deadline, connect_ms,
    )
    return result


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


def walk_live(element, visit, max_depth: int, deadline: float, depth: int = 0, stats: dict | None = None) -> None:
    """Bounded, read-only recursive walk over LIVE pywinauto elements.

    Never invokes/clicks anything -- `visit(element, depth)` is caller-owned
    and must stay read-only too. Every actual mutating action in this
    package happens later, from its own single dedicated call site.

    Phase 1B.1 instrumentation (measurement only -- does NOT change
    traversal behavior): when ``stats`` is provided, increments
    ``stats['visited']`` once per node visited. This still walks the WHOLE
    subtree even after ``visit()`` has recorded a match -- there is no
    early-exit signal yet; ``stats['visited']`` exists specifically to let
    a caller measure, with real numbers, exactly how many nodes a given
    poll iteration walks (see Fase 1B.1's audit request) before any
    early-exit optimization is applied.
    """
    if stats is not None:
        stats["visited"] = stats.get("visited", 0) + 1
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
        walk_live(child, visit, max_depth, deadline, depth + 1, stats=stats)


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
