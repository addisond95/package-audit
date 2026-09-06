"""Desktop controls for pairing a phone with the scanner server."""

from __future__ import annotations

import io
import webbrowser
from collections.abc import Callable

import qrcode
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.scanner_server import ScannerServer
from app.scanner_vision import ScannerCapabilities


class ScannerPairingDialog(QDialog):
    """Non-modal QR/code display while the phone scanner is running."""

    def __init__(
        self,
        server: ScannerServer,
        capabilities: ScannerCapabilities,
        stop_callback: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.server = server
        self.stop_callback = stop_callback
        self.setWindowTitle("Phone Scanner")
        self.setMinimumWidth(440)
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        title = QLabel("Remote phone scanner is ready" if server.remote else "Local phone scanner is ready")
        title.setObjectName("scannerTitle")
        layout.addWidget(title)

        status = QLabel(f"Tracking barcode scanner: {'Ready' if capabilities.barcode else 'Unavailable'}")
        status.setObjectName("scannerStatus")
        layout.addWidget(status)

        route_status = QLabel(
            "Cloudflare HTTPS tunnel: connected" if server.remote else "Direct local Wi-Fi connection"
        )
        route_status.setObjectName("scannerStatus")
        layout.addWidget(route_status)

        qr_label = QLabel()
        qr_label.setAlignment(Qt.AlignCenter)
        qr_label.setPixmap(self._qr_pixmap(server.url))
        layout.addWidget(qr_label)

        code = QLabel(server.pairing_code)
        code.setObjectName("pairingCode")
        code.setAlignment(Qt.AlignCenter)
        code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(code)

        self.connection_status = QLabel()
        self.connection_status.setAlignment(Qt.AlignCenter)
        self.connection_status.setObjectName("scannerStatus")
        layout.addWidget(self.connection_status)

        self.expiry_status = QLabel()
        self.expiry_status.setAlignment(Qt.AlignCenter)
        self.expiry_status.setObjectName("scannerStatus")
        layout.addWidget(self.expiry_status)

        url = QLabel(server.url)
        url.setAlignment(Qt.AlignCenter)
        url.setWordWrap(True)
        url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(url)

        privacy = QLabel(
            (
                "Remote mode sends decoded tracking values or small live camera crops "
                "through Cloudflare over HTTPS. Package Audit processes them on this Mac "
                "and does not save them. "
                "Stopping the scanner closes this temporary public address."
            )
            if server.remote
            else (
                "Local mode stays on your trusted Wi-Fi. Package Audit processes "
                "tracking values and live camera crops on this Mac and does not save them."
            )
        )
        privacy.setWordWrap(True)
        privacy.setObjectName("scannerStatus")
        layout.addWidget(privacy)

        buttons = QHBoxLayout()
        open_button = QPushButton("Open on This Mac")
        open_button.clicked.connect(lambda: webbrowser.open(server.url))
        self.copy_button = QPushButton("Copy Address")
        self.copy_button.clicked.connect(self._copy_address)
        close_button = QPushButton("Hide")
        close_button.clicked.connect(self.hide)
        stop_button = QPushButton("Stop Scanner")
        stop_button.setProperty("variant", "danger")
        stop_button.clicked.connect(self._stop)
        buttons.addWidget(open_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        buttons.addWidget(stop_button)
        layout.addLayout(buttons)

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._refresh_status)
        self.status_timer.start(1000)
        self.copy_reset_timer = QTimer(self)
        self.copy_reset_timer.setSingleShot(True)
        self.copy_reset_timer.timeout.connect(lambda: self.copy_button.setText("Copy Address"))
        self._refresh_status()

    @staticmethod
    def _qr_pixmap(value: str) -> QPixmap:
        image = qrcode.make(value)
        output = io.BytesIO()
        image.save(output, format="PNG")
        pixmap = QPixmap()
        pixmap.loadFromData(output.getvalue(), "PNG")
        return pixmap.scaled(250, 250, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _stop(self) -> None:
        self.hide()
        self.stop_callback()

    def _copy_address(self) -> None:
        QApplication.clipboard().setText(self.server.url)
        self.copy_button.setText("Copied")
        self.copy_reset_timer.start(1500)

    def _refresh_status(self) -> None:
        if self.server.remote and not self.server.tunnel_running:
            self.connection_status.setText("Remote tunnel disconnected — stop and restart the scanner")
            return
        phone_count = self.server.coordinator.active_phone_count()
        if phone_count:
            noun = "phone" if phone_count == 1 else "phones"
            self.connection_status.setText(f"● {phone_count} {noun} connected")
        else:
            self.connection_status.setText("Waiting for a phone to connect…")

        seconds = self.server.pairing_seconds_remaining
        if seconds:
            minutes = max(1, (seconds + 59) // 60)
            self.expiry_status.setText(
                f"New phones can pair for {minutes} more min. Connected phones stay paired."
            )
        else:
            self.expiry_status.setText(
                "Pairing code expired. Stop and restart the scanner to connect another phone."
            )
