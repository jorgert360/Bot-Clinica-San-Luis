"""F0.4 — Read-only diagnostic: locate the "N Factura" label, its Edit input,
and a small button/icon immediately to its right, inside GO/Indigo's
"Trazabilidad de Factura" screen.

This script is a Phase-0 PoC diagnostic. It performs ZERO interaction with
the target application: no clicking, typing, focusing, invoking, or
mouse/keyboard synthesis of any kind (Regla 5, Regla 12,
03_CLAUDE_RULES.md). It only enumerates window trees (UIA + Win32) and reads
properties (text, class name, rectangle, enabled/visible state, tooltip)
to identify which controls correspond to:

  - LABEL:  the "N Factura" text label.
  - INVOICE_INPUT: the Edit-like control immediately to the label's right.
  - INVOICE_ACTION_CANDIDATE: a small button/icon immediately to the
    input's right (existence/location/identity only -- this script never
    assumes/asserts what that control DOES).

Never hardcodes a PID/handle: GO's authenticated window is resolved fresh
every run via find_authenticated_go_window() (poc_move_to_trazabilidad.py),
imported directly rather than reimplemented (Regla 1: never assume a
previously-seen PID/handle is still valid).

Privacy (mandatory): this script NEVER logs or persists an actual invoice
number, patient data, or credentials. Any Edit's window text is recorded
only as a boolean has_text; a printed/logged value is always redacted to
"<non-empty>". The output JSON contains only structural metadata (control
type, class name, automation_id, spatial relationships, and handles
explicitly labeled session-only diagnostic data).

Usage:
    python scripts/inspect_trazabilidad_form.py
    python scripts/inspect_trazabilidad_form.py --max-depth 10 --timeout 30
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
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
    DIAGNOSTIC_SCAN_MAX_DEPTH,
    DIAGNOSTIC_SCAN_TIMEOUT_SECONDS,
    WindowNotFoundError,
    _classify_and_log,
    _descendant_handles,
    enumerate_all_windows,
    find_target_process_ids,
)
from inspect_go import ControlInfo, walk_tree  # noqa: E402
from poc_move_to_trazabilidad import (  # noqa: E402
    GoAmbiguousError,
    find_authenticated_go_window,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Reused as-is from diagnose_go_windows.py (DIAGNOSTIC_SCAN_MAX_DEPTH /
# DIAGNOSTIC_SCAN_TIMEOUT_SECONDS): both were already tuned for a single
# deliberately-selected window scanned in isolation, which is exactly this
# script's use case (one confirmed GO window, one confirmed screen).

OUTPUT_PATH = Path("runtime") / "state" / "trazabilidad_selectors.json"

# Accepted "N Factura" label variants (Regla 1: never assume a single
# encoding of "N°"/"Nº" survives console/log/JSON round-tripping intact).
# All compared case-insensitively and whitespace-normalized via
# _normalize_for_match().
LABEL_VARIANTS: tuple[str, ...] = (
    "n° factura",
    "nº factura",
    "no factura",
    "n factura",
)
# Weaker fallback signal for Step 2 only (per mission spec): a bare
# "factura" substring, considered only if none of LABEL_VARIANTS match.
LABEL_WEAK_FALLBACK = "factura"

# Step 1 gate strings (full strings required for confidence -- never bare
# "Factura" for this gate).
GATE_TRAZABILIDAD = "trazabilidad de factura"
GATE_INFORMACION_FACTURA = "informacion factura"  # accent-insensitive compare below
GATE_LABEL_VARIANTS = LABEL_VARIANTS

# --- Step 3: INVOICE_INPUT spatial-matching tolerances -------------------
# Horizontal: the Edit's left edge must be at or to the right of the
# label's right edge, allowing a small negative tolerance so a label whose
# rendered rectangle very slightly overlaps its adjacent input (common with
# WinForms auto-sized labels that include a couple of px of internal
# padding) is not incorrectly excluded. -5px is a conservative, narrow
# allowance -- wide enough to absorb that padding, narrow enough that it
# could never accidentally include a genuinely separate, non-adjacent
# control positioned further left.
EDIT_LEFT_TOLERANCE_PX = -5
# Vertical: the Edit's vertical center must fall within this many px of the
# label's vertical center. +/-10px comfortably covers the few-px baseline
# offset typical of a WinForms label vs. an adjacent same-row TextBox
# (different control heights, different internal padding) while still
# rejecting a control from a different row entirely (typical WinForms row
# height in this app is 25-30px+, well above 2x this tolerance).
VERTICAL_CENTER_TOLERANCE_PX = 10

# --- Step 4: INVOICE_ACTION_CANDIDATE proximity/size thresholds ----------
# Proximity: the action candidate's left edge must be between MIN and 40px
# to the right of the input's right edge. Real-world finding (live GO
# session, F0.3C): this app's icon-button sits only 3px right of the
# input's right edge -- essentially flush/adjacent, a typical DevExpress
# "repository button" rendered right at the editor's border. MIN was
# originally 5px on the assumption that anything closer must be a rendering
# artifact of the input itself; that assumption didn't hold here, so MIN is
# lowered to comfortably admit a flush-adjacent real button while still
# requiring a non-negative gap (a truly overlapping rect is still excluded,
# since that would mean the "candidate" is actually part of the input's own
# bounds, not a separate control). 40px upper bound unchanged: generous
# enough for a typical WinForms margin/padding gap, well short of the
# distance to an unrelated, further-away control.
ACTION_PROXIMITY_MIN_PX = 0
ACTION_PROXIMITY_MAX_PX = 40
# Size: a "small" action control is defined as width < 40px (an icon-sized
# button is typically 16-32px square; 40px leaves headroom without being
# wide enough to admit a normal full-width "Buscar" text button) and
# height <= the input's own height (an inline icon/button next to a text
# field is never taller than the field itself in this app's WinForms
# layout).
ACTION_MAX_WIDTH_PX = 40
# Embedded-button variant (see _is_embedded_at_right_edge): how close the
# candidate's right edge must be to the input's own right edge to count as
# "embedded at the edge" rather than a coincidence. Real-world finding: the
# observed button.right == input.right EXACTLY (0px delta); a few px of
# slack is kept for rounding/rendering variance across runs.
EMBEDDED_RIGHT_EDGE_TOLERANCE_PX = 5


class TrazabilidadNotActiveError(Exception):
    """Raised (and handled as a clean abort) when Step 1's gate fails."""


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
class FoundControl:
    """One located control, backend-tagged, with structural-only metadata.

    text_has_content/text_redacted implement the mandatory privacy rule:
    the actual window text is NEVER stored here, only a boolean and a
    fixed redaction placeholder.
    """

    backend: str  # "uia" or "win32"
    handle: int | None  # session-only diagnostic data; never a stable selector
    name: str  # control/window "name" (UIA) or window title (win32) -- for
    # LABEL this may legitimately carry the label's own static text (not
    # sensitive data); for INVOICE_INPUT/ACTION this must never carry an
    # actual invoice/patient value (see text_has_content/text_redacted).
    class_name: str
    control_type: str  # UIA control_type, or "?" for win32-only finds
    automation_id: str  # UIA automation_id, or "" for win32-only finds
    control_id: int | None  # win32 GetDlgCtrlID(), best-effort
    rect: Rect
    parent_handle: int | None
    enabled: bool | None
    visible: bool | None
    text_has_content: bool
    text_redacted: str  # always "" or "<non-empty>" -- never the real value
    tooltip: str | None = None


