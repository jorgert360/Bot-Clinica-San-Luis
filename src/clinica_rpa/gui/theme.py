"""Centralized brand theme (Fase 1E.2) -- colors extracted directly from
the official Clinica San Francisco logo asset
(``gui/assets/logo_clinica_san_francisco.png``) via PIL quantization,
never invented. Every widget that needs a brand color imports from here;
do not hardcode a brand hex anywhere else in the ``gui`` package.

Semantic status colors (success green, error red, processing amber) are
UI convention, not brand identity -- those stay in :mod:`clinica_rpa.gui.
state` (``STATUS_COLORS``) and are never replaced with brand teal here.
"""

from __future__ import annotations

PRIMARY = "#1F7780"       # main teal -- primary buttons, header accents, progress bar fill
PRIMARY_DARK = "#1F434D"  # dark teal -- titles, primary text on light background
SECONDARY = "#307E85"     # lighter teal -- hover/secondary accents
LIGHT_BG = "#F4FDFD"      # near-white, teal-tinted -- card/section backgrounds
BORDER = "#BACED4"        # light blue-gray -- borders, separators, disabled fills
WHITE = "#FFFFFF"

LOGO_PATH = "assets/logo_clinica_san_francisco.png"
LOGO_TARGET_HEIGHT_PX = 60
