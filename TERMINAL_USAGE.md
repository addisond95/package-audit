# Terminal usage

These instructions launch Package Audit from source on macOS. The terminal starts the desktop app; opening
the BuildingLink PDF, auditing packages, pairing the phone, and exporting reports happen in the app window.

## First-time setup

Open Terminal and run:

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit

# Install uv if this command says it is missing.
command -v uv || brew install uv

# Install the Python environment exactly from uv.lock.
uv sync --locked
```

The first `uv sync` can take a few minutes. Future launches reuse the environment.

## Launch the app

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit
uv run package-audit
```

This equivalent command is useful when debugging startup:

```bash
uv run python main.py
```

Keep the terminal process running while using the app. Close the app window when finished; audit progress is
saved automatically in `~/.package_audit/audit_state.sqlite3`.

## Run an audit

1. Click **Open PDF** and choose the BuildingLink Event Log PDF.
2. Click audit rows to mark them complete. Search, **Unchecked only**, and the bulk buttons can narrow or speed
   up the work.
   For keyboard-only entry, type a unit, press **Down** to select a result, and press **Enter** to mark it.
   The old search text is selected automatically, so immediately type the next unit. If only one result is
   visible, press **Enter** directly from the search box. A mouse click also returns focus to search.
3. Add exceptions under **Package Errors** and **Double Logged** when needed. Edits save automatically.
4. Use **Export Audit TXT**, **Export CSV**, or **Export Highlighted PDF** and choose an output location.

The original PDF is never overwritten by a highlighted-PDF export.

## Connect a phone

1. Connect the Mac and phone to the same trusted, non-guest Wi-Fi network.
2. Open an audit PDF in the desktop app.
3. Click **Start Phone Scanner**.
4. If macOS asks whether Python or Package Audit may accept incoming connections, allow it on the trusted
   private network.
5. Scan the QR code in the desktop pairing window with the phone camera. If that does not open, type or copy
   the displayed `http://...` address into the phone browser and enter the six-digit code.
6. On the phone, tap **Scan package**, frame one tracking barcode, take the picture, and accept the camera
   preview. The page resizes and sends the image immediately; there is no separate Submit button. Use
   **Existing photo** only as a fallback.
7. The phone displays the unit logged for that exact tracking number. Check the unit against the box label,
   then tap **Confirm unit …** to mark it present. Tap **Wrong barcode — rescan** if the wrong barcode was read.
8. A readable tracking number absent from the audit is recorded automatically under **Package Errors** as
   `Not logged`. A duplicate audit tracking number is flagged for investigation. Use **Undo** if needed.

The pairing code accepts new phones for 15 minutes. A phone that is already paired stays connected until the
scanner stops or a different audit is loaded. Photos are processed in memory on the Mac and are not saved.

## Phone connection troubleshooting

If the phone cannot open the scanner page:

1. Leave the desktop scanner window open and verify that its status says the scanner is running.
2. Confirm both devices are on the same Wi-Fi name. Guest networks often block device-to-device traffic.
3. Temporarily disconnect VPNs on both devices; a VPN can select the wrong route or block local traffic.
4. Retry the exact address shown by the app. Do not replace it with `localhost` or `127.0.0.1`—those addresses
   point back to the phone itself.
5. Check **System Settings → Network → Firewall** and allow incoming connections for Python or Package Audit.
6. Stop and restart the phone scanner after changing Wi-Fi, after the pairing code expires, or if the Mac wakes
   on a different network. Then scan the new QR code.

If the page opens but scans fail, keep the app running, use a sharp well-lit image, fill the frame with only
the tracking barcode, and check that the pairing window says **Tracking barcode scanner: Ready**.

If a scan times out, confirm that the phone still says **Desktop connected**, keep both devices awake, and move
closer to the Wi-Fi access point. The tracking-only decoder normally returns promptly because it does not run
printed-text OCR.

## Verify the project from Terminal

Run the complete local checks before changing or packaging the app:

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit

uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run bandit -q -r app main.py
uv run pip-audit
uv run python main.py --scanner-smoke
uv run python main.py --export-smoke
uv run python main.py --ui-smoke
```

The scanner tests and smoke test open a temporary local port, so macOS or security software may ask for local
network permission.

## Optional macOS app build

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit
uv run pyinstaller package-audit.spec --clean -y
```

The build is written to `dist/Package Audit.app`. It is not Developer ID signed or notarized, so it is best
suited to local testing until release signing is configured.
