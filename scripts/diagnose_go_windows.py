"""F0.2B — Extended, read-only diagnostic sweep to identify GO/Índigo by content.

A prior run picked a window by title containing "Vie" and it turned out to be
the Chromium login screen, not the authenticated "Mis Favoritos" screen the
user actually sees. That violates Regla 1 (03_CLAUDE_RULES.md: no asumir).
This script never selects a window by title/branding. Instead it:

  1. Extended-enumerates ALL relevant top-level windows (not just "Vie"
     branded ones), grouped by PID, with rich diagnostic fields.
  2. Freshly re-resolves every PID currently running as
     "Vie Cloud Platform.exe" via psutil (never assumes a previously-seen
     PID, e.g. 9016, is still valid).
  3. For each such process, discovers candidate windows (its own top-level
     windows plus descendant windows whose class name looks
     Chromium-embedded -- Chrome_WidgetWin_*/BrowserRootView -- or
     WinForms-classed) and runs a READ-ONLY UIA content inspection on each,
     reusing walk_tree()/ControlInfo from inspect_go.py rather than
     duplicating tree-traversal logic.
  4. Searches each candidate's collected controls for 7 target strings
     (name/automation_id/class_name, case-insensitive substring).
  5. Performs a multi-pass Chromium "warm-up" (a Chromium-embedded UIA tree
     can come back empty on the first query and populated on the second),
     up to 3 passes, using the LAST pass's results for the content search.
  6. Flags a window a "probable match" only when it contains at least one
     of the 5 STRONGER content signals -- never based on title or process
     branding alone.

This script never clicks, types, closes, or otherwise mutates any window or
process (Regla 5, Regla 12, 03_CLAUDE_RULES.md). It is diagnostics only.

Untitled-window inclusion policy: an untitled top-level window is still
kept in the extended enumeration if its client rectangle is at least
MIN_SIGNIFICANT_DIMENSION_PX in both width and height (see that constant
below for the exact threshold and rationale) -- so GO/Índigo can never be
hidden by this script just because it happens to render without a title.

Usage:
    python scripts/diagnose_go_windows.py
    python scripts/diagnose_go_windows.py --capture-foreground 8
"""

from __future__ import annotations

import argparse
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
from inspect_go import ControlInfo, DEFAULT_MAX_DEPTH, walk_tree  # noqa: E402

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

TARGET_PROCESS_NAME = "Vie Cloud Platform.exe"

# The 5 stronger content signals: finding any one of these inside a
# candidate window's UIA tree is treated as real evidence of the
# authenticated "Mis Favoritos" screen, per the mission brief.
STRONG_SIGNAL_TEXTS: tuple[str, ...] = (
    "Mis Favoritos",
    "Trazabilidad de Factura",
    "Consulta historias",
    "Vie Finance",
    "Vie Clinical",
)
# Recorded, but never alone sufficient to call a window a probable match.
WEAK_SIGNAL_TEXTS: tuple[str, ...] = (
    "Vie RCM",
    "Introduzca texto a buscar",
)
TARGET_TEXTS: tuple[str, ...] = STRONG_SIGNAL_TEXTS + WEAK_SIGNAL_TEXTS

# inspect_go.py's DEFAULT_MAX_DEPTH (8) is tuned for one deliberately
# selected window inspected in isolation. This script instead walks every
# candidate window discovered under Vie Cloud Platform.exe -- each of which
# may also take up to 3 warm-up passes -- in a single run, so the per-window
# depth is trimmed to keep total wall-clock time bounded while still
# reaching nested module buttons/panes observed in prior single-window
# scans at depth 8.
DIAGNOSTIC_SCAN_MAX_DEPTH = max(DEFAULT_MAX_DEPTH - 2, 1)
DIAGNOSTIC_SCAN_TIMEOUT_SECONDS = 20.0

# A window with both dimensions below this is treated as visually
# insignificant (tooltips, drag-image helpers, 0x0 message-only windows)
# and is only kept in the extended enumeration if it also has a non-empty
# title. Chosen conservatively per Regla 1 (03_CLAUDE_RULES.md): biased
# toward over-inclusion, never toward hiding a real candidate window.
MIN_SIGNIFICANT_DIMENSION_PX = 50

# A window whose client rectangle is at least this large in both
# dimensions is flagged "large" in the report even when the OS-reported
# maximized bit (win32gui.IsZoomed) is False, since some WinForms/Chromium
# apps render a borderless near-fullscreen window without ever setting it.
LARGE_WINDOW_DIMENSION_PX = 900

# A first-pass UIA walk returning fewer controls than this is treated as
# "suspicious" -- almost certainly an un-warmed Chromium accessibility
# tree rather than a genuinely simple window -- and triggers warm-up passes
# even for a window whose class name doesn't already look Chromium-hosted.
SUSPICIOUS_CONTROL_COUNT_THRESHOLD = 5

# Wait between warm-up passes. Explicitly authorized for this diagnostic
# only (see module docstring); never used as a general synchronization
# primitive elsewhere in this file.
WARMUP_SLEEP_SECONDS = 1.0
MAX_WARMUP_PASSES = 3

DEFAULT_REPORT_PATH = Path("runtime") / "logs" / "go_windows_diagnostic.txt"


# --------------------------------------------------------------------------
# Error taxonomy (03_CLAUDE_RULES.md): never collapse everything into one
# bucket. SECURITY_OR_AUTHORIZATION_BLOCK is reserved for real evidence
# (access-denied style failures); everything else is classified into the
# remaining, more specific buckets.
# --------------------------------------------------------------------------


class WindowNotFoundError(Exception):
    """Raised when a target window cannot be found or resolved at all."""


class ControlNotFoundError(Exception):
    """Raised when an expected control cannot be located inside a window."""


class UiaTimeoutError(Exception):
    """Raised when a UIA operation exceeds its allotted time budget."""


class ApplicationUnresponsiveError(Exception):
    """Raised when a window exists but does not respond to UIA in time."""


class AmbiguousWindowError(Exception):
    """Raised when multiple candidates exist and cannot be auto-resolved."""


