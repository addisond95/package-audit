"""Focused dialogs for final audit review and alert resolution."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from app.constants import (
    AUDIT_AREAS,
    AUDIT_NOTE_METRICS,
    AUDIT_NOTE_OPERATORS,
    CARRIER_OPTIONS,
    LOCATION_OPTIONS,
)
from app.models import AuditNoteRule, PackageError, ScannerAlert, resolve_last4


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
        self.unit = QLineEdit(alert.label_unit if is_mislog else (alert.unit or alert.label_unit))
        self.location = QComboBox()
        self.location.addItems(LOCATION_OPTIONS)
        self.location.setEditable(True)
        self.location.setCurrentText(alert.location)
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
        elif alert.kind == "unit_unreadable":
            self.reason.setCurrentText("Unit number unreadable — investigated")
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


class AuditNoteRulesDialog(QDialog):
    """Edit reusable percentage-triggered notes without exposing SQL/formulas."""

    def __init__(self, rules: list[AuditNoteRule], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Audit note trigger rules")
        self.resize(980, 430)
        layout = QVBoxLayout(self)
        help_text = QLabel(
            "Each enabled rule adds its note when the selected unit-based percentage meets the condition. "
            "Available placeholders: {score}, {completion_rate}, {open_unit_rate}, {error_unit_rate}, "
            "{duplicate_unit_rate}, {affected_unit_rate}, {affected_units}, {total_units}."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        controls = QHBoxLayout()
        add_button = QPushButton("Add Rule")
        add_button.clicked.connect(lambda: self._append_rule(None))
        delete_button = QPushButton("Delete Selected Rule")
        delete_button.clicked.connect(self._delete_selected)
        controls.addWidget(add_button)
        controls.addWidget(delete_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Enabled", "Metric", "Condition", "Threshold %", "Note"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 75)
        self.table.setColumnWidth(1, 245)
        self.table.setColumnWidth(2, 95)
        self.table.setColumnWidth(3, 115)
        layout.addWidget(self.table)
        for rule in rules:
            self._append_rule(rule)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._accept_valid)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _append_rule(self, rule: AuditNoteRule | None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        enabled = QCheckBox()
        enabled.setChecked(True if rule is None else rule.enabled)
        metric = QComboBox()
        for key, label in AUDIT_NOTE_METRICS:
            metric.addItem(label, key)
        if rule:
            index = metric.findData(rule.metric)
            if index >= 0:
                metric.setCurrentIndex(index)
        condition = QComboBox()
        condition.addItems(AUDIT_NOTE_OPERATORS)
        condition.setCurrentText(rule.operator if rule else ">=")
        threshold = QDoubleSpinBox()
        threshold.setRange(0.0, 100.0)
        threshold.setDecimals(1)
        threshold.setSuffix("%")
        threshold.setValue(rule.threshold if rule else 5.0)
        message = QLineEdit(rule.message if rule else "")
        message.setPlaceholderText("Note added when this condition is true")
        for column, widget in enumerate((enabled, metric, condition, threshold, message)):
            self.table.setCellWidget(row, column, widget)

    def _delete_selected(self) -> None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        for row in sorted(rows, reverse=True):
            self.table.removeRow(row)

    @property
    def rules(self) -> list[AuditNoteRule]:
        values: list[AuditNoteRule] = []
        for row in range(self.table.rowCount()):
            enabled = self.table.cellWidget(row, 0)
            metric = self.table.cellWidget(row, 1)
            condition = self.table.cellWidget(row, 2)
            threshold = self.table.cellWidget(row, 3)
            message = self.table.cellWidget(row, 4)
            if not all((enabled, metric, condition, threshold, message)):
                continue
            values.append(
                AuditNoteRule(
                    message=message.text().strip(),
                    metric=metric.currentData(),
                    operator=condition.currentText(),
                    threshold=threshold.value(),
                    enabled=enabled.isChecked(),
                )
            )
        return values

    def _accept_valid(self) -> None:
        if any(not rule.message for rule in self.rules):
            QMessageBox.warning(self, "Complete the rules", "Every audit-note rule needs a note message.")
            return
        self.accept()
