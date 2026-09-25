"""F0.2C — Read-only MSAA/IAccessible diagnostic for GO/Indigo.

Prior UIA diagnostics (inspect_go.py, diagnose_go_windows.py — both
backend="uia" and backend="win32", walk_tree depth 6+) found several
controls inside GO/Indigo ("Mis Favoritos", "Vie Finance", "Vie Clinical",
"Introduzca texto a buscar") cleanly, but NOT "Trazabilidad de Factura" or
"Consulta historias" — those two only ever surfaced via UIA's
legacy_properties() fallback, which returned "?" for every real field. A
window literally titled "BackstageViewControl1" was also observed,
suggesting a third-party owner-drawn ribbon/backstage control with poor
modern UIA support but a real MSAA/IAccessible implementation underneath.

This script talks to that older MSAA/IAccessible COM interface directly
(via comtypes + oleacc.dll), independent of pywinauto's UIA backend, to try
to identify those two controls by evidence.

MANDATORY SAFETY RULES (read-only diagnostic, no exceptions):
  - NEVER calls accDoDefaultAction() on any object. Only READS
    accDefaultAction (e.g. "Press"/"Click"/"Open") and reports it as text.
  - NEVER sends synthetic mouse or keyboard input of any kind (no
    SetCursorPos, no mouse_event/SendInput, no .click()/.click_input()/
    .invoke(), no SendKeys, no PostMessage/SendMessage with click/key/
    activate semantics). --capture-cursor and --watch-cursor only ever
    READ the cursor position via win32api.GetCursorPos() (--watch-cursor
    polls it repeatedly on a bounded interval) — neither ever moves the
    cursor.
  - Never assumes which child/index is a target: walks the real tree and
    searches by content.
  - Never closes, kills, or activates any window or process.
  - Every single MSAA property access is wrapped in its OWN individual
    try/except (see _safe_call) — COM interop is fragile (objects can
    disappear mid-walk, some accessible objects throw on property access),
    and one bad property/node must never abort the walk.

Usage:
    python scripts/inspect_go_msaa.py
    python scripts/inspect_go_msaa.py --handle 123456 --max-depth 10 --timeout 20
    python scripts/inspect_go_msaa.py --capture-cursor 8 --target trazabilidad
    python scripts/inspect_go_msaa.py --capture-cursor 8 --target historias
    python scripts/inspect_go_msaa.py --watch-cursor 30 --target-text "Trazabilidad de Factura"

F0.2F adds --watch-cursor N --target-text TEXT: a continuous "watch" mode
(polls GetCursorPos() every ~300ms for up to N seconds, still NEVER moves
the cursor) so a human can move the mouse at their own pace instead of
racing a fixed countdown. It stops automatically the instant the requested
text is found under the cursor (or in its ancestor chain) AND that object
is confirmed to belong to GO/Indigo's own process
("Vie Cloud Platform.exe") -- a match belonging to any other process
(Chrome, Warp, Explorer, ...) is logged and rejected, and polling
continues. --watch-cursor and --capture-cursor are mutually exclusive.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import byref, wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clinica_rpa.infrastructure.logging_config import setup_logging  # noqa: E402
from loguru import logger  # noqa: E402

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_HANDLE = 1967058
DEFAULT_MAX_DEPTH = 15
DEFAULT_TIMEOUT_SECONDS = 30.0

# Win32 OBJID_* constants (winuser.h). OBJID_CLIENT's natural literal
# (0xFFFFFFFC) is out of C `int` range; see get_iaccessible_for_window()
# for how that is handled safely at the ctypes call site.
OBJID_WINDOW = 0x00000000
OBJID_CLIENT = 0xFFFFFFFC

CHILDID_SELF = 0

BACKSTAGE_TITLE = "BackstageViewControl1"

TARGET_TEXTS: tuple[str, ...] = (
    "Trazabilidad de Factura",
    "Consulta historias",
    "Trazabilidad",
    "historias",
    "Mis Favoritos",
)

TARGET_LABELS: dict[str, str] = {
    "trazabilidad": "Trazabilidad de Factura",
    "historias": "Consulta historias",
}
# All texts flagged as "known target" when found in a --capture-cursor
# ancestor chain, regardless of which --target was requested.
KNOWN_TARGET_TEXTS: tuple[str, ...] = (
    "Trazabilidad de Factura",
    "Consulta historias",
    "Mis Favoritos",
)

# Rectangle overlap ratio (intersection-area / area-of-the-SMALLER-rect)
# above which two rectangles are considered "the same visual element" for
# read-only geometric correlation. Documented, conservative choice: using
# the smaller rectangle's area as the denominator (rather than the union)
# means a small MSAA leaf fully contained inside a larger UIA/win32
# container rectangle (or vice versa) still counts as correlated, which is
# common for wrapper controls.
CORRELATION_OVERLAP_THRESHOLD = 0.5

UIA_CORRELATION_MAX_DEPTH = 4
UIA_CORRELATION_TIMEOUT_SECONDS = 5.0
# Qualitative UIA control_type strings treated as "looks invokable" for the
# UIA_INVOKE method-potencial decision rule. Documented as a heuristic only
# (see decide_method) — no uia_controls import is used, this is a plain
# string check against pywinauto's element_info.control_type text.
INVOKABLE_UIA_CONTROL_TYPES: tuple[str, ...] = ("Button", "MenuItem", "Hyperlink", "SplitButton")

DEFAULT_TREE_OUTPUT_PATH = Path("runtime") / "logs" / "go_msaa_tree.txt"
DEFAULT_CONCLUSION_OUTPUT_PATH = Path("runtime") / "logs" / "go_msaa_conclusion.txt"

# --------------------------------------------------------------------------
# --watch-cursor N --target-text TEXT mode (F0.2F)
# --------------------------------------------------------------------------

# Literal filename requested by the mission (not parameterized by
# --target-text's actual value, even though --target-text is a general
# string and this mode's first real use case happens to target
# "Trazabilidad de Factura").
DEFAULT_WATCH_TARGET_OUTPUT_PATH = Path("runtime") / "logs" / "msaa_target_trazabilidad.txt"

# Deliberate, bounded, LOW-FREQUENCY diagnostic poll. 300ms sits inside the
# specced 250-400ms range: fast enough that a human moving the mouse at a
# normal pace won't visibly overshoot past the target control before the
# next sample, slow enough to stay a diagnostic sampling loop (a handful of
# COM calls per second) rather than a tight synchronization primitive. This
# value is scoped to THIS diagnostic poll only -- it is not a general
# sync/polling convention for the rest of the codebase.
WATCH_POLL_INTERVAL_SECONDS = 0.3

WATCH_ANCESTOR_MAX_LEVELS = 20

# The process that must own a --watch-cursor match for it to be ACCEPTED.
# Regla 1 (no hardcoded root handle) is honored by resolving ownership
# dynamically per match (see resolve_watch_ownership/validate_go_ownership)
# rather than trusting DEFAULT_HANDLE; this is the one identity that IS
# allowed to be a fixed literal, since "does this belong to GO/Indigo" is
# the actual acceptance criterion the mission specifies.
GO_PROCESS_NAME = "Vie Cloud Platform.exe"

# Printed (and logged) verbatim, exactly once, right before the poll loop
# starts -- the most accurate possible timing signal for the human, since
# it fires the instant polling truly begins. Hardcoded rather than built
# from --target-text: --target-text is a free-form string and composing a
# grammatically correct Spanish imperative sentence around an arbitrary
# value is unreliable, whereas this is the exact sentence the mission
# specified verbatim for this mode's real first use. The coordinator may
# also say this line separately in chat; printing it here too is harmless
# and self-documenting.
WATCH_TARGET_INSTRUCTION = "MUEVE AHORA EL CURSOR SOBRE TRAZABILIDAD DE FACTURA Y DEJALO QUIETO"


# --------------------------------------------------------------------------
# Error taxonomy (matches the style used across scripts/diagnose_go_windows.py
# and scripts/inspect_go.py): never collapse everything into one bucket.
# --------------------------------------------------------------------------


class WindowNotFoundError(Exception):
    """Raised when a target window cannot be found or resolved at all."""


class ControlNotFoundError(Exception):
    """Raised when an expected control cannot be located."""


class UiaTimeoutError(Exception):
    """Raised when a UIA operation exceeds its allotted time budget."""


class ApplicationUnresponsiveError(Exception):
    """Raised when a window exists but does not respond in time."""


class AmbiguousWindowError(Exception):
    """Raised when multiple candidates exist and cannot be auto-resolved."""


class MsaaUnavailableError(Exception):
    """Raised when the MSAA/IAccessible COM interop layer itself is unavailable."""


def _classify_and_log(exc: Exception, context: str) -> str:
    """Classify an unexpected exception into the project's error taxonomy.

    Logs one line tagged with the chosen code and returns it. Reused
    verbatim in spirit from diagnose_go_windows.py's _classify_and_log so
    generic exceptions never all collapse into a single label.
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
# ROLE_SYSTEM_* / STATE_SYSTEM_* translation tables
#
# Verified against oleacc.h / MSDN. ROLE_SYSTEM_CLOCK is 0x3D (61) — an
# initial suggestion for this table used 0x38, which is actually
# ROLE_SYSTEM_BUTTONDROPDOWN; that has been corrected here. See the final
# report for the explicit flag of this correction.
# --------------------------------------------------------------------------

