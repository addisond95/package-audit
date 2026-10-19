"""Exercise the real Android/Mac BLE transport using only temporary synthetic data.

Build/install the debug APK and androidTest APK first. This checks transport and
device UI; use bluetooth_hardware_test.py separately for optical camera/OCR tests.
"""

from __future__ import annotations

import os
import shlex
import subprocess  # nosec B404
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.main_window as main_window  # noqa: E402
from app.bluetooth_scanner import BluetoothScanner  # noqa: E402
from app.models import AuditEntry  # noqa: E402
from scripts.build_android import resolve_android_home  # noqa: E402


def main() -> None:
    adb = resolve_android_home(dict(os.environ)) / "platform-tools" / "adb"
    with tempfile.TemporaryDirectory(prefix="package-audit-device-smoke-") as temporary:
        main_window.APP_DIR = Path(temporary)
        app = QApplication([])
        window = main_window.PackageAuditApp()
        window.pdf_hash = "disposable-device-test"
        window.entries = [AuditEntry("one", 0, "1701S", "Synthetic", "UPS - #1 - 1Z999AA10123456784", "", "")]
        window.scanner_coordinator.configure(window.pdf_hash, window.entries)
        scanner = BluetoothScanner(
            window.scanner_coordinator,
            window._process_scanner_actions,
            window._bluetooth_save_verified,
            window._save_bluetooth_training_sample,
            window,
        )
        receive = scanner.session.receive

        def checked_receive(packet: bytes) -> bytes:
            try:
                return receive(packet)
            except Exception as exc:
                print(f"Receiver failure: {type(exc).__name__}: {exc}", flush=True)
                raise

        scanner.session.receive = checked_receive
        outcome: list[subprocess.CompletedProcess[str] | Exception] = []
        started = False
        previous_message = ""

        def instrument() -> None:
            try:
                # The SDK executable, test component, and generated pairing data
                # are local; quote the one value interpreted by adb's remote shell.
                completed = subprocess.run(  # nosec B603
                    [
                        str(adb),
                        "shell",
                        "am",
                        "instrument",
                        "-w",
                        "-e",
                        "pairing",
                        shlex.quote(scanner.pairing_qr),
                        "com.packageaudit.scanner.debug.test/com.packageaudit.scanner.ScannerInstrumentation",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=100,
                    check=False,
                )
                outcome.append(completed)
            except Exception as exc:
                outcome.append(exc)

        def state(message: str) -> None:
            nonlocal started, previous_message
            if message != previous_message:
                print(message, flush=True)
                previous_message = message
            if message == "Ready to pair" and not started:
                started = True
                print("Mac receiver ready; testing phone over Bluetooth…", flush=True)
                threading.Thread(target=instrument, daemon=True).start()

        scanner.status_changed.connect(state)
        scanner.start()
        poll = QTimer()
        poll.timeout.connect(lambda: app.quit() if outcome else None)
        poll.start(100)
        QTimer.singleShot(115_000, app.quit)
        app.exec()
        try:
            if not outcome or isinstance(outcome[0], Exception):
                raise RuntimeError("Device test did not finish; check Bluetooth permission/radio state.")
            completed = outcome[0]
            print(completed.stdout, end="")
            if completed.returncode or "PASS:" not in completed.stdout:
                raise RuntimeError("Android instrumentation failed.")
            events = window.db.load_scanner_events(window.pdf_hash)
            if len(events) != 1 or events[0].status != "undone":
                raise RuntimeError("Reconnect created extra events or undo was not persisted.")
            if window.db.load_state(window.pdf_hash).get("one"):
                raise RuntimeError("Undo failed to clear presence.")
            if len(window.training_dataset.list_samples("pending")) != 1:
                raise RuntimeError("Encrypted training transfer was not persisted.")
            print("bluetooth-device-smoke-ok")
        finally:
            scanner.stop()
            window.close()


if __name__ == "__main__":
    main()