def _classify_and_log(exc: Exception, context: str) -> str:
    """Classify an unexpected exception into the project's error taxonomy.

    Logs one line tagged with the chosen code and returns it, so callers
    that need to branch on the classification still can. This keeps
    generic exceptions from all collapsing into a single label.
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
class WindowDiagnostic:
    """Extended, read-only snapshot of one desktop window for diagnostics."""

    index: int
    title: str
    handle: int
    process_id: int | None
    process_name: str | None
    executable_path: str | None
    visible: bool
    enabled: bool
    minimized: bool
    maximized: bool
    class_name: str
    rectangle: str
    width: int
    height: int
    parent_handle: int | None
    owner_handle: int | None

    @property
    def is_flagged_large(self) -> bool:
        """True if OS-maximized, or large enough to be flagged regardless."""
        return self.maximized or (
            self.width >= LARGE_WINDOW_DIMENSION_PX and self.height >= LARGE_WINDOW_DIMENSION_PX
        )


@dataclass(frozen=True)
class CandidateInspection:
    """Read-only UIA content-inspection result for one candidate window."""

    window: WindowDiagnostic
    pass_counts: list[int]
    controls: list[ControlInfo]
    matched_strong: list[str]
    matched_weak: list[str]
    probable_match: bool


# --------------------------------------------------------------------------
# Step 1-2: extended enumeration + fresh PID resolution
# --------------------------------------------------------------------------


def _resolve_process_name(pid: int | None) -> str | None:
    """Best-effort process name resolution; None on any failure."""
    if pid is None:
        return None
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception as exc:
        logger.debug("No se pudo resolver proceso para PID {}: {}", pid, exc)
        return None


def _resolve_executable_path(pid: int | None) -> str | None:
    """Best-effort executable path resolution; None on any failure."""
    if pid is None:
        return None
    try:
        import psutil

        return psutil.Process(pid).exe()
    except Exception as exc:
        logger.debug("No se pudo resolver ruta ejecutable para PID {}: {}", pid, exc)
        return None


def _get_parent_handle(handle: int) -> int | None:
    try:
        import win32gui

        parent = win32gui.GetParent(handle)
        return parent or None
    except Exception as exc:
        logger.debug("No se pudo obtener parent handle de {}: {}", handle, exc)
        return None


def _get_owner_handle(handle: int) -> int | None:
    try:
        import win32con
        import win32gui

        owner = win32gui.GetWindow(handle, win32con.GW_OWNER)
        return owner or None
    except Exception as exc:
        logger.debug("No se pudo obtener owner handle de {}: {}", handle, exc)
        return None


def _is_significant(title: str, width: int, height: int) -> bool:
    """Inclusion filter for the extended enumeration.

    Keeps any window with a non-empty title, or an untitled one whose
    client rectangle meets MIN_SIGNIFICANT_DIMENSION_PX in both
    dimensions. See module docstring for rationale.
    """
    if title.strip():
        return True
    return width >= MIN_SIGNIFICANT_DIMENSION_PX and height >= MIN_SIGNIFICANT_DIMENSION_PX


def enumerate_all_windows() -> list[WindowDiagnostic]:
    """Enumerate ALL relevant top-level windows with extended diagnostic fields.

    Unlike list_windows.enumerate_windows(), this makes no assumption about
    "Vie" branding and captures the extra fields (exe path, enabled state,
    minimized/maximized, rectangle geometry, parent/owner handles) needed to
    identify GO/Índigo by evidence rather than by title (Regla 1).

    Raises:
        WindowNotFoundError: if the desktop window list itself cannot be
            obtained (e.g. the UIA subsystem is unavailable or blocked).
    """
    import win32gui

    from pywinauto import Desktop

    try:
        raw_windows = Desktop(backend="uia").windows()
    except Exception as exc:
        _classify_and_log(exc, "fallo al enumerar ventanas via UIA (barrido extendido)")
        raise WindowNotFoundError(str(exc)) from exc

    results: list[WindowDiagnostic] = []
    for idx, element in enumerate(raw_windows):
        try:
            handle = element.handle
            title = element.window_text() or ""

            try:
                rect = element.rectangle()
                width, height = rect.width(), rect.height()
                rectangle = str(rect)
            except Exception as exc:
                logger.debug("No se pudo obtener rectangle de ventana {}: {}", handle, exc)
                width = height = 0
                rectangle = "?"

            if not _is_significant(title, width, height):
                continue

            try:
                visible = bool(element.is_visible())
            except Exception:
                visible = False
            try:
                enabled = bool(element.is_enabled())
            except Exception:
                enabled = False
            try:
                minimized = bool(win32gui.IsIconic(handle))
            except Exception:
                minimized = False
            try:
                maximized = bool(win32gui.IsZoomed(handle))
            except Exception:
                maximized = False
            try:
                class_name = element.element_info.class_name or "?"
            except Exception:
                class_name = "?"

            pid: int | None = None
            try:
                pid = element.process_id()
            except Exception as exc:
                logger.debug("No se pudo obtener PID de ventana {}: {}", handle, exc)

            results.append(
                WindowDiagnostic(
                    index=idx,
                    title=title,
                    handle=handle,
                    process_id=pid,
                    process_name=_resolve_process_name(pid),
                    executable_path=_resolve_executable_path(pid),
                    visible=visible,
                    enabled=enabled,
                    minimized=minimized,
                    maximized=maximized,
                    class_name=class_name,
                    rectangle=rectangle,
                    width=width,
                    height=height,
                    parent_handle=_get_parent_handle(handle),
                    owner_handle=_get_owner_handle(handle),
                )
            )
        except Exception as exc:
            _classify_and_log(
                exc, f"ventana omitida durante enumeración extendida (idx={idx})"
            )
            continue

    logger.info("Enumeración extendida completada: {} ventanas relevantes", len(results))
    return results


def group_by_pid(windows: list[WindowDiagnostic]) -> dict[int | None, list[WindowDiagnostic]]:
    """Group extended-enumeration results by process id."""
    groups: dict[int | None, list[WindowDiagnostic]] = {}
    for w in windows:
        groups.setdefault(w.process_id, []).append(w)
    return groups


def find_target_process_ids(process_name: str = TARGET_PROCESS_NAME) -> list[int]:
    """Fresh-resolve every PID currently matching process_name via psutil.

    Never assumes a previously-seen PID (e.g. 9016 from a prior run) is
    still valid (Regla 1, 03_CLAUDE_RULES.md). Multiple simultaneous
    matches are all returned so the caller inspects every one of them.
    """
    import psutil

    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == process_name.lower():
                pids.append(proc.info["pid"])
        except Exception as exc:
            logger.debug("No se pudo inspeccionar un proceso durante la búsqueda: {}", exc)
            continue

    if not pids:
        logger.warning(
            "WINDOW_NOT_FOUND: no se encontró ningún proceso '{}' en ejecución", process_name
        )
    elif len(pids) > 1:
        logger.info(
            "AMBIGUOUS_WINDOW: se encontraron {} procesos '{}' (PIDs={}); se inspeccionarán todos",
            len(pids),
            process_name,
            pids,
        )
    else:
        logger.info("Proceso '{}' encontrado: PID={}", process_name, pids[0])
    return pids


# --------------------------------------------------------------------------
# Step 3-5: candidate discovery, warm-up walk, content search
# --------------------------------------------------------------------------


def _is_chromium_class(class_name: str) -> bool:
    lowered = class_name.lower()
    return lowered.startswith("chrome_widgetwin") or "browserrootview" in lowered


def _is_winforms_class(class_name: str) -> bool:
    return class_name.lower().startswith("windowsforms")


def _descendant_handles(handle: int) -> list[int]:
    """Return every descendant HWND of handle (win32gui.EnumChildWindows).

    Note: despite the Win32 API's "child" naming, EnumChildWindows walks
    the full descendant subtree, not just immediate children. That is
    intentional here -- it is how a Chrome_WidgetWin_*/BrowserRootView pane
    nested several levels inside a CEF container gets found.
    """
    import win32gui

    children: list[int] = []
    try:
        win32gui.EnumChildWindows(handle, lambda h, _: children.append(h), None)
    except Exception as exc:
        logger.debug(
            "CONTROL_NOT_FOUND: no se pudieron enumerar ventanas descendientes de {}: {}",
            handle,
            exc,
        )
    return children


def discover_candidate_handles(target_windows: list[WindowDiagnostic]) -> list[tuple[int, str]]:
    """Collect (handle, class_name) pairs worth a UIA content inspection.

    Always includes each given top-level window itself. Also walks every
    descendant HWND and keeps only the ones whose class name matches a
    Chromium-embedded pattern (Chrome_WidgetWin_*, *BrowserRootView*) or a
    WinForms pattern (WindowsForms*), per the mission brief -- this keeps
    the candidate set focused instead of individually UIA-connecting to
    every native control descendant.
    """
    import win32gui

    candidates: dict[int, str] = {}
    for w in target_windows:
        candidates[w.handle] = w.class_name
        for child_handle in _descendant_handles(w.handle):
            try:
                class_name = win32gui.GetClassName(child_handle)
            except Exception as exc:
                logger.debug(
                    "CONTROL_NOT_FOUND: no se pudo leer class_name de {}: {}", child_handle, exc
                )
                continue
            if _is_chromium_class(class_name) or _is_winforms_class(class_name):
                candidates[child_handle] = class_name
    return list(candidates.items())


def search_content(controls: list[ControlInfo]) -> tuple[list[str], list[str]]:
    """Case-insensitive substring search for the 7 target strings.

    Searches name/automation_id/class_name of every collected control.

    Returns:
        (matched_strong, matched_weak) -- the subsets of STRONG_SIGNAL_TEXTS
        and WEAK_SIGNAL_TEXTS that were found anywhere in the tree.
    """
    haystacks = [f"{c.name} {c.automation_id} {c.class_name}".lower() for c in controls]
    matched_strong = [t for t in STRONG_SIGNAL_TEXTS if any(t.lower() in h for h in haystacks)]
    matched_weak = [t for t in WEAK_SIGNAL_TEXTS if any(t.lower() in h for h in haystacks)]
    return matched_strong, matched_weak


def _synthesize_window_diagnostic(handle: int, class_name: str) -> WindowDiagnostic:
    """Build a WindowDiagnostic for a handle not present in the top-level sweep.

    Descendant/child window handles (e.g. a Chrome_WidgetWin_* pane) are not
    top-level windows, so they never appear in enumerate_all_windows(). This
    synthesizes the same reporting fields directly from the HWND, purely for
    report readability.
    """
    import win32gui
    import win32process

    try:
        title = win32gui.GetWindowText(handle)
    except Exception:
        title = ""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(handle)
        width, height = right - left, bottom - top
        rectangle = f"(L{left}, T{top}, R{right}, B{bottom})"
    except Exception:
        width = height = 0
        rectangle = "?"

    pid: int | None = None
    try:
        _, pid = win32process.GetWindowThreadProcessId(handle)
    except Exception:
        pid = None

    try:
        visible = bool(win32gui.IsWindowVisible(handle))
    except Exception:
        visible = False
    try:
        enabled = bool(win32gui.IsWindowEnabled(handle))
    except Exception:
        enabled = False
    try:
        minimized = bool(win32gui.IsIconic(handle))
    except Exception:
        minimized = False
    try:
        maximized = bool(win32gui.IsZoomed(handle))
    except Exception:
        maximized = False

    return WindowDiagnostic(
        index=-1,
        title=title,
        handle=handle,
        process_id=pid,
        process_name=_resolve_process_name(pid),
        executable_path=_resolve_executable_path(pid),
        visible=visible,
        enabled=enabled,
        minimized=minimized,
        maximized=maximized,
        class_name=class_name,
        rectangle=rectangle,
        width=width,
        height=height,
        parent_handle=_get_parent_handle(handle),
        owner_handle=_get_owner_handle(handle),
    )


def inspect_candidate(
    handle: int, class_name: str, window_lookup: dict[int, WindowDiagnostic]
) -> CandidateInspection | None:
    """Connect to one candidate window handle and run a warm-up-aware UIA scan.

    Returns None (after logging the appropriate taxonomy code) if the
    window cannot be connected to at all; a bad candidate never aborts the
    rest of the sweep (Regla 14).
    """
    from pywinauto import Desktop

    try:
        element = Desktop(backend="uia").window(handle=handle)
        element.wait("exists", timeout=5)
    except Exception as exc:
        lowered = str(exc).lower()
        if "timed out" in lowered or "timeout" in lowered:
            logger.warning(
                "APPLICATION_UNRESPONSIVE: la ventana {} no respondió a UIA a tiempo: {}",
                handle,
                exc,
            )
        else:
            logger.warning(
                "WINDOW_NOT_FOUND: no se pudo conectar a la ventana candidata {}: {}", handle, exc
            )
        return None

    pass_counts: list[int] = []
    controls: list[ControlInfo] = []
    needs_warmup = _is_chromium_class(class_name)

    for pass_num in range(1, MAX_WARMUP_PASSES + 1):
        if pass_num > 1:
            time.sleep(WARMUP_SLEEP_SECONDS)

        deadline = time.monotonic() + DIAGNOSTIC_SCAN_TIMEOUT_SECONDS
        try:
            controls = walk_tree(element, max_depth=DIAGNOSTIC_SCAN_MAX_DEPTH, deadline=deadline)
        except Exception as exc:
            _classify_and_log(
                exc, f"fallo durante recorrido UIA de ventana {handle} (pase {pass_num})"
            )
            controls = []
        pass_counts.append(len(controls))

        if pass_num == 1 and not needs_warmup and len(controls) < SUSPICIOUS_CONTROL_COUNT_THRESHOLD:
            needs_warmup = True

        if not needs_warmup or pass_num >= MAX_WARMUP_PASSES:
            break

    matched_strong, matched_weak = search_content(controls)

    diag = window_lookup.get(handle) or _synthesize_window_diagnostic(handle, class_name)

    return CandidateInspection(
        window=diag,
        pass_counts=pass_counts,
        controls=controls,
        matched_strong=matched_strong,
        matched_weak=matched_weak,
        probable_match=bool(matched_strong),
    )


# --------------------------------------------------------------------------
# --capture-foreground mode
# --------------------------------------------------------------------------


def _countdown_wait(seconds: int) -> None:
    """Print and block for the user-facing countdown before a foreground capture.

    This is the only wait in this module (Regla 5/12, 03_CLAUDE_RULES.md):
    a bounded, user-facing pause so a human can manually bring a window to
    the foreground -- never a synchronization primitive, never used to wait
    on application state.
    """
    print(f"En los próximos {seconds} segundos coloque GO/Índigo en primer plano.")
    for remaining in range(seconds, 0, -1):
        print(f"  {remaining}...")
        time.sleep(1)


def _capture_foreground_handle() -> int | None:
    """Read win32gui.GetForegroundWindow() once; never sends synthetic input."""
    import win32gui

    try:
        handle = win32gui.GetForegroundWindow()
    except Exception as exc:
        logger.error("WINDOW_NOT_FOUND: no se pudo obtener la ventana en primer plano: {}", exc)
        return None

    if not handle:
        logger.error("WINDOW_NOT_FOUND: GetForegroundWindow() no devolvió un handle válido")
        return None
    return handle


def capture_foreground_window(seconds: int) -> CandidateInspection | None:
    """Countdown, then read-only inspect whatever window the human brought to front.

    Never sends synthetic keyboard or mouse input; only reads
    win32gui.GetForegroundWindow() after the human manually focuses
    GO/Índigo (Regla 5, Regla 12, 03_CLAUDE_RULES.md).

    Kept as a simpler, single-window entry point alongside
    analyze_foreground_window() (the full hierarchy/MDI/content analysis
    added for this task); both share _countdown_wait()/
    _capture_foreground_handle() so the countdown logic is never duplicated.
    """
    import win32gui

    _countdown_wait(seconds)

    handle = _capture_foreground_handle()
    if handle is None:
        return None

    try:
        class_name = win32gui.GetClassName(handle)
    except Exception as exc:
        logger.warning("CONTROL_NOT_FOUND: no se pudo leer class_name de {}: {}", handle, exc)
        class_name = "?"

    diag = _synthesize_window_diagnostic(handle, class_name)
    logger.info(
        "Ventana en primer plano capturada: titulo={!r} handle={} pid={} class={}",
        diag.title,
        diag.handle,
        diag.process_id,
        diag.class_name,
    )

    try:
        return inspect_candidate(handle, class_name, {handle: diag})
    except Exception as exc:
        _classify_and_log(exc, f"fallo inesperado inspeccionando ventana en primer plano {handle}")
        return None


# --------------------------------------------------------------------------
# Full foreground analysis: hierarchy classification, MDI, BackstageViewControl,
# and dual-backend (uia + win32) content inspection across up to 4 targets.
# --------------------------------------------------------------------------

# The 7 target strings from the mission brief: reuse STRONG_SIGNAL_TEXTS (5)
# + WEAK_SIGNAL_TEXTS (2) instead of redefining a duplicate tuple.
ALL_TARGET_TEXTS: tuple[str, ...] = STRONG_SIGNAL_TEXTS + WEAK_SIGNAL_TEXTS

# Order in which the 4 possible analysis targets are searched for the
# "first match wins" rule (report step 6): foreground -> root -> active MDI
# child -> BackstageViewControl.
TARGET_LABEL_ORDER: tuple[str, ...] = ("foreground", "root", "mdi_active", "backstage")
# uia before win32 for each target, per spec.
BACKEND_ORDER: tuple[str, ...] = ("uia", "win32")

HIERARCHY_A_ROOT = "A_ROOT_WINDOW"
HIERARCHY_B_MDI_CHILD = "B_MDI_CHILD"
HIERARCHY_C_DESCENDANT = "C_DESCENDANT_CONTROL"
HIERARCHY_D_OTHER = "D_OTHER"

# Bounded climb: a real Win32 ancestor chain is never this deep; this only
# guards against an unexpected parent cycle (Regla 14: never hang/crash on
# unrecognized behavior).
MAX_ANCESTOR_CLIMB = 64


@dataclass(frozen=True)
class HierarchyClassification:
    """Classification of the foreground handle within GO/Índigo's window tree."""

    code: str
    label_es: str
    root_handle: int | None
    ancestors: list[tuple[int, str]]


