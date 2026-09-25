"""Entry point for the Bot-San-Francisco desktop GUI (Phase 1D).

Thin launcher -- all GUI code lives under
``src/clinica_rpa/gui/``, and all automation logic lives under
``src/clinica_rpa/automation/``/``services/``. This script performs no UI
automation itself.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_gui.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clinica_rpa.gui.app import run  # noqa: E402
from clinica_rpa.infrastructure.logging_config import setup_logging  # noqa: E402


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
