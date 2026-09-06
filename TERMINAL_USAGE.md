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

# Build the Mac receiver for the native offline Android scanner.
bash scripts/build_bluetooth.sh
```

The first `uv sync` can take a few minutes. Future launches reuse the environment.
The receiver build needs Apple's Command Line Tools (`xcode-select --install` if missing).
The Android installation and offline acceptance test are in [BLUETOOTH_USAGE.md](BLUETOOTH_USAGE.md).

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

## Connect your Samsung offline over Bluetooth

1. Install the native **Package Audit Scanner** APK once, following [BLUETOOTH_USAGE.md](BLUETOOTH_USAGE.md).
2. Turn on Bluetooth on both devices. Internet, Wi-Fi, hotspot, and Cloudflare are not needed.
3. Open an audit PDF on the Mac and click **Bluetooth Phone Scanner**. Allow Bluetooth permission if prompted.
4. In the Android app, tap **Pair with Mac** and scan that window's QR code. Do not use the Samsung camera app
   or the macOS Bluetooth pairing screen; pairing happens inside Package Audit.
5. Once connected, tap **Scan packages** and point at one tracking barcode. Check the displayed unit and tap
   **Confirm unit**. Wait for **Saved on Mac**; scanning resumes automatically for the next package.
6. Keep the Mac awake and nearby. To prevent idle sleep for this run, launch with
   `caffeinate -i uv run package-audit`. Keep the phone scanner in the foreground.

Stop the scanner on the Mac when finished. Opening a different audit requires a new pairing QR.

## Optional: connect a phone browser locally

1. Connect the Mac and phone to the same trusted, non-guest Wi-Fi network.
2. Open an audit PDF in the desktop app.
3. Click **Local Phone Scanner**.
4. If macOS asks whether Python or Package Audit may accept incoming connections, allow it on the trusted
   private network.
5. Scan the QR code in the desktop pairing window with the phone camera. If that does not open, type or copy
   the displayed `http://...` address into the phone browser and enter the six-digit code.
6. On the phone, tap **Scan package** once and hold one tracking barcode inside the green live guide. The page
   reads it automatically; there is no shutter, camera preview, or Submit button. Use **Existing photo** only as
   a fallback. If the browser blocks live camera access on the local HTTP address, **Scan package** switches to
   the phone's normal camera-photo screen; use Remote mode for the full HTTPS live-camera workflow.
7. The phone displays the unit logged for that exact tracking number. Check the unit against the box label,
   then tap **Confirm unit …** to mark it present. Tap **Wrong barcode — rescan** if the wrong barcode was read,
   or **Scan next package** to resume the already-open live camera.
8. A readable tracking number absent from the audit is recorded automatically under **Package Errors** as
   `Not logged`. A duplicate audit tracking number is flagged for investigation. Use **Undo** if needed.

The pairing code accepts new phones for 15 minutes. A phone that is already paired stays connected until the
scanner stops or a different audit is loaded. Tracking values and fallback camera images are processed in memory
on the Mac and are not saved.

## Optional: connect a phone browser on public Wi-Fi or through VPNs

Install `cloudflared` once if you skipped it during setup:

```bash
brew install cloudflared
cloudflared --version
```

Then:

1. Leave the VPN enabled on both the Mac and phone. They do not need to be on the same Wi-Fi, but both need
   internet access.
2. Open the audit PDF and click **Remote Phone Scanner**.
3. Wait for **Cloudflare HTTPS tunnel: connected**, then scan the new QR code with the phone. The app verifies
   the temporary address before displaying it and automatically retries once if Cloudflare returns a bad address.
4. Tap **Connect to desktop**. The QR link fills the six-digit code automatically; it is not sent to Cloudflare
   in the initial address request.
5. Scan and confirm packages exactly as in Local mode.
6. Click **Stop Scanner** when finished. This shuts down the temporary public address immediately.

Remote mode is free and does not require a Cloudflare account. The address changes on every start and is not a
permanent hosted service. The encrypted requests, decoded tracking values, and any fallback live camera crops
pass through Cloudflare on their way to the Mac. Package Audit does not save them.

## Phone connection troubleshooting

If the phone cannot open the Local scanner page:

1. Leave the desktop scanner window open and verify that its status says the scanner is running.
2. Confirm both devices are on the same Wi-Fi name. Guest networks often block device-to-device traffic.
3. Temporarily disconnect VPNs on both devices; a VPN can select the wrong route or block local traffic.
4. Retry the exact address shown by the app. Do not replace it with `localhost` or `127.0.0.1`—those addresses
   point back to the phone itself.
5. Check **System Settings → Network → Firewall** and allow incoming connections for Python or Package Audit.
6. Stop and restart the phone scanner after changing Wi-Fi, after the pairing code expires, or if the Mac wakes
   on a different network. Then scan the new QR code.

For the native Android app, use **Bluetooth Phone Scanner** to avoid network routing entirely. If using a
browser, **Remote Phone Scanner** is the optional internet-based alternative.

If Remote mode does not start:

1. Run `cloudflared --version`. If it is missing, run `brew install cloudflared`.
2. Confirm the Mac can load ordinary HTTPS sites through its current VPN.
3. Stop and restart Remote mode to request a fresh address.
4. If the error mentions `config.yml` or `config.yaml`, temporarily move your existing Cloudflare Tunnel config;
   Quick Tunnels cannot run while that config is active.
5. If `cloudflared` is installed somewhere unusual, launch with its full path configured:

   ```bash
   PACKAGE_AUDIT_CLOUDFLARED=/full/path/to/cloudflared uv run package-audit
   ```

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

To test the external service too, run this separately; it needs `cloudflared` and internet access:

```bash
uv run python main.py --remote-scanner-smoke
```

The scanner tests and smoke test open a temporary local port, so macOS or security software may ask for local
network permission.

## Optional macOS app build

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit
bash scripts/build_bluetooth.sh
uv run pyinstaller package-audit.spec --clean -y
```

The build is written to `dist/Package Audit.app`. It is not Developer ID signed or notarized, so it is best
suited to local testing until release signing is configured.