ROLE_NAMES: dict[int, str] = {
    0x1: "ROLE_SYSTEM_TITLEBAR",
    0x2: "ROLE_SYSTEM_MENUBAR",
    0x3: "ROLE_SYSTEM_SCROLLBAR",
    0x4: "ROLE_SYSTEM_GRIP",
    0x5: "ROLE_SYSTEM_SOUND",
    0x6: "ROLE_SYSTEM_CURSOR",
    0x7: "ROLE_SYSTEM_CARET",
    0x8: "ROLE_SYSTEM_ALERT",
    0x9: "ROLE_SYSTEM_WINDOW",
    0xA: "ROLE_SYSTEM_CLIENT",
    0xB: "ROLE_SYSTEM_MENUPOPUP",
    0xC: "ROLE_SYSTEM_MENUITEM",
    0xD: "ROLE_SYSTEM_TOOLTIP",
    0xE: "ROLE_SYSTEM_APPLICATION",
    0xF: "ROLE_SYSTEM_DOCUMENT",
    0x10: "ROLE_SYSTEM_PANE",
    0x11: "ROLE_SYSTEM_CHART",
    0x12: "ROLE_SYSTEM_DIALOG",
    0x13: "ROLE_SYSTEM_BORDER",
    0x14: "ROLE_SYSTEM_GROUPING",
    0x15: "ROLE_SYSTEM_SEPARATOR",
    0x16: "ROLE_SYSTEM_TOOLBAR",
    0x17: "ROLE_SYSTEM_STATUSBAR",
    0x18: "ROLE_SYSTEM_TABLE",
    0x19: "ROLE_SYSTEM_COLUMNHEADER",
    0x1A: "ROLE_SYSTEM_ROWHEADER",
    0x1B: "ROLE_SYSTEM_COLUMN",
    0x1C: "ROLE_SYSTEM_ROW",
    0x1D: "ROLE_SYSTEM_CELL",
    0x1E: "ROLE_SYSTEM_LINK",
    0x1F: "ROLE_SYSTEM_HELPBALLOON",
    0x20: "ROLE_SYSTEM_CHARACTER",
    0x21: "ROLE_SYSTEM_LIST",
    0x22: "ROLE_SYSTEM_LISTITEM",
    0x23: "ROLE_SYSTEM_OUTLINE",
    0x24: "ROLE_SYSTEM_OUTLINEITEM",
    0x25: "ROLE_SYSTEM_PAGETAB",
    0x26: "ROLE_SYSTEM_PROPERTYPAGE",
    0x27: "ROLE_SYSTEM_INDICATOR",
    0x28: "ROLE_SYSTEM_GRAPHIC",
    0x29: "ROLE_SYSTEM_STATICTEXT",
    0x2A: "ROLE_SYSTEM_TEXT",
    0x2B: "ROLE_SYSTEM_PUSHBUTTON",
    0x2C: "ROLE_SYSTEM_CHECKBUTTON",
    0x2D: "ROLE_SYSTEM_RADIOBUTTON",
    0x2E: "ROLE_SYSTEM_COMBOBOX",
    0x2F: "ROLE_SYSTEM_DROPLIST",
    0x30: "ROLE_SYSTEM_PROGRESSBAR",
    0x31: "ROLE_SYSTEM_DIAL",
    0x32: "ROLE_SYSTEM_HOTKEYFIELD",
    0x33: "ROLE_SYSTEM_SLIDER",
    0x34: "ROLE_SYSTEM_SPINBUTTON",
    0x35: "ROLE_SYSTEM_DIAGRAM",
    0x36: "ROLE_SYSTEM_ANIMATION",
    0x37: "ROLE_SYSTEM_EQUATION",
    0x38: "ROLE_SYSTEM_BUTTONDROPDOWN",
    0x39: "ROLE_SYSTEM_BUTTONMENU",
    0x3A: "ROLE_SYSTEM_BUTTONDROPDOWNGRID",
    0x3B: "ROLE_SYSTEM_WHITESPACE",
    0x3C: "ROLE_SYSTEM_PAGETABLIST",
    0x3D: "ROLE_SYSTEM_CLOCK",  # corrected: 0x3D, not 0x38 (see module docstring)
    0x3E: "ROLE_SYSTEM_SPLITBUTTON",
    0x3F: "ROLE_SYSTEM_IPADDRESS",
    0x40: "ROLE_SYSTEM_OUTLINEBUTTON",
}

STATE_NAMES: dict[int, str] = {
    0x1: "STATE_SYSTEM_UNAVAILABLE",
    0x2: "STATE_SYSTEM_SELECTED",
    0x4: "STATE_SYSTEM_FOCUSED",
    0x8: "STATE_SYSTEM_PRESSED",
    0x10: "STATE_SYSTEM_CHECKED",
    0x20: "STATE_SYSTEM_MIXED",
    0x40: "STATE_SYSTEM_READONLY",
    0x80: "STATE_SYSTEM_HOTTRACKED",
    0x100: "STATE_SYSTEM_DEFAULT",
    0x200: "STATE_SYSTEM_EXPANDED",
    0x400: "STATE_SYSTEM_COLLAPSED",
    0x800: "STATE_SYSTEM_BUSY",
    0x1000: "STATE_SYSTEM_FLOATING",
    0x2000: "STATE_SYSTEM_MARQUEED",
    0x4000: "STATE_SYSTEM_ANIMATED",
    0x8000: "STATE_SYSTEM_INVISIBLE",
    0x10000: "STATE_SYSTEM_OFFSCREEN",
    0x20000: "STATE_SYSTEM_SIZEABLE",
    0x40000: "STATE_SYSTEM_MOVEABLE",
    0x80000: "STATE_SYSTEM_SELFVOICING",
    0x100000: "STATE_SYSTEM_FOCUSABLE",
    0x200000: "STATE_SYSTEM_SELECTABLE",
    0x400000: "STATE_SYSTEM_LINKED",
    0x800000: "STATE_SYSTEM_TRAVERSED",
    0x1000000: "STATE_SYSTEM_MULTISELECTABLE",
    0x2000000: "STATE_SYSTEM_EXTSELECTABLE",
    0x4000000: "STATE_SYSTEM_ALERT_LOW",
    0x8000000: "STATE_SYSTEM_ALERT_MEDIUM",
    0x10000000: "STATE_SYSTEM_ALERT_HIGH",
    0x20000000: "STATE_SYSTEM_PROTECTED",
    0x40000000: "STATE_SYSTEM_HASPOPUP",
}
STATE_SYSTEM_UNAVAILABLE = 0x1
STATE_SYSTEM_INVISIBLE = 0x8000
STATE_SYSTEM_OFFSCREEN = 0x10000


def _translate_role(raw: object) -> str:
    """Translate a raw accRole() value into a readable role name.

    Never raises. Unmapped integer roles fall back to "desconocido(0xNN)"
    per spec, instead of crashing on an unknown role.
    """
    if isinstance(raw, bool):
        return "desconocido(?)"
    if isinstance(raw, int):
        name = ROLE_NAMES.get(raw)
        return name if name else f"desconocido(0x{raw:X})"
    if isinstance(raw, str) and raw.strip():
        return raw
    return "desconocido(?)"


