"""Human annotation UI for opt-in shipping-label training examples."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.constants import CARRIER_OPTIONS
from app.label_training import LABEL_CONDITIONS, REGION_KINDS, validate_annotations

REGION_LABELS = {
    "barcode": "Barcode",
    "tracking_text": "Printed tracking number",
    "unit": "Destination unit",
    "address": "Destination address",
    "last_name": "Resident last name",
}
REGION_COLORS = {
    "barcode": QColor("#e63946"),
    "tracking_text": QColor("#f4a261"),
    "unit": QColor("#2a9d8f"),
    "address": QColor("#457b9d"),
    "last_name": QColor("#9b5de5"),
}


class AnnotationCanvas(QWidget):
    """Display one label and capture normalized rectangular regions."""

    def __init__(self, image_bytes: bytes, regions: list[dict[str, Any]] | None = None):
        super().__init__()
        self.pixmap = QPixmap()
        if not self.pixmap.loadFromData(image_bytes, "JPEG"):
            raise ValueError("The training image could not be displayed.")
        self.regions = {
            item["kind"]: tuple(float(value) for value in item["box"])
            for item in (regions or [])
            if isinstance(item, dict)
            and item.get("kind") in REGION_KINDS
            and isinstance(item.get("box"), list)
            and len(item["box"]) == 4
        }
        self.current_kind = "barcode"
        self.drag_start: QPoint | None = None
        self.drag_end: QPoint | None = None
        self.setMinimumSize(720, 480)
        self.setMouseTracking(True)

    def _image_rect(self) -> QRectF:
        margin = 8.0
        available_width = max(1.0, self.width() - margin * 2)
        available_height = max(1.0, self.height() - margin * 2)
        scale = min(available_width / self.pixmap.width(), available_height / self.pixmap.height())
        width = self.pixmap.width() * scale
        height = self.pixmap.height() * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def set_kind(self, kind: str) -> None:
        if kind in REGION_KINDS:
            self.current_kind = kind

    def remove_current(self) -> None:
        self.regions.pop(self.current_kind, None)
        self.update()

    def normalized_regions(self) -> list[dict[str, Any]]:
        return [
            {"kind": kind, "box": [round(value, 6) for value in box]} for kind, box in self.regions.items()
        ]

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#17212b"))
        target = self._image_rect()
        painter.drawPixmap(target, self.pixmap, QRectF(self.pixmap.rect()))
        for kind, (x, y, width, height) in self.regions.items():
            rect = QRectF(
                target.x() + x * target.width(),
                target.y() + y * target.height(),
                width * target.width(),
                height * target.height(),
            )
            color = REGION_COLORS[kind]
            painter.setPen(QPen(color, 3))
            painter.drawRect(rect)
            painter.fillRect(QRectF(rect.x(), rect.y(), min(rect.width(), 190), 22), color)
            painter.setPen(Qt.white)
            painter.drawText(rect.x() + 5, rect.y() + 16, REGION_LABELS[kind])
        if self.drag_start is not None and self.drag_end is not None:
            painter.setPen(QPen(REGION_COLORS[self.current_kind], 2, Qt.DashLine))
            painter.drawRect(QRectF(self.drag_start, self.drag_end).normalized())

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._image_rect().contains(event.position()):
            self.drag_start = event.position().toPoint()
            self.drag_end = self.drag_start
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.drag_start is not None:
            point = event.position()
            target = self._image_rect()
            self.drag_end = QPoint(
                int(min(max(point.x(), target.left()), target.right())),
                int(min(max(point.y(), target.top()), target.bottom())),
            )
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self.drag_start is None or self.drag_end is None:
            return
        drawn = QRectF(self.drag_start, self.drag_end).normalized()
        target = self._image_rect()
        self.drag_start = self.drag_end = None
        if drawn.width() >= 8 and drawn.height() >= 8:
            clipped = drawn.intersected(target)
            self.regions[self.current_kind] = (
                (clipped.x() - target.x()) / target.width(),
                (clipped.y() - target.y()) / target.height(),
                clipped.width() / target.width(),
                clipped.height() / target.height(),
            )
        self.update()


class LabelAnnotationDialog(QDialog):
    """Review, annotate, approve, or reject one diagnostic example."""

    def __init__(self, sample: dict[str, Any], image_bytes: bytes, parent: QWidget | None = None):
        super().__init__(parent)
        metadata = sample.get("metadata", {})
        existing = metadata.get("annotations", {})
        self.review_status = ""
        self.setWindowTitle("Review label training example")
        self.resize(1180, 820)

        root = QVBoxLayout(self)
        warning = QLabel(
            "This image can contain resident information. Draw only model-relevant regions; do not type "
            "resident names, addresses, or tracking numbers into annotations."
        )
        warning.setWordWrap(True)
        root.addWidget(warning)

        form = QFormLayout()
        self.carrier = QComboBox()
        self.carrier.setEditable(True)
        self.carrier.addItems(CARRIER_OPTIONS)
        self.carrier.setCurrentText(existing.get("carrier") or metadata.get("carrier") or "PKG")
        self.condition = QComboBox()
        self.condition.addItems(LABEL_CONDITIONS)
        self.condition.setCurrentText(existing.get("condition", "good"))
        self.orientation = QComboBox()
        self.orientation.addItems(["0", "90", "180", "270"])
        self.orientation.setCurrentText(str(existing.get("orientation", 0)))
        self.group_id = QLineEdit(
            existing.get("group_id")
            or metadata.get("capture_session")
            or str(metadata.get("created_at", ""))[:10]
        )
        self.group_id.setPlaceholderText("Example: property-a-2026-09-23")
        self.split = QComboBox()
        self.split.addItems(["train", "validation", "test"])
        if sample.get("dataset_split"):
            self.split.setCurrentText(sample["dataset_split"])
        form.addRow("Carrier / label family", self.carrier)
        form.addRow("Primary condition", self.condition)
        form.addRow("Correct orientation", self.orientation)
        form.addRow("Capture group", self.group_id)
        form.addRow("Dataset split", self.split)
        root.addLayout(form)

        controls = QHBoxLayout()
        self.region_kind = QComboBox()
        for kind in REGION_KINDS:
            self.region_kind.addItem(REGION_LABELS[kind], kind)
        remove = QPushButton("Remove selected region")
        controls.addWidget(QLabel("Draw or replace:"))
        controls.addWidget(self.region_kind)
        controls.addWidget(remove)
        controls.addStretch(1)
        root.addLayout(controls)

        self.canvas = AnnotationCanvas(image_bytes, existing.get("regions", []))
        self.region_kind.currentIndexChanged.connect(
            lambda: self.canvas.set_kind(self.region_kind.currentData())
        )
        remove.clicked.connect(self.canvas.remove_current)
        root.addWidget(self.canvas, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        reject = self.buttons.addButton("Reject sample", QDialogButtonBox.DestructiveRole)
        approve = self.buttons.addButton("Approve annotation", QDialogButtonBox.AcceptRole)
        reject.clicked.connect(self._reject_sample)
        approve.clicked.connect(self._approve_sample)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

    @property
    def annotations(self) -> dict[str, Any]:
        return {
            "carrier": self.carrier.currentText(),
            "condition": self.condition.currentText(),
            "orientation": int(self.orientation.currentText()),
            "group_id": self.group_id.text(),
            "regions": self.canvas.normalized_regions(),
        }

    @property
    def dataset_split(self) -> str:
        return self.split.currentText()

    def _approve_sample(self) -> None:
        try:
            validate_annotations(self.annotations)
        except ValueError as exc:
            QMessageBox.warning(self, "Complete the annotation", str(exc))
            return
        self.review_status = "approved"
        self.accept()

    def _reject_sample(self) -> None:
        self.review_status = "rejected"
        self.accept()
