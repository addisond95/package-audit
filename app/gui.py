from __future__ import annotations

import csv
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QCheckBox,
    QComboBox,
    QStyledItemDelegate,
)

from app.audit_report import write_audit_report
from app.database import AuditDatabase
from app.export_pdf import write_highlighted_pdf
from app.models import (
    AuditEntry,
    DoubleLoggedPackage,
    PackageError,
    normalize_carrier,
    normalize_last4,
    normalize_location,
    normalize_unit,
)
from app.parser import file_hash, parse_buildinglink_pdf


VALID_LOCATIONS = ["", "SHELF", "BIN", "BB", "CG", "UG", "ALPHA", "FCR"]

VALID_CARRIERS = [
    "",
    "USPS",
    "UPS",
    "FEDEX",
    "AMZ",
    "ONTRAC",
    "DHL",
    "PKG",
    "KEY",
    "FOOD",
    "PHARMACY",
]


class ComboBoxDelegate(QStyledItemDelegate):
    def __init__(self, options: list[str], parent=None):
        super().__init__(parent)
        self.options = options

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.addItems(self.options)
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        return combo

    def setEditorData(self, editor, index):
        value = index.data(Qt.EditRole) or index.data(Qt.DisplayRole) or ""
        pos = editor.findText(value)
        if pos >= 0:
            editor.setCurrentIndex(pos)
        else:
            editor.setEditText(value)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText().strip(), Qt.EditRole)


