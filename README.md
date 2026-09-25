# clinica-rpa

Phase-0 proof-of-concept for automating invoice retrieval from the GO/Índigo
desktop application at a clinic in Tuluá, Colombia, using Python and UI
Automation (pywinauto). This repository currently implements **Phase 0 PoC
scope only** (F0.1 window detection and F0.2 UI tree inspection) — it does
not search for, print, or save any invoices yet.

## Requirements

- Python 3.12+
- Windows (pywinauto UIA backend)

## Usage

```
python scripts/list_windows.py
python scripts/inspect_go.py --contains <text>
```

See `docs/` and the root planning documents (`01_PROJECT_CONTEXT.md` through
`09_ROADMAP.md`) for full project context.