# --------------------------------------------------------------------------
# Small local helpers (fresh, per mission spec -- not imported from
# inspect_go_msaa.py; mirrors its normalization/substring-search pattern).
# --------------------------------------------------------------------------


def _normalize_for_match(text: str | None) -> str:
    """Whitespace-normalize and casefold text for tolerant matching.

    Collapses any run of whitespace to a single space, strips, and
    casefolds -- so "N°  Factura\\r\\n" and "n° factura" compare equal.
    Never raises.
    """
    if not text:
        return ""
    return " ".join(text.split()).casefold()


def _matches_any(haystack: str, needles: tuple[str, ...]) -> bool:
    """True if any needle appears in haystack at a word boundary.

    Real-world finding (live GO session, F0.3C): a plain substring check
    let the short fallback variant "n factura" match INSIDE unrelated text
    like "1. Información Factura" (the last letter of "información" is "n",
    immediately followed by " factura" -- a spurious substring hit with no
    word boundary). An ASCII-only `[a-z0-9]` lookbehind is NOT enough to
    block this: the preceding character is the accented "ó", which isn't in
    that class, so the ASCII check still let it through. `\\w` (Unicode word
    character, the default for str patterns in Python 3) correctly treats
    "ó" as a letter and blocks the match, while still matching a genuine
    standalone "N Factura" label.
    """
    normalized = _normalize_for_match(haystack)
    for needle in needles:
        pattern = r"(?<!\w)" + re.escape(needle)
        if re.search(pattern, normalized):
            return True
    return False


def _redact_text(has_text: bool) -> str:
    """Never return the real value -- only "" or the fixed placeholder."""
    return "<non-empty>" if has_text else ""


# --------------------------------------------------------------------------
# Step 1: validate the Trazabilidad screen is active
# --------------------------------------------------------------------------


def _gate_confirmed(controls: list[ControlInfo]) -> bool:
    """Confirm the UIA tree contains ALL of: "Trazabilidad de Factura",
    "Informacion Factura" (accent-insensitive), and an accepted "N Factura"
    variant. Bare "Factura" alone never satisfies this gate.
    """
    haystacks = [_normalize_for_match(f"{c.name} {c.automation_id}") for c in controls]

    found_trazabilidad = any(GATE_TRAZABILIDAD in h for h in haystacks)
    # "Informacion Factura" vs "Información Factura": normalize away the
    # accent by comparing both the accented and unaccented spellings.
    found_informacion = any(
        ("informacion factura" in h) or ("información factura" in h) for h in haystacks
    )
    found_n_factura = any(any(v in h for v in GATE_LABEL_VARIANTS) for h in haystacks)

    logger.info(
        "PASO 1 (gate): trazabilidad={} informacion_factura={} n_factura_variant={}",
        found_trazabilidad,
        found_informacion,
        found_n_factura,
    )
    return found_trazabilidad and found_informacion and found_n_factura


def validate_screen_active(
    go_handle: int, max_depth: int, timeout_seconds: float
) -> list[ControlInfo]:
    """Run the Step-1 UIA scan and gate check.

    Raises:
        TrazabilidadNotActiveError: the gate did not pass -- an expected,
            clean abort path (never a crash).
    """
    from pywinauto import Desktop

    try:
        element = Desktop(backend="uia").window(handle=go_handle)
        element.wait("exists", timeout=5)
    except Exception as exc:
        _classify_and_log(exc, f"fallo al conectar UIA a la ventana GO {go_handle}")
        raise TrazabilidadNotActiveError(str(exc)) from exc

    deadline = time.monotonic() + timeout_seconds
    try:
        controls = walk_tree(element, max_depth=max_depth, deadline=deadline)
    except Exception as exc:
        _classify_and_log(exc, f"fallo durante recorrido UIA de la ventana GO {go_handle}")
        raise TrazabilidadNotActiveError(str(exc)) from exc

    if not _gate_confirmed(controls):
        raise TrazabilidadNotActiveError(
            "La pantalla 'Trazabilidad de Factura' no esta confirmada como activa."
        )
    return controls


