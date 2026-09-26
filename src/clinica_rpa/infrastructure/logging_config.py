"""Centralized loguru configuration for clinica_rpa scripts.

Configures loguru to log to the console (INFO+ by default) and to a
rotating file under ``runtime/logs/``. Scripts must call
:func:`setup_logging` once at startup, then use ``from loguru import
logger`` as usual.

Regla 6 (03_CLAUDE_RULES.md): every meaningful action must be logged
(stage, window info, action, result, duration, error), and logs must
NEVER contain passwords, tokens, clinical content, full documents, or
other unnecessary sensitive information. Callers are responsible for not
passing sensitive values into log calls; this module only wires up sinks.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from clinica_rpa.config import get_settings

_LOG_FILE_NAME = "clinica_rpa.log"
_ROTATION = "10 MB"
_RETENTION = "14 days"

_configured = False


def setup_logging(log_dir: Path | str | None = None, level: str | None = None) -> None:
    """Configure loguru sinks for console and rotating file output.

    Idempotent: calling this more than once is a no-op, so scripts and any
    modules they import can each call it safely at startup.

    Args:
        log_dir: Directory where rotating log files are written. Defaults
            to ``<RUNTIME_DIR>/logs`` (see :mod:`clinica_rpa.config`).
            Created if it does not already exist.
        level: Minimum log level for both sinks (e.g. "INFO", "DEBUG").
            Defaults to the ``LOG_LEVEL`` setting.
    """
    global _configured
    if _configured:
        return

    settings = get_settings()
    resolved_level = level or settings.log_level
    resolved_log_dir = Path(log_dir) if log_dir is not None else settings.runtime_dir / "logs"
    resolved_log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    # A PyInstaller windowed build (console=False, Fase 1F) has no
    # attached console -- sys.stderr is None there, and loguru raises
    # ("Cannot log to objects of type 'NoneType'") if handed it directly.
    # Live-confirmed: the packaged Bot-San-Francisco.exe crashed on launch
    # with exactly this error before this guard was added.
    if sys.stderr is not None:
        logger.add(sys.stderr, level=resolved_level, backtrace=False, diagnose=False)
    logger.add(
        resolved_log_dir / _LOG_FILE_NAME,
        level=resolved_level,
        rotation=_ROTATION,
        retention=_RETENTION,
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

    _configured = True
    logger.debug("Logging configurado (level={}, dir={})", resolved_level, resolved_log_dir)