@dataclass(frozen=True)
class MatchedControl:
    """First control found containing one target text, with best-effort HWND state."""

    target_text: str
    source_label: str
    backend: str
    handle: int | None
    control_type: str
    name: str
    automation_id: str
    class_name: str
    rectangle: str
    visible: str
    enabled: str
    parent: str


@dataclass(frozen=True)
class ForegroundAnalysisResult:
    """Full read-only analysis result for --capture-foreground's extended mode."""

    foreground: WindowDiagnostic
    hierarchy: HierarchyClassification
    root: WindowDiagnostic
    mdiclient_handle: int | None
    mdi_active_handle: int | None
    mdi_active_window: WindowDiagnostic | None
    backstage_handle: int | None
    backstage_window: WindowDiagnostic | None
    scans: dict[tuple[str, str], list[ControlInfo]]
    matches: dict[str, MatchedControl | None]
    visual_candidates: list[ControlInfo]


def _is_mdiclient_class(class_name: str) -> bool:
    return "mdiclient" in (class_name or "").lower()


def _is_backstage(title: str, class_name: str) -> bool:
    lowered_title = (title or "").lower()
    lowered_class = (class_name or "").lower()
    return "backstage" in lowered_title or "backstage" in lowered_class


def _get_root_ancestor(handle: int) -> int | None:
    """win32gui.GetAncestor(handle, GA_ROOT) -- read-only ancestor query."""
    import win32con
    import win32gui

    try:
        root = win32gui.GetAncestor(handle, win32con.GA_ROOT)
        return root or None
    except Exception as exc:
        logger.debug("No se pudo obtener GA_ROOT de {}: {}", handle, exc)
        return None


