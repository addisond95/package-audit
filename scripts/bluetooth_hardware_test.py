"""Disposable end-to-end Bluetooth/camera acceptance test for the native scanner."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
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
            "2. Keep Mode: Tracking + unit selected, then tap Scan packages.\n"
            "3. Fit both TEST UNIT and TEST TRACKING QR below in the phone preview.\n"
            f"4. Verify the phone says tracking and unit 1701 match audit unit {UNIT}, then confirm."
        )
        instructions.setWordWrap(True)
        unit_label = QLabel("APT 1701")
        unit_label.setStyleSheet("font-size: 44px; font-weight: 800; color: #17212b;")
        unit_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tracking_label = QLabel(f"Test tracking: {TRACKING}")
        tracking_qr = QLabel()
        tracking_qr.setPixmap(ScannerPairingDialog._qr_pixmap(TRACKING))
        result = QLabel("Waiting for a saved confirmation…")
        result.setWordWrap(True)
        layout = scanner.dialog.layout()
        layout.addWidget(instructions)
        layout.addWidget(unit_label)
        layout.addWidget(tracking_label)
        layout.addWidget(tracking_qr)
        layout.addWidget(result)
        scanner.dialog.adjustSize()

        def check_saved() -> None:
            if window.db.load_state(AUDIT_ID).get(ITEM_ID):
                events = window.db.load_scanner_events(AUDIT_ID)
                matched = [event for event in events if event.status == "matched"]
                if len(matched) == 1 and matched[0].details.get("unit_status") == "verified":
                    result.setText(
                        "PASS — the S24 decoded tracking and unit 1701, the Mac matched them to unit "
                        f"{UNIT}, and the encrypted link saved exactly one confirmation."
                    )
                    result.setStyleSheet("color: #176b4d; font-weight: 700;")
                    print("bluetooth-hardware-test-pass", flush=True)
                    timer.stop()
                elif len(matched) == 1:
                    result.setText(
                        "Tracking was saved, but unit OCR was not verified. Undo it and rerun this test in "
                        "Tracking + unit mode with both APT 1701 and the QR visible."
                    )

        timer = QTimer(window)
        timer.timeout.connect(check_saved)
        timer.start(250)
        exit_code = app.exec()
        window.close()
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