# --------------------------------------------------------------------------
# Step 2: locate the "N Factura" label (UIA + Win32)
# --------------------------------------------------------------------------


def find_label_uia(controls: list[ControlInfo], include_weak: bool) -> list[ControlInfo]:
    """Search Step 1's already-collected controls for the "N Factura" label.

    Accepts the documented variants (strong); a bare "Factura" substring
    (weak) is only included when `include_weak` is True. Real-world finding
    (F0.3C, live GO session): the real label is UIA-only (a DevExpress
    "DataItem" with no matching Win32 window), so a per-backend "fall back
    to weak if THIS backend found no strong match" policy let the Win32 side
    always fall back to the noisy weak search even when UIA already found
    the real strong match. The caller now decides `include_weak` ONCE,
    after combining strong results from BOTH backends (see run()).
    """
    strong = [c for c in controls if _matches_any(c.name, LABEL_VARIANTS)]
    if not include_weak:
        return strong
    weak = [c for c in controls if LABEL_WEAK_FALLBACK in _normalize_for_match(c.name)]
    return weak


def find_label_win32(go_handle: int, include_weak: bool) -> list[FoundControl]:
    """Search every descendant HWND of go_handle for the "N Factura" label
    via GetWindowText()/GetClassName(). Same matching rules as UIA; see
    find_label_uia's docstring for why `include_weak` is caller-decided.
    """
    import win32gui

    matches: list[FoundControl] = []
    handles = _descendant_handles(go_handle)

    texts: list[tuple[int, str]] = []
    for h in handles:
        try:
            text = win32gui.GetWindowText(h)
        except Exception as exc:
            logger.debug("No se pudo leer window text de {}: {}", h, exc)
            continue
        texts.append((h, text))

    if include_weak:
        candidates = [(h, t) for h, t in texts if LABEL_WEAK_FALLBACK in _normalize_for_match(t)]
    else:
        candidates = [(h, t) for h, t in texts if _matches_any(t, LABEL_VARIANTS)]

    for h, text in candidates:
        try:
            class_name = win32gui.GetClassName(h)
        except Exception:
            class_name = "?"
        try:
            left, top, right, bottom = win32gui.GetWindowRect(h)
        except Exception:
            continue
        try:
            parent = win32gui.GetParent(h) or None
        except Exception:
            parent = None
        matches.append(
            FoundControl(
                backend="win32",
                handle=h,
                name=text,
                class_name=class_name,
                control_type="?",
                automation_id="",
                control_id=_best_effort_control_id(h),
                rect=Rect(left, top, right, bottom),
                parent_handle=parent,
                enabled=_best_effort_enabled(h),
                visible=_best_effort_visible(h),
                text_has_content=bool(text.strip()),
                text_redacted="",  # label text itself is not sensitive data
            )
        )
    return matches


def _control_info_rect(c: ControlInfo) -> Rect | None:
    """Best-effort parse of ControlInfo.rectangle's pywinauto str() form,
    e.g. "(L100, T200, R300, B400)". Returns None if unparsable.
    """
    text = c.rectangle
    try:
        parts = text.strip("()").split(",")
        values = {}
        for part in parts:
            part = part.strip()
            key, value = part[0], part[1:]
            values[key] = int(value)
        return Rect(values["L"], values["T"], values["R"], values["B"])
    except Exception:
        return None


def _uia_control_to_found(c: ControlInfo) -> FoundControl | None:
    rect = _control_info_rect(c)
    if rect is None:
        return None
    return FoundControl(
        backend="uia",
        handle=None,  # ControlInfo carries no HWND -- backend-agnostic by design
        name=c.name,
        class_name=c.class_name,
        control_type=c.control_type,
        automation_id=c.automation_id,
        control_id=None,
        rect=rect,
        parent_handle=None,
        enabled=None,
        visible=None,
        text_has_content=bool(c.name.strip()),
        text_redacted="",  # label text itself is not sensitive data
    )


# --------------------------------------------------------------------------
# Win32 best-effort property readers
# --------------------------------------------------------------------------


def _best_effort_enabled(handle: int) -> bool | None:
    import win32gui

    try:
        return bool(win32gui.IsWindowEnabled(handle))
    except Exception:
        return None


def _best_effort_visible(handle: int) -> bool | None:
    import win32gui

    try:
        return bool(win32gui.IsWindowVisible(handle))
    except Exception:
        return None


def _best_effort_control_id(handle: int) -> int | None:
    """GetDlgCtrlID() -- best-effort, never fails the whole script."""
    import win32gui

    try:
        cid = win32gui.GetDlgCtrlID(handle)
        return int(cid) if cid else None
    except Exception as exc:
        logger.debug("GetDlgCtrlID no disponible para {}: {}", handle, exc)
        return None


def _best_effort_tooltip(handle: int | None) -> str | None:
    """Best-effort UIA HelpText-style tooltip read. Never fails the script.

    Returns None ("N/D") if the handle is missing or UIA is unavailable for
    it, or if HelpText itself is empty/unreadable.
    """
    if handle is None:
        return None
    try:
        from pywinauto import Desktop

        element = Desktop(backend="uia").window(handle=handle)
        element.wait("exists", timeout=2)
        help_text = element.element_info.element.CurrentHelpText  # type: ignore[attr-defined]
        text = str(help_text) if help_text else ""
        return text or None
    except Exception as exc:
        logger.debug("Tooltip/HelpText no disponible para handle {}: {}", handle, exc)
        return None