def _translate_state(raw: object) -> tuple[str, bool | None, bool | None]:
    """Translate a raw accState() bitmask into (readable, visible, enabled).

    Never raises. Returns ("?", None, None) when raw isn't a usable int.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return "?", None, None
    names = [name for bit, name in STATE_NAMES.items() if raw & bit]
    readable = ", ".join(names) if names else "(ninguno)"
    visible = not (raw & STATE_SYSTEM_INVISIBLE) and not (raw & STATE_SYSTEM_OFFSCREEN)
    enabled = not (raw & STATE_SYSTEM_UNAVAILABLE)
    return readable, visible, enabled


def _fmt_bool(value: bool | None) -> str:
    if value is None:
        return "?"
    return "SI" if value else "NO"


# --------------------------------------------------------------------------
# comtypes/oleacc COM interop plumbing
# --------------------------------------------------------------------------

_accessibility_module = None


def _get_accessibility_module():
    """Lazily generate/import comtypes' wrapper for oleacc.dll's IAccessible.

    Cached after the first successful call. Raises MsaaUnavailableError
    (never a bare comtypes/OSError) so every caller has one exception type
    to catch.
    """
    global _accessibility_module
    if _accessibility_module is not None:
        return _accessibility_module
    try:
        import comtypes.client

        comtypes.client.GetModule("oleacc.dll")
        from comtypes.gen import Accessibility  # type: ignore
    except Exception as exc:
        raise MsaaUnavailableError(
            f"No se pudo inicializar el modulo MSAA (oleacc.dll): {exc}"
        ) from exc
    _accessibility_module = Accessibility
    return Accessibility


def get_iaccessible_for_window(hwnd: int, obj_id: int = OBJID_CLIENT):
    """AccessibleObjectFromWindow(hwnd, obj_id) -> IAccessible pointer.

    obj_id is wrapped explicitly as ctypes.wintypes.DWORD (unsigned 32-bit):
    a plain Python int argument passed to a ctypes foreign function with no
    declared argtypes is marshaled via C `int` (signed 32-bit) by default,
    and OBJID_CLIENT's literal (0xFFFFFFFC) overflows that range. Wrapping
    it as DWORD avoids that without needing full argtypes for this COM
    out-param call. ctypes.oledll auto-raises OSError on a failed HRESULT.
    """
    Accessibility = _get_accessibility_module()
    oleacc = ctypes.oledll.oleacc
    acc_ptr = ctypes.POINTER(Accessibility.IAccessible)()
    oleacc.AccessibleObjectFromWindow(
        wintypes.HWND(hwnd),
        wintypes.DWORD(obj_id),
        byref(Accessibility.IAccessible._iid_),
        byref(acc_ptr),
    )
    return acc_ptr


def get_iaccessible_at_point(x: int, y: int):
    """AccessibleObjectFromPoint((x, y)) -> (IAccessible pointer, VARIANT child).

    READ-ONLY: only ever queries which accessible object is at a screen
    point already read via GetCursorPos(). Never moves the cursor, never
    sends input.
    """
    import comtypes.automation

    Accessibility = _get_accessibility_module()
    oleacc = ctypes.oledll.oleacc
    pt = wintypes.POINT(x, y)
    acc_ptr = ctypes.POINTER(Accessibility.IAccessible)()
    var_child = comtypes.automation.VARIANT()
    oleacc.AccessibleObjectFromPoint(pt, byref(acc_ptr), byref(var_child))
    return acc_ptr, var_child


def _safe_call(fn, default: Any = None, label: str = "") -> Any:
    """Call fn() and return its result, or `default` on ANY exception.

    Every single MSAA property access in this module goes through this (see
    _build_node_info) individually — COM interop is fragile (objects can
    disappear mid-walk, some throw on property access), and one bad
    property must never abort the rest of a node or the whole walk.
    """
    try:
        return fn()
    except Exception as exc:
        if label:
            logger.debug("Propiedad MSAA inaccesible ({}): {}", label, exc)
        return default


def _safe_get_parent(acc):
    """Best-effort accParent -> IAccessible. Never raises; None if unavailable.

    accParent is declared in the MSAA IDL as a bare [propget] with no [in]
    params, which comtypes typically exposes as a Python property
    (`acc.accParent`) rather than a method. This could not be verified live
    against a real multi-level IAccessible tree in this environment (no GUI
    windows), so both shapes are handled defensively: property access,
    zero-arg method call, and (belt-and-braces) a callable result. See the
    final report for what this means for confidence in this fallback.
    """
    Accessibility = _get_accessibility_module()
    parent_disp = None
    try:
        parent_disp = acc.accParent
    except TypeError:
        try:
            parent_disp = acc.accParent()
        except Exception:
            return None
    except Exception:
        return None
    if callable(parent_disp):
        try:
            parent_disp = parent_disp()
        except Exception:
            return None
    if parent_disp is None:
        return None
    try:
        return parent_disp.QueryInterface(Accessibility.IAccessible)
    except Exception:
        return None


def _try_get_full_child(acc, index: int):
    """Try to resolve accChild(index) into its own, distinct IAccessible.

    Returns the child's IAccessible if it is a genuine "full child" (its
    own IDispatch, distinct from the parent) — the caller should then
    recurse into it at CHILDID_SELF. Returns None if it is a "simple child"
    (no own object) — the caller must address it via the PARENT object
    using varChild=index for every subsequent call. Never raises.
    """
    Accessibility = _get_accessibility_module()
    try:
        disp = acc.accChild(index)
    except Exception as exc:
        logger.debug("accChild({}) fallo: {}", index, exc)
        return None
    if disp is None:
        return None
    try:
        return disp.QueryInterface(Accessibility.IAccessible)
    except Exception as exc:
        logger.debug("QueryInterface(IAccessible) fallo para accChild({}): {}", index, exc)
        return None


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MsaaNode:
    """Snapshot of one MSAA/IAccessible node discovered during a tree walk."""

    depth: int
    child_id: int
    name: str
    role_raw: int | str | None
    role_readable: str
    state_raw: int | None
    state_readable: str
    visible: bool | None
    enabled: bool | None
    description: str
    value: str
    default_action: str
    child_count: int
    location: tuple[int, int, int, int] | None  # (left, top, width, height), SCREEN coords
    parent_name: str
    parent_role: str


def _build_node_info(
    acc,
    child_id: int,
    depth: int,
    parent_summary: tuple[str, str] | None,
) -> MsaaNode:
    """Build an MsaaNode for (acc, child_id), wrapping every property read."""
    name = _safe_call(lambda: acc.accName(child_id), default="", label="accName") or ""
    role_raw = _safe_call(lambda: acc.accRole(child_id), default=None, label="accRole")
    role_readable = _translate_role(role_raw)
    state_raw = _safe_call(lambda: acc.accState(child_id), default=None, label="accState")
    state_readable, visible, enabled = _translate_state(state_raw)
    description = _safe_call(lambda: acc.accDescription(child_id), default="", label="accDescription") or ""
    value = _safe_call(lambda: acc.accValue(child_id), default="", label="accValue") or ""
    default_action = (
        _safe_call(lambda: acc.accDefaultAction(child_id), default="", label="accDefaultAction") or ""
    )

    child_count = 0
    if child_id == CHILDID_SELF:
        # accChildCount is only meaningful for a full object (CHILDID_SELF);
        # simple children cannot have children of their own per the MSAA
        # spec, so it's left at 0 without an extra COM call in that case.
        raw_count = _safe_call(lambda: acc.accChildCount, default=0, label="accChildCount")
        child_count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0

    raw_location = _safe_call(lambda: acc.accLocation(child_id), default=None, label="accLocation")
    location: tuple[int, int, int, int] | None = None
    if raw_location is not None:
        try:
            coords = tuple(int(v) for v in raw_location)
            if len(coords) == 4:
                location = coords  # type: ignore[assignment]
        except Exception as exc:
            logger.debug("accLocation devolvio un formato inesperado: {} ({})", raw_location, exc)

    parent_name, parent_role = parent_summary if parent_summary is not None else ("", "")

    return MsaaNode(
        depth=depth,
        child_id=child_id,
        name=str(name) if name is not None else "",
        role_raw=role_raw if isinstance(role_raw, (int, str)) else None,
        role_readable=role_readable,
        state_raw=state_raw if isinstance(state_raw, int) and not isinstance(state_raw, bool) else None,
        state_readable=state_readable,
        visible=visible,
        enabled=enabled,
        description=str(description) if description is not None else "",
        value=str(value) if value is not None else "",
        default_action=str(default_action) if default_action is not None else "",
        child_count=child_count,
        location=location,
        parent_name=parent_name,
        parent_role=parent_role,
    )


# --------------------------------------------------------------------------
# Tree walk
# --------------------------------------------------------------------------


def _walk_msaa(
    acc,
    child_id: int,
    depth: int,
    max_depth: int,
    deadline: float,
    parent_summary: tuple[str, str] | None,
    collected: list[MsaaNode],
) -> None:
    """Recursively walk one MSAA subtree, appending MsaaNode entries to collected.

    A single unreachable/inaccessible node never aborts the rest of the
    walk (every property read goes through _safe_call). Bounded by
    max_depth and a wall-clock deadline (time.monotonic() timestamp).
    """
    if time.monotonic() > deadline:
        logger.warning("Se alcanzo el timeout durante el recorrido MSAA (profundidad {})", depth)
        return

    node = _build_node_info(acc, child_id, depth, parent_summary)
    collected.append(node)

    if depth >= max_depth:
        return
    if child_id != CHILDID_SELF:
        # Simple children cannot have children of their own (MSAA spec) —
        # this branch is always a leaf.
        return

    child_count = node.child_count
    if child_count <= 0:
        return

    this_summary = (node.name, node.role_readable)
    # Child indices for accChild()/property access via a simple child are
    # 1-based (1..child_count); varChild/CHILDID_SELF for the object itself
    # is 0.
    for i in range(1, child_count + 1):
        if time.monotonic() > deadline:
            logger.warning("Se alcanzo el timeout durante el recorrido MSAA (profundidad {})", depth)
            return
        try:
            full_child = _try_get_full_child(acc, i)
        except Exception as exc:
            logger.debug("Fallo inesperado resolviendo accChild({}) en profundidad {}: {}", i, depth, exc)
            full_child = None

        try:
            if full_child is not None:
                _walk_msaa(full_child, CHILDID_SELF, depth + 1, max_depth, deadline, this_summary, collected)
            else:
                _walk_msaa(acc, i, depth + 1, max_depth, deadline, this_summary, collected)
        except Exception as exc:
            # A single bad subtree must never abort its siblings.
            logger.warning("Nodo MSAA omitido por error en profundidad {}: {}", depth + 1, exc)
            continue


def render_msaa_tree(nodes: list[MsaaNode]) -> str:
    """Render collected MsaaNodes as an indented, human-readable text tree."""
    lines = []
    for n in nodes:
        indent = "  " * n.depth
        loc = n.location if n.location is not None else "?"
        lines.append(
            f"{indent}[{n.role_readable}] name={n.name!r} child_id={n.child_id} "
            f"state=({n.state_readable}) visible={_fmt_bool(n.visible)} "
            f"enabled={_fmt_bool(n.enabled)} value={n.value!r} description={n.description!r} "
            f"default_action={n.default_action!r} children={n.child_count} "
            f"location={loc} parent=({n.parent_name!r}, {n.parent_role})"
        )
    return "\n".join(lines)


def save_tree(text: str, output_path: Path = DEFAULT_TREE_OUTPUT_PATH) -> Path:
    """Write the rendered tree to output_path, creating parent dirs as needed."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return output_path


