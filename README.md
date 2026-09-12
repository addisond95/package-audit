# BuildingLink Package Audit

Package Audit turns a BuildingLink Event Log PDF into a fast, local package-auditing workflow. It replaces
paper markups and retyped discrepancy notes with a searchable desktop audit, automatic local persistence,
structured exception records, and a direct offline Android scanner.

The primary scanner workflow is a Samsung/Android phone connected directly to a Mac over Bluetooth Low Energy:
no browser, Wi-Fi, hotspot, internet connection, Cloudflare tunnel, account, or subscription is needed during
an audit.

> Status: versions 0.10 and 0.11 completed successful real package-room audits on a Mac and Samsung Galaxy S24
> Ultra. Version 0.13 adds package-location results, scan-to-alert creation, and consistent dated export names
> on top of the v0.12 unit-reading workflow. The release passes automated, Android release-build, and packaged-Mac
> checks and is installed on the test S24 Ultra. Real shipping-label verification during an audit is still required. It is not
> yet a notarized macOS release, Play Store release, or formally independently security-audited product.

## What it does today

### Desktop audit

- Opens BuildingLink Event Log PDFs and extracts unit, resident, package, tower, timestamp, and tracking data.
- Lets you mark packages present with a mouse, keyboard, search, filters, and bulk actions.
- Supports keyboard-first auditing: type a unit, use Up/Down, press Enter to mark a result, then type the next
  unit without reselecting the search field.
- Saves audit progress locally in SQLite and restores it when the same PDF is reopened.
- Keeps package errors and double-logged packages in spreadsheet-style editable tables.
- Keeps unmatched scans in Alerts until you rescan/dismiss them or convert them to a categorized Package Error.
- Provides built-in and locally customizable error-reason templates for consistent final reporting.
- Collapses duplicate tracking records into one physical audit row while retaining one Double Logged summary.
- Classifies Package, Perishable, Keys, Envelope, and Alpha audit entries and shows the type in the audit table.
- Scores completion, errors, duplicates, and overall impact by distinct unit—not raw package count—so five
  exceptions for one apartment count as one affected unit. Raw occurrence counts remain visible for context.
- Provides editable audit-note trigger rules: choose a unit-based metric, comparison, percentage threshold, and
  reusable message with supported result placeholders.
- Blocks final export until open alerts are reviewed and Shelf, BB, Alpha bin, Bin, Perishables, Cage/CG, UG,
  FCR, and Oddities/other storage are explicitly checked.
- Exports a text audit report, spreadsheet-safe CSV, and a highlighted copy of the source PDF.

### Offline Android Bluetooth scanner

- Runs as a native Android app, not a web page.
- Uses the phone camera for live barcode and printed unit-number recognition; photos and video frames remain in
  phone memory.
- Combines a bundled on-device ML Kit detector with the local ZXing fallback, higher-resolution analysis,
  automatic zoom suggestions, center refocus, and an aiming guide for difficult shipping labels.
- Sends only decoded barcode values, normalized unit candidates/confidences, and audit responses through the
  encrypted Bluetooth connection to the Mac. It does not send camera images or resident names.
- Uses one-time QR pairing for each scanner/audit session; normal macOS Bluetooth-device pairing is not used.
- Requires an exact full tracking match and, in normal **Tracking + unit** mode, verifies the label's unit-number
  identity against the audit. Building suffixes and leading zeroes are abstracted (`2803` matches `2803S`), but
  different apartment numbers (`305` and `3005`) do not match. Resident names are never read.
- If no reliable unit is readable, shows the audit unit and allows manual confirmation, rescan, or review instead
  of hard-blocking the audit. **Tracking-only mode** intentionally skips OCR enforcement for labels without a
  useful destination unit and asks the operator to compare the returned audit unit manually.
- A clear unit conflict automatically creates a Mac alert carrying both the audit unit and label unit and warns
  the phone operator to investigate. It does not mark the row present.
- Shows the package's audit location (such as BIN, BB, CAGE, or FCR) with a successful tracking result.
- Provides **Scan package into alert**: scan tracking and unit, choose the physical location, and save a manual
  alert on the Mac without retyping those package details.
- Returns to live scanning automatically after a saved confirmation. Flashlight, tap-to-focus, wrong-barcode
  rescan, and undo are available on the phone.
- Preserves reliable unknown tracking numbers in Alerts and detects duplicate tracking values for review.
- For an unmatched tracking number, offers **Rescan label** or **Flag for end review** instead of automatically
  adding a miscanned value to Package Errors.
- If a known tracking returns the wrong logged unit, offers **Flag unit mismatch** and carries the logged unit
  and tracking into final review; entering the correct unit generates the complete mislog reason automatically.