def classify_foreground_hierarchy(handle: int) -> HierarchyClassification:
    """Classify handle as A) root, B) MDI child, C) descendant control, or D) other.

    Per spec: A if handle is its own root; B if its immediate parent's class
    contains "mdiclient"; C otherwise (climbing GetParent() until an
    MDICLIENT or the root is reached, recording the climb path); D as a
    non-crashing fallback (e.g. no parent and not equal to root).
    """
    import win32gui

    root = _get_root_ancestor(handle)
    if root is not None and handle == root:
        return HierarchyClassification(HIERARCHY_A_ROOT, "ventana raíz de GO", root, [])

    parent = _get_parent_handle(handle)
    if parent is None:
        logger.warning(
            "UNKNOWN_ERROR: clasificación D (otra cosa) para handle {} -- sin parent y "
            "no es la raíz (root={})",
            handle,
            root,
        )
        return HierarchyClassification(HIERARCHY_D_OTHER, "otra cosa", root, [])

    try:
        parent_class = win32gui.GetClassName(parent)
    except Exception as exc:
        logger.debug("No se pudo leer class_name del parent {}: {}", parent, exc)
        parent_class = "?"

    if _is_mdiclient_class(parent_class):
        return HierarchyClassification(
            HIERARCHY_B_MDI_CHILD, "formulario MDI hijo", root, [(parent, parent_class)]
        )

    # C) control descendiente: climb GetParent() until an MDICLIENT or the
    # root is reached, bounded by MAX_ANCESTOR_CLIMB (Regla 14).
    ancestors: list[tuple[int, str]] = []
    current = handle
    seen: set[int] = {handle}
    for _ in range(MAX_ANCESTOR_CLIMB):
        p = _get_parent_handle(current)
        if p is None or p in seen:
            break
        try:
            p_class = win32gui.GetClassName(p)
        except Exception:
            p_class = "?"
        ancestors.append((p, p_class))
        seen.add(p)
        if _is_mdiclient_class(p_class) or (root is not None and p == root):
            break
        current = p

    return HierarchyClassification(HIERARCHY_C_DESCENDANT, "control descendiente", root, ancestors)