def search_targets(nodes: list[MsaaNode]) -> dict[str, list[MsaaNode]]:
    """Case-insensitive substring search for TARGET_TEXTS across accName
    AND accDescription/accValue (bonus, per spec)."""
    matches: dict[str, list[MsaaNode]] = {t: [] for t in TARGET_TEXTS}
    for n in nodes:
        haystack = f"{n.name} {n.description} {n.value}".lower()
        for text in TARGET_TEXTS:
            if text.lower() in haystack:
                matches[text].append(n)
    return matches


def _select_for_report(nodes_matched: list[MsaaNode], target_text: str) -> list[MsaaNode]:
    """Narrow a broad (name+description+value) match list down to accName-only
    matches for the two required report blocks (spec: "exact-or-substring
    match on accName"), preferring an exact match over a substring one.
    """
    needle = target_text.lower()
    exact = [n for n in nodes_matched if n.name.strip().lower() == needle]
    if exact:
        return exact
    return [n for n in nodes_matched if needle in n.name.lower()]


# --------------------------------------------------------------------------
# BackstageViewControl1 discovery / walk-root resolution
# --------------------------------------------------------------------------


def find_backstage_hwnd(root_hwnd: int) -> int | None:
    """Light EnumChildWindows pre-pass for a descendant titled BackstageViewControl1.

    Never raises; returns None (never hard-fails) if not found or if the
    enumeration itself fails.
    """
    import win32gui

    found: list[int] = []

    def _cb(h: int, _: object) -> bool:
        try:
            title = win32gui.GetWindowText(h)
        except Exception:
            title = ""
        if title == BACKSTAGE_TITLE:
            found.append(h)
        return True

    try:
        win32gui.EnumChildWindows(root_hwnd, _cb, None)
    except Exception as exc:
        logger.debug("No se pudo enumerar hijos buscando {}: {}", BACKSTAGE_TITLE, exc)
        return None
    return found[0] if found else None


def resolve_walk_root(hwnd: int) -> tuple[Any, str]:
    """Resolve the IAccessible to start the walk from.

    Tries a BackstageViewControl1 descendant window first; falls back to
    the main window's client-area IAccessible if not found or if MSAA
    fails for it. Raises MsaaUnavailableError only if BOTH fail.
    """
    backstage_hwnd = find_backstage_hwnd(hwnd)
    if backstage_hwnd is not None:
        try:
            acc = get_iaccessible_for_window(backstage_hwnd, OBJID_CLIENT)
            return acc, f"{BACKSTAGE_TITLE}(hwnd={backstage_hwnd})"
        except Exception as exc:
            logger.warning(
                "No se pudo obtener IAccessible de {} (hwnd={}): {} -- fallback a area cliente principal",
                BACKSTAGE_TITLE,
                backstage_hwnd,
                exc,
            )

    acc = get_iaccessible_for_window(hwnd, OBJID_CLIENT)
    return acc, f"area-cliente-principal(hwnd={hwnd})"


# --------------------------------------------------------------------------
# Correlation with UIA / Win32 (read-only, purely geometric)
# --------------------------------------------------------------------------


