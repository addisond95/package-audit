"""Desktop controls for pairing a phone with the local scanner server."""

from __future__ import annotations

import io
import webbrowser
from collections.abc import Callable

import qrcode
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
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

        title = QLabel("Phone scanner is ready")
        title.setObjectName("scannerTitle")
        layout.addWidget(title)

        status = QLabel(
            f"Barcode/QR: {'Ready' if capabilities.barcode else 'Unavailable'}   "
            f"OCR: {capabilities.ocr_engine if capabilities.ocr else 'Unavailable'}"
        )
        status.setObjectName("scannerStatus")
        layout.addWidget(status)

        qr_label = QLabel()
        qr_label.setAlignment(Qt.AlignCenter)
        qr_label.setPixmap(self._qr_pixmap(server.url))
        layout.addWidget(qr_label)

        code = QLabel(server.pairing_code)
        code.setObjectName("pairingCode")
        code.setAlignment(Qt.AlignCenter)
        code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(code)

        expiry = QLabel("Pairing code expires in 15 minutes. Paired phones stay connected.")
        expiry.setAlignment(Qt.AlignCenter)
        expiry.setObjectName("scannerStatus")
        layout.addWidget(expiry)

        url = QLabel(server.url)
        url.setAlignment(Qt.AlignCenter)
        url.setWordWrap(True)
        url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(url)

        buttons = QHBoxLayout()
        open_button = QPushButton("Open on This Mac")
        open_button.clicked.connect(lambda: webbrowser.open(server.url))
        close_button = QPushButton("Hide")
        close_button.clicked.connect(self.hide)
        stop_button = QPushButton("Stop Scanner")
        stop_button.setProperty("variant", "danger")
        stop_button.clicked.connect(self._stop)
        buttons.addWidget(open_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        buttons.addWidget(stop_button)
        layout.addLayout(buttons)

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
