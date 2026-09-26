"""Canonical invoice-number masking (Regla 6) -- the single source of
truth every layer (automation, batch, services, gui) imports from.

Phase 1E.2: previously duplicated identically in three places
(``automation.trazabilidad``, ``batch.excel_loader``, ``gui.state``) --
consolidated here since ``domain`` has zero dependencies, so every other
layer can import it without risking an import cycle.
"""

from __future__ import annotations


def mask_invoice(value: str) -> str:
    """Mask `value`, showing only the first 3 and last 2 characters.

    For length <= 5 the ENTIRE value is masked. Result always has the
    same length as the input. Empty string returns empty string.
    """
    length = len(value)
    if length == 0:
        return ""
    if length <= 5:
        return "*" * length
    return value[:3] + ("*" * (length - 5)) + value[-2:]
