"""CLI entry point for the reusable invoice-download engine (Phase 1A).

Thin wrapper around
``clinica_rpa.services.invoice_download_service.process_invoice`` -- all
automation logic lives under ``src/clinica_rpa/``. This script performs no
UI automation itself.

Usage:
    .venv\\Scripts\\python.exe scripts\\download_invoice.py --invoice FHC000000 --output runtime/downloads/test

Privacy (Regla 6): never prints/logs the raw invoice number -- only the
masked value and structural evidence (status, size, page count, elapsed
time, error code/message).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clinica_rpa.infrastructure.logging_config import setup_logging  # noqa: E402
from clinica_rpa.services.invoice_download_service import process_invoice  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Descarga el documento origen de una factura ya cargada en GO/Indigo, como PDF."
    )
    parser.add_argument(
        "--invoice",
        required=True,
        help="Numero de factura a consultar (p. ej. FHC000000). Nunca se imprime sin enmascarar.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Directorio destino donde se guardara el PDF descargado.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging()
    args = parse_args(argv)

    result = process_invoice(invoice_number=args.invoice, destination_directory=args.output)

    print(f"STATUS: {result.status}")
    print(f"PDF_CREATED: {'true' if result.pdf_created else 'false'}")
    print(f"SIZE: {result.pdf_size_bytes if result.pdf_size_bytes is not None else 'N/A'}")
    print(f"PAGES: {result.pdf_pages if result.pdf_pages is not None else 'N/A'}")
    print(f"ELAPSED: {result.elapsed_seconds:.1f}")
    if result.status != "COMPLETED":
        print(f"ERROR_CODE: {result.error_code}")
        print(f"ERROR_MESSAGE: {result.error_message_safe}")

    return 0 if result.status == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