def _sweep_mdiclient_and_backstage(root_handle: int) -> tuple[int | None, int | None]:
    """One EnumChildWindows sweep from root_handle finding both an MDICLIENT
    descendant and a BackstageViewControl-like descendant (spec steps 3-4
    explicitly share this single sweep).
    """
    import win32gui

    mdiclient_handle: int | None = None
    backstage_handle: int | None = None
    for h in _descendant_handles(root_handle):
        try:
            class_name = win32gui.GetClassName(h)
        except Exception as exc:
            logger.debug("No se pudo leer class_name de descendiente {}: {}", h, exc)
            class_name = ""
        try:
            title = win32gui.GetWindowText(h)
        except Exception:
            title = ""

        if mdiclient_handle is None and _is_mdiclient_class(class_name):
            mdiclient_handle = h
        if backstage_handle is None and _is_backstage(title, class_name):
            backstage_handle = h
        if mdiclient_handle is not None and backstage_handle is not None:
            break

    if mdiclient_handle is None:
        logger.info(
            "No se encontró ningún descendiente MDICLIENT bajo la raíz {} (puede no ser "
            "una app MDI clásica, o el MDICLIENT puede estar anidado de otra forma)",
            root_handle,
        )
    return mdiclient_handle, backstage_handle


def _query_active_mdi_child(mdiclient_handle: int) -> int | None:
    """Query the active MDI child via WM_MDIGETACTIVE -- read-only exception.

    WM_MDIGETACTIVE is the ONLY window message this diagnostic tool is
    authorized to send (03_CLAUDE_RULES.md, Regla 5/Regla 12): per MSDN, it
    merely retrieves the handle of the currently active MDI child window
    and does not alter focus, z-order, or window state. This is deliberately
    distinct from -- and must never be confused with -- the MUTATING
    WM_MDIACTIVATE / WM_MDIRESTORE / WM_MDIMAXIMIZE messages, which this
    script must never send.
    """
    import win32con
    import win32gui

    try:
        active = win32gui.SendMessage(mdiclient_handle, win32con.WM_MDIGETACTIVE, 0, 0)
    except Exception as exc:
        _classify_and_log(exc, f"fallo al consultar WM_MDIGETACTIVE en MDICLIENT {mdiclient_handle}")
        return None
    return int(active) if active else None


def _run_backend_scan(target_label: str, backend: str, handle: int) -> list[ControlInfo]:
    """Connect via one backend and walk_tree() the target -- reused as-is
    from inspect_go.py (backend-agnostic). No Chromium warm-up passes: this
    is confirmed native WinForms content, so a single pass per backend is
    sufficient (per spec). One bad target/backend never aborts the others.
    """
    from pywinauto import Desktop

    try:
        element = Desktop(backend=backend).window(handle=handle)
        element.wait("exists", timeout=5)
    except Exception as exc:
        lowered = str(exc).lower()
        if "timed out" in lowered or "timeout" in lowered:
            logger.warning(
                "APPLICATION_UNRESPONSIVE: {}/{} handle {} no respondió a UIA a tiempo: {}",
                target_label,
                backend,
                handle,
                exc,
            )
        else:
            logger.warning(
                "WINDOW_NOT_FOUND: no se pudo conectar {}/{} handle {}: {}",
                target_label,
                backend,
                handle,
                exc,
            )
        return []

    deadline = time.monotonic() + DIAGNOSTIC_SCAN_TIMEOUT_SECONDS
    try:
        return walk_tree(element, max_depth=DIAGNOSTIC_SCAN_MAX_DEPTH, deadline=deadline)
    except Exception as exc:
        _classify_and_log(exc, f"fallo recorrido {target_label}/{backend} handle {handle}")
        return []


def _collect_legacy_haystack(handle: int, max_depth: int = DIAGNOSTIC_SCAN_MAX_DEPTH) -> str:
    """Best-effort supplementary text via legacy (MSAA/IAccessible) properties.

    UIA-backend only -- .legacy_properties() is a UIA/MSAA-bridge concept,
    not applicable to the win32 backend. Used only as a last-resort fallback
    (see analyze_foreground_window) when a target string is not found via
    the normal name/automation_id/class_name search across any target or
    backend. Never raises: returns "" on any failure, so this is always a
    pure bonus signal, never a hard dependency (older pywinauto versions or
    non-MSAA-exposed controls may not support it at all).
    """
    from pywinauto import Desktop

    try:
        element = Desktop(backend="uia").window(handle=handle)
        element.wait("exists", timeout=5)
    except Exception:
        return ""

    deadline = time.monotonic() + DIAGNOSTIC_SCAN_TIMEOUT_SECONDS
    parts: list[str] = []

    def _recurse(el, depth: int) -> None:
        if depth > max_depth or time.monotonic() > deadline:
            return
        try:
            legacy = el.legacy_properties()
            if isinstance(legacy, dict):
                name = legacy.get("Name")
                value = legacy.get("Value")
                if name:
                    parts.append(str(name))
                if value:
                    parts.append(str(value))
        except Exception:
            pass
        try:
            children = el.children()
        except Exception:
            return
        for child in children:
            _recurse(child, depth + 1)

    try:
        _recurse(element, 0)
    except Exception:
        pass

    return " ".join(parts)


def _find_first_match(controls: list[ControlInfo], text: str) -> ControlInfo | None:
    """First ControlInfo whose name/automation_id/class_name contains text."""
    needle = text.lower()
    for c in controls:
        haystack = f"{c.name} {c.automation_id} {c.class_name}".lower()
        if needle in haystack:
            return c
    return None