- Briefly collects stable codes from multi-barcode labels so USPS/FedEx or other carrier values are all checked
  against the audit before the result is decided.
- Stores a bounded local feedback profile of scan outcomes and barcode formats without images, names, tracking
  values, or raw unit values. A separate local alias map retains confirmed normalized label/audit unit pairs;
  this supports safe tuning but does not claim that ML Kit retrains itself or learns destroyed barcodes.
- Confirms the SQLite save on the Mac before it tells the phone that a package is saved. Reconnect retries use
  idempotent request IDs so an interrupted confirmation is not applied twice.

### Privacy and connection model

- The Android scanner declares Camera, Nearby Devices/Bluetooth, and vibration permissions only. It has no
  Internet, storage, location, microphone, analytics, or cloud barcode-service permission.
- The Bluetooth protocol uses a fresh QR-provisioned key per scanner session, authenticated encryption, ordered
  messages, replay protection, and a bounded reconnect strategy. See [BLUETOOTH_PROTOCOL.md](BLUETOOTH_PROTOCOL.md).
- Audit state remains in `~/.package_audit/audit_state.sqlite3` on the Mac. Use macOS account protection and
  full-disk encryption for production records; SQLite data is not encrypted by this application.
- Optional legacy browser scanners remain available for local Wi-Fi or temporary Cloudflare access, but they are
  not required for the native Android Bluetooth workflow. Remote browser mode sends traffic through Cloudflare.

## Requirements

For the recommended offline workflow:

- macOS with Bluetooth and Apple Command Line Tools
- Python 3.10+ and [uv](https://docs.astral.sh/uv/)
- Android 12+ phone with Bluetooth LE and a camera (the current tested device is Samsung Galaxy S24 Ultra)
- The locally built `Package Audit Scanner` Android APK installed once

Internet is needed only to install development dependencies or build tools. It is not needed while auditing over
Bluetooth. USB is only used to install/update the Android app; unplug it after installation.

## Quick start: run an audit tonight

First-time setup on the Mac:

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit

# Install uv if necessary.
command -v uv || brew install uv

# Create the locked Python environment and build the macOS Bluetooth receiver.
uv sync --locked
bash scripts/build_bluetooth.sh
```

Launch Package Audit and prevent the Mac from idling asleep during the audit:

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit
caffeinate -i uv run package-audit
```

Then:

1. Open the correct BuildingLink Event Log PDF.
2. Click **Bluetooth Phone Scanner** on the Mac.
3. Open **Package Audit Scanner** on Android, tap **Pair with Mac**, and scan the pairing QR in the Mac dialog.
4. Wait for **Connected securely • Bluetooth only**.
5. Leave **Mode: Tracking + unit** selected, tap **Scan packages** once, and keep the tracking barcode plus the
   destination unit visible. For a tracking-only label, switch to **Mode: Tracking only**.
6. If both values match, confirm the result. If the unit cannot be read, compare the displayed audit unit and
   confirm manually, rescan, or flag it. Never confirm a clear mismatch.
7. Wait for **Saved on Mac**. The phone resumes scanning for the next package automatically.
8. For a package that needs an operator-created exception, tap **Scan package into alert**, scan its label, and
   choose its physical location. The Mac retains it in Alerts for final review.
9. Click **Stop Bluetooth Scanner** on the Mac when finished, then export any needed report.

Keep the Mac awake, the phone app in the foreground, and both devices within tested Bluetooth range. VPN,
public Wi-Fi, mobile data, and hotspot configuration do not affect Bluetooth scanning.

## Install the Android scanner once

The signed installer produced by this workspace is:

```text
dist/PackageAuditScanner-0.13.0.apk
```

Enable **USB debugging** in Android Developer options, connect an unlocked phone with a USB data cable, approve
the Mac's debugging key, and run:

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit
adb devices -l
adb install -r dist/PackageAuditScanner-0.13.0.apk
```

The device must display `device` in the first command. Choose **Transferring files / Android Auto** for the USB
connection, not tethering or MIDI. After installation, allow **Camera** and **Nearby devices** in the scanner,
unplug the cable, and disable USB debugging again if you do not otherwise need it.

For detailed installation, recovery, range, privacy, and real-hardware acceptance instructions, read
[BLUETOOTH_USAGE.md](BLUETOOTH_USAGE.md).

## Core workflow details

### Exact matching and exceptions

The scanner deliberately prioritizes correctness over guessing:

- A full tracking number must exactly match an audit record before the app offers a unit to confirm.
- In normal native mode the on-device reader extracts unit candidates from formats such as `Apt 2803`,
  `Unit #2803`, `Suite 2803`, or a standalone `2803`. Matching uses numeric unit identity so building suffixes
  remain configurable without permitting partial-number matches.
- If the audit has only a shortened tracking value, the app does not infer a full match; use desktop search/manual
  verification instead.
- A readable tracking number absent from the audit creates a review alert. Rescan it if needed, or flag it for
  end review and convert it to a categorized Package Error with its full tracking number retained.
- A clear label-unit conflict becomes an automatic mismatch alert with the tracking, audit unit, and scanned
  unit. An unreadable unit can be manually checked and confirmed, rescanned, or flagged. Tracking-only mode is
  the deliberate fallback for labels that contain no reliable destination unit.
- A tracking number logged under more than one audit item is treated as a duplicate for review.
- Damaged, poorly lit, unsupported, or multi-label barcodes may need a rescan or manual audit entry. A scan never
  silently marks an arbitrary row.

### Keyboard-only desktop auditing

| Input | Result |
| --- | --- |
| Type in search | Filters by unit, resident, tracking, package, or tower |
| Down / Up from search | Selects the first / last visible result |
| Down / Up in audit table | Moves between visible rows |
| Enter | Marks or unmarks the selected row, then returns focus to search |
| Enter with one visible result | Marks or unmarks it directly from search |
| Ctrl+A | Marks all visible rows |
| Ctrl+Shift+A | Unmarks all visible rows |

### Audit data and exports

Audit state is keyed to the content hash of the source PDF, so reopening the same export restores its checked
rows and manual sections. **Clear Current Audit** removes checked state, scanner events, alerts, and exceptions
for the current PDF. **Clear Manual Sections** removes only package-error and duplicate rows.

Available exports:

- Audit report (`.txt`)
- Spreadsheet-safe raw audit data (`.csv`)
- Highlighted copy of the source PDF

The default filename for every export is `YYYY_MMDD_package_audit` with the appropriate `.txt`, `.csv`, or
`.pdf` extension (for example, `2026_0912_package_audit.txt`).

Before any export, all open scanner alerts must be reviewed. The final area checklist then requires confirmation
of every configured storage area; completed checks are saved with that audit so interrupted reviews can resume.
The text report's open-package section uses `unit | carrier/type | location | tracking last four` (or a useful
special type such as Perishable/Keys instead of `NaN`). Audit Notes count unique units while also reporting raw
package/error occurrences. Use **Audit Note Rules…** in Alerts to add custom metric/operator/threshold messages.

