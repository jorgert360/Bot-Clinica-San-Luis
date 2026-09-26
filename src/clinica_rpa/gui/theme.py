"""Centralized brand theme (Fase 1E/1F) -- colors extracted directly from
the official Clinica San Francisco logo asset
(``gui/assets/logo_clinica_san_francisco.png``) via PIL quantization,
never invented. Every widget that needs a brand color, font, padding, or
ttk style imports from here; do not hardcode a brand hex, ad-hoc font
tuple, or raw padding number anywhere else in the ``gui`` package.

Semantic status colors (success green, error red, processing amber) are
UI convention, not brand identity -- those stay in :mod:`clinica_rpa.gui.
state` (``STATUS_COLORS``) and are never replaced with brand teal here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tkinter import ttk

# --------------------------------------------------------------------------
# Brand colors -- extracted from the real logo (never invented)
# --------------------------------------------------------------------------

PRIMARY = "#1F7780"       # main teal -- primary buttons, header accents, progress bar fill
PRIMARY_DARK = "#1F434D"  # dark teal -- titles, primary text on light background
SECONDARY = "#307E85"     # lighter teal -- hover/secondary accents
LIGHT_BG = "#F4FDFD"      # near-white, teal-tinted -- card/section backgrounds
BORDER = "#BACED4"        # light blue-gray -- borders, separators, disabled fills
WHITE = "#FFFFFF"

# Neutral UI text colors (not brand identity -- standard readable neutrals,
# same category as gui/state.py's pre-existing "#555555" muted text).
TEXT_MUTED = "#5B6B70"
TEXT_DISABLED = "#8A9598"

LOGO_PATH = "assets/logo_clinica_san_francisco.png"
LOGO_TARGET_HEIGHT_PX = 56

# --------------------------------------------------------------------------
# Color math -- pure Tk-safe hex interpolation, no PIL/new dependency.
# Used to derive the header gradient's light end and hover/pressed shades
# from the real extracted palette above, never a new unrelated hue.
# --------------------------------------------------------------------------


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, c)) for c in rgb))


def lighten(hex_color: str, factor: float) -> str:
    """Blend ``hex_color`` toward white by ``factor`` (0=no change, 1=white)."""
    r, g, b = _hex_to_rgb(hex_color)
    return _rgb_to_hex((
        round(r + (255 - r) * factor),
        round(g + (255 - g) * factor),
        round(b + (255 - b) * factor),
    ))


def darken(hex_color: str, factor: float) -> str:
    """Blend ``hex_color`` toward black by ``factor`` (0=no change, 1=black)."""
    r, g, b = _hex_to_rgb(hex_color)
    return _rgb_to_hex((
        round(r * (1 - factor)),
        round(g * (1 - factor)),
        round(b * (1 - factor)),
    ))


def gradient_colors(start_hex: str, end_hex: str, steps: int) -> list[str]:
    """Linear interpolation between two hex colors, ``steps`` colors
    inclusive of both ends. Used to paint the header banner's gradient on
    a plain ``tk.Canvas`` (no PIL, no image asset for the gradient
    itself)."""
    start = _hex_to_rgb(start_hex)
    end = _hex_to_rgb(end_hex)
    steps = max(2, steps)
    colors = []
    for i in range(steps):
        t = i / (steps - 1)
        rgb = tuple(round(start[c] + (end[c] - start[c]) * t) for c in range(3))
        colors.append(_rgb_to_hex(rgb))
    return colors


# Header banner gradient -- dark teal (left, behind the logo) fading to a
# lighter teal derived from PRIMARY (right), both real-palette-derived.
HEADER_GRADIENT_START = PRIMARY_DARK
HEADER_GRADIENT_END = lighten(PRIMARY, 0.35)
HEADER_CLINIC_TEXT = lighten(PRIMARY, 0.65)  # pale teal, reads against the dark-left part of the gradient where this line sits

# --------------------------------------------------------------------------
# Fonts -- centralized so no ad-hoc ("Segoe UI", N, ...) tuple is scattered
# through app.py's widget-construction code.
# --------------------------------------------------------------------------

FONT_TITLE = ("Segoe UI", 20, "bold")
FONT_SUBTITLE = ("Segoe UI", 10)
FONT_CLINIC = ("Segoe UI", 9, "bold")
FONT_TAB = ("Segoe UI", 10)
FONT_TAB_SELECTED = ("Segoe UI", 10, "bold")
FONT_CARD_TITLE = ("Segoe UI", 12, "bold")
FONT_CARD_SUBTITLE = ("Segoe UI", 9)
FONT_BODY = ("Segoe UI", 10)
FONT_BODY_BOLD = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI", 9)
FONT_SMALL_ITALIC = ("Segoe UI", 9, "italic")
FONT_BUTTON = ("Segoe UI", 10, "bold")
FONT_ICON = ("Segoe UI Emoji", 16)
FONT_CHIP_ICON = ("Segoe UI Emoji", 11)
FONT_PERCENT = ("Segoe UI", 10, "bold")

# --------------------------------------------------------------------------
# Spacing
# --------------------------------------------------------------------------

CARD_PADDING = 16
SECTION_SPACING = 14
WINDOW_PADDING = 20

# --------------------------------------------------------------------------
# Icons -- plain Unicode glyphs only (no icon library, no generated image).
# --------------------------------------------------------------------------

ICON_EXCEL = "\U0001F4C4"       # 📄
ICON_FOLDER = "\U0001F4C1"      # 📁
ICON_PLAY = "▶"            # ▶
ICON_CHART = "\U0001F4CA"       # 📊
ICON_INFO = "ℹ"            # ℹ
ICON_SUCCESS = "✓"         # ✓
ICON_ERROR = "⚠"           # ⚠
ICON_PENDING = "\U0001F551"     # 🕑
ICON_TAB_EXCEL = "\U0001F4CB"   # 📋
ICON_TAB_SINGLE = "⚙"      # ⚙


def apply_theme(style: "ttk.Style") -> None:
    """Configure every custom ttk style name used by ``gui.app`` in ONE
    place. Called once from ``BotSanFranciscoApp._build_ui``."""
    import tkinter as tk

    # "vista"/"xpnative" draw buttons with native Windows UxTheme chrome,
    # which silently IGNORES ttk.Style's configured background on many
    # Windows builds -- only the style DATABASE holds the right color
    # (style.lookup(...) reads back correctly), the actual rendered pixels
    # do not (live-confirmed 2026-09-25: "Seleccionar Excel" rendered
    # near-invisible white-on-white despite a verified-correct
    # Primary.TButton background, only showing color on native hover).
    # "clam" is a fully Tk-drawn theme (no native chrome) that reliably
    # respects configured colors -- required for this app's branded
    # buttons to render correctly at all.
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass  # fall back to whatever default ttk theme is available

    # -- Primary action button ("Procesar facturas") -----------------
    # Explicit foreground+background for every state, including disabled
    # -- the exact complaint this phase fixes was low/invisible contrast.
    style.configure(
        "Primary.TButton",
        foreground=WHITE,
        background=PRIMARY,
        font=FONT_BUTTON,
        padding=(16, 11),
        borderwidth=0,
    )
    style.map(
        "Primary.TButton",
        foreground=[("disabled", TEXT_DISABLED), ("!disabled", WHITE)],
        background=[("disabled", BORDER), ("pressed", darken(PRIMARY, 0.15)), ("active", SECONDARY), ("!disabled", PRIMARY)],
    )

    # -- Secondary button ("Abrir carpeta", "Seleccionar") ------------
    style.configure(
        "Secondary.TButton",
        foreground=PRIMARY_DARK,
        background=WHITE,
        font=FONT_BODY,
        padding=(12, 8),
        borderwidth=1,
    )
    style.map(
        "Secondary.TButton",
        foreground=[("disabled", TEXT_DISABLED), ("!disabled", PRIMARY_DARK)],
        background=[("disabled", LIGHT_BG), ("active", LIGHT_BG), ("!disabled", WHITE)],
    )

    # -- Card frame + text styles (white card body, teal-dark title) --
    style.configure("Card.TFrame", background=WHITE)
    style.configure("CardIcon.TLabel", background=WHITE, font=FONT_ICON)
    style.configure("CardTitle.TLabel", background=WHITE, foreground=PRIMARY_DARK, font=FONT_CARD_TITLE)
    style.configure("CardSubtitle.TLabel", background=WHITE, foreground=TEXT_MUTED, font=FONT_CARD_SUBTITLE)
    style.configure("CardBody.TLabel", background=WHITE, font=FONT_BODY)
    style.configure("CardBodyBold.TLabel", background=WHITE, font=FONT_BODY_BOLD)
    style.configure("CardBodyMuted.TLabel", background=WHITE, foreground=TEXT_MUTED, font=FONT_SMALL)

    # -- Info banner (organization hint box) --------------------------
    style.configure("InfoBanner.TFrame", background=LIGHT_BG)
    style.configure("InfoBanner.TLabel", background=LIGHT_BG, foreground=PRIMARY_DARK, font=FONT_SMALL)
    style.configure("InfoBannerIcon.TLabel", background=LIGHT_BG, foreground=PRIMARY, font=FONT_SMALL)

    # -- Notebook tabs -------------------------------------------------
    style.configure("TNotebook", background=LIGHT_BG, borderwidth=0)
    style.configure("TNotebook.Tab", padding=(16, 10), font=FONT_TAB, foreground=TEXT_MUTED, background=LIGHT_BG)
    style.map(
        "TNotebook.Tab",
        foreground=[("selected", PRIMARY_DARK), ("!selected", TEXT_MUTED)],
        background=[("selected", WHITE), ("!selected", LIGHT_BG)],
        font=[("selected", FONT_TAB_SELECTED), ("!selected", FONT_TAB)],
    )

    # -- Progress bar ---------------------------------------------------
    style.configure(
        "Teal.Horizontal.TProgressbar",
        troughcolor=BORDER,
        background=PRIMARY,
        bordercolor=BORDER,
        lightcolor=PRIMARY,
        darkcolor=PRIMARY,
        thickness=14,
    )

    # -- Counter chip labels (Completadas/Errores/Pendientes) ----------
    style.configure("ChipSuccess.TLabel", foreground="#1a7f37", font=FONT_CHIP_ICON, background=LIGHT_BG)
    style.configure("ChipError.TLabel", foreground="#c0392b", font=FONT_CHIP_ICON, background=LIGHT_BG)
    style.configure("ChipPending.TLabel", foreground=TEXT_MUTED, font=FONT_CHIP_ICON, background=LIGHT_BG)
    style.configure("ChipText.TLabel", foreground=PRIMARY_DARK, font=FONT_BODY_BOLD, background=LIGHT_BG)