def _correlate_win32_handle(sweep_root_handle: int, control: ControlInfo) -> int | None:
    """Best-effort: correlate a win32-backend ControlInfo back to its HWND.

    walk_tree()/ControlInfo (inspect_go.py) is intentionally backend-agnostic
    and does not carry a handle. For win32-backend matches specifically,
    this re-sweeps sweep_root_handle's descendants (EnumChildWindows) and
    looks for HWNDs whose class_name AND window rectangle both match the
    ControlInfo -- a reasonably distinctive combination for native WinForms
    controls. Returns None (never raises, never guesses) unless exactly one
    HWND matches; callers then report visible/enabled as "?".
    """
    import win32gui

    if control.class_name in ("", "?") or control.rectangle in ("", "?"):
        return None

    candidates: list[int] = []
    for h in _descendant_handles(sweep_root_handle):
        try:
            if win32gui.GetClassName(h) != control.class_name:
                continue
            left, top, right, bottom = win32gui.GetWindowRect(h)
            rect_str = f"(L{left}, T{top}, R{right}, B{bottom})"
        except Exception:
            continue
        if rect_str == control.rectangle:
            candidates.append(h)

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logger.debug(
            "Correlación win32->HWND ambigua para class={} rectangle={}: {} candidatos",
            control.class_name,
            control.rectangle,
            len(candidates),
        )
    return None


def _build_matched_control(
    target_text: str,
    source_label: str,
    backend: str,
    target_handle: int,
    control: ControlInfo,
) -> MatchedControl:
    """Build a MatchedControl, adding best-effort HWND-level state for win32 matches."""
    visible = "?"
    enabled = "?"
    parent = "?"
    handle: int | None = None

    if backend == "win32":
        import win32gui

        handle = _correlate_win32_handle(target_handle, control)
        if handle is not None:
            try:
                visible = str(bool(win32gui.IsWindowVisible(handle)))
            except Exception:
                visible = "?"
            try:
                enabled = str(bool(win32gui.IsWindowEnabled(handle)))
            except Exception:
                enabled = "?"
            try:
                p = win32gui.GetParent(handle)
                parent = str(p) if p else "?"
            except Exception:
                parent = "?"

    return MatchedControl(
        target_text=target_text,
        source_label=source_label,
        backend=backend,
        handle=handle,
        control_type=control.control_type,
        name=control.name,
        automation_id=control.automation_id,
        class_name=control.class_name,
        rectangle=control.rectangle,
        visible=visible,
        enabled=enabled,
        parent=parent,
    )