Source PDFs, generated reports, APKs, build products, and signing material are ignored by Git to reduce the
risk of committing resident or credential data.

## Optional browser scanner modes

These pre-existing modes remain available but are not the recommended solution for the offline workflow:

- **Local Phone Scanner**: phone browser and Mac must reach one another on a trusted non-guest Wi-Fi network.
- **Remote Phone Scanner**: uses free, temporary Cloudflare Quick Tunnel access when devices are on separate
  networks or using VPNs. It requires internet and Cloudflare receives the proxied traffic.

Install Cloudflare's utility only if you specifically need remote browser scanning:

```bash
brew install cloudflared
```

See [TERMINAL_USAGE.md](TERMINAL_USAGE.md) for full browser-mode instructions and troubleshooting.

## Test before a production audit

Run the disposable end-to-end test on the actual Mac and phone after rebuilding or changing Bluetooth code:

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit
uv run python scripts/bluetooth_hardware_test.py
```

It creates a temporary one-package audit, shows a pairing QR plus `APT 1701` and a test tracking QR, and reports
PASS only after **Tracking + unit** mode reads both, matches audit unit `1701S`, and the Mac saves exactly one
confirmation. A tracking-only/manual confirmation does not pass the unit-OCR acceptance test. Rejected rescans
do not cause a false failure. The temporary database is removed when the test closes.

For a real acceptance test, also try representative carrier labels, poor lighting, known unknown/duplicate
labels, background/reconnect behavior, and the actual package-room range. The hardware checklist is in
[BLUETOOTH_USAGE.md](BLUETOOTH_USAGE.md).

## Development and builds

Run local checks:

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit

uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run bandit -q -r app main.py scripts
uv run pip-audit
uv run python main.py --scanner-smoke
uv run python main.py --export-smoke
uv run python main.py --ui-smoke
```

Build the signed Android APK from source (JDK 17, Android SDK platform 35, and Build Tools 35.0.0 required):

```bash
uv run python scripts/build_android.py
```

The build creates or reuses a private local update key under `.signing/` and writes the APK to `dist/`. Back up
that key securely; future updates require it. Never commit or share it.

Build the local macOS app bundle:

