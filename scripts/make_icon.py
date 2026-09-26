"""Build-time helper: convert the approved clinic logo PNG into a
multi-resolution ``.ico`` for the packaged exe's icon (Fase 1F).

BUILD-TIME ONLY -- imports Pillow, which is intentionally NOT a runtime
dependency of the shipped GUI app (see ``pyproject.toml``'s
``[project.optional-dependencies].build`` group). Never imported by
``clinica_rpa.gui``.

Usage:
    python scripts/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Pillow no esta instalado. Instalar con: pip install Pillow (o: pip install -e .[build])")
    raise SystemExit(1)

_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_PNG = _ROOT / "src" / "clinica_rpa" / "gui" / "assets" / "logo_clinica_san_francisco.png"
_OUTPUT_ICO = _ROOT / "src" / "clinica_rpa" / "gui" / "assets" / "bot_san_francisco.ico"

# Standard Windows icon resolutions.
_ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    if not _SOURCE_PNG.exists():
        print(f"No se encontro el logo fuente: {_SOURCE_PNG}")
        return 1

    image = Image.open(_SOURCE_PNG).convert("RGBA")

    # The logo is a wide banner (491x191), not square -- pad it onto a
    # square transparent canvas first so the icon isn't squished, then
    # let Pillow generate every resolution from that square master.
    side = max(image.width, image.height)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    offset = ((side - image.width) // 2, (side - image.height) // 2)
    square.paste(image, offset, image)

    square.save(_OUTPUT_ICO, format="ICO", sizes=_ICON_SIZES)
    print(f"Icono generado: {_OUTPUT_ICO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