def _rect_overlap_ratio(rect_a: tuple[int, int, int, int], rect_b: tuple[int, int, int, int]) -> float:
    """Intersection-area / area-of-the-smaller-rectangle. Both rects are
    (left, top, right, bottom) in SCREEN coordinates. See
    CORRELATION_OVERLAP_THRESHOLD for the documented correlation threshold.
    """
    l1, t1, r1, b1 = rect_a
    l2, t2, r2, b2 = rect_b
    ix1, iy1 = max(l1, l2), max(t1, t2)
    ix2, iy2 = min(r1, r2), min(b1, b2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = max(0, r1 - l1) * max(0, b1 - t1)
    area_b = max(0, r2 - l2) * max(0, b2 - t2)
    smallest = min(area_a, area_b)
    if smallest <= 0:
        return 0.0
    return inter / smallest


def correlate_win32(target_hwnd: int, msaa_location: tuple[int, int, int, int]) -> tuple[bool, str | None]:
    """Read-only geometric correlation: does any native descendant HWND of
    target_hwnd overlap msaa_location (left, top, width, height SCREEN
    coords) by >= CORRELATION_OVERLAP_THRESHOLD? Returns (correlated,
    best-matching class_name).
    """
    import win32gui

    left, top, w, h = msaa_location
    msaa_rect = (left, top, left + w, top + h)

    handles: list[int] = []
    try:
        win32gui.EnumChildWindows(target_hwnd, lambda hh, _: handles.append(hh), None)
    except Exception as exc:
        logger.debug("No se pudo enumerar ventanas hijas para correlacion win32: {}", exc)
        return False, None

    best_ratio = 0.0
    best_class: str | None = None
    for h_ in handles:
        try:
            rect = win32gui.GetWindowRect(h_)
        except Exception:
            continue
        ratio = _rect_overlap_ratio(msaa_rect, rect)
        if ratio > best_ratio:
            best_ratio = ratio
            try:
                best_class = win32gui.GetClassName(h_)
            except Exception:
                best_class = "?"

    return best_ratio >= CORRELATION_OVERLAP_THRESHOLD, best_class


def _local_uia_walk(element, depth: int, deadline: float, collected: list[tuple[str, Any]]) -> None:
    """Small, self-contained, bounded local UIA walker (not imported from
    inspect_go.py, per spec, to keep this script standalone). Read-only:
    only reads control_type and rectangle().
    """
    if depth > UIA_CORRELATION_MAX_DEPTH or time.monotonic() > deadline:
        return
    control_type = "?"
    rect = None
    try:
        control_type = element.element_info.control_type or "?"
    except Exception:
        pass
    try:
        rect = element.rectangle()
    except Exception:
        rect = None
    collected.append((control_type, rect))

    try:
        children = element.children()
    except Exception:
        return
    for child in children:
        if time.monotonic() > deadline:
            return
        _local_uia_walk(child, depth + 1, deadline, collected)


def correlate_uia(hwnd: int, msaa_location: tuple[int, int, int, int]) -> tuple[bool, str]:
    """Read-only geometric correlation against a shallow local UIA walk of
    hwnd. Returns (correlated, note) where note is
    "<control_type> (invocable|no-invocable-o-desconocido)".
    """
    left, top, w, h = msaa_location
    msaa_rect = (left, top, left + w, top + h)

    try:
        from pywinauto import Desktop

        window = Desktop(backend="uia").window(handle=hwnd)
        window.wait("exists", timeout=5)
    except Exception as exc:
        logger.debug("No se pudo conectar UIA para correlacion (hwnd={}): {}", hwnd, exc)
        return False, "? (no-invocable-o-desconocido)"

    collected: list[tuple[str, Any]] = []
    deadline = time.monotonic() + UIA_CORRELATION_TIMEOUT_SECONDS
    try:
        _local_uia_walk(window, 0, deadline, collected)
    except Exception as exc:
        logger.debug("Fallo recorrido UIA local de correlacion: {}", exc)

    best_ratio = 0.0
    best_type = "?"
    for control_type, rect in collected:
        if rect is None:
            continue
        try:
            uia_rect = (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:
            continue
        ratio = _rect_overlap_ratio(msaa_rect, uia_rect)
        if ratio > best_ratio:
            best_ratio = ratio
            best_type = control_type

    invokable_note = "invocable" if best_type in INVOKABLE_UIA_CONTROL_TYPES else "no-invocable-o-desconocido"
    return best_ratio >= CORRELATION_OVERLAP_THRESHOLD, f"{best_type} ({invokable_note})"


def decide_method(
    node: MsaaNode,
    uia_correlated: bool,
    uia_note: str,
    win32_correlated: bool,
    win32_class: str | None,
) -> str:
    """Decision rule for "metodo potencial" (report classification only —
    NEVER executed anywhere in this script):

      1. MSAA_DEFAULT_ACTION: accDefaultAction is a non-empty string
         (e.g. "Press"/"Click"/"Open"/"Select").
      2. UIA_INVOKE: no usable default action, but UIA correlation found a
         geometrically overlapping control whose control_type looks
         invokable (see INVOKABLE_UIA_CONTROL_TYPES — a qualitative note
         only, no uia_controls import needed).
      3. WIN32_CLICK: no default action and no invokable UIA correlation,
         but Win32 correlation found a real (non-"?") native window class.
      4. MOUSE_POINT_FALLBACK: none of the above, but accLocation is a
         valid, non-degenerate rectangle. Documented ONLY as a last-resort
         diagnostic classification label — never used to click anywhere in
         this script.
      5. NO_DETERMINADO: none of the above evidence is available.
    """
    if node.default_action and node.default_action.strip():
        return "MSAA_DEFAULT_ACTION"
    if uia_correlated and "invocable" in uia_note and "no-invocable" not in uia_note:
        return "UIA_INVOKE"
    if win32_correlated and win32_class and win32_class != "?":
        return "WIN32_CLICK"
    if node.location is not None and node.location[2] > 0 and node.location[3] > 0:
        return "MOUSE_POINT_FALLBACK"
    return "NO_DETERMINADO"


# --------------------------------------------------------------------------
# Report — per-target detail blocks
# --------------------------------------------------------------------------


def build_target_block(label: str, matches: list[MsaaNode], target_hwnd: int) -> str:
    """Render one TARGET LABEL block using the exact template from the spec."""
    lines = [f"{label}:"]
    if not matches:
        lines.append("- encontrado por MSAA: NO")
        return "\n".join(lines)

    node = matches[0]
    win32_correlated, win32_class = False, None
    uia_correlated, uia_note = False, "? (no-invocable-o-desconocido)"
    if node.location is not None:
        win32_correlated, win32_class = correlate_win32(target_hwnd, node.location)
        uia_correlated, uia_note = correlate_uia(target_hwnd, node.location)

    method = decide_method(node, uia_correlated, uia_note, win32_correlated, win32_class)

    lines += [
        "- encontrado por MSAA: SI",
        f"- accName: {node.name!r}",
        f"- accRole: {node.role_readable}",
        f"- accState: {node.state_readable}",
        f"- accDefaultAction: {node.default_action!r}",
        f"- rectangle: {node.location}",
        f"- child_id: {node.child_id}",
        f"- parent: {node.parent_name!r} ({node.parent_role})",
        # Only the immediate parent is tracked per node during the walk
        # (per spec: "one-level-up name/role only, don't recurse upward
        # exhaustively"); the full ancestor chain is only available for the
        # node currently under the cursor via --capture-cursor, or by
        # cross-referencing the saved tree file below.
        f"- ancestors: ver arbol completo en {DEFAULT_TREE_OUTPUT_PATH} (solo se registra 1 nivel de parent por nodo durante el recorrido)",
        f"- UIA correlation: {'SI' if uia_correlated else 'NO'} ({uia_note})",
        f"- Win32 correlation: {'SI' if win32_correlated else 'NO'} (class={win32_class})",
        "- metodo potencial:",
        f"  - {method}",
    ]
    return "\n".join(lines)


def build_mis_favoritos_block(matches: list[MsaaNode]) -> str:
    """Render the MIS FAVORITOS block using the exact template from the spec."""
    lines = ["MIS FAVORITOS:"]
    if not matches:
        lines.append("- estructura MSAA encontrada: NO")
        return "\n".join(lines)
    node = matches[0]
    lines += [
        "- estructura MSAA encontrada: SI",
        f"- role: {node.role_readable}",
        f"- children: {node.child_count}",
        f"- rectangle: {node.location}",
    ]
    return "\n".join(lines)


def classify_conclusion(walk_succeeded: bool, report_matches: dict[str, list[MsaaNode]]) -> str:
    """Overall CONCLUSION for the default tree-walk mode.

    Only ever emits MSAA_CONTROLS_IDENTIFIED, CURSOR_CAPTURE_REQUIRED, or
    REQUIERE_MAS_DIAGNOSTICO. VISUAL_FALLBACK_REQUIRED is intentionally
    NEVER emitted here: a single invocation of this script only ever runs
    ONE of (default tree-walk mode) or (--capture-cursor mode), never both
    in the same run — so a tree-walk-only invocation never has enough
    information to know whether a SEPARATE --capture-cursor invocation
    would also fail to find a target. That combined "even cursor capture
    didn't help" judgment call belongs to the human coordinator, who sees
    the output of both invocations; this script never self-assigns
    VISUAL_FALLBACK_REQUIRED.
    """
    if not walk_succeeded:
        return "REQUIERE_MAS_DIAGNOSTICO"
    both_found = bool(report_matches.get("Trazabilidad de Factura")) and bool(
        report_matches.get("Consulta historias")
    )
    if both_found:
        return "MSAA_CONTROLS_IDENTIFIED"
    return "CURSOR_CAPTURE_REQUIRED"


# --------------------------------------------------------------------------
# --capture-cursor N --target {trazabilidad,historias} mode
# --------------------------------------------------------------------------


def _countdown_wait(seconds: int, target_label: str) -> None:
    """Bounded, user-facing countdown (same pattern as
    diagnose_go_windows.py's --capture-foreground) so a human can manually
    position the cursor. Never a synchronization primitive.
    """
    print(f"En los proximos {seconds} segundos coloque el cursor ENCIMA de {target_label} SIN HACER CLIC.")
    for remaining in range(seconds, 0, -1):
        print(f"  {remaining}...")
        time.sleep(1)


def _read_cursor_pos() -> tuple[int, int] | None:
    """READ-ONLY: only ever calls win32api.GetCursorPos(). Never moves the
    cursor, never calls SetCursorPos or any input-injection API.
    """
    import win32api

    try:
        return win32api.GetCursorPos()
    except Exception as exc:
        logger.error("No se pudo leer la posicion del cursor: {}", exc)
        return None


def walk_ancestor_chain(acc, child_id: int, max_levels: int = 20) -> list[tuple[str, str]]:
    """Read-only climb via accParent, bounded to max_levels. Stops early if
    accParent fails/returns None (window-level object reached, or parents
    run out).
    """
    chain: list[tuple[str, str]] = []
    current_acc = acc

    if child_id != CHILDID_SELF:
        # A simple child has no accParent distinct from the full object
        # that returned it — that object (at CHILDID_SELF) IS its first
        # logical ancestor.
        name = _safe_call(lambda: acc.accName(CHILDID_SELF), default="", label="accName(self-as-ancestor)") or ""
        role_raw = _safe_call(lambda: acc.accRole(CHILDID_SELF), default=None, label="accRole(self-as-ancestor)")
        chain.append((str(name), _translate_role(role_raw)))

    for _ in range(max_levels):
        parent_acc = _safe_get_parent(current_acc)
        if parent_acc is None:
            break
        name = _safe_call(lambda: parent_acc.accName(CHILDID_SELF), default="", label="accName(ancestor)") or ""
        role_raw = _safe_call(lambda: parent_acc.accRole(CHILDID_SELF), default=None, label="accRole(ancestor)")
        chain.append((str(name), _translate_role(role_raw)))
        current_acc = parent_acc

    return chain


def print_capture_cursor_report(
    x: int, y: int, node: MsaaNode, ancestor_chain: list[tuple[str, str]], target: str
) -> None:
    expected_text = TARGET_LABELS[target]
    print(f"Posicion del cursor: ({x}, {y})")
    print(f"accName: {node.name!r}")
    print(f"accRole: {node.role_readable}")
    print(f"accState: {node.state_readable}")
    print(f"accDefaultAction: {node.default_action!r}")
    print(f"accLocation: {node.location}")
    print(f"child_id: {node.child_id}")
    print("Cadena de ancestros (hasta 20 niveles, via accParent):")
    if not ancestor_chain:
        print("  (sin ancestros disponibles)")
    for i, (name, role) in enumerate(ancestor_chain, start=1):
        lowered = name.lower()
        flag = ""
        if expected_text.lower() in lowered:
            flag = f"  <-- COINCIDE con el objetivo esperado ({expected_text!r})"
        elif any(t.lower() in lowered for t in KNOWN_TARGET_TEXTS):
            flag = "  <-- coincide con otro texto objetivo conocido"
        print(f"  [{i}] name={name!r} role={role}{flag}")


def run_capture_cursor(seconds: int, target: str) -> int:
    """Orchestrate --capture-cursor mode. Read-only end to end."""
    target_label = TARGET_LABELS[target]
    _countdown_wait(seconds, target_label)

    pos = _read_cursor_pos()
    if pos is None:
        print("ERROR: no se pudo leer la posicion del cursor.", file=sys.stderr)
        return 1
    x, y = pos

    try:
        acc_ptr, var_child = get_iaccessible_at_point(x, y)
    except Exception as exc:
        code = _classify_and_log(exc, "AccessibleObjectFromPoint")
        print(f"ERROR ({code}): no se pudo obtener el objeto MSAA bajo el cursor: {exc}", file=sys.stderr)
        return 1

    raw_child = _safe_call(lambda: var_child.value, default=None, label="varChild.value")
    child_id = raw_child if isinstance(raw_child, int) and not isinstance(raw_child, bool) else CHILDID_SELF

    node = _build_node_info(acc_ptr, child_id, depth=0, parent_summary=None)
    ancestor_chain = walk_ancestor_chain(acc_ptr, child_id)
    print_capture_cursor_report(x, y, node, ancestor_chain, target)
    return 0


# --------------------------------------------------------------------------
# --watch-cursor N --target-text TEXT mode (F0.2F)
#
# Continuous alternative to --capture-cursor: instead of racing a fixed
# countdown, this polls GetCursorPos() (same read-only call as
# --capture-cursor, just invoked repeatedly) roughly every
# WATCH_POLL_INTERVAL_SECONDS for up to N seconds, and stops the instant
# --target-text is found under the cursor (or in its ancestor chain) on an
# object confirmed to belong to GO/Indigo's own process. Reuses
# get_iaccessible_at_point, _build_node_info, walk_ancestor_chain,
# correlate_uia/correlate_win32 and decide_method from above -- no MSAA/UIA
# logic is duplicated here.
# --------------------------------------------------------------------------


def _normalize_for_match(text: str | None) -> str:
    """Normalize text for --watch-cursor target matching.

    Collapses ANY run of whitespace (str.split()'s default separator,
    which in Python treats Unicode space separators -- including '\\xa0'
    NBSP -- as whitespace, so real GO control text observed with embedded
    '\\r\\n'/'\\xa0' normalizes cleanly) to single spaces, strips, and
    casefolds for case-insensitive comparison. Never raises.
    """
    if not text:
        return ""
    return " ".join(text.split()).casefold()


def _collect_watch_haystack_entries(node: MsaaNode, ancestor_chain: list[tuple[str, str]]) -> list[str]:
    """Build the list of raw text entries --watch-cursor searches for a
    match: the current object's accName/accDescription/accValue, plus
    every ancestor's accName (ancestor_chain is produced by the existing
    walk_ancestor_chain climb, reused as-is).
    """
    entries = [node.name, node.description, node.value]
    entries.extend(name for name, _role in ancestor_chain)
    return entries


def _find_target_text_match(target_text: str, entries: list[str]) -> str | None:
    """Return the first raw haystack entry whose NORMALIZED text contains
    the NORMALIZED target_text as a substring (exact match counts too,
    since exact-equal strings are trivially substrings of each other).
    Returns None if no entry matches. Never raises.
    """
    needle = _normalize_for_match(target_text)
    if not needle:
        return None
    for entry in entries:
        if needle in _normalize_for_match(entry):
            return entry
    return None


def _watch_state_key(node: MsaaNode, parent_name: str) -> tuple[str, str, int, str]:
    """Change-detection key used to decide whether --watch-cursor should
    print a new line for the current poll.

    True COM identity (e.g. comparing the underlying IUnknown pointer)
    would be more rigorous, but is awkward to obtain reliably across the
    simple-child/full-child distinction this module already has to handle
    defensively. (accName, accRole readable, child_id, parent_name) is used
    instead, as a documented, simpler proxy -- sufficient to detect "the
    cursor is now over a visibly different control", which is the only
    thing this key is used for; it is never treated as a true identity
    check anywhere else in this module (see _describe_object_identity for
    the separate, best-effort debug-only identity label used in the final
    report).
    """
    return (node.name, node.role_readable, node.child_id, parent_name)


def _format_watch_timestamp() -> str:
    """HH:MM:SS.mmm local-time timestamp for a --watch-cursor change line."""
    now = time.time()
    millis = int((now % 1) * 1000)
    return f"{time.strftime('%H:%M:%S', time.localtime(now))}.{millis:03d}"


def _print_watch_change(cursor_pos: tuple[int, int], node: MsaaNode) -> None:
    """Print one changed-state line for --watch-cursor, per the exact
    format specified: a timestamp line, then cursor=, name=, role=.
    """
    x, y = cursor_pos
    print(f"[{_format_watch_timestamp()}]")
    print(f"cursor=({x},{y})")
    print(f"name={node.name!r}")
    print(f"role={node.role_readable}")


def _hwnd_from_point(x: int, y: int) -> int | None:
    """READ-ONLY: win32gui.WindowFromPoint((x, y)) -> HWND at that screen
    point, or None.

    Chosen as the PRIMARY ownership-resolution mechanism (see
    resolve_watch_ownership): it only needs the cursor coordinates already
    read via GetCursorPos(), and — unlike WindowFromAccessibleObject — does
    not depend on the specific IAccessible object resolving to a window on
    its own, which commonly fails for deeply-nested MSAA "simple children"
    that have no HWND distinct from their parent's.
    """
    import win32gui

    try:
        hwnd = win32gui.WindowFromPoint((x, y))
        return hwnd if hwnd else None
    except Exception as exc:
        logger.debug("WindowFromPoint fallo para ({}, {}): {}", x, y, exc)
        return None


def _hwnd_from_accessible(acc) -> int | None:
    """WindowFromAccessibleObject(IAccessible*, HWND*) via oleacc.dll.

    Standard, read-only oleacc API; ctypes signature declared explicitly
    here (argtypes only -- restype is left at oledll's default so its
    automatic HRESULT-failure-raises-OSError behavior, already relied on by
    get_iaccessible_for_window() above, keeps working unchanged).

    SECONDARY/fallback ownership-resolution mechanism (see
    resolve_watch_ownership and the docstring on _hwnd_from_point for why
    WindowFromPoint is tried first): many deeply-nested "simple child"
    IAccessible objects fail here because they have no HWND of their own,
    which is exactly what walking the accParent chain (see
    _resolve_hwnd_via_accessible_chain) compensates for.
    """
    try:
        func = ctypes.oledll.oleacc.WindowFromAccessibleObject
        func.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.HWND))
        hwnd_out = wintypes.HWND()
        func(acc, byref(hwnd_out))
        return int(hwnd_out.value) if hwnd_out.value else None
    except Exception as exc:
        logger.debug("WindowFromAccessibleObject fallo: {}", exc)
        return None


