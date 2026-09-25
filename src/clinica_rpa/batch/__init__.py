"""Excel-driven batch loading and NIT-based folder routing (Phase 1E).

This package never touches GO/UIA/Win32 automation directly -- it only
produces :class:`clinica_rpa.domain.models.InvoiceBatchItem` objects and
computes destination directories for the already-validated single-invoice
engine (``clinica_rpa.services.invoice_download_service.process_invoice``).
"""
