"""F0.2 — Inspect the UI Automation tree of a user-selected window.

Lets the user pick a candidate window (reusing scripts/list_windows.py's
generic, non-assuming enumeration) and walks its UI Automation tree with
pywinauto (backend="uia"), printing control_type, name, automation_id,
class_name, and rectangle for each control found. This script never
assumes which window is GO/Índigo (Regla 1, 03_CLAUDE_RULES.md) and never
clicks, types into, or otherwise interacts with any control (Regla 5,
Regla 12) — it is read-only inspection only.

Usage:
    python scripts/inspect_go.py
    python scripts/inspect_go.py --window-contains factura
    python scripts/inspect_go.py --window-contains factura --max-depth 5 --timeout 30
    python scripts/inspect_go.py --control-contains "Vie Finance"
    python scripts/inspect_go.py --handle 123456
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from clinica_rpa.infrastructure.logging_config import setup_logging  # noqa: E402
from loguru import logger  # noqa: E402
from list_windows import WindowInfo, enumerate_windows, format_table  # noqa: E402

DEFAULT_MAX_DEPTH = 8
# Wall-clock budget for a full tree walk. A few seconds is too tight for a
# real desktop app's UIA tree, and a couple of minutes is a safe upper
# bound that still fails fast instead of hanging indefinitely.
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_OUTPUT_PATH = Path("runtime") / "logs" / "go_ui_tree.txt"


class NoWindowSelectedError(Exception):
    """Raised when no window could be selected for inspection."""


@dataclass(frozen=True)
class ControlInfo:
    """Snapshot of one UI Automation control discovered during the walk."""

    depth: int
    control_type: str
    name: str
    automation_id: str
    class_name: str
    rectangle: str


def _filter_by_contains(windows: list[WindowInfo], contains: str) -> list[WindowInfo]:
    """Case-insensitive substring filter over window titles."""
    needle = contains.lower()
    return [w for w in windows if needle in w.title.lower()]


def select_window(windows: list[WindowInfo], window_contains: str | None) -> WindowInfo:
    """Select one window from candidates, via --window-contains and/or a prompt.

    Args:
        windows: All enumerated candidate windows.
        window_contains: Optional case-insensitive title substring filter.

    Returns:
        The selected WindowInfo.

    Raises:
        NoWindowSelectedError: if there are no candidates, the process is
            non-interactive, or the user provides an invalid selection.
    """
    candidates = windows
    if window_contains:
        candidates = _filter_by_contains(windows, window_contains)
        logger.info(
            "Filtro --window-contains={!r} produjo {} candidatos",
            window_contains,
            len(candidates),
        )

    if not candidates:
        raise NoWindowSelectedError("No hay ventanas candidatas para inspeccionar.")

    if len(candidates) == 1:
        logger.info("Selección automática (único candidato): {}", candidates[0].title)
        return candidates[0]

    print(format_table(candidates))
    print()
    try:
        raw = input(
            f"Seleccione el indice de la ventana a inspeccionar [0-{len(candidates) - 1}]: "
        )
    except EOFError as exc:
        raise NoWindowSelectedError(
            "Entrada no interactiva; no se pudo seleccionar ventana."
        ) from exc

    try:
        index = int(raw.strip())
        return candidates[index]
    except (ValueError, IndexError) as exc:
        raise NoWindowSelectedError(f"Selección inválida: {raw!r}") from exc


def resolve_window_by_handle(handle: int) -> WindowInfo:
    """Connect directly to a known window handle, bypassing enumeration/selection.

    Used by --handle, which takes precedence over --window-contains when
    both are given (see parse_args/main).

    Args:
        handle: A raw Win32 window handle (HWND) as an int.

    Returns:
        A WindowInfo describing the resolved window.

    Raises:
        NoWindowSelectedError: if the handle cannot be resolved to a live
            window (invalid handle, window closed, UIA connection failure).
    """
    from pywinauto import Desktop

    try:
        element = Desktop(backend="uia").window(handle=handle)
        element.wait("exists", timeout=5)
    except Exception as exc:
        logger.error("WINDOW_NOT_FOUND: no se pudo resolver --handle={}: {}", handle, exc)
        raise NoWindowSelectedError(f"No se pudo resolver --handle={handle}: {exc}") from exc

    try:
        title = element.window_text() or ""
    except Exception as exc:
        logger.debug("No se pudo leer titulo de --handle={}: {}", handle, exc)
        title = ""

    pid: int | None = None
    try:
        pid = element.process_id()
    except Exception as exc:
        logger.debug("No se pudo leer PID de --handle={}: {}", handle, exc)

    process_name: str | None = None
    if pid is not None:
        try:
            import psutil

            process_name = psutil.Process(pid).name()
        except Exception as exc:
            logger.debug("No se pudo resolver proceso para PID {}: {}", pid, exc)

    return WindowInfo(title=title, handle=handle, process_id=pid, process_name=process_name)


def _safe_str(getter: Callable[[], object]) -> str:
    """Call getter and stringify the result, defaulting to '?' on error."""
    try:
        value = getter()
        return str(value) if value is not None else ""
    except Exception:
        return "?"


def walk_tree(
    element,
    max_depth: int = DEFAULT_MAX_DEPTH,
    deadline: float | None = None,
    depth: int = 0,
) -> list[ControlInfo]:
    """Recursively walk a UI Automation subtree, collecting ControlInfo.

    A single unreachable/inaccessible control is logged and skipped rather
    than aborting the whole walk. Recursion stops at max_depth and, if a
    deadline (a ``time.monotonic()`` timestamp) is provided and exceeded,
    the walk stops early and returns what has been collected so far.

    Args:
        element: A pywinauto UIA wrapper element.
        max_depth: Maximum recursion depth (the root element is depth 0).
        deadline: Optional ``time.monotonic()`` timestamp past which the
            walk stops early.
        depth: Current recursion depth (internal use).

    Returns:
        A flat list of ControlInfo for this element and its descendants.
    """
    if deadline is not None and time.monotonic() > deadline:
        logger.warning("Se alcanzó el timeout durante el recorrido del árbol UIA")
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
    except Exception as exc:
        logger.warning("No se pudieron obtener hijos en profundidad {}: {}", depth, exc)
        return collected

    for child in children:
        if deadline is not None and time.monotonic() > deadline:
            logger.warning("Se alcanzó el timeout durante el recorrido del árbol UIA")
            break
        try:
            collected.extend(
                walk_tree(child, max_depth=max_depth, deadline=deadline, depth=depth + 1)
            )
        except Exception as exc:
            logger.warning(
                "Control omitido por error durante recorrido (profundidad {}): {}",
                depth,
                exc,
            )
            continue

    return collected


def render_tree(controls: list[ControlInfo]) -> str:
    """Render collected controls as an indented, human-readable text tree."""
    lines = []
    for c in controls:
        indent = "  " * c.depth
        lines.append(
            f"{indent}[{c.control_type}] name={c.name!r} "
            f"automation_id={c.automation_id!r} class_name={c.class_name!r} "
            f"rectangle={c.rectangle}"
        )
    return "\n".join(lines)


def save_tree(text: str, output_path: Path = DEFAULT_OUTPUT_PATH) -> Path:
    """Write the rendered tree to output_path, creating parent dirs as needed."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return output_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for inspect_go.py."""
    parser = argparse.ArgumentParser(
        description="F0.2: inspecciona el árbol UI Automation de una ventana seleccionada."
    )
    parser.add_argument(
        "--window-contains",
        default=None,
        help="Filtra ventanas candidatas por subcadena de título (case-insensitive).",
    )
    parser.add_argument(
        "--control-contains",
        default=None,
        help=(
            "Tras el recorrido del árbol, filtra los controles por subcadena "
            "(case-insensitive) en name/automation_id/class_name antes de "
            "imprimir/guardar. El conteo total sin filtrar siempre se registra."
        ),
    )
    parser.add_argument(
        "--handle",
        default=None,
        help=(
            "Handle de ventana (HWND, entero) al que conectarse directamente, "
            "saltando la enumeración/selección. Si se pasa junto con "
            "--window-contains, --handle tiene precedencia."
        ),
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_MAX_DEPTH,
        help=f"Profundidad máxima de recorrido del árbol UIA (default: {DEFAULT_MAX_DEPTH}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=(
            "Presupuesto de tiempo en segundos para el recorrido "
            f"(default: {DEFAULT_TIMEOUT_SECONDS})."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: select a window, walk its UIA tree, save and print results."""
    # Windows consoles often default to a legacy codepage (e.g. cp1252) that
    # cannot encode every character a control name may contain. Force UTF-8
    # on stdout/stderr so real UIA content never crashes this read-only
    # inspection; fall back silently if the stream doesn't support it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    args = parse_args(argv)
    logger.info(
        "F0.2: iniciando inspección UIA (handle={}, window_contains={}, "
        "control_contains={}, max_depth={}, timeout={}s)",
        args.handle,
        args.window_contains,
        args.control_contains,
        args.max_depth,
        args.timeout,
    )

    try:
        if args.handle is not None:
            # --handle takes precedence over --window-contains when both
            # are given: it bypasses enumeration/selection entirely.
            try:
                handle_int = int(args.handle)
            except ValueError:
                logger.error(
                    "WINDOW_NOT_FOUND: --handle no es un entero válido: {!r}", args.handle
                )
                print(f"ERROR: --handle no es un entero válido: {args.handle!r}", file=sys.stderr)
                return 1
            window_info = resolve_window_by_handle(handle_int)
        else:
            windows = enumerate_windows()
            window_info = select_window(windows, args.window_contains)
    except NoWindowSelectedError as exc:
        logger.error("No se pudo seleccionar ventana: {}", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.error(
            "SECURITY_OR_AUTHORIZATION_BLOCK: fallo al listar/seleccionar ventanas: {}",
            exc,
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    logger.info(
        "Ventana seleccionada: titulo={!r} handle={} pid={} proceso={}",
        window_info.title,
        window_info.handle,
        window_info.process_id,
        window_info.process_name,
    )

    start = time.monotonic()
    try:
        from pywinauto import Desktop

        window = Desktop(backend="uia").window(handle=window_info.handle)
        window.wait("exists", timeout=5)
    except Exception as exc:
        logger.error(
            "SECURITY_OR_AUTHORIZATION_BLOCK: no se pudo conectar a la ventana "
            "seleccionada (backend uia): {}",
            exc,
        )
        print(f"ERROR: no se pudo conectar a la ventana: {exc}", file=sys.stderr)
        return 1

    deadline = time.monotonic() + args.timeout
    try:
        controls = walk_tree(window, max_depth=args.max_depth, deadline=deadline)
    except Exception as exc:
        logger.error(
            "SECURITY_OR_AUTHORIZATION_BLOCK: error inesperado durante el recorrido: {}",
            exc,
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    duration = time.monotonic() - start
    logger.info("Recorrido UIA completado: {} controles en {:.2f}s", len(controls), duration)

    total_controls = len(controls)
    display_controls = controls
    if args.control_contains:
        needle = args.control_contains.lower()
        display_controls = [
            c
            for c in controls
            if needle in c.name.lower()
            or needle in c.automation_id.lower()
            or needle in c.class_name.lower()
        ]
        logger.info(
            "{} controles totales, {} coinciden con --control-contains={!r}",
            total_controls,
            len(display_controls),
            args.control_contains,
        )

    tree_text = render_tree(display_controls)
    output_path = save_tree(tree_text)
    logger.info("Árbol UIA guardado en {}", output_path)

    print(tree_text)
    print(f"\n({len(display_controls)} de {total_controls} controles) Arbol guardado en: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