class PackageAuditApp(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("BuildingLink Package Audit")
        self.resize(1500, 900)

        app_data = Path.home() / ".package_audit"
        app_data.mkdir(exist_ok=True)
        self.db = AuditDatabase(app_data / "audit_state.sqlite3")

        self.pdf_path: Path | None = None
        self.pdf_hash: str | None = None
        self.entries: list[AuditEntry] = []
        self.filtered_indices: list[int] = []
        self.highlight_color = QColor(80, 200, 120, 95)
        self.loading_manual_tables = False

        self._build_ui()
        self._build_menu()

    def _build_menu(self) -> None:
        open_action = QAction("Open PDF", self)
        open_action.triggered.connect(self.open_pdf)

        export_report_action = QAction("Export Audit TXT", self)
        export_report_action.triggered.connect(self.export_audit_txt)

        export_csv_action = QAction("Export CSV", self)
        export_csv_action.triggered.connect(self.export_csv)

        export_pdf_action = QAction("Export Highlighted PDF", self)
        export_pdf_action.triggered.connect(self.export_highlighted_pdf)

        clear_current_action = QAction("Clear Current Audit", self)
        clear_current_action.triggered.connect(self.clear_current_audit)

        clear_manual_action = QAction("Clear Manual Report Sections", self)
        clear_manual_action.triggered.connect(self.clear_manual_sections)

        menu = self.menuBar().addMenu("File")
        menu.addAction(open_action)
        menu.addAction(export_report_action)
        menu.addAction(export_csv_action)
        menu.addAction(export_pdf_action)
        menu.addSeparator()
        menu.addAction(clear_current_action)
        menu.addAction(clear_manual_action)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)

        top = QHBoxLayout()

        self.open_button = QPushButton("Open PDF")
        self.open_button.clicked.connect(self.open_pdf)

        self.export_report_button = QPushButton("Export Audit TXT")
        self.export_report_button.clicked.connect(self.export_audit_txt)

        self.export_csv_button = QPushButton("Export CSV")
        self.export_csv_button.clicked.connect(self.export_csv)

        self.export_pdf_button = QPushButton("Export Highlighted PDF")
        self.export_pdf_button.clicked.connect(self.export_highlighted_pdf)

        self.color_button = QPushButton("Highlight Color")
        self.color_button.clicked.connect(self.choose_color)

        self.clear_current_button = QPushButton("Clear Current Audit")
        self.clear_current_button.clicked.connect(self.clear_current_audit)

        self.clear_manual_button = QPushButton("Clear Manual Sections")
        self.clear_manual_button.clicked.connect(self.clear_manual_sections)

        top.addWidget(self.open_button)
        top.addWidget(self.export_report_button)
        top.addWidget(self.export_csv_button)
        top.addWidget(self.export_pdf_button)
        top.addWidget(self.color_button)
        top.addWidget(self.clear_current_button)
        top.addWidget(self.clear_manual_button)

        self.summary_label = QLabel("Open a BuildingLink PDF to begin.")
        self.progress = QProgressBar()
        self.progress.setMinimum(0)
        self.progress.setMaximum(100)

        self.tabs = QTabWidget()
        self.audit_tab = self._build_audit_tab()
        self.errors_tab = self._build_errors_tab()
        self.double_tab = self._build_double_logged_tab()

        self.tabs.addTab(self.audit_tab, "Audit")
        self.tabs.addTab(self.errors_tab, "Package Errors")
        self.tabs.addTab(self.double_tab, "Double Logged")

        self.loading_manual_tables = True
        try:
            self.ensure_blank_last_row(self.errors_table, 5)
            self.ensure_blank_last_row(self.double_table, 4)
        finally:
            self.loading_manual_tables = False

        root_layout.addLayout(top)
        root_layout.addWidget(self.summary_label)
        root_layout.addWidget(self.progress)
        root_layout.addWidget(self.tabs, stretch=1)

        self.setCentralWidget(root)

        # Quality of life shortcuts. Tiny mercy, apparently legal in desktop apps.
        self.mark_all_shortcut = QShortcut(QKeySequence("Ctrl+A"), self)
        self.mark_all_shortcut.activated.connect(self.mark_all_visible)

        self.unmark_all_shortcut = QShortcut(QKeySequence("Ctrl+Shift+A"), self)
        self.unmark_all_shortcut.activated.connect(self.unmark_all_visible)

    def _build_audit_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        controls = QHBoxLayout()

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search unit, resident, package, tracking, tower...")
        self.search_box.textChanged.connect(self.refresh_table)

        self.unchecked_only = QCheckBox("Unchecked only")
        self.unchecked_only.stateChanged.connect(self.refresh_table)

        self.mark_all_visible_button = QPushButton("Mark All Visible")
        self.mark_all_visible_button.clicked.connect(self.mark_all_visible)

        self.unmark_all_visible_button = QPushButton("Unmark All Visible")
        self.unmark_all_visible_button.clicked.connect(self.unmark_all_visible)

        controls.addWidget(self.search_box, stretch=1)
        controls.addWidget(self.unchecked_only)
        controls.addWidget(self.mark_all_visible_button)
        controls.addWidget(self.unmark_all_visible_button)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Done", "Unit", "Last 4", "Resident", "Package", "Tower", "Timestamp", "Page"]
        )
        self.table.cellClicked.connect(self.on_audit_cell_clicked)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 70)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(3, 260)
        self.table.setColumnWidth(4, 570)
        self.table.setColumnWidth(5, 120)
        self.table.setColumnWidth(6, 170)
        self.table.setColumnWidth(7, 60)

        layout.addLayout(controls)
        layout.addWidget(self.table)

        return tab

    def _build_errors_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        help_label = QLabel(
            "Type directly into the table using Tab between cells, or paste multiple package errors below. "
            "Format: Unit | Location | Carrier | Last4 | Note"
        )

        self.error_paste = QTextEdit()
        self.error_paste.setPlaceholderText(
            "1708S | BIN | USPS | 8572 | Logged for wrong unit\n"
            "1803S | BB | AMZ | 1968 | Package found but not logged"
        )
        self.error_paste.setFixedHeight(110)

        buttons = QHBoxLayout()
        add_button = QPushButton("Add Pasted Errors")
        add_button.clicked.connect(self.add_pasted_errors)

        delete_button = QPushButton("Delete Selected Error Rows")
        delete_button.clicked.connect(self.delete_selected_errors)

        add_blank_button = QPushButton("Add Blank Error Row")
        add_blank_button.clicked.connect(self.add_blank_error_row)

        save_button = QPushButton("Save Error Rows")
        save_button.clicked.connect(self.save_error_rows)

        buttons.addWidget(add_button)
        buttons.addWidget(add_blank_button)
        buttons.addWidget(delete_button)
        buttons.addWidget(save_button)

        self.errors_table = QTableWidget(0, 5)
        self.errors_table.setHorizontalHeaderLabels(
            ["Unit", "Location", "Carrier", "Last 4", "Error Note"]
        )
        self.errors_table.setAlternatingRowColors(True)
        self.errors_table.setColumnWidth(0, 90)
        self.errors_table.setColumnWidth(1, 100)
        self.errors_table.setColumnWidth(2, 110)
        self.errors_table.setColumnWidth(3, 90)
        self.errors_table.setColumnWidth(4, 700)
        self.errors_table.setItemDelegateForColumn(
            1, ComboBoxDelegate(VALID_LOCATIONS, self.errors_table)
        )
        self.errors_table.setItemDelegateForColumn(
            2, ComboBoxDelegate(VALID_CARRIERS, self.errors_table)
        )
        self.errors_table.itemChanged.connect(self.on_errors_table_changed)

        layout.addWidget(help_label)
        layout.addWidget(self.error_paste)
        layout.addLayout(buttons)
        layout.addWidget(self.errors_table)

        return tab

    def _build_double_logged_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        help_label = QLabel(
            "Type directly into the table using Tab between cells, or paste multiple double logged packages below. "
            "Format: Unit | Location | Carrier | Last4"
        )

        self.double_paste = QTextEdit()
        self.double_paste.setPlaceholderText(
            "0205S | BIN | FEDEX | 9669\n"
            "3207S | CG | UPS | 1821"
        )
        self.double_paste.setFixedHeight(110)

        buttons = QHBoxLayout()
        add_button = QPushButton("Add Pasted Double Logs")
        add_button.clicked.connect(self.add_pasted_double_logged)

        delete_button = QPushButton("Delete Selected Double Rows")
        delete_button.clicked.connect(self.delete_selected_double_logged)

        add_blank_button = QPushButton("Add Blank Double Row")
        add_blank_button.clicked.connect(self.add_blank_double_row)

        save_button = QPushButton("Save Double Rows")
        save_button.clicked.connect(self.save_double_rows)

        buttons.addWidget(add_button)
        buttons.addWidget(add_blank_button)
        buttons.addWidget(delete_button)
        buttons.addWidget(save_button)

        self.double_table = QTableWidget(0, 4)
        self.double_table.setHorizontalHeaderLabels(
            ["Unit", "Location", "Carrier", "Last 4"]
        )
        self.double_table.setAlternatingRowColors(True)
        self.double_table.setColumnWidth(0, 90)
        self.double_table.setColumnWidth(1, 100)
        self.double_table.setColumnWidth(2, 110)
        self.double_table.setColumnWidth(3, 90)
        self.double_table.setItemDelegateForColumn(
            1, ComboBoxDelegate(VALID_LOCATIONS, self.double_table)
        )
        self.double_table.setItemDelegateForColumn(
            2, ComboBoxDelegate(VALID_CARRIERS, self.double_table)
        )
        self.double_table.itemChanged.connect(self.on_double_table_changed)

        layout.addWidget(help_label)
        layout.addWidget(self.double_paste)
        layout.addLayout(buttons)
        layout.addWidget(self.double_table)

        return tab

    def open_pdf(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open BuildingLink Event Log PDF",
            "",
            "PDF Files (*.pdf)",
        )

        if not path_str:
            return

        path = Path(path_str)

        try:
            entries = parse_buildinglink_pdf(path)
        except Exception as exc:
            QMessageBox.critical(self, "Parse failed", f"Could not parse PDF:\n{exc}")
            return

        if not entries:
            QMessageBox.warning(
                self,
                "No entries found",
                "No package entries were detected. Is this a BuildingLink event log PDF?",
            )
            return

        self.pdf_path = path
        self.pdf_hash = file_hash(path)
        self.entries = entries

        saved = self.db.load_state(self.pdf_hash)
        for entry in self.entries:
            entry.audited = saved.get(entry.item_id, False)

        self.load_manual_rows()
        self.refresh_table()

    def load_manual_rows(self) -> None:
        if not self.pdf_hash:
            return

        errors = self.db.load_package_errors(self.pdf_hash)
        doubles = self.db.load_double_logged(self.pdf_hash)

        self.populate_errors_table(errors)
        self.populate_double_table(doubles)

    def refresh_table(self) -> None:
        query = self.search_box.text().strip().lower()
        unchecked_only = self.unchecked_only.isChecked()

        self.filtered_indices = []

        for idx, entry in enumerate(self.entries):
            haystack = " ".join(
                [
                    entry.unit,
                    entry.last4,
                    entry.resident,
                    entry.package,
                    entry.tower,
                    entry.timestamp,
                    str(entry.page_index + 1),
                ]
            ).lower()

            if query and query not in haystack:
                continue

            if unchecked_only and entry.audited:
                continue

            self.filtered_indices.append(idx)

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.filtered_indices))

        for row, entry_index in enumerate(self.filtered_indices):
            entry = self.entries[entry_index]
            values = [
                "✓" if entry.audited else "",
                entry.unit,
                entry.last4,
                entry.resident,
                entry.package,
                entry.tower,
                entry.timestamp,
                str(entry.page_index + 1),
            ]

            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)

                if col == 0:
                    item.setTextAlignment(Qt.AlignCenter)

                if entry.audited:
                    item.setBackground(self.highlight_color)

                self.table.setItem(row, col, item)

        self.table.setSortingEnabled(True)
        self.update_summary()

    def update_summary(self) -> None:
        total = len(self.entries)
        done = sum(1 for e in self.entries if e.audited)
        remaining = total - done
        percent = int((done / total) * 100) if total else 0
        unique_units = len({e.unit for e in self.entries})

        if total:
            self.summary_label.setText(
                f"{done}/{total} packages audited | {remaining} picked up but not closed out | "
                f"{unique_units} unique units | Showing {len(self.filtered_indices)} rows"
            )
        else:
            self.summary_label.setText("Open a BuildingLink PDF to begin.")

        self.progress.setValue(percent)

    def on_audit_cell_clicked(self, row: int, column: int) -> None:
        if row < 0 or row >= len(self.filtered_indices):
            return

        entry_index = self.filtered_indices[row]
        entry = self.entries[entry_index]
        entry.audited = not entry.audited

        if self.pdf_hash:
            self.db.set_state(self.pdf_hash, entry.item_id, entry.audited)

        self.refresh_table()

    def mark_all_visible(self) -> None:
        """
        Mark every row currently visible in the audit table.

        This respects search and the unchecked-only filter. So if you search one unit,
        only that unit's visible rows get marked. Astonishingly, convenience survives.
        """
        if not self.pdf_hash or not self.filtered_indices:
            return

        changed = False

        for entry_index in self.filtered_indices:
            entry = self.entries[entry_index]

            if not entry.audited:
                entry.audited = True
                self.db.set_state(self.pdf_hash, entry.item_id, True)
                changed = True

        if changed:
            self.refresh_table()

    def unmark_all_visible(self) -> None:
        """
        Unmark every row currently visible in the audit table.

        This also respects search and filters.
        """
        if not self.pdf_hash or not self.filtered_indices:
            return

        changed = False

        for entry_index in self.filtered_indices:
            entry = self.entries[entry_index]

            if entry.audited:
                entry.audited = False
                self.db.set_state(self.pdf_hash, entry.item_id, False)
                changed = True

        if changed:
            self.refresh_table()

    def choose_color(self) -> None:
        color = QColorDialog.getColor(self.highlight_color, self, "Choose audit highlight color")

        if color.isValid():
            color.setAlpha(95)
            self.highlight_color = color
            self.refresh_table()

    def parse_pipe_lines(self, text: str, expected_min_fields: int) -> list[list[str]]:
        rows: list[list[str]] = []

        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue

            if "|" in line:
                parts = [part.strip() for part in line.split("|")]
            else:
                parts = [part.strip() for part in line.split(",")]

            if len(parts) < expected_min_fields:
                QMessageBox.warning(
                    self,
                    "Bad pasted row",
                    f"Line {line_number} has too few fields:\n{raw_line}",
                )
                continue

            rows.append(parts)

        return rows

    def add_pasted_errors(self) -> None:
        rows = self.parse_pipe_lines(self.error_paste.toPlainText(), 5)

        self.loading_manual_tables = True
        try:
            self.remove_trailing_blank_rows(self.errors_table, 5)

            for parts in rows:
                unit, location, carrier, last4 = parts[:4]
                note = " | ".join(parts[4:]).strip()

                self.add_table_row(
                    self.errors_table,
                    [
                        normalize_unit(unit),
                        normalize_location(location),
                        normalize_carrier(carrier),
                        normalize_last4(last4),
                        note,
                    ],
                )

            self.ensure_blank_last_row(self.errors_table, 5)
        finally:
            self.loading_manual_tables = False

        self.error_paste.clear()
        self.save_error_rows()

    def add_pasted_double_logged(self) -> None:
        rows = self.parse_pipe_lines(self.double_paste.toPlainText(), 4)

        self.loading_manual_tables = True
        try:
            self.remove_trailing_blank_rows(self.double_table, 4)

            for parts in rows:
                unit, location, carrier, last4 = parts[:4]

                self.add_table_row(
                    self.double_table,
                    [
                        normalize_unit(unit),
                        normalize_location(location),
                        normalize_carrier(carrier),
                        normalize_last4(last4),
                    ],
                )

            self.ensure_blank_last_row(self.double_table, 4)
        finally:
            self.loading_manual_tables = False

        self.double_paste.clear()
        self.save_double_rows()

    def add_table_row(self, table: QTableWidget, values: list[str]) -> None:
        row = table.rowCount()
        table.insertRow(row)

        for col, value in enumerate(values):
            table.setItem(row, col, QTableWidgetItem(value))

    def add_blank_error_row(self) -> None:
        self.add_table_row(self.errors_table, ["", "", "", "", ""])

    def add_blank_double_row(self) -> None:
        self.add_table_row(self.double_table, ["", "", "", ""])

    def ensure_blank_last_row(self, table: QTableWidget, column_count: int) -> None:
        if table.rowCount() == 0:
            self.add_table_row(table, [""] * column_count)
            return

        last_row = table.rowCount() - 1
        has_any_value = False

        for col in range(column_count):
            item = table.item(last_row, col)
            if item and item.text().strip():
                has_any_value = True
                break

        if has_any_value:
            self.add_table_row(table, [""] * column_count)

    def on_errors_table_changed(self, item: QTableWidgetItem) -> None:
        if self.loading_manual_tables:
            return

        self.normalize_manual_cell(self.errors_table, item)
        self.ensure_blank_last_row(self.errors_table, 5)
        self.save_error_rows()

    def on_double_table_changed(self, item: QTableWidgetItem) -> None:
        if self.loading_manual_tables:
            return

        self.normalize_manual_cell(self.double_table, item)
        self.ensure_blank_last_row(self.double_table, 4)
        self.save_double_rows()

    def normalize_manual_cell(self, table: QTableWidget, item: QTableWidgetItem) -> None:
        col = item.column()
        value = item.text().strip()

        if col == 0:
            new_value = normalize_unit(value)
        elif col == 1:
            new_value = normalize_location(value)
        elif col == 2:
            new_value = normalize_carrier(value)
        elif col == 3:
            new_value = normalize_last4(value) if value else ""
        else:
            new_value = value

        if new_value != value:
            old_state = self.loading_manual_tables
            self.loading_manual_tables = True
            item.setText(new_value)
            self.loading_manual_tables = old_state

    def delete_selected_errors(self) -> None:
        self.delete_selected_rows(self.errors_table)
        self.save_error_rows()

    def delete_selected_double_logged(self) -> None:
        self.delete_selected_rows(self.double_table)
        self.save_double_rows()

    def remove_trailing_blank_rows(self, table: QTableWidget, column_count: int) -> None:
        for row in range(table.rowCount() - 1, -1, -1):
            has_any_value = False

            for col in range(column_count):
                item = table.item(row, col)
                if item and item.text().strip():
                    has_any_value = True
                    break

            if has_any_value:
                break

            table.removeRow(row)

    def delete_selected_rows(self, table: QTableWidget) -> None:
        rows = sorted({index.row() for index in table.selectedIndexes()}, reverse=True)

        for row in rows:
            table.removeRow(row)

    def collect_error_rows(self) -> list[PackageError]:
        rows: list[PackageError] = []

        for row in range(self.errors_table.rowCount()):
            values = [
                self.errors_table.item(row, col).text().strip()
                if self.errors_table.item(row, col)
                else ""
                for col in range(5)
            ]

            if not any(values):
                continue

            unit, location, carrier, last4, note = values

            rows.append(
                PackageError(
                    unit=normalize_unit(unit),
                    location=normalize_location(location),
                    carrier=normalize_carrier(carrier),
                    last4=normalize_last4(last4),
                    note=note.strip(),
                )
            )

        return rows

    def collect_double_rows(self) -> list[DoubleLoggedPackage]:
        rows: list[DoubleLoggedPackage] = []

        for row in range(self.double_table.rowCount()):
            values = [
                self.double_table.item(row, col).text().strip()
                if self.double_table.item(row, col)
                else ""
                for col in range(4)
            ]

            if not any(values):
                continue

            unit, location, carrier, last4 = values

            rows.append(
                DoubleLoggedPackage(
                    unit=normalize_unit(unit),
                    location=normalize_location(location),
                    carrier=normalize_carrier(carrier),
                    last4=normalize_last4(last4),
                )
            )

        return rows

    def save_error_rows(self) -> None:
        if not self.pdf_hash:
            return

        self.db.replace_package_errors(self.pdf_hash, self.collect_error_rows())

    def save_double_rows(self) -> None:
        if not self.pdf_hash:
            return

        self.db.replace_double_logged(self.pdf_hash, self.collect_double_rows())

    def populate_errors_table(self, rows: list[PackageError]) -> None:
        self.loading_manual_tables = True
        try:
            self.errors_table.setRowCount(0)

            for row in rows:
                self.add_table_row(
                    self.errors_table,
                    [row.unit, row.location, row.carrier, row.last4, row.note],
                )

            self.ensure_blank_last_row(self.errors_table, 5)
        finally:
            self.loading_manual_tables = False

    def populate_double_table(self, rows: list[DoubleLoggedPackage]) -> None:
        self.loading_manual_tables = True
        try:
            self.double_table.setRowCount(0)

            for row in rows:
                self.add_table_row(
                    self.double_table,
                    [row.unit, row.location, row.carrier, row.last4],
                )

            self.ensure_blank_last_row(self.double_table, 4)
        finally:
            self.loading_manual_tables = False

    def clear_current_audit(self) -> None:
        """
        Clear everything saved for the currently loaded PDF:
        checked rows, package errors, and double logged entries.

        This is for starting a fresh audit from the same reused PDF/export.
        """
        if not self.pdf_hash:
            QMessageBox.information(self, "No PDF loaded", "Open a PDF first.")
            return

        answer = QMessageBox.question(
            self,
            "Clear current audit?",
            "This will clear checked rows, package errors, and double logged rows for the currently loaded PDF.\n\n"
            "It will not delete the PDF. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if answer != QMessageBox.Yes:
            return

        self.db.clear_all_for_pdf(self.pdf_hash)

        for entry in self.entries:
            entry.audited = False

        self.populate_errors_table([])
        self.populate_double_table([])
        self.refresh_table()

        QMessageBox.information(self, "Cleared", "Current audit data has been cleared.")

    def clear_manual_sections(self) -> None:
        """
        Clear only Section 2 and Section 3 manual report rows.

        This keeps the checked audit rows intact.
        """
        if not self.pdf_hash:
            QMessageBox.information(self, "No PDF loaded", "Open a PDF first.")
            return

        answer = QMessageBox.question(
            self,
            "Clear manual sections?",
            "This will clear Package Errors and Double Logged rows only.\n\n"
            "Checked audit rows will stay as they are. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if answer != QMessageBox.Yes:
            return

        self.db.clear_manual_rows(self.pdf_hash)
        self.populate_errors_table([])
        self.populate_double_table([])

        QMessageBox.information(self, "Cleared", "Manual report sections have been cleared.")

    def export_audit_txt(self) -> None:
        if not self.entries:
            QMessageBox.information(self, "Nothing to export", "Open a PDF first.")
            return

        self.save_error_rows()
        self.save_double_rows()

        default_name = "package_audit_report.txt"
        if self.pdf_path:
            default_name = f"{self.pdf_path.stem}_audit_report.txt"

        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export Audit TXT",
            default_name,
            "Text Files (*.txt)",
        )

        if not path_str:
            return

        write_audit_report(
            output_path=Path(path_str),
            entries=self.entries,
            package_errors=self.collect_error_rows(),
            double_logged=self.collect_double_rows(),
            source_pdf_name=self.pdf_path.name if self.pdf_path else "",
        )

        QMessageBox.information(self, "Exported", f"Audit report saved:\n{path_str}")

    def export_csv(self) -> None:
        if not self.entries:
            QMessageBox.information(self, "Nothing to export", "Open a PDF first.")
            return

        default_name = "package_audit.csv"
        if self.pdf_path:
            default_name = f"{self.pdf_path.stem}_audit.csv"

        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export Audit CSV",
            default_name,
            "CSV Files (*.csv)",
        )

        if not path_str:
            return

        with open(path_str, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["audited", "unit", "last4", "resident", "package", "tower", "timestamp", "page"]
            )

            for entry in self.entries:
                writer.writerow(
                    [
                        "yes" if entry.audited else "no",
                        entry.unit,
                        entry.last4,
                        entry.resident,
                        entry.package,
                        entry.tower,
                        entry.timestamp,
                        entry.page_index + 1,
                    ]
                )

        QMessageBox.information(self, "Exported", f"CSV saved:\n{path_str}")

    def export_highlighted_pdf(self) -> None:
        if not self.pdf_path or not self.entries:
            QMessageBox.information(self, "Nothing to export", "Open a PDF first.")
            return

        default_name = f"{self.pdf_path.stem}_highlighted.pdf"
        output_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export Highlighted PDF",
            default_name,
            "PDF Files (*.pdf)",
        )

        if not output_str:
            return

        try:
            write_highlighted_pdf(
                input_pdf_path=self.pdf_path,
                output_pdf_path=Path(output_str),
                entries=self.entries,
                highlight_color=self.highlight_color,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", f"Could not write highlighted PDF:\n{exc}")
            return

        QMessageBox.information(self, "Exported", f"Highlighted PDF saved:\n{output_str}")

    def closeEvent(self, event) -> None:
        self.save_error_rows()
        self.save_double_rows()
        self.db.close()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = PackageAuditApp()
    window.show()
    sys.exit(app.exec())