def _resolve_hwnd_via_accessible_chain(acc, max_levels: int = WATCH_ANCESTOR_MAX_LEVELS) -> int | None:
    """Fallback ownership resolution: try WindowFromAccessibleObject on the
    matched object itself (acc is always a full IDispatch/IAccessible
    object here, even when addressing a "simple child" via varChild), then
    climb accParent (same bounded, defensive pattern as
    walk_ancestor_chain/_safe_get_parent) until one resolves or the chain
    is exhausted. Never raises; returns None if nothing resolves.
    """
    hwnd = _hwnd_from_accessible(acc)
    if hwnd is not None:
        return hwnd
    current_acc = acc
    for _ in range(max_levels):
        parent_acc = _safe_get_parent(current_acc)
        if parent_acc is None:
            return None
        hwnd = _hwnd_from_accessible(parent_acc)
        if hwnd is not None:
            return hwnd
        current_acc = parent_acc
    return None


def resolve_watch_ownership(acc, cursor_pos: tuple[int, int]) -> int | None:
    """Resolve the HWND that owns the object currently under the cursor for
    --watch-cursor's process-ownership validation. PRIMARY:
    _hwnd_from_point(cursor_pos). FALLBACK: _hwnd_from_accessible climbing
    the accParent chain, used only if WindowFromPoint itself is
    unavailable/fails.
    """
    x, y = cursor_pos
    hwnd = _hwnd_from_point(x, y)
    if hwnd is not None:
        return hwnd
    return _resolve_hwnd_via_accessible_chain(acc)


