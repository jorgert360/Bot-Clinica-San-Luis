"""UI automation adapters (pywinauto/UIA) for GO/Indigo.

Phase 1A: ``go_session`` (session discovery, foreground verification, and
shared UIA element utilities), ``waits`` (progressive-backoff window waits
and filesystem-first file waits), ``trazabilidad`` (open Trazabilidad, write
the invoice, trigger the search), ``report_viewer`` (Documento Origen ->
Visor de Reportes -> Export Document -> PDF Options), and ``dialogs``
(Guardar como -> final Exportar dialog).

The original PoC scripts under ``scripts/`` remain the regression reference
and are not imported from here.
"""
