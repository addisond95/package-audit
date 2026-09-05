# BuildingLink Package Audit

A desktop application that turns BuildingLink Event Log PDF exports into a fast,
structured package‑auditing workflow for residential concierge and property
management teams.

Instead of marking up printed audit sheets and writing summaries by hand,
auditors open an export, verify packages with a single click, record exceptions
in spreadsheet‑style tables, and generate a standardized audit report
automatically.

---

## Why

Package audits were traditionally done on paper:

- Print the BuildingLink package report
- Mark verified packages by hand
- Write discrepancies and double‑logged packages on the side
- Re‑type everything into a summary

That process is slow, repetitive, and easy to get wrong. This tool removes the
transcription work by capturing audit observations directly as structured data.

---

## Features

**PDF processing**
- Parse BuildingLink Event Log PDFs
- Extract unit, resident, package, tower, timestamp, and tracking last‑four

**Audit workflow**
- One‑click verification with row highlighting
- Live search across unit, resident, package, tracking, and tower
- Keyboard-first unit lookup: type, use Up/Down, and press Enter to mark a row; focus returns to search
- "Unchecked only" filter
- Bulk *Mark All Visible* / *Unmark All Visible* (respect the active search/filter)
- Progress is saved automatically and resumes when the same PDF is reopened

**Phone scanner (local and free)**
- Pair a phone browser over the same Wi‑Fi using a temporary six-digit code or QR code
- Capture one tracking barcode at a time; no resident-name or unit OCR is performed
- See live audited/remaining totals, alert counts, and desktop connection state on the phone
- Tap **Scan package**, take the picture, and the phone automatically resizes and submits it
- Decode barcodes locally on the desktop with ZXing; no slow OCR or cloud service is involved
- Require an exact full-tracking-number match—never fuzzy matching, names, units, or last-four alone
- Show the audit's logged unit on the phone and mark it present only after explicit confirmation
- Log reliable tracking barcodes absent from the audit as `Not logged`
- Detect duplicate tracking across units, create alerts, preserve full tracking values, and support undo

**Manual report sections**
- *Package Errors* — `Unit | Location | Carrier | Tracking | Last 4 | Note`
- *Double Logged Packages* — `Unit | Location | Carrier | Tracking | Last 4`
- Spreadsheet‑style editing with dropdowns, tab navigation, and automatic blank rows

**Exports**
- Audit report (`.txt`)
- Spreadsheet-safe raw data (`.csv`)
- Highlighted copy of the source PDF

---

## Tech stack

- **Python** 3.10+
- **PySide6** — desktop UI
- **PyMuPDF** — PDF parsing and highlighting
- **SQLite** — local audit‑state persistence (standard library)

---

## Project structure

```
package-audit/
├── main.py                 # Entry point
├── pyproject.toml          # Metadata, dependencies, tooling config
├── app/
│   ├── constants.py        # Locations, carriers, paths, defaults
│   ├── models.py           # Dataclasses + value normalization
│   ├── parser.py           # BuildingLink PDF parsing
│   ├── database.py         # SQLite persistence layer
│   ├── diagnostics.py      # Private rotating crash diagnostics
│   ├── audit_report.py     # Plain-text report generation
│   ├── export_pdf.py       # Highlighted PDF export
│   ├── export_utils.py     # Safe CSV cells + atomic private exports
│   ├── delegates.py        # Table cell editors (dropdowns)
│   ├── scanner_matching.py # Exact tracking-number lookup
│   ├── scanner_server.py   # Paired local-network phone web app
│   ├── scanner_vision.py   # Fast local barcode decoding
│   ├── scanner_ui.py       # Desktop pairing dialog
│   ├── theme.py            # Qt stylesheet
│   └── main_window.py      # Main window and application wiring
├── tests/                  # Regression and smoke-test suite
├── data/                   # Put source PDFs here (local)
└── exports/                # Generated reports (local)
```

---

## Getting started