# --------------------------------------------------------------------------
# Step 3: locate INVOICE_INPUT
# --------------------------------------------------------------------------

# UIA control_type strings treated as "Edit-like" for this search. WinForms
# controls exposed via UIA commonly surface a native TextBox as "Edit", but
# some WinForms-via-UIA bridging scenarios have been observed (in this
# project's own prior diagnostics, see inspect_go_msaa.py's module
# docstring) to instead surface as "Document" or a generic "Custom" -- both
# are included here as documented, broader nets rather than a single rigid
# "Edit" check.
EDIT_LIKE_UIA_TYPES: tuple[str, ...] = ("edit", "document", "custom")


def _is_contained_within(outer: Rect, inner: Rect, margin: int = 3) -> bool:
    """True if `inner` sits (within `margin` px) inside `outer`.

    Real-world finding (live GO session, F0.3C): this app's DevExpress
    LayoutControl exposes each form row as ONE UIA element (control_type
    DataItem) whose name is the row's caption ("N Factura") but whose
    rectangle spans the FULL row -- caption text AND its paired editor
    together -- not just the caption glyph. So the real Edit's rectangle
    falls INSIDE the "label" rectangle, not strictly to its right. This
    containment check is the correct relationship for that layout; see
    _is_right_and_aligned for the classic separate-label-then-textbox case,
    which this app's OTHER rows may still use.
    """
    return (
        inner.left >= outer.left - margin
        and inner.right <= outer.right + margin
        and inner.top >= outer.top - margin
        and inner.bottom <= outer.bottom + margin
    )


def _is_right_and_aligned(label_rect: Rect, candidate_rect: Rect) -> bool:
    """True if candidate is either (a) immediately right of and vertically
    aligned with label_rect (classic separate label+textbox), or (b)
    contained within label_rect (this app's DataItem-row pattern, see
    _is_contained_within). Both are real, observed relationships in this
    form -- accepting either avoids silently missing the live layout GO
    actually uses (Regla 1: identify by evidence, not by one assumed shape).
    """
    if _is_contained_within(label_rect, candidate_rect):
        return True
    if candidate_rect.left < label_rect.right + EDIT_LEFT_TOLERANCE_PX:
        return False
    vcenter_delta = abs(candidate_rect.vertical_center - label_rect.vertical_center)
    return vcenter_delta <= VERTICAL_CENTER_TOLERANCE_PX


def find_invoice_input_candidates_uia(
    controls: list[ControlInfo], label_rect: Rect
) -> list[FoundControl]:
    candidates: list[FoundControl] = []
    for c in controls:
        if not any(t in c.control_type.lower() for t in EDIT_LIKE_UIA_TYPES):
            continue
        found = _uia_control_to_found(c)
        if found is None:
            continue
        if _is_right_and_aligned(label_rect, found.rect):
            candidates.append(found)
    return candidates


def find_invoice_input_candidates_win32(
    go_handle: int, label_rect: Rect
) -> list[FoundControl]:
    import win32gui

    candidates: list[FoundControl] = []
    for h in _descendant_handles(go_handle):
        try:
            class_name = win32gui.GetClassName(h)
        except Exception:
            continue
        if "edit" not in class_name.lower():
            continue
        try:
            left, top, right, bottom = win32gui.GetWindowRect(h)
        except Exception:
            continue
        rect = Rect(left, top, right, bottom)
        if not _is_right_and_aligned(label_rect, rect):
            continue
        try:
            text = win32gui.GetWindowText(h)
        except Exception:
            text = ""
        try:
            parent = win32gui.GetParent(h) or None
        except Exception:
            parent = None
        candidates.append(
            FoundControl(
                backend="win32",
                handle=h,
                name="",  # never store an Edit's actual text as "name"
                class_name=class_name,
                control_type="?",
                automation_id="",
                control_id=_best_effort_control_id(h),
                rect=rect,
                parent_handle=parent,
                enabled=_best_effort_enabled(h),
                visible=_best_effort_visible(h),
                # Privacy rule: never log/persist the actual value, only a
                # boolean, and always redact any printed value.
                text_has_content=bool(text.strip()),
                text_redacted=_redact_text(bool(text.strip())),
            )
        )
    return candidates


def select_unambiguous_invoice_input(
    candidates: list[FoundControl], label_rect: Rect
) -> tuple[FoundControl | None, str]:
    """Pick INVOICE_INPUT only if exactly one candidate is clearly closest.

    Returns (control_or_None, evidence_note).
    """
    if not candidates:
        return None, "Sin candidatos Edit-like a la derecha del label."
    if len(candidates) == 1:
        return candidates[0], "Unico candidato Edit-like alineado a la derecha del label."

    # Multiple candidates: only unambiguous if exactly one is strictly
    # closest (by horizontal distance from the label's right edge).
    ranked = sorted(candidates, key=lambda f: f.rect.left - label_rect.right)
    closest, second = ranked[0], ranked[1]
    if closest.rect.left < second.rect.left:
        return (
            closest,
            f"{len(candidates)} candidatos; el mas cercano es claramente distinto "
            f"({closest.rect.left} vs {second.rect.left}).",
        )
    return None, f"{len(candidates)} candidatos ambiguos (empate en distancia horizontal)."


# --------------------------------------------------------------------------
# Step 4: locate INVOICE_ACTION_CANDIDATE
# --------------------------------------------------------------------------

ACTION_LIKE_UIA_TYPES: tuple[str, ...] = ("button", "custom")