def _resolve_root_hwnd(hwnd: int) -> int:
    """Walk up to the top-level ancestor window via GetAncestor(GA_ROOT)
    (same read-only pattern already used in diagnose_go_windows.py).
    Regla 1: this is resolved fresh per match, never assumed to be
    DEFAULT_HANDLE / 1967058. Falls back to hwnd itself on any failure.
    """
    import win32con
    import win32gui

    try:
        root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
        return root if root else hwnd
    except Exception as exc:
        logger.debug("GetAncestor(GA_ROOT) fallo para hwnd={}: {}", hwnd, exc)
        return hwnd


def _resolve_process_info(hwnd: int) -> tuple[int | None, str]:
    """(pid, process_name) for the process owning hwnd, via
    win32process.GetWindowThreadProcessId + psutil.Process(pid).name() --
    same pattern already used in inspect_go.py/diagnose_go_windows.py.
    process_name is "" and/or pid is None on any resolution failure; never
    raises.
    """
    import win32process

    try:
        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception as exc:
        logger.debug("GetWindowThreadProcessId fallo para hwnd={}: {}", hwnd, exc)
        return None, ""
    try:
        import psutil

        name = psutil.Process(pid).name()
    except Exception as exc:
        logger.debug("No se pudo resolver el nombre del proceso para pid={}: {}", pid, exc)
        return pid, ""
    return pid, name or ""


def _resolve_window_class(hwnd: int) -> str:
    """win32gui.GetClassName(hwnd), defaulting to '?' on any failure."""
    import win32gui

    try:
        return win32gui.GetClassName(hwnd) or "?"
    except Exception:
        return "?"


@dataclass(frozen=True)
class WatchOwnership:
    """Resolved process-ownership evidence for a --watch-cursor match."""

    matched_hwnd: int | None
    root_hwnd: int
    pid: int | None
    process_name: str
    window_class: str
    is_go: bool


def validate_go_ownership(acc, cursor_pos: tuple[int, int]) -> WatchOwnership:
    """Resolve process ownership for the object currently under the cursor
    and decide whether it belongs to GO/Indigo (GO_PROCESS_NAME =
    "Vie Cloud Platform.exe"). Regla 1: the owning process/root window are
    resolved DYNAMICALLY per match here -- DEFAULT_HANDLE/1967058 is never
    consulted. Never raises; degrades to is_go=False (never accepted) on
    any resolution failure, since an unresolved owner must never be
    silently treated as a GO match.
    """
    matched_hwnd = resolve_watch_ownership(acc, cursor_pos)
    if matched_hwnd is None:
        return WatchOwnership(None, 0, None, "", "?", False)
    root_hwnd = _resolve_root_hwnd(matched_hwnd)
    pid, process_name = _resolve_process_info(root_hwnd)
    window_class = _resolve_window_class(root_hwnd)
    is_go = process_name.strip().lower() == GO_PROCESS_NAME.lower()
    return WatchOwnership(matched_hwnd, root_hwnd, pid, process_name, window_class, is_go)


def _describe_object_identity(acc, node: MsaaNode) -> str:
    """Best-effort, DEBUG-ONLY identity label for the matched object in the
    final report (included per spec "for debugging"). Tries
    ctypes.addressof() on the underlying COM pointer for a real memory
    address; falls back to a descriptive (role, child_id) tag if that
    isn't available (e.g. a "simple child" addressed via the parent
    object). Never used as an actual identity check anywhere in this
    module -- see _watch_state_key for the documented, simpler proxy used
    for change-detection during the poll loop.
    """
    try:
        return f"0x{ctypes.addressof(acc.contents):X}"
    except Exception:
        return f"{node.role_readable}#child_id={node.child_id}"


