# Offline Samsung-to-Mac scanner

The Android app reads tracking barcodes with its camera and talks directly to the Mac over Bluetooth Low
Energy. No browser, Wi-Fi, mobile data, hotspot, account, subscription, or cloud service is used during scanning.
Initial build-tool/dependency downloads need internet. Installing the APK can use a USB data cable;
the cable is not needed for audits. Android 12 or newer and a Bluetooth-capable Mac are required.

## Install once

The locally built installer is `dist/PackageAuditScanner-0.9.0.apk`. Keep the matching Mac version (0.9.0).

1. On the Samsung, enable Developer options by tapping **Settings → About phone → Software information →
   Build number** seven times, then enable **USB debugging** in Developer options.
2. Connect a USB **data** cable to the Mac, unlock the phone, and approve **Allow USB debugging** for this Mac.
   The USB notification can use **Transferring files / Android Auto**; do not select tethering or MIDI.
   File transfer alone does not enable debugging: the separate **USB debugging** switch must be on.
   See the [Android hardware-device setup guide](https://developer.android.com/studio/run/device).
3. With Android SDK Platform-Tools on your PATH, run:

   ```bash
   cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit
   adb devices -l
   adb install -r dist/PackageAuditScanner-0.9.0.apk
   ```

   The device must say `device`, not `unauthorized`. A blank list means USB debugging or the data connection
   is not available. Try a known data cable/direct Mac port and check the phone's USB notification.
   If several devices are connected, use `adb -s SERIAL install -r ...` with the intended phone's serial.
4. Open **Package Audit Scanner** on the Samsung. Allow **Camera** and **Nearby devices** when prompted.
5. After installation/testing, unplug the cable. You can turn USB debugging off again; Bluetooth scanning
   does not need it. Do not disable other phone security settings globally.

If an old debug build is already installed and Android reports a signing mismatch, do not silently uninstall
it during an audit. Finish/check any pending scan first. A debug APK cannot be updated in place by the locally
signed release APK. Future release updates built with the same private signing key use `adb install -r`.

## Start an audit

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit
uv sync --locked
bash scripts/build_bluetooth.sh  # once, and again after receiver code changes
caffeinate -i uv run package-audit
```

The receiver build needs Apple's Command Line Tools. If absent, run `xcode-select --install` and complete
Apple's installer. After setup, the ordinary launch command is `uv run package-audit`; `caffeinate -i` also
prevents idle Mac sleep for the life of the app. Leave the Mac lid open and keep both devices charged.

1. Open the correct BuildingLink audit PDF on the Mac.
2. Click **Bluetooth Phone Scanner**. Allow Bluetooth access in the macOS prompt. If denied, check
   **System Settings → Privacy & Security → Bluetooth** for the app/launcher used to run it.
3. Wait for **Ready to pair**. Open the Android app, tap **Pair with Mac**, and point at the QR in that dialog.
   This is an app-specific pairing QR, not a website. Do not pair through the system Bluetooth device list.
4. Wait for **Connected securely • Bluetooth only**, then tap **Scan packages** once.
5. Fill the camera preview with one package's tracking barcode. Keep the entire barcode and its white margins
   visible. Tap the preview to focus or use **Flashlight** if needed. Two consistent readings are required.
6. The phone shows the audit's logged unit. Check it against the label, then tap **Confirm unit …**.
   Only confirmation marks the matching audit row checked. Wait for **Saved on Mac**.
7. Scanning resumes automatically after the saved acknowledgment. Move to the next box; the same visible
   barcode is suppressed until it leaves the frame or a different barcode is detected.
8. An unknown tracking number or duplicate produces a review result. Check it, use **Undo last saved scan**
   if needed, then **Scan next package**. **Wrong barcode — rescan** dismisses a pending suggestion.
9. Finish/export in the Mac app, then click **Stop Bluetooth Scanner**. A new audit or scanner restart needs
   a fresh pairing QR.

Matching is exact tracking-only; there is no printed-text/name/unit OCR and no last-four-only matching.
If the PDF contains only a shortened tracking value, the scanner cannot safely infer the full tracking number;
use the Mac's manual unit search for that package. Wrinkled, damaged, very small, or unsupported barcodes may
also require manual lookup. Automatically logged unknown barcodes can be undone.

## Disconnects and safe recovery

- Stay within the range you have actually tested in the package room; metal shelving and walls can shorten
  Bluetooth range. Range and scan latency are hardware/environment-dependent, not guaranteed.
- Keep the phone app in the foreground. Backgrounding pauses its camera and Bluetooth transport. Returning
  reconnects and retries an in-memory pending request without applying it twice during the same Mac session.
- If a confirmation is still waiting, reconnect and wait for the result. **Waiting for Mac to save** is not a
  saved acknowledgment. Do not assume the row was marked.
- If the Android process or Mac app restarts, check the row on the Mac before continuing and pair again.
  Pairing keys and pending requests are deliberately not persisted on the phone. There is no durable offline
  queue for scanning while out of range.
- If discovery fails, check Bluetooth/permissions on both devices, keep the Mac awake, and move close.
  Tap **Reconnect**. If the receiver stopped, click **Stop Bluetooth Scanner** and start it again, then pair
  with the new QR. VPN settings do not control this application's Bluetooth transport.
- Keep the QR private: it grants access to this audit session. Stop/restart the scanner to revoke it.

## Acceptance test before relying on it for an audit

Use a throwaway/test audit first, not production rows. Automated tests are not a substitute for this S24/Mac
radio and camera test:

1. Unplug USB, turn Wi-Fi and mobile data off on the phone, and leave Bluetooth on on both devices.
2. Pair and scan a known full tracking barcode. Verify the exact logged unit appears and the Mac row remains
   unchecked until confirmation. Confirm once and verify **Saved on Mac**, one checked row, and correct totals.
3. Scan several real label types/orientations and check that the result appears promptly. Verify damaged or
   ambiguous labels do not mark an arbitrary row.
4. Scan an unknown tracking barcode and a duplicate test tracking number; check the Mac exception rows and undo.
5. Interrupt Bluetooth while a confirmation is pending. Restore it and verify the pending operation resolves
   without a second event or falsely reporting a save. Check undo restores the previous audit state.
6. Background/reopen the phone app, re-pair once while the camera is already open, and test across the actual
   room with its shelving. Reopen the test PDF to verify checked state persisted.

Do not treat successful compilation or a Mac **Ready to pair** message as evidence this hardware test passed.

## Build the Android installer from source

Install JDK 17 and the Android SDK (Android Studio can provide both the SDK and Platform-Tools). Set
`JAVA_HOME` to JDK 17 and `ANDROID_HOME` to your Android SDK directory. Install Android platform 35 and
Build-Tools 35.0.0 and accept SDK licenses through the SDK manager. Then:

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit
uv run python scripts/build_android.py
```

The Gradle wrapper downloads Gradle 8.11.1 on first use. The script runs release unit tests and Android lint,
builds a signed APK, and copies it to `dist/PackageAuditScanner-0.9.0.apk`. `.signing/` contains the private
local update key and password: back it up securely, never commit/share it, and retain it for future updates.
This is a privately installed build, not a Play Store release or an independently audited security product.

For a build without your release key: `cd android && ./gradlew testDebugUnitTest lintDebug assembleDebug`.
The debug APK is for development only. See [BLUETOOTH_PROTOCOL.md](BLUETOOTH_PROTOCOL.md) for the protocol.
