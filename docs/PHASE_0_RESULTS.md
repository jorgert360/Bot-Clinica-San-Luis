# Phase 0 — Results

Proof-of-concept results for automating invoice source-document retrieval
from GO/Índigo (`Vie Cloud Platform.exe`). No patient data, invoice numbers,
or other clinical content is included in this document.

## Validated capabilities

- **GO automation validated**: the target application window can be located
  dynamically at runtime (by process name and live content evidence), never
  by a hardcoded PID, handle, or window title. Two concurrent instances of
  the process were observed in practice; the authenticated instance was
  distinguished from a stale/login instance purely by UIA content evidence.
- **Trazabilidad navigation validated**: the "Trazabilidad de Factura" entry
  point is a custom-drawn tile with no UIA/MSAA/Win32 accessible identity.
  Opening it requires a calibrated, relative-coordinate click (never
  absolute), preceded by a mandatory human visual confirmation of the
  calculated point before the first click of a session.
- **Invoice input via UIA validated**: once inside the Trazabilidad screen,
  the invoice-number field exposes a stable `automation_id`, allowing a
  ValuePattern write with exact readback verification — no coordinate
  fallback needed here.
- **Invoice lookup validated**: querying by invoice number is triggered by
  a single Enter keystroke on the input field (not a button click, which
  was tried first and had no effect). The backend response can take
  significantly longer than typical UI timeouts, during which the
  application's UI Automation surface becomes intermittently unresponsive.
- **Documento Origen validated**: the source-document link is a UIA element
  identifiable by a stable `automation_id`, distinguishable from a sibling
  element with an empty automation id via that identity.
- **FrmReportViewer validated**: the report viewer opens as an MDI child
  *within* the main application window, not as a new top-level OS window —
  standard top-level window enumeration alone does not detect it; a
  dedicated MDI-child search routine was required.
- **PDF export validated**: the export control is a split-button whose
  center performs a different default action (a different file format)
  than intended. Only the dropdown arrow opens the format-selection menu.
  The format menu itself is a fully custom-drawn popup exposing no
  individual item identity via UIA or MSAA/IAccessible — confirmed by
  live inspection of both accessibility APIs. The exact target item was
  located via coordinates computed dynamically from the popup's real
  on-screen rectangle (never hardcoded), with mandatory human visual
  confirmation before the first click.
- **Filesystem validation validated**: after submitting the save dialog,
  waiting for file creation used filesystem polling (existence + stable
  size across consecutive reads) rather than further UI Automation queries
  against a busy application — deliberately lighter-weight and more
  reliable than UIA-based waiting during this phase of the flow.
- **DOWNLOAD_COMPLETED**: the end-to-end flow (open Trazabilidad → enter
  invoice → query → open source document → open report viewer → export as
  PDF → save to disk → dismiss the completion dialog) was executed once,
  end to end, producing a structurally valid PDF file (correct header,
  non-zero size, readable page count) in a local, git-ignored output
  directory.

## Custom-drawn controls encountered

Multiple UI surfaces in this application expose no accessible identity
through standard automation APIs and had to be treated as fully
custom-drawn (owner-painted) controls:

- The favorites/navigation tiles on the landing screen.
- The export-format dropdown menu opened from the report viewer's toolbar.

In both cases, this was confirmed — not assumed — by direct, live
inspection through multiple accessibility layers (UI Automation and
MSAA/IAccessible) before falling back to coordinates.

## Relative fallbacks used

Every coordinate-based interaction in this PoC is computed relative to a
dynamically discovered reference (a content panel's or a popup window's
real on-screen rectangle), never as an absolute hardcoded screen point.
Each such fallback required an explicit, one-time human visual confirmation
of the calculated target before the first click against it in a session.
No coordinate-based click was ever executed without either a prior
successful confirmation or a fresh move-only confirmation step.

## UI Automation performance considerations

- The application's backend response after a search action can take
  substantially longer than the UI thread stays fully responsive to UI
  Automation queries — attempts to measure elapsed time via repeated UIA
  tree walks during that window produced intermittent connection failures.
  This is a characteristic of the target application under load, not a
  defect in the automation code; the correct handling is a light,
  filesystem- or state-based wait followed by a single read-only
  verification, rather than continuous UIA polling.
- Every mutating interaction (click, keystroke, value write) in this PoC
  has exactly one call site, is preceded by a foreground-ownership check,
  and is never automatically retried or followed by a fallback mechanism
  on failure — a deliberate safety discipline given this same backend
  slowness: an action can appear to fail client-side while having already
  succeeded server-side, making any second attempt ambiguous and unsafe.