This project uses [uv](https://docs.astral.sh/uv/).

```bash
# Install dependencies into a virtual environment
uv sync

# Launch the application
uv run python main.py

# Or use the installed console entry point
uv run package-audit
```

Barcode scanning is included in the Python environment and packaged application;
there is no separate OCR program to install.

For a complete terminal walkthrough, phone-pairing steps, and connection troubleshooting, see
[TERMINAL_USAGE.md](TERMINAL_USAGE.md).

### Development

```bash
uv run pytest        # run the test suite
uv run ruff check .  # lint
uv run ruff format --check .
uv run bandit -q -r app main.py
uv run pip-audit
uv run python main.py --export-smoke
uv run python main.py --scanner-smoke
uv run python main.py --ui-smoke
```

### Build a standalone macOS app

```bash
uv run pyinstaller package-audit.spec --clean -y
# Output: dist/Package Audit.app (double-clickable, no Python required)
```

---

## Workflow

```mermaid
flowchart LR
    A[Open BuildingLink PDF] --> B[Parse packages]
    B --> C[Verify packages<br/>search · filter · bulk mark]
    C --> D[Record errors &<br/>double-logged packages]
    D --> E[Export report<br/>TXT · CSV · highlighted PDF]
    C -. auto-saved .-> F[(SQLite audit state)]
    F -. resume .-> C
```

1. **Open** a BuildingLink Event Log PDF.
2. **Verify** packages by clicking a row. Use search and *Unchecked only* to
   focus, and the bulk actions to clear large groups quickly.
3. **Record** any package errors and double‑logged packages in their tabs.
4. **Export** the audit report, CSV, or a highlighted PDF.

### Phone scanner workflow

1. Open an audit PDF on the desktop.
2. Click **Start Phone Scanner**.
3. Scan the displayed QR code with a phone on the same Wi‑Fi, or enter the shown URL and pairing code.
   The desktop dialog confirms when the phone is connected and can copy the address when manual entry is easier.
4. Tap **Scan package**, frame one tracking barcode, take the picture, and accept the phone's camera preview.
   The image is resized and submitted automatically—there is no app-level Submit step. **Existing photo** is a
   fallback for an image already in the photo library.
5. The phone shows the exact unit recorded for that tracking number. Check that unit against the label, then tap
   **Confirm unit …**. Only that confirmation marks the desktop audit row present.
6. A reliable tracking barcode absent from the audit is logged in Package Errors as `Not logged`. Duplicate
   tracking produces a Double Logged row and orange alert. An unreadable or multi-label image asks for a rescan.

The phone scanner binds to the local network only, requires a temporary pairing code, uses a signed browser
session plus CSRF checks, and does not require internet access or a paid service. Keep the phone and computer
on a trusted private Wi‑Fi network. Photos are decoded in memory and are not saved.

If the phone reports that the desktop is unavailable, keep the desktop scanner running and confirm both devices
are on the same non-guest Wi‑Fi; VPNs and access-point client isolation can prevent local-device connections.
The phone disables new scans while disconnected and offers a direct retry or re-pair action instead of silently
continuing with stale state.

Audit state and newly exported files are created with private user-only permissions on platforms that support
POSIX permission bits. The `data/` and `exports/` directory contents are ignored by Git because PDFs, reports,
and CSV files may contain resident information. SQLite state remains local and is not encrypted at rest; use
the operating system's disk encryption and account protections on production workstations. A small rotating
diagnostic log is stored at `~/.package_audit/package-audit.log` with user-only permissions where supported;
it records failures and file paths, but the application does not intentionally log parsed resident data.

The pairing code expires after 15 minutes; an already paired phone remains connected until the scanner stops
or a different PDF is loaded. macOS may ask whether Python can accept incoming network connections the first
time the scanner starts. Allow it only on a trusted private network.

### Keyboard shortcuts

In the Audit tab:

| Shortcut | Action |
| --- | --- |
| Type in search | Filter by unit, resident, tracking, package, or tower |
| `Down` / `Up` from search | Move into the first / last visible result |
| `Down` / `Up` in results | Move between rows |
| `Enter` | Mark/unmark the selected row and return to search |
| `Enter` with one search result | Mark/unmark it directly and stay in search |
| `Ctrl+A` | Mark all visible |
| `Ctrl+Shift+A` | Unmark all visible |

Standard text selection (`Ctrl+A`) still works in the search box and entry tables.

### Reference values

| Locations | Carriers |
| --- | --- |
| SHELF, BIN, BB, CG, UG, ALPHA, FCR | USPS, UPS, FEDEX, AMZ, ONTRAC, DHL, PKG, KEY, FOOD, RX |

---

## Report format

The exported `.txt` report has three sections:

```text
PACKAGE AUDIT REPORT
06/15/2026 08:07 PM
Source: Event log _ BuildingLink.pdf

==================================================
1. PICKED UP BUT NOT CLOSED OUT
==================================================

0207S | 5193
3804S | 9823

==================================================
2. PACKAGE ERRORS
==================================================

1701S | BIN | ONTRAC |  | 6651 | wrong unit
9901S |  | PKG | 1Z000ZZ00000000001 | 0001 | Not logged

==================================================
3. DOUBLE LOGGED PACKAGES
==================================================

0201S | BIN | AMZ |  | 5561
1802S |  | UPS | 1Z999AA10123456784 | 6784
```

- **Section 1** is generated from packages left unchecked during the audit.
- **Sections 2 and 3** come from the manual entry tables and automatic scanner records. Their columns are
  `Unit | Location | Carrier | Tracking | Last 4` plus the Package Errors note.

---

## Audit state management

Audit progress is stored in a local SQLite database keyed by a hash of the PDF
contents (`~/.package_audit/audit_state.sqlite3`). Reopening the same export
restores checked rows and manual entries automatically.

- **Clear Current Audit** — removes checked rows, package errors, double‑logged rows,
  scanner events, and alerts for the loaded PDF.
- **Clear Manual Sections** — removes only the package errors and double‑logged
  rows, keeping package verification intact.

---

## Release status

**v0.6 (current)** — fast tracking-only phone scanning, exact audit lookup,
explicit unit confirmation, automatic image submission, and keyboard-first desktop
auditing, while preserving Not logged/duplicate records, alerts, undo, and exports.

**v0.5** — local phone QR/barcode/OCR scanner, confidence review, automatic
Not logged and duplicate records, alerts, undo, and adaptive matching.

**v0.4** — complete manual audit workflow: parsing, verification, search/filter,
bulk actions, manual report sections, persistence, and exports.

The remaining distribution work is platform trust and release infrastructure:
Developer ID signing/notarization for macOS and native Windows artifact validation
through the included GitHub Actions workflow. Neither requires changing the audit
or scanner data model.