def _is_embedded_at_right_edge(input_rect: Rect, candidate_rect: Rect) -> bool:
    """True if candidate is a small control embedded AT the input's own
    right edge (not strictly outside it).

    Real-world finding (F0.3C, live GO session): when INVOICE_INPUT resolves
    to the DevExpress composite wrapper (automation_id=INDbteInvoiceNumber,
    e.g. rect (435,309)-(705,337)) rather than the raw native Edit HWND
    nested inside it, the small icon-button sits INSIDE that wrapper's own
    bounds, flush against its right edge (e.g. button rect
    (684,309)-(705,337) -- button.right == input.right exactly) -- a classic
    DevExpress "ButtonEdit" layout (textbox + inline repository button
    rendered as one composite area). This is a different, equally real
    relationship from "separate control positioned to the right of the
    input" (_is_small_action_right_of_input), so both are accepted.
    """
    right_edge_delta = abs(candidate_rect.right - input_rect.right)
    if right_edge_delta > EMBEDDED_RIGHT_EDGE_TOLERANCE_PX:
        return False
    if candidate_rect.left < input_rect.left - EMBEDDED_RIGHT_EDGE_TOLERANCE_PX:
        return False
    if candidate_rect.width >= ACTION_MAX_WIDTH_PX:
        return False
    if candidate_rect.height > input_rect.height:
        return False
    vcenter_delta = abs(candidate_rect.vertical_center - input_rect.vertical_center)
    return vcenter_delta <= VERTICAL_CENTER_TOLERANCE_PX


def _is_small_action_right_of_input(input_rect: Rect, candidate_rect: Rect) -> bool:
    if _is_embedded_at_right_edge(input_rect, candidate_rect):
        return True
    gap = candidate_rect.left - input_rect.right
    if not (ACTION_PROXIMITY_MIN_PX <= gap <= ACTION_PROXIMITY_MAX_PX):
        return False
    if candidate_rect.width >= ACTION_MAX_WIDTH_PX:
        return False
    if candidate_rect.height > input_rect.height:
        return False
    return True


def find_action_candidates_uia(
    controls: list[ControlInfo], input_rect: Rect
) -> list[FoundControl]:
    candidates: list[FoundControl] = []
    for c in controls:
        if not any(t in c.control_type.lower() for t in ACTION_LIKE_UIA_TYPES):
            continue
        found = _uia_control_to_found(c)
        if found is None:
            continue
        if _is_small_action_right_of_input(input_rect, found.rect):
            candidates.append(found)
    return candidates


def find_action_candidates_win32(go_handle: int, input_rect: Rect, input_handle: int | None) -> list[FoundControl]:
    import win32gui

    candidates: list[FoundControl] = []
    for h in _descendant_handles(go_handle):
        if input_handle is not None and h == input_handle:
            continue
        try:
            class_name = win32gui.GetClassName(h)
        except Exception:
            continue
        try:
            left, top, right, bottom = win32gui.GetWindowRect(h)
        except Exception:
            continue
        rect = Rect(left, top, right, bottom)
        if not _is_small_action_right_of_input(input_rect, rect):
            continue
        try:
            text = win32gui.GetWindowText(h)
        except Exception:
            text = ""
        try:
            parent = win32gui.GetParent(h) or None
        except Exception:
            parent = None
        candidates.append(
            FoundControl(
                backend="win32",
                handle=h,
                name=text,  # icon-only buttons often have empty text; not
                # treated as sensitive (never an invoice/patient value)
                class_name=class_name,
                control_type="?",
                automation_id="",
                control_id=_best_effort_control_id(h),
                rect=rect,
                parent_handle=parent,
                enabled=_best_effort_enabled(h),
                visible=_best_effort_visible(h),
                text_has_content=bool(text.strip()),
                text_redacted="",  # a button's own caption is not invoice/patient data
                tooltip=_best_effort_tooltip(h),
            )
        )
    return candidates


def select_unambiguous_action(
    candidates: list[FoundControl],
) -> tuple[FoundControl | None, str]:
    if not candidates:
        return None, "Sin candidatos pequeños inmediatamente a la derecha del input."
    if len(candidates) == 1:
        return candidates[0], "Unico control pequeño inmediatamente a la derecha del input."
    return None, f"{len(candidates)} candidatos ambiguos inmediatamente a la derecha del input."


# --------------------------------------------------------------------------
# Step 5: visual relationship summary (human sanity-check only)
# --------------------------------------------------------------------------


