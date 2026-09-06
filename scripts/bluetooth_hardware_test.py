"""Disposable end-to-end Bluetooth/camera acceptance test for the native scanner."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import app.main_window as main_window  # noqa: E402
from app.constants import APP_NAME  # noqa: E402
from app.models import AuditEntry  # noqa: E402
from app.scanner_ui import ScannerPairingDialog  # noqa: E402
from app.theme import build_stylesheet  # noqa: E402

TRACKING = "1Z999AA10123456784"
UNIT = "1701S"
ITEM_ID = "bluetooth-test-package"
AUDIT_ID = "bluetooth-hardware-test"


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("This Bluetooth receiver test requires macOS.")

    with tempfile.TemporaryDirectory(prefix="package-audit-bluetooth-") as temporary:
        main_window.APP_DIR = Path(temporary)
        app = QApplication(sys.argv)
        app.setApplicationName(f"{APP_NAME} Bluetooth Test")
        app.setStyle("Fusion")
        app.setStyleSheet(build_stylesheet())

        window = main_window.PackageAuditApp()
        window.setWindowTitle("Package Audit — Disposable Bluetooth Test")
        window.pdf_hash = AUDIT_ID
        window.entries = [
            AuditEntry(
                item_id=ITEM_ID,
                page_index=0,
                unit=UNIT,
                resident="Test Resident",
                package=f"UPS - test - {TRACKING}",
                tower="South",
                timestamp="Test only",
            )
        ]
        window.source_label.setText("Disposable test audit • no production data")
        window._configure_scanner_for_audit()
        window._refresh_table()
        window.show()
        window.start_bluetooth_scanner()
        scanner = window.bluetooth_scanner
        if scanner is None:
            raise SystemExit("Bluetooth receiver did not start.")

        instructions = QLabel(
            "HARDWARE TEST\n"
            "1. On the phone, scan the large pairing QR above.\n"
            "2. When connected, tap Scan packages.\n"
            "3. Point the phone at the TEST TRACKING QR below.\n"
            f"4. Verify the phone shows Unit {UNIT}, then confirm it."
        )
        instructions.setWordWrap(True)
        tracking_label = QLabel(f"Test tracking: {TRACKING}")
        tracking_qr = QLabel()
        tracking_qr.setPixmap(ScannerPairingDialog._qr_pixmap(TRACKING))
        result = QLabel("Waiting for a saved confirmation…")
        result.setWordWrap(True)
        layout = scanner.dialog.layout()
        layout.addWidget(instructions)
        layout.addWidget(tracking_label)
        layout.addWidget(tracking_qr)
        layout.addWidget(result)
        scanner.dialog.adjustSize()

        def check_saved() -> None:
            if window.db.load_state(AUDIT_ID).get(ITEM_ID):
                events = window.db.load_scanner_events(AUDIT_ID)
                if len(events) == 1 and events[0].status == "matched":
                    result.setText(
                        "PASS — the S24 camera decoded the tracking QR, the encrypted Bluetooth link "
                        "returned the correct unit, and the Mac saved exactly one confirmation."
                    )
                    result.setStyleSheet("color: #176b4d; font-weight: 700;")
                    print("bluetooth-hardware-test-pass", flush=True)
                    timer.stop()

        timer = QTimer(window)
        timer.timeout.connect(check_saved)
        timer.start(250)
        exit_code = app.exec()
        window.close()
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
