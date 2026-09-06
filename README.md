# BuildingLink Package Audit

Package Audit turns a BuildingLink Event Log PDF into a fast, local package-auditing workflow. It replaces
paper markups and retyped discrepancy notes with a searchable desktop audit, automatic local persistence,
structured exception records, and a direct offline Android scanner.

The primary scanner workflow is a Samsung/Android phone connected directly to a Mac over Bluetooth Low Energy:
no browser, Wi-Fi, hotspot, internet connection, Cloudflare tunnel, account, or subscription is needed during
an audit.

> Status: version 0.9.0 is a locally installed Mac + Android solution that has been tested end-to-end on a
> Mac and Samsung Galaxy S24 Ultra. It is not yet a notarized macOS release, Play Store release, or formally
> independently security-audited product.

## What it does today

### Desktop audit

- Opens BuildingLink Event Log PDFs and extracts unit, resident, package, tower, timestamp, and tracking data.
- Lets you mark packages present with a mouse, keyboard, search, filters, and bulk actions.
- Supports keyboard-first auditing: type a unit, use Up/Down, press Enter to mark a result, then type the next
  unit without reselecting the search field.
- Saves audit progress locally in SQLite and restores it when the same PDF is reopened.
- Keeps package errors and double-logged packages in spreadsheet-style editable tables.
- Exports a text audit report, spreadsheet-safe CSV, and a highlighted copy of the source PDF.

### Offline Android Bluetooth scanner

- Runs as a native Android app, not a web page.
- Uses the phone camera for live barcode recognition; photos and video frames remain in phone memory.
- Sends only decoded barcode values and audit responses through an encrypted Bluetooth connection to the Mac.
- Uses one-time QR pairing for each scanner/audit session; normal macOS Bluetooth-device pairing is not used.
- Matches only an exact, full tracking number. It does not OCR resident names, printed labels, or unit numbers,
  and never marks an audit row from a last-four match alone.
- Shows the unit recorded by the audit on the phone. You compare it with the box label and explicitly confirm
  before the Mac marks the row present.
- Returns to live scanning automatically after a saved confirmation. Flashlight, tap-to-focus, wrong-barcode
  rescan, and undo are available on the phone.
- Flags reliable unknown tracking numbers as `Not logged` and detects duplicate tracking values for review.
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
5. Tap **Scan packages** once. Fill the camera view with one tracking barcode, including its white margins.
6. Check the returned unit against the label and tap **Confirm unit …**.
7. Wait for **Saved on Mac**. The phone resumes scanning for the next package automatically.
8. Click **Stop Bluetooth Scanner** on the Mac when finished, then export any needed report.

Keep the Mac awake, the phone app in the foreground, and both devices within tested Bluetooth range. VPN,
public Wi-Fi, mobile data, and hotspot configuration do not affect Bluetooth scanning.

## Install the Android scanner once

The signed installer produced by this workspace is:

```text
dist/PackageAuditScanner-0.9.0.apk
```

Enable **USB debugging** in Android Developer options, connect an unlocked phone with a USB data cable, approve
the Mac's debugging key, and run:

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit
adb devices -l
adb install -r dist/PackageAuditScanner-0.9.0.apk
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
- If the audit has only a shortened tracking value, the app does not infer a full match; use desktop search/manual
  verification instead.
- A readable tracking number absent from the audit creates a `Not logged` package-error record.
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

It creates a temporary one-package audit, shows a pairing QR and a test tracking QR, and reports PASS only after
the phone scans, receives the expected unit, confirms it, and the Mac has saved exactly one audit event. The
temporary database is removed when the test closes. It does not touch production audit data.

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
- Bluetooth range and barcode performance depend on real room layout, metal shelving, lighting, label quality,
  and supported barcode formats.
- Browser scanner modes remain for compatibility, but their networking/security behavior differs from the
  offline Bluetooth mode.

## Recommended next steps

The best next work should be driven by observations from real audits. In priority order:

1. **Operational polish:** capture scan latency, pairing friction, repeated rescans, barcode formats that miss,
   and any confusing wording during several real audit sessions; use that evidence to simplify the phone flow.
2. **Scan quality and recovery:** tune detection for the real label mix, add clearer guidance for multi-label and
   damaged labels, and improve the reconnect/pending-save experience only where testing exposes friction.
3. **Production packaging:** make a repeatable release process, preserve the Android signing key safely, and add
   macOS Developer ID signing/notarization before distributing to other Macs.
4. **Field hardening:** test the real package-room range, battery behavior, different Samsung/Android versions,
   and audit interruption/recovery procedures; keep a small pre-audit acceptance checklist.
5. **Product decisions:** define user roles, supported property-management exports, onboarding, update delivery,
   support/privacy policy, and whether the browser scanner should remain a supported fallback.
6. **Only after the core flow is stable:** consider multi-building configuration, richer operational reporting,
   scanner analytics that respect privacy, and support for additional hardware/platforms.

## Version history

- **v0.9.0** — native Android live barcode scanner, direct encrypted Mac Bluetooth transport, one-time QR
  pairing, exact tracking-only matching, explicit unit confirmation, save verification, reconnect/idempotence,
  undo, signed local APK build, and disposable S24/Mac hardware acceptance test.
- **v0.8.0** — one-tap browser live-camera tracking scan with two-frame stability checks.
- **v0.7.0** — optional temporary Cloudflare remote browser scanner.
- **v0.6.0** — tracking-only matching, explicit unit confirmation, and keyboard-first desktop auditing.