def render_relationship_diagram(
    label_rect: Rect | None, input_rect: Rect | None, action_rect: Rect | None
) -> str:
    """Small ASCII diagram + side-by-side rectangles for human sanity-check.

    This is a human-readability aid ONLY. The technical identification
    above never uses screenshot pixel data as input -- these rectangles
    come exclusively from live UIA/Win32 property reads.
    """
    label_box = "[N Factura]" if label_rect else "[N Factura: ?]"
    input_box = "[ Edit ]" if input_rect else "[ Edit: ? ]"
    action_box = "[*]" if action_rect else "[?]"
    diagram = f"{label_box} -- {input_box} -- {action_box}"
    lines = [
        "DIAGRAMA (solo para verificacion visual humana frente a la captura real; "
        "la identificacion tecnica NUNCA usa datos de pixeles de captura):",
        diagram,
        f"  label rect : {label_rect.as_tuple() if label_rect else 'N/D'}",
        f"  input rect : {input_rect.as_tuple() if input_rect else 'N/D'}",
        f"  action rect: {action_rect.as_tuple() if action_rect else 'N/D'}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Step 6: selector priority / backend comparison
# --------------------------------------------------------------------------


def build_selector_data(found: FoundControl | None, relation: str) -> dict:
    """Build the Step-7 JSON entry for one identified control, using the
    documented selector priority: automation_id > win32 control_id >
    class_name+parent+relation > raw handle (session-only, never a stable
    selector on its own).
    """
    if found is None:
        return {"status": "NOT_FOUND"}

    return {
        "backend": found.backend,
        "automation_id": found.automation_id or None,
        "control_id": found.control_id,
        "class_name": found.class_name or None,
        "relation": relation,
        "session_handle": found.handle,
        "identified_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------
# Step 7: write JSON
# --------------------------------------------------------------------------


def save_selectors_json(
    invoice_input: dict, invoice_action: dict, output_path: Path = OUTPUT_PATH
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"invoice_input": invoice_input, "invoice_action": invoice_action}
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


# --------------------------------------------------------------------------
# Final report rendering
# --------------------------------------------------------------------------


def _fmt_tristate(value: bool | None) -> str:
    if value is True:
        return "SI"
    if value is False:
        return "NO"
    return "?"


def render_label_block(found: FoundControl | None) -> list[str]:
    lines = ["LABEL N FACTURA:"]
    if found is None:
        lines.append("- encontrado: NO")
        return lines
    lines += [
        "- encontrado: SI",
        f"- backend: {found.backend}",
        f"- handle sesion: {found.handle if found.handle is not None else '?'}",
        f"- class: {found.class_name}",
        f"- rectangle: {found.rect.as_tuple()}",
    ]
    return lines


def render_input_block(found: FoundControl | None, evidence: str) -> list[str]:
    lines = ["INVOICE INPUT:"]
    if found is None:
        lines += ["- identificado: NO", f"- evidencia: {evidence}"]
        return lines
    lines += [
        "- identificado: SI",
        f"- backend: {found.backend}",
        f"- handle sesion: {found.handle if found.handle is not None else '?'}",
        f"- automation_id: {found.automation_id or 'N/D'}",
        f"- control_id: {found.control_id if found.control_id is not None else 'N/D'}",
        f"- class: {found.class_name}",
        f"- rectangle: {found.rect.as_tuple()}",
        f"- enabled: {_fmt_tristate(found.enabled)}",
        f"- visible: {_fmt_tristate(found.visible)}",
        f"- focusable: ?",
        f"- evidencia: {evidence} (has_text={found.text_has_content}, "
        f"valor: {found.text_redacted or '(vacio)'})",
    ]
    return lines


def render_action_block(found: FoundControl | None, status: str, evidence: str) -> list[str]:
    lines = ["INVOICE ACTION CANDIDATE:"]
    lines.append(f"- identificado: {status}")
    if found is None:
        lines.append(f"- evidencia: {evidence}")
        return lines
    lines += [
        f"- backend: {found.backend}",
        f"- handle sesion: {found.handle if found.handle is not None else '?'}",
        f"- automation_id: {found.automation_id or 'N/D'}",
        f"- control_id: {found.control_id if found.control_id is not None else 'N/D'}",
        f"- class: {found.class_name}",
        f"- name: {found.name!r}",
        f"- tooltip: {found.tooltip if found.tooltip else 'N/D'}",
        f"- rectangle: {found.rect.as_tuple()}",
        f"- enabled: {_fmt_tristate(found.enabled)}",
        f"- visible: {_fmt_tristate(found.visible)}",
        f"- evidencia: {evidence}",
    ]
    return lines


def build_final_report(
    label: FoundControl | None,
    invoice_input: FoundControl | None,
    input_evidence: str,
    action: FoundControl | None,
    action_status: str,
    action_evidence: str,
    conclusion: str,
) -> str:
    lines: list[str] = []
    lines += render_label_block(label)
    lines.append("")
    lines += render_input_block(invoice_input, input_evidence)
    lines.append("")
    lines += render_action_block(action, action_status, action_evidence)
    lines.append("")
    lines.append("CONCLUSION:")
    lines.append(f"- {conclusion}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run() -> tuple[str, int]:
    """Run the full read-only diagnostic. Returns (report_text, exit_code).

    Never raises for an expected condition -- every documented abort path
    (TRAZABILIDAD_NOT_ACTIVE, ambiguous/not-found states) is handled here
    and turned into a clean report instead of a crash (Regla 14).
    """
    logger.info("PASO 1: resolviendo ventana autenticada de GO (sin PID/handle recordado)...")
    try:
        pids = find_target_process_ids()
    except Exception as exc:
        code = _classify_and_log(exc, "fallo al resolver PIDs de GO")
        return f"TRAZABILIDAD_NOT_ACTIVE ({code})", 0

    if not pids:
        logger.warning("TRAZABILIDAD_NOT_ACTIVE: no hay ningun proceso GO en ejecucion")
        return "TRAZABILIDAD_NOT_ACTIVE", 0

    try:
        all_windows = enumerate_all_windows()
    except WindowNotFoundError as exc:
        logger.warning("TRAZABILIDAD_NOT_ACTIVE: {}", exc)
        return "TRAZABILIDAD_NOT_ACTIVE", 0

    try:
        go_window = find_authenticated_go_window(
            pids, all_windows, require_favoritos_signals=False
        )
    except (WindowNotFoundError, GoAmbiguousError) as exc:
        logger.warning("TRAZABILIDAD_NOT_ACTIVE: {}", exc)
        return "TRAZABILIDAD_NOT_ACTIVE", 0
    except Exception as exc:
        code = _classify_and_log(exc, "fallo inesperado localizando la ventana autenticada de GO")
        return f"TRAZABILIDAD_NOT_ACTIVE ({code})", 0

    logger.info("GO localizada: handle={} pid={}", go_window.handle, go_window.process_id)

    logger.info("PASO 1b: validando que la pantalla Trazabilidad de Factura este activa...")
    try:
        controls = validate_screen_active(
            go_window.handle, DIAGNOSTIC_SCAN_MAX_DEPTH, DIAGNOSTIC_SCAN_TIMEOUT_SECONDS
        )
    except TrazabilidadNotActiveError:
        logger.warning("TRAZABILIDAD_NOT_ACTIVE")
        return "TRAZABILIDAD_NOT_ACTIVE", 0

    logger.info("PASO 2: localizando label 'N Factura' (UIA + Win32)...")
    label_uia_matches = find_label_uia(controls, include_weak=False)
    label_win32_matches = find_label_win32(go_window.handle, include_weak=False)
    if not label_uia_matches and not label_win32_matches:
        logger.info(
            "Sin coincidencias fuertes en ningun backend -- usando fallback debil "
            "('{}' como substring, con limite de palabra)",
            LABEL_WEAK_FALLBACK,
        )
        label_uia_matches = find_label_uia(controls, include_weak=True)
        label_win32_matches = find_label_win32(go_window.handle, include_weak=True)

    label_candidates: list[FoundControl] = []
    for c in label_uia_matches:
        found = _uia_control_to_found(c)
        if found is not None:
            label_candidates.append(found)
    label_candidates.extend(label_win32_matches)

    logger.info(
        "Label 'N Factura': {} candidatos ({} uia, {} win32)",
        len(label_candidates),
        len(label_uia_matches),
        len(label_win32_matches),
    )
    for cand in label_candidates:
        logger.info(
            "Candidato LABEL: backend={} rect={} name={!r}",
            cand.backend,
            cand.rect.as_tuple(),
            cand.name,
        )

    if not label_candidates:
        report = build_final_report(
            None, None, "Label no identificado de forma unambigua.", None, "NO",
            "Label no identificado de forma unambigua.", "REQUIERE_MAS_DIAGNOSTICO",
        )
        return report, 0

    # Multiple label candidates can legitimately share the exact same name
    # (real-world finding, F0.3C: this app's LayoutControl exposes more than
    # one row captioned "N Factura", e.g. an inactive/duplicate row for a
    # different tab state). Rather than aborting on the first ambiguity,
    # try Step 3 (INVOICE_INPUT search) against EACH label candidate --
    # exactly one label "anchoring" to a clean, unambiguous input is itself
    # the disambiguating evidence (Regla 1: still evidence-based, just
    # evidence gathered one step later than a single-label shortcut).
    label = None
    invoice_input = None
    input_evidence = "Label no identificado de forma unambigua."
    all_input_candidates: list[FoundControl] = []
    # (label, picked_input, label_had_a_visible_enabled_win32_candidate) --
    # tracked per-label rather than read off just the picked FoundControl,
    # since a UIA-backend FoundControl always carries visible=None/
    # enabled=None (ControlInfo has no such properties -- see
    # _uia_control_to_found); the reliable signal is whether THIS label's
    # candidate set contains any real, visible+enabled Win32 Edit at all.
    resolved_pairs: list[tuple[FoundControl, FoundControl, bool]] = []

    for cand_label in label_candidates:
        input_uia = find_invoice_input_candidates_uia(controls, cand_label.rect)
        input_win32 = find_invoice_input_candidates_win32(go_window.handle, cand_label.rect)
        candidates_for_this_label = input_uia + input_win32
        picked, evidence = select_unambiguous_invoice_input(
            candidates_for_this_label, cand_label.rect
        )
        logger.info(
            "Label candidato rect={}: {} candidatos INVOICE_INPUT -> {}",
            cand_label.rect.as_tuple(),
            len(candidates_for_this_label),
            "identificado" if picked is not None else "no identificado",
        )
        if picked is not None:
            has_visible_enabled_win32 = any(
                c.backend == "win32" and c.visible and c.enabled
                for c in candidates_for_this_label
            )
            resolved_pairs.append((cand_label, picked, has_visible_enabled_win32))
            all_input_candidates.extend(candidates_for_this_label)

    if len(resolved_pairs) == 1:
        label, invoice_input, _ = resolved_pairs[0]
        input_evidence = (
            "Label desambiguado por anclar a un unico INVOICE_INPUT no ambiguo "
            "(de entre multiples candidatos de label con el mismo nombre)."
            if len(label_candidates) > 1
            else "Unico candidato Edit-like alineado/contenido en el label."
        )
    elif len(resolved_pairs) > 1:
        # Real-world finding (F0.3C, live GO session): this app's LayoutControl
        # exposes more than one row captioned "N Factura" (e.g. an inactive
        # duplicate for a different tab/state); each anchors independently to
        # its own nearby Edit. Prefer the pair whose label had a genuinely
        # visible+enabled Win32 Edit among its candidates -- the disabled/
        # invisible duplicates are not the field the user actually sees and
        # could type into.
        visible_enabled_pairs = [
            (lab, inp) for lab, inp, has_ve in resolved_pairs if has_ve
        ]
        if len(visible_enabled_pairs) == 1:
            label, invoice_input = visible_enabled_pairs[0]
            input_evidence = (
                f"{len(resolved_pairs)} pares (label, input) candidatos; desambiguado "
                "por ser el UNICO cuyo input esta visible Y enabled (los demas son "
                "filas duplicadas/inactivas del mismo formulario)."
            )
        else:
            logger.warning(
                "{} labels distintos anclan cada uno a un INVOICE_INPUT propio -- "
                "genuinamente ambiguo, no se elige ninguno.",
                len(resolved_pairs),
            )
            label = None
            invoice_input = None
            input_evidence = (
                f"{len(resolved_pairs)} pares (label, input) distintos e igualmente "
                "validos -- ambiguedad real, no resuelta."
            )
    else:
        # No label candidate anchored to a clean input; report the single
        # label if there was exactly one, for visibility in the final report.
        if len(label_candidates) == 1:
            label = label_candidates[0]

    for cand in all_input_candidates:
        logger.info(
            "Candidato INVOICE_INPUT: backend={} class={} rect={} has_text={}",
            cand.backend,
            cand.class_name,
            cand.rect.as_tuple(),
            cand.text_has_content,
        )

    if label is None:
        report = build_final_report(
            None, None, "Label no identificado de forma unambigua.", None, "NO",
            "Label no identificado de forma unambigua.", "REQUIERE_MAS_DIAGNOSTICO",
        )
        return report, 0

    if invoice_input is None:
        report = build_final_report(
            label, None, input_evidence, None, "NO",
            "No se evaluo INVOICE_ACTION_CANDIDATE (input no identificado).",
            "REQUIERE_MAS_DIAGNOSTICO",
        )
        return report, 0

    logger.info(
        "INVOICE_INPUT identificado: backend={} rect={}",
        invoice_input.backend,
        invoice_input.rect.as_tuple(),
    )

    logger.info("PASO 4: localizando INVOICE_ACTION_CANDIDATE relativo al input...")
    action_uia = find_action_candidates_uia(controls, invoice_input.rect)
    action_win32 = find_action_candidates_win32(
        go_window.handle, invoice_input.rect, invoice_input.handle
    )
    all_action_candidates = action_uia + action_win32
    for cand in all_action_candidates:
        logger.info(
            "Candidato INVOICE_ACTION_CANDIDATE: backend={} class={} rect={}",
            cand.backend,
            cand.class_name,
            cand.rect.as_tuple(),
        )
    action, action_evidence = select_unambiguous_action(all_action_candidates)
    if action is not None:
        action.__dict__  # no-op; tooltip already best-effort-read in win32 path
    action_status = "SI" if action is not None else ("AMBIGUO" if all_action_candidates else "NO")

    logger.info("PASO 5: resumen de relacion visual (solo verificacion humana)...")
    diagram = render_relationship_diagram(
        label.rect, invoice_input.rect, action.rect if action else None
    )
    print(diagram)
    logger.info("\n{}", diagram)

    logger.info("PASO 6: comparacion de backends / prioridad de selectores...")
    logger.info(
        "LABEL: uia_matches={} win32_matches={}",
        len(label_uia_matches),
        len(label_win32_matches),
    )
    logger.info(
        "INVOICE_INPUT: uia_candidates={} win32_candidates={}", len(input_uia), len(input_win32)
    )
    logger.info(
        "INVOICE_ACTION_CANDIDATE: uia_candidates={} win32_candidates={}",
        len(action_uia),
        len(action_win32),
    )

    invoice_input_selector = build_selector_data(invoice_input, "right_of_label_n_factura")
    invoice_action_selector = build_selector_data(
        action if action is not None else None, "immediately_right_of_invoice_input"
    )
    if action is None:
        invoice_action_selector["status"] = "AMBIGUOUS" if all_action_candidates else "NOT_FOUND"

    logger.info("PASO 7: guardando selectores en {}", OUTPUT_PATH)
    output_path = save_selectors_json(invoice_input_selector, invoice_action_selector)
    logger.info("Selectores guardados en {}", output_path)

    if action is not None:
        conclusion = "INVOICE_CONTROLS_IDENTIFIED"
    else:
        conclusion = "INPUT_IDENTIFIED_ACTION_AMBIGUOUS"

    report = build_final_report(
        label, invoice_input, input_evidence, action, action_status, action_evidence, conclusion
    )
    return report, 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments. Default invocation is argument-free -- everything
    is discovered dynamically (Regla 1: never hardcode PID/handle).
    """
    parser = argparse.ArgumentParser(
        description=(
            "F0.4: diagnostico de solo lectura (UIA + Win32) para localizar el label "
            "'N Factura', su Edit asociado, y un boton/icono pequeño inmediatamente a "
            "su derecha, dentro de la pantalla 'Trazabilidad de Factura' de GO/Indigo. "
            "Nunca hace clic, escribe, ni interactua con la aplicacion."
        )
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=DIAGNOSTIC_SCAN_MAX_DEPTH,
        help=f"Profundidad maxima de recorrido UIA (default: {DIAGNOSTIC_SCAN_MAX_DEPTH}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DIAGNOSTIC_SCAN_TIMEOUT_SECONDS,
        help=(
            "Presupuesto de tiempo en segundos para el recorrido UIA del Paso 1 "
            f"(default: {DIAGNOSTIC_SCAN_TIMEOUT_SECONDS})."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the read-only diagnostic and print the final report."""
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
    logger.info(
        "F0.4: iniciando diagnostico de solo lectura del formulario Trazabilidad "
        "(max_depth={}, timeout={}s)",
        args.max_depth,
        args.timeout,
    )

    global DIAGNOSTIC_SCAN_MAX_DEPTH, DIAGNOSTIC_SCAN_TIMEOUT_SECONDS  # noqa: PLW0603
    DIAGNOSTIC_SCAN_MAX_DEPTH = args.max_depth
    DIAGNOSTIC_SCAN_TIMEOUT_SECONDS = args.timeout

    try:
        report, exit_code = run()
    except Exception as exc:
        # Defensive last-resort net: run() is written to catch and classify
        # every expected failure mode internally. This only guards against a
        # genuinely unexpected error, so the script still prints a clean
        # report instead of an unhandled traceback (Regla 14).
        code = _classify_and_log(exc, "fallo inesperado no clasificado en el flujo del diagnostico")
        report = f"CONCLUSION:\n- REQUIERE_MAS_DIAGNOSTICO ({code})"
        exit_code = 0

    print(report)
    logger.info("Reporte final:\n{}", report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
