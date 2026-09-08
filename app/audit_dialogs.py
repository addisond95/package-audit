"""Focused dialogs for final audit review and alert resolution."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from app.constants import AUDIT_AREAS, CARRIER_OPTIONS, LOCATION_OPTIONS
from app.models import PackageError, ScannerAlert, resolve_last4


class AuditAreaChecklistDialog(QDialog):
    """Require an explicit physical-area check before an audit export."""

    def __init__(self, checked: set[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Final area check before export")
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        message = QLabel(
            "Confirm that every package-storage area was physically audited. "
            "Export stays locked until every area is checked."
        )
        message.setWordWrap(True)
        layout.addWidget(message)
        self.checkboxes: dict[str, QCheckBox] = {}
        for key, label in AUDIT_AREAS:
            box = QCheckBox(label)
            box.setChecked(key in checked)
            box.stateChanged.connect(self._update_ready)
            self.checkboxes[key] = box
            layout.addWidget(box)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        self.buttons.button(QDialogButtonBox.Ok).setText("Confirm areas and export")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._update_ready()

    def _update_ready(self) -> None:
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(
            all(box.isChecked() for box in self.checkboxes.values())
        )

    @property
    def checked_areas(self) -> set[str]:
        return {key for key, box in self.checkboxes.items() if box.isChecked()}


class AlertResolutionDialog(QDialog):
    """Convert a reviewed scanner alert into a structured package error."""

    def __init__(self, alert: ScannerAlert, templates: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.alert = alert
        self.setWindowTitle("Add reviewed alert to Package Errors")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        summary = QLabel(alert.message or "Review this scanner alert.")
        summary.setWordWrap(True)
        layout.addWidget(summary)
        form = QFormLayout()
        is_mislog = alert.kind == "mislog"
        self.unit = QLineEdit("" if is_mislog else alert.unit)
        self.location = QComboBox()
        self.location.addItems(LOCATION_OPTIONS)
        self.location.setEditable(True)
        self.carrier = QComboBox()
        self.carrier.addItems(CARRIER_OPTIONS)
        self.carrier.setEditable(True)
        carrier_index = self.carrier.findText(alert.carrier)
        self.carrier.setCurrentIndex(carrier_index if carrier_index >= 0 else 0)
        self.tracking = QLineEdit(alert.tracking)
        self.tracking.setReadOnly(True)
        self.reason = QComboBox()
        self.reason.setEditable(True)
        self.reason.addItems(templates)
        if alert.kind == "duplicate":
            self.reason.setCurrentText("Relogged / duplicate entry")
        elif is_mislog:
            self.reason.setCurrentText("Logged for one unit but belongs to another")
        elif alert.kind in {"not_found", "unmatched"}:
            self.reason.setCurrentText("Not logged — entered")
        if is_mislog:
            logged_unit = QLineEdit(alert.unit)
            logged_unit.setReadOnly(True)
            form.addRow("Logged unit", logged_unit)
            form.addRow("Correct unit", self.unit)
        else:
            form.addRow("Unit", self.unit)
        form.addRow("Location", self.location)
        form.addRow("Carrier / type", self.carrier)
        form.addRow("Tracking", self.tracking)
        form.addRow("Error reason", self.reason)
        layout.addLayout(form)
        note = QLabel("You can edit the selected reason to include the correct and incorrectly logged units.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        self.buttons.button(QDialogButtonBox.Ok).setText("Add error and resolve alert")
        self.buttons.accepted.connect(self._accept_if_complete)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _accept_if_complete(self) -> None:
        if not self.unit.text().strip() or not self.reason.currentText().strip():
            QMessageBox.warning(
                self, "Complete the alert", "Enter a unit and choose or type an error reason."
            )
            return
        self.accept()

    @property
    def package_error(self) -> PackageError:
        tracking = self.tracking.text().strip()
        reason = self.reason.currentText()
        if self.alert.kind == "mislog" and reason == "Logged for one unit but belongs to another":
            reason = f"Logged for unit {self.alert.unit} but belongs to unit {self.unit.text().strip()}"
        return PackageError(
            unit=self.unit.text(),
            location=self.location.currentText(),
            carrier=self.carrier.currentText(),
            tracking=tracking,
            last4=resolve_last4("", tracking),
            note=reason,
        )
