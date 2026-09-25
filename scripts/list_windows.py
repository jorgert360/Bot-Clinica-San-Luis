"""F0.1 — Enumerate visible top-level desktop windows.

Lists every visible top-level window with its title, handle, PID, resolved
process name, and the recommended pywinauto backend ("uia"). This script
does not assume GO/Índigo's window title, automation_id, or any other
identifying selector (Regla 1, 03_CLAUDE_RULES.md) — it surfaces every
relevant candidate so a human can identify GO/Índigo by eye once it is
running.

Filtering policy (documented, conservative): a window is skipped only if
it has BOTH an empty title AND no resolvable process name, since such
windows are almost always untitled system/helper windows with nothing to
identify them by. Everything else is shown, biased toward over-inclusion
so GO/Índigo is never hidden by this filter.

This script performs read-only enumeration only: it never clicks, closes,
or otherwise interacts with any window or process (Regla 5, Regla 12).

Usage:
    python scripts/list_windows.py
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clinica_rpa.infrastructure.logging_config import setup_logging  # noqa: E402
from loguru import logger  # noqa: E402

RECOMMENDED_BACKEND = "uia"


class WindowEnumerationError(Exception):
    """Raised when the desktop window list itself cannot be obtained."""


@dataclass(frozen=True)
class WindowInfo:
    """Snapshot of one top-level desktop window."""

    title: str
    handle: int
    process_id: int | None
    process_name: str | None
    backend: str = RECOMMENDED_BACKEND


def _resolve_process_name(pid: int | None) -> str | None:
    """Best-effort resolution of a process executable name from its PID.

    Returns None if the PID is unknown or the process cannot be inspected
    (already exited, access denied, etc.) rather than raising.
    """
    if pid is None:
        return None
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception as exc:  # psutil.NoSuchProcess, AccessDenied, etc.
        logger.debug("No se pudo resolver el proceso para PID {}: {}", pid, exc)
        return None


def _is_relevant(title: str, process_name: str | None) -> bool:
    """Conservative filter: keep any window with a title or a known process.

    See module docstring for the documented filtering policy (Regla 1:
    no asumir — bias toward showing more candidates, not fewer).
    """
    return bool(title.strip()) or process_name is not None


def enumerate_windows() -> list[WindowInfo]:
    """Enumerate visible top-level windows using pywinauto's UIA backend.

    Returns:
        A list of WindowInfo for every relevant visible top-level window.

    Raises:
        WindowEnumerationError: if the desktop window list cannot be
            obtained at all (e.g. the UIA subsystem is unavailable or
            access is blocked).
    """
    from pywinauto import Desktop

    start = time.monotonic()
    try:
        raw_windows = Desktop(backend="uia").windows()
    except Exception as exc:
        logger.error(
            "SECURITY_OR_AUTHORIZATION_BLOCK: fallo al enumerar ventanas via UIA: {}",
            exc,
        )
        raise WindowEnumerationError(str(exc)) from exc

    results: list[WindowInfo] = []
    for element in raw_windows:
        try:
            if not element.is_visible():
                continue

            title = element.window_text() or ""
            handle = element.handle

            pid: int | None = None
            try:
                pid = element.process_id()
            except Exception as exc:
                logger.debug("No se pudo obtener PID de una ventana: {}", exc)

            process_name = _resolve_process_name(pid)

            if not _is_relevant(title, process_name):
                continue

            results.append(
                WindowInfo(
                    title=title,
                    handle=handle,
                    process_id=pid,
                    process_name=process_name,
                )
            )
        except Exception as exc:
            # A window can disappear mid-enumeration or become briefly
            # inaccessible; skip it without aborting the whole enumeration.
            logger.warning("Ventana omitida por error durante enumeración: {}", exc)
            continue

    duration = time.monotonic() - start
    logger.info(
        "Enumeración de ventanas completada: {} ventanas relevantes en {:.2f}s",
        len(results),
        duration,
    )
    return results


def format_table(windows: list[WindowInfo]) -> str:
    """Render a clean, fixed-width console table for a list of windows."""
    if not windows:
        return "No se encontraron ventanas visibles relevantes."

    headers = ("#", "Titulo", "Handle", "PID", "Proceso", "Backend")
    rows = [
        (
            str(idx),
            w.title[:60],
            str(w.handle),
            str(w.process_id) if w.process_id is not None else "?",
            w.process_name or "?",
            w.backend,
        )
        for idx, w in enumerate(windows)
    ]
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))
    ]

    def _fmt_row(cols: tuple[str, ...]) -> str:
        return " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols))

    lines = [_fmt_row(headers), "-+-".join("-" * w for w in widths)]
    lines.extend(_fmt_row(r) for r in rows)
    return "\n".join(lines)


def main() -> int:
    """Entry point: enumerate windows, print a table, and log the outcome."""
    # Windows consoles often default to a legacy codepage (e.g. cp1252) that
    # cannot encode every character a window title may contain. Force UTF-8
    # on stdout/stderr so real window titles never crash this read-only
    # enumeration; fall back silently if the stream doesn't support it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    logger.info("F0.1: iniciando enumeración de ventanas del escritorio")

    try:
        windows = enumerate_windows()
    except WindowEnumerationError as exc:
        logger.error("No se pudo completar la enumeración de ventanas: {}", exc)
        print(f"ERROR: no se pudo enumerar ventanas: {exc}", file=sys.stderr)
        return 1

    table = format_table(windows)
    print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