def analyze_foreground_window(seconds: int) -> ForegroundAnalysisResult | None:
    """Full read-only foreground analysis: hierarchy, MDI, Backstage, dual-backend content.

    Countdown -> GetForegroundWindow() -> hierarchy classification -> MDICLIENT
    + active MDI child (WM_MDIGETACTIVE only) + BackstageViewControl sweep ->
    dual-backend (uia + win32) content inspection of up to 4 targets
    (foreground, root, active MDI child, BackstageViewControl) -> search for
    the 7 target strings, first match across targets/backends wins.

    Never sends synthetic input; the only window message sent anywhere in
    this module is the read-only WM_MDIGETACTIVE query (see
    _query_active_mdi_child). Regla 1: every handle used here is either
    freshly obtained this run or re-validated live via win32gui.IsWindow()
    before use -- never a handle remembered from a previous run.
    """
    import win32gui

    _countdown_wait(seconds)

    handle = _capture_foreground_handle()
    if handle is None:
        return None

    try:
        class_name = win32gui.GetClassName(handle)
    except Exception as exc:
        logger.warning("CONTROL_NOT_FOUND: no se pudo leer class_name de {}: {}", handle, exc)
        class_name = "?"

    foreground_diag = _synthesize_window_diagnostic(handle, class_name)
    logger.info(
        "Ventana en primer plano capturada: titulo={!r} handle={} pid={} class={}",
        foreground_diag.title,
        foreground_diag.handle,
        foreground_diag.process_id,
        foreground_diag.class_name,
    )

    hierarchy = classify_foreground_hierarchy(handle)
    logger.info(
        "Clasificación jerárquica: {} ({}), root={}, ancestros={}",
        hierarchy.code,
        hierarchy.label_es,
        hierarchy.root_handle,
        hierarchy.ancestors,
    )

    root_handle = hierarchy.root_handle if hierarchy.root_handle is not None else handle
    if not win32gui.IsWindow(root_handle):
        # Regla 1: never reuse a handle without re-validating it live.
        logger.warning(
            "UNKNOWN_ERROR: root_handle {} ya no es una ventana válida; se usa el "
            "handle en primer plano {} como raíz de respaldo",
            root_handle,
            handle,
        )
        root_handle = handle

    if root_handle == handle:
        root_diag = foreground_diag
    else:
        try:
            root_class = win32gui.GetClassName(root_handle)
        except Exception as exc:
            logger.debug("No se pudo leer class_name de root {}: {}", root_handle, exc)
            root_class = "?"
        root_diag = _synthesize_window_diagnostic(root_handle, root_class)

    mdiclient_handle, backstage_handle = _sweep_mdiclient_and_backstage(root_handle)

    mdi_active_handle: int | None = None
    mdi_active_window: WindowDiagnostic | None = None
    if mdiclient_handle is not None:
        mdi_active_handle = _query_active_mdi_child(mdiclient_handle)
        if mdi_active_handle is not None and win32gui.IsWindow(mdi_active_handle):
            try:
                mac_class = win32gui.GetClassName(mdi_active_handle)
            except Exception:
                mac_class = "?"
            mdi_active_window = _synthesize_window_diagnostic(mdi_active_handle, mac_class)
        elif mdi_active_handle is not None:
            logger.warning(
                "UNKNOWN_ERROR: hijo MDI activo {} ya no es una ventana válida", mdi_active_handle
            )
            mdi_active_handle = None

    backstage_window: WindowDiagnostic | None = None
    if backstage_handle is not None and win32gui.IsWindow(backstage_handle):
        try:
            bs_class = win32gui.GetClassName(backstage_handle)
        except Exception:
            bs_class = "?"
        backstage_window = _synthesize_window_diagnostic(backstage_handle, bs_class)
    elif backstage_handle is not None:
        backstage_handle = None

    # Build the ordered, deduplicated target list (foreground -> root ->
    # mdi_active -> backstage), skipping any handle already covered and
    # re-validating liveness before scanning it (Regla 1).
    targets: list[tuple[str, int]] = []
    seen_handles: set[int] = set()
    for label, h in (
        ("foreground", handle),
        ("root", root_handle),
        ("mdi_active", mdi_active_handle),
        ("backstage", backstage_handle),
    ):
        if h is None or h in seen_handles:
            continue
        if not win32gui.IsWindow(h):
            logger.warning("Handle para target '{}' ya no es una ventana válida: {}", label, h)
            continue
        seen_handles.add(h)
        targets.append((label, h))

    scans: dict[tuple[str, str], list[ControlInfo]] = {}
    for label, h in targets:
        for backend in BACKEND_ORDER:
            scans[(label, backend)] = _run_backend_scan(label, backend, h)

    legacy_haystacks: dict[str, str] = {label: _collect_legacy_haystack(h) for label, h in targets}

    matches: dict[str, MatchedControl | None] = {}
    for text in ALL_TARGET_TEXTS:
        found: MatchedControl | None = None
        for label, h in targets:
            for backend in BACKEND_ORDER:
                ctrl = _find_first_match(scans.get((label, backend), []), text)
                if ctrl is not None:
                    found = _build_matched_control(text, label, backend, h, ctrl)
                    break
            if found is not None:
                break
        if found is None:
            needle = text.lower()
            legacy_label = next(
                (label for label, hay in legacy_haystacks.items() if needle in hay.lower()), None
            )
            if legacy_label is not None:
                logger.info(
                    "'{}' encontrado solo via legacy_properties en target='{}' (backend=uia)",
                    text,
                    legacy_label,
                )
                found = MatchedControl(
                    target_text=text,
                    source_label=legacy_label,
                    backend="uia (legacy)",
                    handle=None,
                    control_type="?",
                    name="?",
                    automation_id="?",
                    class_name="?",
                    rectangle="?",
                    visible="?",
                    enabled="?",
                    parent="?",
                )
        matches[text] = found

    visual_candidates: list[ControlInfo] = []
    mis_favoritos_match = matches.get("Mis Favoritos")
    trazabilidad_missing = matches.get("Trazabilidad de Factura") is None
    consulta_missing = matches.get("Consulta historias") is None
    if mis_favoritos_match is not None and (trazabilidad_missing or consulta_missing):
        source_controls = scans.get((mis_favoritos_match.source_label, mis_favoritos_match.backend), [])
        for c in source_controls:
            if not c.name.strip() and c.class_name not in ("", "?") and c.rectangle not in ("", "?"):
                visual_candidates.append(c)
        visual_candidates = visual_candidates[:20]

    return ForegroundAnalysisResult(
        foreground=foreground_diag,
        hierarchy=hierarchy,
        root=root_diag,
        mdiclient_handle=mdiclient_handle,
        mdi_active_handle=mdi_active_handle,
        mdi_active_window=mdi_active_window,
        backstage_handle=backstage_handle,
        backstage_window=backstage_window,
        scans=scans,
        matches=matches,
        visual_candidates=visual_candidates,
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def render_candidate_block(idx: int, insp: CandidateInspection) -> str:
    """Render one candidate window as the console/log block from the spec."""
    w = insp.window
    matched = insp.matched_strong + insp.matched_weak
    controls_detected = insp.pass_counts[-1] if insp.pass_counts else 0
    lines = [
        f"WINDOW {idx}",
        f"title: {w.title!r}",
        f"handle: {w.handle}",
        f"pid: {w.process_id}",
        f"process: {w.process_name}",
        f"class: {w.class_name}",
        f"rectangle: {w.rectangle}",
        f"controls detected: {controls_detected} (pases: {insp.pass_counts})",
        f"matched texts: {matched}",
        f"probable match: {insp.probable_match}",
    ]
    return "\n".join(lines)


def _fmt_tristate(value: bool | None) -> str:
    if value is True:
        return "SI"
    if value is False:
        return "NO"
    return "?"


def _render_signal_block(title: str, match: MatchedControl | None) -> list[str]:
    """Render the MIS FAVORITOS-style block: encontrado/backend/handle/class/rectangle."""
    lines = [f"{title}:"]
    if match is None:
        lines.append("- encontrado: NO")
        return lines
    lines += [
        "- encontrado: SI",
        f"- backend: {match.backend}",
        f"- handle: {match.handle if match.handle is not None else '?'}",
        f"- class: {match.class_name}",
        f"- rectangle: {match.rectangle}",
    ]
    return lines


def _render_target_block(title: str, match: MatchedControl | None) -> list[str]:
    """Render the TRAZABILIDAD/CONSULTA-style block with full control detail."""
    lines = [f"{title}:"]
    if match is None:
        lines.append("- encontrado: NO")
        return lines
    lines += [
        "- encontrado: SI",
        f"- backend: {match.backend}",
        f"- handle: {match.handle if match.handle is not None else '?'}",
        f"- control_type: {match.control_type}",
        f"- name/window_text: {match.name!r}",
        f"- automation_id: {match.automation_id}",
        f"- class_name: {match.class_name}",
        f"- rectangle: {match.rectangle}",
        f"- visible: {match.visible}",
        f"- enabled: {match.enabled}",
        f"- parent: {match.parent}",
    ]
    return lines


def build_foreground_report(result: ForegroundAnalysisResult) -> str:
    """Render the full --capture-foreground analysis report (spec step 6).

    Field order and section names follow the mission brief's report format
    exactly. One addition beyond that literal format: a
    "CLASIFICACION JERARQUICA" section right after "ROOT GO", carrying the
    hierarchy code/label and the climbed ancestor path -- the spec's step 2
    explicitly asks for that climb path to be recorded "for the report" but
    the step 6 format block has no named field for it, so it is surfaced
    here as a clearly-labeled, additive section rather than silently
    dropped. Flagged for coordinator sanity-check.
    """
    fg = result.foreground
    root = result.root
    lines: list[str] = [
        "FOREGROUND:",
        f"- handle: {fg.handle}",
        f"- PID: {fg.process_id}",
        f"- proceso: {fg.process_name}",
        f"- title: {fg.title!r}",
        f"- class: {fg.class_name}",
        f"- rectangle: {fg.rectangle}",
        f"- minimized: {_fmt_tristate(fg.minimized)}",
        f"- visible: {_fmt_tristate(fg.visible)}",
        "",
        "ROOT GO:",
        f"- handle: {root.handle}",
        f"- PID: {root.process_id}",
        f"- class: {root.class_name}",
        f"- rectangle: {root.rectangle}",
        "",
        "CLASIFICACION JERARQUICA:",
        f"- tipo: {result.hierarchy.code} ({result.hierarchy.label_es})",
        f"- ancestros inmediatos: {result.hierarchy.ancestors}",
        "",
        "MDI:",
        f"- MDICLIENT encontrado: {'SI' if result.mdiclient_handle else 'NO'}",
        f"- handle: {result.mdiclient_handle if result.mdiclient_handle else '-'}",
        f"- hijo MDI activo: {result.mdi_active_handle if result.mdi_active_handle else '-'}",
        f"- class: {result.mdi_active_window.class_name if result.mdi_active_window else '-'}",
        f"- rectangle: {result.mdi_active_window.rectangle if result.mdi_active_window else '-'}",
        "",
    ]

    lines += _render_signal_block("MIS FAVORITOS", result.matches.get("Mis Favoritos"))
    lines.append("")
    lines += _render_target_block(
        "TRAZABILIDAD DE FACTURA", result.matches.get("Trazabilidad de Factura")
    )
    lines.append("")
    lines += _render_target_block("CONSULTA HISTORIAS", result.matches.get("Consulta historias"))
    lines.append("")

    lines.append("OTROS CONTROLES:")
    for label in ("Vie Finance", "Vie Clinical", "Vie RCM", "Introduzca texto a buscar"):
        m = result.matches.get(label)
        if m is None:
            lines.append(f"- {label}: NO")
        else:
            lines.append(f"- {label}: SI ({m.source_label}/{m.backend})")
    lines.append("")

    if result.visual_candidates:
        lines.append("CONTROLES_VISUALES_SIN_TEXTO:")
        for c in result.visual_candidates:
            lines.append(
                f"- control_type={c.control_type} class={c.class_name} rectangle={c.rectangle}"
            )
        lines.append("")

    lines.append("CONCLUSION:")
    if (
        result.matches.get("Trazabilidad de Factura") is not None
        and result.matches.get("Consulta historias") is not None
    ):
        lines.append("- CONTROLES_IDENTIFICADOS")
    elif result.matches.get("Mis Favoritos") is not None:
        lines.append("- CONTROLES_VISUALES_SIN_TEXTO")
    else:
        lines.append("- REQUIERE_MAS_DIAGNOSTICO")

    return "\n".join(lines)


def build_full_sweep_report(
    all_windows: list[WindowDiagnostic],
    target_pids: list[int],
    inspections: list[CandidateInspection],
) -> str:
    """Assemble the complete diagnostic report text (sections A-D)."""
    sections: list[str] = []

    sections.append("=== SECCION A: Enumeracion extendida (agrupada por PID) ===")
    groups = group_by_pid(all_windows)
    for pid, windows in sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
        sections.append(f"\n--- PID {pid} ({windows[0].process_name or '?'}) ---")
        for w in windows:
            flag = " [MAXIMIZADA/GRANDE]" if w.is_flagged_large else ""
            sections.append(
                f"  handle={w.handle} title={w.title!r} class={w.class_name} "
                f"rect={w.rectangle} {w.width}x{w.height} visible={w.visible} "
                f"enabled={w.enabled} minimized={w.minimized} maximized={w.maximized} "
                f"exe={w.executable_path} parent={w.parent_handle} owner={w.owner_handle}{flag}"
            )

    sections.append("\n=== SECCION B: Resolucion de PIDs de Vie Cloud Platform.exe ===")
    sections.append(
        f"PIDs encontrados: {target_pids}"
        if target_pids
        else "No se encontro Vie Cloud Platform.exe en ejecucion."
    )

    sections.append("\n=== SECCION C: Inspeccion de contenido UIA de ventanas candidatas ===")
    if not inspections:
        sections.append("No se inspeccionaron ventanas candidatas.")
    for idx, insp in enumerate(inspections, start=1):
        sections.append("\n" + render_candidate_block(idx, insp))

    sections.append("\n=== SECCION D: Resumen de coincidencias probables ===")
    probable = [insp for insp in inspections if insp.probable_match]
    if probable:
        for insp in probable:
            sections.append(
                f"  handle={insp.window.handle} title={insp.window.title!r} "
                f"señales fuertes={insp.matched_strong}"
            )
    else:
        sections.append("  Ninguna ventana candidata mostro señales fuertes de contenido autenticado.")

    return "\n".join(sections)


def save_report(text: str, output_path: Path = DEFAULT_REPORT_PATH) -> Path:
    """Write the rendered report to output_path, creating parent dirs as needed."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return output_path


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run_full_sweep() -> str:
    """Run the complete read-only diagnostic sweep (spec steps 1-8) and return the report text."""
    start = time.monotonic()

    all_windows = enumerate_all_windows()
    target_pids = find_target_process_ids()

    target_top_level = [w for w in all_windows if w.process_id in target_pids]
    if target_pids and not target_top_level:
        logger.warning(
            "WINDOW_NOT_FOUND: Vie Cloud Platform.exe (PIDs={}) no tiene ventanas de nivel "
            "superior visibles/significativas",
            target_pids,
        )

    window_lookup = {w.handle: w for w in all_windows}
    candidate_pairs = discover_candidate_handles(target_top_level)

    inspections: list[CandidateInspection] = []
    for handle, class_name in candidate_pairs:
        try:
            insp = inspect_candidate(handle, class_name, window_lookup)
        except Exception as exc:
            _classify_and_log(exc, f"fallo inesperado inspeccionando ventana candidata {handle}")
            continue
        if insp is not None:
            inspections.append(insp)

    report = build_full_sweep_report(all_windows, target_pids, inspections)

    duration = time.monotonic() - start
    logger.info(
        "Barrido diagnostico completo: {} ventanas totales, {} candidatas inspeccionadas, "
        "{} coincidencias probables, {:.2f}s",
        len(all_windows),
        len(inspections),
        sum(1 for i in inspections if i.probable_match),
        duration,
    )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for diagnose_go_windows.py."""
    parser = argparse.ArgumentParser(
        description=(
            "F0.2B: barrido diagnostico extendido y de solo lectura para identificar "
            "GO/Indigo por evidencia de contenido, no por titulo/branding."
        )
    )
    parser.add_argument(
        "--capture-foreground",
        type=int,
        default=None,
        metavar="N",
        help=(
            "En vez del barrido completo, espera N segundos (con cuenta regresiva) para "
            "que el usuario coloque manualmente una ventana en primer plano, y luego "
            "inspecciona solo esa ventana (solo lectura; nunca envia entradas sinteticas "
            "de teclado/mouse)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the default full sweep, or --capture-foreground mode."""
    # Windows consoles often default to a legacy codepage (e.g. cp1252) that
    # cannot encode every character a window/control name may contain. Force
    # UTF-8 on stdout/stderr so real content never crashes this read-only
    # diagnostic; fall back silently if the stream doesn't support it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    args = parse_args(argv)

    if args.capture_foreground is not None:
        logger.info("F0.2D: modo --capture-foreground={}s (analisis completo)", args.capture_foreground)
        try:
            result = analyze_foreground_window(args.capture_foreground)
        except Exception as exc:
            _classify_and_log(exc, "fallo inesperado en modo --capture-foreground")
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        if result is None:
            print("ERROR: no se pudo inspeccionar la ventana en primer plano.", file=sys.stderr)
            return 1

        report = build_foreground_report(result)
        print(report)
        report_path = save_report(report)
        logger.info("Reporte de captura en primer plano guardado en {}", report_path)
        print(f"\nReporte guardado en: {report_path}")
        return 0

    logger.info("F0.2B: iniciando barrido diagnostico completo")
    try:
        report = run_full_sweep()
    except WindowNotFoundError as exc:
        logger.error("WINDOW_NOT_FOUND: no se pudo completar el barrido: {}", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado durante el barrido diagnostico completo")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(report)
    report_path = save_report(report)
    logger.info("Reporte diagnostico guardado en {}", report_path)
    print(f"\nReporte guardado en: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