def save_watch_target_report(text: str, output_path: Path = DEFAULT_WATCH_TARGET_OUTPUT_PATH) -> Path:
    """Persist the final --watch-cursor match report. Mirrors save_tree()'s
    existing pattern (create parent dirs, UTF-8 write)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return output_path


def _render_watch_match_report(
    cursor_pos: tuple[int, int],
    node: MsaaNode,
    ancestor_chain: list[tuple[str, str]],
    parent_name: str,
    ownership: WatchOwnership,
    target_text: str,
    acc,
) -> str:
    """Render the final TRAZABILIDAD DE FACTURA report block, using the
    exact structure from the spec. UIA/Win32 correlation and
    metodo-potencial reuse the existing correlate_uia/correlate_win32/
    decide_method helpers unchanged -- no duplicated logic.
    """
    parent_role = ancestor_chain[0][1] if ancestor_chain else ""
    ancestors_text = (
        "; ".join(f"{i}:{name!r}({role})" for i, (name, role) in enumerate(ancestor_chain, start=1))
        if ancestor_chain
        else "(sin ancestros disponibles)"
    )

    uia_correlated, uia_note = False, "? (no-invocable-o-desconocido)"
    win32_correlated, win32_class = False, None
    if node.location is not None and ownership.root_hwnd:
        uia_correlated, uia_note = correlate_uia(ownership.root_hwnd, node.location)
        win32_correlated, win32_class = correlate_win32(ownership.root_hwnd, node.location)

    raw_method = decide_method(node, uia_correlated, uia_note, win32_correlated, win32_class)
    # decide_method()'s canonical internal labels (MSAA_DEFAULT_ACTION /
    # UIA_INVOKE / WIN32_CLICK / MOUSE_POINT_FALLBACK / NO_DETERMINADO) stay
    # unchanged everywhere else in this file (e.g. build_target_block()
    # above); only THIS report's rendering maps the two labels the spec
    # spelled differently here ("WIN32" / "MOUSE_FALLBACK") to match the
    # exact text requested for this specific block.
    method_display = {
        "WIN32_CLICK": "WIN32",
        "MOUSE_POINT_FALLBACK": "MOUSE_FALLBACK",
    }.get(raw_method, raw_method)

    x, y = cursor_pos
    lines = [
        "TRAZABILIDAD DE FACTURA:",
        "- identificado: SI",
        f"- cursor: ({x}, {y})",
        f"- accName: {node.name!r}",
        f"- accRole: {node.role_readable}",
        f"- accState: {node.state_readable}",
        f"- accDefaultAction: {node.default_action!r}",
        f"- rectangle: {node.location}",
        f"- child_id: {node.child_id}",
        f"- parent: {parent_name!r} ({parent_role})",
        f"- ancestors: {ancestors_text}",
        f"- root handle: {ownership.root_hwnd}",
        f"- PID: {ownership.pid}",
        f"- process: {ownership.process_name!r}",
        f"- UIA correlation: {'SI' if uia_correlated else 'NO'} ({uia_note})",
        f"- Win32 correlation: {'SI' if win32_correlated else 'NO'} (class={win32_class})",
        # Supplementary evidence from the mission's "gather full evidence"
        # step, appended AFTER (not inside) the labeled block above so that
        # block's own order/labels stay exactly as specified.
        f"- accDescription: {node.description!r}",
        f"- accValue: {node.value!r}",
        f"- object identity: {_describe_object_identity(acc, node)}",
        f"- window class (root): {ownership.window_class}",
        f"- target-text buscado: {target_text!r}",
        "",
        "METODO POTENCIAL DE INTERACCION:",
        f"- {method_display}",
        "",
        "CONCLUSION:",
        "TRAZABILIDAD_MSAA_IDENTIFICADA",
    ]
    return "\n".join(lines)


def _report_watch_match(
    cursor_pos: tuple[int, int],
    node: MsaaNode,
    ancestor_chain: list[tuple[str, str]],
    parent_name: str,
    ownership: WatchOwnership,
    target_text: str,
    acc,
) -> None:
    """Render, print, and save the final --watch-cursor match report."""
    report_text = _render_watch_match_report(
        cursor_pos, node, ancestor_chain, parent_name, ownership, target_text, acc
    )
    print(f"\n{report_text}")
    output_path = save_watch_target_report(report_text)
    logger.info(
        "Coincidencia de --watch-cursor confirmada (GO/Indigo, pid={}); reporte guardado en {}",
        ownership.pid,
        output_path,
    )


def run_watch_cursor(seconds: float, target_text: str) -> int:
    """Orchestrate --watch-cursor mode. READ-ONLY end to end: only ever
    calls win32api.GetCursorPos() repeatedly via _read_cursor_pos() (same
    call --capture-cursor already uses); never moves the cursor, never
    sends synthetic input, never calls accDoDefaultAction/Invoke/Select/
    Focus on anything.
    """
    logger.info("F0.2F: modo --watch-cursor={}s target-text={!r}", seconds, target_text)
    print(WATCH_TARGET_INSTRUCTION)

    deadline = time.monotonic() + seconds
    last_key: tuple[str, str, int, str] | None = None

    while time.monotonic() < deadline:
        pos = _read_cursor_pos()
        if pos is None:
            time.sleep(WATCH_POLL_INTERVAL_SECONDS)
            continue
        x, y = pos

        try:
            acc_ptr, var_child = get_iaccessible_at_point(x, y)
        except Exception as exc:
            _classify_and_log(exc, "AccessibleObjectFromPoint (--watch-cursor)")
            time.sleep(WATCH_POLL_INTERVAL_SECONDS)
            continue

        raw_child = _safe_call(lambda: var_child.value, default=None, label="varChild.value")
        child_id = raw_child if isinstance(raw_child, int) and not isinstance(raw_child, bool) else CHILDID_SELF

        node = _build_node_info(acc_ptr, child_id, depth=0, parent_summary=None)
        ancestor_chain = walk_ancestor_chain(acc_ptr, child_id, max_levels=WATCH_ANCESTOR_MAX_LEVELS)
        parent_name = ancestor_chain[0][0] if ancestor_chain else ""

        key = _watch_state_key(node, parent_name)
        if key != last_key:
            _print_watch_change(pos, node)
            last_key = key

        entries = _collect_watch_haystack_entries(node, ancestor_chain)
        matched_entry = _find_target_text_match(target_text, entries)
        if matched_entry is not None:
            ownership = validate_go_ownership(acc_ptr, pos)
            if ownership.is_go:
                _report_watch_match(pos, node, ancestor_chain, parent_name, ownership, target_text, acc_ptr)
                return 0
            logger.info(
                "Coincidencia de texto encontrada pero RECHAZADA (proceso={!r}, pid={}): {!r}",
                ownership.process_name,
                ownership.pid,
                matched_entry,
            )

        time.sleep(WATCH_POLL_INTERVAL_SECONDS)

    # Regla 14: a plain timeout with no accepted match is a normal negative
    # result, NEVER SECURITY_OR_AUTHORIZATION_BLOCK and never a crash.
    logger.info("--watch-cursor: tiempo agotado ({}s) sin encontrar coincidencia valida de GO/Indigo", seconds)
    print("\nCONCLUSION:")
    print("TRAZABILIDAD_NO_IDENTIFICADA")
    return 0


# --------------------------------------------------------------------------
# Default tree-walk mode orchestration
# --------------------------------------------------------------------------


def run_tree_walk(handle: int, max_depth: int, timeout: float) -> int:
    logger.info(
        "Iniciando recorrido MSAA (handle={}, max_depth={}, timeout={}s)", handle, max_depth, timeout
    )
    start = time.monotonic()

    try:
        acc, root_source = resolve_walk_root(handle)
    except Exception as exc:
        code = _classify_and_log(exc, f"no se pudo obtener ningun IAccessible para handle={handle}")
        print(f"ERROR ({code}): no se pudo obtener ningun objeto MSAA para la ventana: {exc}", file=sys.stderr)
        conclusion = classify_conclusion(False, {})
        print(f"\nCONCLUSION:\n- {conclusion}")
        _save_conclusion(conclusion)
        return 1

    logger.info("Raiz de recorrido MSAA resuelta: {}", root_source)

    deadline = start + timeout
    collected: list[MsaaNode] = []
    walk_succeeded = False
    try:
        _walk_msaa(acc, CHILDID_SELF, 0, max_depth, deadline, None, collected)
        walk_succeeded = True
    except Exception as exc:
        _classify_and_log(exc, "fallo inesperado durante el recorrido MSAA")
        # Partial results (if any were collected before the failure) still
        # count as "the walk itself worked" for CONCLUSION purposes — only
        # a total inability to get any IAccessible at all (handled above)
        # is REQUIERE_MAS_DIAGNOSTICO.
        walk_succeeded = bool(collected)

    duration = time.monotonic() - start
    logger.info(
        "Recorrido MSAA completado: {} nodos en {:.2f}s (root={})", len(collected), duration, root_source
    )

    tree_text = render_msaa_tree(collected)
    output_path = save_tree(tree_text)
    logger.info("Arbol MSAA guardado en {}", output_path)

    matches_raw = search_targets(collected)
    traz_report = _select_for_report(matches_raw.get("Trazabilidad de Factura", []), "Trazabilidad de Factura")
    hist_report = _select_for_report(matches_raw.get("Consulta historias", []), "Consulta historias")
    mis_favoritos = matches_raw.get("Mis Favoritos", [])

    print(tree_text)
    print(f"\n(Arbol guardado en: {output_path})\n")

    print(build_target_block("TRAZABILIDAD DE FACTURA", traz_report, handle))
    print()
    print(build_target_block("CONSULTA HISTORIAS", hist_report, handle))
    print()
    print(build_mis_favoritos_block(mis_favoritos))
    print()

    conclusion = classify_conclusion(
        walk_succeeded, {"Trazabilidad de Factura": traz_report, "Consulta historias": hist_report}
    )
    print("CONCLUSION:")
    print(f"- {conclusion}")
    _save_conclusion(conclusion)

    return 0


def _save_conclusion(conclusion: str, output_path: Path = DEFAULT_CONCLUSION_OUTPUT_PATH) -> None:
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(conclusion, encoding="utf-8")
    except Exception as exc:
        logger.debug("No se pudo guardar la conclusion en {}: {}", output_path, exc)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "F0.2C: diagnostico MSAA/IAccessible de solo lectura para identificar "
            "'Trazabilidad de Factura' y 'Consulta historias' en GO/Indigo."
        )
    )
    parser.add_argument(
        "--handle",
        type=int,
        default=DEFAULT_HANDLE,
        help=f"HWND de la ventana a inspeccionar (default: {DEFAULT_HANDLE}).",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_MAX_DEPTH,
        help=f"Profundidad maxima del recorrido MSAA (default: {DEFAULT_MAX_DEPTH}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Presupuesto de tiempo en segundos para el recorrido (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--capture-cursor",
        type=int,
        default=None,
        metavar="N",
        help=(
            "En vez del recorrido del arbol, espera N segundos (con cuenta regresiva) y luego "
            "lee SOLO la posicion del cursor (solo lectura; nunca mueve el cursor ni envia clicks "
            "o teclas). Requiere --target."
        ),
    )
    parser.add_argument(
        "--target",
        choices=["trazabilidad", "historias"],
        default=None,
        help="Obligatorio junto con --capture-cursor: que boton se espera bajo el cursor.",
    )
    parser.add_argument(
        "--watch-cursor",
        type=float,
        default=None,
        metavar="N",
        help=(
            "Modo continuo (F0.2F): en vez de una cuenta regresiva fija, escucha la posicion del "
            "cursor (solo lectura; nunca mueve el cursor ni envia clicks o teclas) durante hasta N "
            "segundos y se detiene automaticamente en cuanto detecta --target-text bajo el cursor "
            "(o en su cadena de ancestros) perteneciendo al proceso de GO/Indigo. Requiere "
            "--target-text. Mutuamente excluyente con --capture-cursor."
        ),
    )
    parser.add_argument(
        "--target-text",
        type=str,
        default=None,
        metavar="TEXT",
        help="Obligatorio junto con --watch-cursor: texto a buscar (comparacion normalizada por substring).",
    )

    args = parser.parse_args(argv)
    if args.capture_cursor is not None and args.target is None:
        parser.error("--target es obligatorio cuando se usa --capture-cursor.")
    if args.watch_cursor is not None and args.capture_cursor is not None:
        parser.error("--watch-cursor y --capture-cursor son mutuamente excluyentes; use solo uno por ejecucion.")
    if args.watch_cursor is not None and args.target_text is None:
        parser.error("--target-text es obligatorio cuando se usa --watch-cursor.")
    return args


def main(argv: list[str] | None = None) -> int:
    # Windows consoles often default to a legacy codepage (e.g. cp1252) that
    # cannot encode every character a control name may contain. Force
    # UTF-8 on stdout/stderr so real MSAA content never crashes this
    # read-only diagnostic; fall back silently if unsupported.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    args = parse_args(argv)

    if args.watch_cursor is not None:
        logger.info("F0.2F: modo --watch-cursor={}s target-text={!r}", args.watch_cursor, args.target_text)
        try:
            return run_watch_cursor(args.watch_cursor, args.target_text)
        except Exception as exc:
            code = _classify_and_log(exc, "fallo inesperado en modo --watch-cursor")
            print(f"ERROR ({code}): {exc}", file=sys.stderr)
            return 1

    if args.capture_cursor is not None:
        logger.info("F0.2C: modo --capture-cursor={}s target={}", args.capture_cursor, args.target)
        try:
            return run_capture_cursor(args.capture_cursor, args.target)
        except Exception as exc:
            code = _classify_and_log(exc, "fallo inesperado en modo --capture-cursor")
            print(f"ERROR ({code}): {exc}", file=sys.stderr)
            return 1

    logger.info("F0.2C: modo recorrido MSAA (handle={})", args.handle)
    try:
        return run_tree_walk(args.handle, args.max_depth, args.timeout)
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado durante el recorrido MSAA")
        print(f"ERROR ({code}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
