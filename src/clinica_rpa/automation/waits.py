"""Progressive-backoff light waiting helpers and filesystem-first file wait.

Ported from ``scripts/poc_download_invoice_document.py``. Two deliberately
different waiting disciplines, both explicit mission requirements:

- Window/MDI-child waits: a LIGHT poll (one top-level window enumeration, or
  one bounded live-element walk, per tick) with a fixed-then-growing
  interval -- never a tight loop doing a deep UIA tree walk every tick. GO
  has been observed to become UIA-unresponsive for 20+ seconds while
  genuinely processing a backend query; a transient read failure during that
  window is tolerated (retried), never treated as proof of failure.
- File creation: filesystem-only polling (``Path.exists()`` +
  ``Path.stat().st_size``), never a single UIA read anywhere in
  :func:`wait_for_file_stable`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from clinica_rpa.automation.go_session import (
    WindowDiagnostic,
    live_element_automation_id,
    live_element_control_type,
    live_element_name,
    walk_live,
)
from clinica_rpa.automation.go_session import _matches_any as matches_any

DEFAULT_LIGHT_POLL_INTERVAL_SECONDS = 0.5
FILE_WAIT_POLL_INTERVAL_SECONDS = 0.5
FILE_STABLE_READS_REQUIRED = 3

# Progressive backoff schedule for window/dialog waits (mission "ESPERAS"
# example: 0.5s/1s/1.5s/2s/2s...), never a flat tight loop. The schedule
# grows until DEFAULT_BACKOFF_CAP_SECONDS, then holds steady there for the
# remainder of the caller's own global timeout.
DEFAULT_BACKOFF_STEP_SECONDS = 0.5
DEFAULT_BACKOFF_CAP_SECONDS = 2.0


def _next_backoff_interval(current: float) -> float:
    return min(current + DEFAULT_BACKOFF_STEP_SECONDS, DEFAULT_BACKOFF_CAP_SECONDS)


@dataclass
class FileWaitResult:
    appeared: bool
    stabilized: bool
    size_bytes: int
    elapsed_first_seen: float | None
    elapsed_stabilized: float | None


def light_wait_for_window(
    window_source_fn,
    before_handles: set[int],
    predicate,
    timeout_seconds: float,
    poll_interval: float = DEFAULT_LIGHT_POLL_INTERVAL_SECONDS,
    require_new: bool = False,
) -> WindowDiagnostic | None:
    """Poll ``window_source_fn()`` (a light top-level enumeration) until a
    window matches ``predicate``, or ``timeout_seconds`` elapses.

    A single enumeration failure is tolerated silently and retried on the
    next tick (GO can be transiently UIA-unresponsive while busy) -- it is
    never treated as evidence the wait failed.
    """
    start = time.monotonic()
    interval = poll_interval
    while True:
        try:
            windows = window_source_fn()
        except Exception:
            windows = []
        for w in windows:
            if (not require_new or w.handle not in before_handles) and predicate(w):
                return w
        if time.monotonic() - start > timeout_seconds:
            return None
        time.sleep(interval)
        interval = _next_backoff_interval(interval)


def find_mdi_child_live(
    container,
    automation_id_hints: tuple[str, ...],
    title_hints: tuple[str, ...],
    max_depth: int,
    timeout_seconds: float,
):
    """Search GO's own live UIA tree for a Window-type MDI child descendant.

    Real-world finding (F0.5): a report viewer opens as an MDI child INSIDE
    GO's own top-level window, not as a new top-level OS window -- detecting
    only new top-level windows cannot see it. Read-only: never
    invokes/clicks anything found.
    """
    deadline = time.monotonic() + timeout_seconds
    found: list = []

    def _visit(element, _depth):
        ctype = live_element_control_type(element)
        if "window" not in ctype:
            return
        aid = live_element_automation_id(element)
        if aid and aid in automation_id_hints:
            found.append(element)
            return
        name = live_element_name(element)
        if matches_any(name, title_hints):
            found.append(element)

    walk_live(container, _visit, max_depth, deadline)
    return found[0] if found else None


def wait_for_file_stable(
    path: Path,
    poll_interval: float = FILE_WAIT_POLL_INTERVAL_SECONDS,
    timeout_seconds: float = 30.0,
    stable_reads_required: int = FILE_STABLE_READS_REQUIRED,
) -> FileWaitResult:
    """Filesystem-only wait: never a UIA read anywhere in this function."""
    start = time.monotonic()
    last_size: int | None = None
    stable_streak = 0
    elapsed_first_seen: float | None = None
    appeared = False
    final_size = 0

    while True:
        elapsed = time.monotonic() - start
        if path.exists():
            appeared = True
            if elapsed_first_seen is None:
                elapsed_first_seen = elapsed
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            final_size = size if size >= 0 else final_size
            if size == last_size:
                stable_streak += 1
            else:
                stable_streak = 1
            last_size = size
            if stable_streak >= stable_reads_required:
                return FileWaitResult(
                    appeared=True,
                    stabilized=True,
                    size_bytes=final_size,
                    elapsed_first_seen=elapsed_first_seen,
                    elapsed_stabilized=elapsed,
                )

        if elapsed > timeout_seconds:
            return FileWaitResult(
                appeared=appeared,
                stabilized=False,
                size_bytes=final_size,
                elapsed_first_seen=elapsed_first_seen,
                elapsed_stabilized=None,
            )

        time.sleep(poll_interval)
