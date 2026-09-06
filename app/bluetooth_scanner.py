"""Qt integration for the offline Mac Bluetooth transport."""

from __future__ import annotations

import base64
import json
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Signal
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

from app.bluetooth_protocol import MAX_PACKET, ScannerBluetoothSession, compact, encode64
from app.scanner_ui import ScannerPairingDialog


class BluetoothScanner(QObject):
    status_changed = Signal(str)
    paired_changed = Signal(bool)

    def __init__(self, coordinator, flush, verify_saved, parent=None):
        super().__init__(parent)
        self.session = ScannerBluetoothSession(coordinator, flush, verify_saved)
        self.service = str(uuid.uuid4())
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(self._discard_stderr)
        self.process.errorOccurred.connect(
            lambda _error: self.status_changed.emit("Bluetooth helper failed.")
        )
        self.process.finished.connect(self._finished)
        self.buffer = bytearray()
        self.dialog = None

    @property
    def pairing_qr(self) -> str:
        return (
            "packageaudit:"
            + compact({"v": 1, "service": self.service, "key": encode64(self.session.secret)}).decode()
        )

    def start(self) -> None:
        if sys.platform != "darwin":
            raise OSError("The Bluetooth desktop receiver currently requires macOS.")
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        helper = root / "native" / "build" / "PackageAuditBluetooth"
        if not helper.is_file():
            raise OSError("Build the Bluetooth helper first: bash scripts/build_bluetooth.sh")
        self.process.start(str(helper), [self.service])

    def stop(self) -> None:
        self.process.closeWriteChannel()
        if not self.process.waitForFinished(1000):
            self.process.terminate()
            if not self.process.waitForFinished(1000):
                self.process.kill()
                self.process.waitForFinished(1000)
        self.session.disconnect()
        if self.dialog:
            self.dialog.close()

    def _finished(self, *_args) -> None:
        self.session.disconnect()
        self.paired_changed.emit(False)
        self.status_changed.emit("Bluetooth stopped. Click Stop Bluetooth Scanner, then start it again.")

    def _discard_stderr(self) -> None:
        # OS diagnostics can include device identifiers. Do not persist them.
        self.process.readAllStandardError()

    def _send(self, value: dict) -> None:
        self.process.write(compact(value) + b"\n")

    def _read(self) -> None:
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        if len(self.buffer) > MAX_PACKET * 8:
            self.buffer.clear()
            self._send({"op": "reset"})
            return
        while b"\n" in self.buffer:
            line, _, remaining = self.buffer.partition(b"\n")
            self.buffer = bytearray(remaining)
            try:
                event = json.loads(line)
                kind = event.get("event")
                if kind in {"connected", "disconnected"}:
                    self.session.disconnect()
                    self.paired_changed.emit(False)
                    self.status_changed.emit(
                        "Authenticating phone…" if kind == "connected" else "Waiting for phone…"
                    )
                elif kind == "packet":
                    response = self.session.receive(base64.b64decode(event["data"], validate=True))
                    self._send({"op": "send", "data": encode64(response)})
                    if self.session.authenticated:
                        self.paired_changed.emit(True)
                        self.status_changed.emit("Phone connected securely • Bluetooth only")
                elif kind in {"ready", "state", "error"}:
                    self.status_changed.emit(event.get("message", "Bluetooth unavailable"))
            except Exception:
                # Fail closed, including persistence errors. Never emit a saved acknowledgment.
                self.session.disconnect()
                self._send({"op": "reset"})
                self.paired_changed.emit(False)
                self.status_changed.emit(
                    "Connection reset. Reconnect the phone; check the audit if a save was pending."
                )


class BluetoothPairingDialog(QDialog):
    def __init__(self, scanner, stop, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Offline Bluetooth Scanner")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        instruction = QLabel("Open Package Audit Scanner on your Android phone and scan this pairing QR.")
        instruction.setWordWrap(True)
        layout.addWidget(instruction)
        self.qr = QLabel()
        self.qr.setPixmap(ScannerPairingDialog._qr_pixmap(scanner.pairing_qr))
        layout.addWidget(self.qr)
        self.status = QLabel("Starting Bluetooth…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        scanner.status_changed.connect(self.status.setText)
        scanner.paired_changed.connect(lambda paired: self.qr.setVisible(not paired))
        note = QLabel(
            "No internet, Wi-Fi, browser, or photos transmitted. Keep the Mac awake.\n"
            "The pairing key expires when Bluetooth scanning stops or the audit changes."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        button = QPushButton("Stop Bluetooth Scanner")
        button.clicked.connect(stop)
        layout.addWidget(button)