```bash
bash scripts/build_bluetooth.sh
uv run pyinstaller package-audit.spec --clean -y
```

The result is `dist/Package Audit.app`. It is suitable for local use but is not currently Developer ID signed or
notarized for distribution to other Macs.

## Project layout

```text
package-audit/
├── app/                       # Desktop UI, parsing, matching, persistence, browser scanner
├── android/                   # Native Android scanner application and JVM tests
├── native/                    # macOS CoreBluetooth receiver
├── scripts/                   # Build and disposable hardware-test utilities
├── tests/                     # Python/Qt regression tests
├── BLUETOOTH_USAGE.md         # Setup, operation, recovery, acceptance checklist
├── BLUETOOTH_PROTOCOL.md      # Bluetooth protocol and security boundaries
└── TERMINAL_USAGE.md          # Detailed terminal and optional browser-scanner guide
```

## Current limitations

- The native Bluetooth desktop receiver currently targets macOS; the Android app is not a general iOS scanner.
- Android installation is currently a local signed APK, not Play Store distribution.
- macOS packaging is currently local and not notarized; macOS may require normal local-app approval.
- Pairing credentials and pending requests intentionally stay in memory. If either app process restarts during a
  pending confirmation, check the Mac audit before continuing and pair again.
- Bluetooth range and barcode/OCR performance depend on real room layout, metal shelving, lighting, label
  quality, address layout, and supported barcode formats. Manual and tracking-only fallbacks remain necessary.
- Local learning is intentionally conservative: it records privacy-safe aggregate outcomes and bounded confirmed
  unit aliases. It does not store label photos or train a custom damaged-barcode vision model.
- Browser scanner modes remain for compatibility, but their networking/security behavior differs from the
  offline Bluetooth mode.

## Recommended next steps

For the product roadmap and working field-test checklist, see [ROADMAP.md](ROADMAP.md).

The best next work should be driven by observations from real audits. In priority order:

1. **Operational polish:** measure the v0.12 barcode-plus-unit scanner against the real label mix, capturing
   latency, unit-read/mismatch accuracy, remaining misses, glare/damage conditions, and confusing wording.
2. **Scan quality and recovery:** use those missed-label examples to tune supported formats, camera behavior,
   guidance for multi-label/damaged labels, and reconnect handling without weakening exact matching.
3. **Production packaging:** make a repeatable release process, preserve the Android signing key safely, and add
   macOS Developer ID signing/notarization before distributing to other Macs.
4. **Field hardening:** test the real package-room range, battery behavior, different Samsung/Android versions,
   and audit interruption/recovery procedures; keep a small pre-audit acceptance checklist.
5. **Product decisions:** define user roles, supported property-management exports, onboarding, update delivery,
   support/privacy policy, and whether the browser scanner should remain a supported fallback.
6. **Only after the core flow is stable:** consider multi-building configuration, richer operational reporting,
   scanner analytics that respect privacy, and support for additional hardware/platforms.

## Version history

- **v0.13.0** — audit location shown on phone scan results, phone-created alerts populated from scanned
  tracking/unit plus an operator-selected location, location-aware desktop alert review, and standardized
  `YYYY_MMDD_package_audit` export filenames.
- **v0.12.0** — bundled on-device destination-unit OCR, exact tracking plus abstracted numeric-unit verification,
  automatic audit-unit/label-unit mismatch alerts, manual unreadable-unit confirmation, tracking-only mode,
  privacy-safe scan outcome profiling, unit-based audit scoring, custom percentage-triggered audit notes, and
  richer open-package rows (`unit | carrier | location | last four`).
- **v0.11.0** — final-area export checklist, deferred/categorized alert review with reusable templates,
  percentage-based audit assessment, one-row physical handling for duplicate logs, multi-barcode collection,
  and phone-side unmatched/mislogged-unit review actions.
- **v0.10.0** — bundled on-device ML Kit plus ZXing fallback, higher-resolution scanning, auto-zoom/refocus and
  aiming guide, package-type classification, meaningful special-item report labels, and threshold-driven Audit
  Notes.
- **v0.9.0** — native Android live barcode scanner, direct encrypted Mac Bluetooth transport, one-time QR
  pairing, exact tracking-only matching, explicit unit confirmation, save verification, reconnect/idempotence,
  undo, signed local APK build, and disposable S24/Mac hardware acceptance test.
- **v0.8.0** — one-tap browser live-camera tracking scan with two-frame stability checks.
- **v0.7.0** — optional temporary Cloudflare remote browser scanner.
- **v0.6.0** — tracking-only matching, explicit unit confirmation, and keyboard-first desktop auditing.
