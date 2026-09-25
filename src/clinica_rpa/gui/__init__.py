"""Bot-San-Francisco desktop GUI (Phase 1D).

Pure presentation layer over ``clinica_rpa.services.invoice_download_service
.process_invoice``. This package never touches GO/UIA/Win32 automation
directly -- it only calls the already-validated engine (Phases 1A-1C) from a
background thread and renders its ``InvoiceDownloadResult``.
"""
