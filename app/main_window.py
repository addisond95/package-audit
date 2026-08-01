"""Main application window for the BuildingLink Package Audit tool."""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.audit_report import write_audit_report
from app.constants import (
    APP_DIR,
    APP_NAME,
    CARRIER_OPTIONS,
    DB_FILENAME,
    DEFAULT_HIGHLIGHT_RGBA,
    LOCATION_OPTIONS,
)
from app.database import AuditDatabase
from app.delegates import ComboBoxDelegate
from app.export_pdf import write_highlighted_pdf
from app.models import (
    AuditEntry,
    DoubleLoggedPackage,
    PackageError,
    ScannerAlert,
    ScannerEvent,
    normalize_carrier,
    normalize_last4,
    normalize_location,
    normalize_tracking,
    normalize_unit,
)
from app.parser import file_hash, parse_buildinglink_pdf
from app.scanner_server import ScannerAction, ScannerCoordinator, ScannerServer
from app.scanner_ui import ScannerPairingDialog
from app.scanner_vision import scanner_capabilities
from app.theme import build_stylesheet

AUDIT_HEADERS = ["Done", "Unit", "Last 4", "Resident", "Package", "Tower", "Timestamp", "Page"]
ERROR_HEADERS = ["Unit", "Location", "Carrier", "Tracking", "Last 4", "Error Note"]
DOUBLE_HEADERS = ["Unit", "Location", "Carrier", "Tracking", "Last 4"]
ALERT_HEADERS = ["Status", "Type", "Unit", "Carrier", "Tracking", "Last 4", "Message", "Time"]

ERROR_COLUMNS = len(ERROR_HEADERS)
DOUBLE_COLUMNS = len(DOUBLE_HEADERS)

#: Stored on the first column of each audit row so toggling stays correct even
#: after the user re-sorts the table by clicking a header.
ENTRY_INDEX_ROLE = Qt.UserRole
ALERT_KEY_ROLE = Qt.UserRole + 1

ALERT_COLORS = {
    "error": QColor(242, 95, 92, 95),
    "warning": QColor(245, 166, 35, 95),
    "review": QColor(242, 201, 76, 95),
}


class PackageAuditApp(QMainWindow):
    """Top-level window orchestrating parsing, auditing, and exports."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1500, 900)
        self.setMinimumSize(1100, 640)

        APP_DIR.mkdir(exist_ok=True)
        self.db = AuditDatabase(APP_DIR / DB_FILENAME)

        self.pdf_path: Path | None = None
        self.pdf_hash: str | None = None
        self.entries: list[AuditEntry] = []
        self.filtered_indices: list[int] = []
        self.highlight_color = QColor(*DEFAULT_HIGHLIGHT_RGBA)
        self.loading_manual_tables = False
        self.scanner_coordinator = ScannerCoordinator()
        self.scanner_server: ScannerServer | None = None
        self.scanner_dialog: ScannerPairingDialog | None = None
        self.scanner_undo: dict[str, dict] = {}
        self.scanner_item_states: dict[str, str] = {}
        self.open_alert_count = 0

        self._build_menu()
        self._build_ui()
        self._build_shortcuts()
        self._refresh_table()

        self.scanner_timer = QTimer(self)
        self.scanner_timer.timeout.connect(self._process_scanner_actions)
        self.scanner_timer.start(100)

    # ------------------------------------------------------------------ UI
    def _build_menu(self) -> None:
        actions = [
            ("Open PDF", self.open_pdf),
            None,
            ("Export Audit TXT", self.export_audit_txt),
            ("Export CSV", self.export_csv),
            ("Export Highlighted PDF", self.export_highlighted_pdf),
            None,
            ("Clear Current Audit", self.clear_current_audit),
            ("Clear Manual Report Sections", self.clear_manual_sections),
        ]

        menu = self.menuBar().addMenu("File")
        for item in actions:
            if item is None:
                menu.addSeparator()
                continue
            label, handler = item
            action = QAction(label, self)
            action.triggered.connect(handler)
            menu.addAction(action)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(12)

        layout.addLayout(self._build_header())
        layout.addLayout(self._build_toolbar())
        layout.addLayout(self._build_stats_row())
        layout.addWidget(self.progress)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_audit_tab(), "Audit")
        self.tabs.addTab(self._build_errors_tab(), "Package Errors")
        self.tabs.addTab(self._build_double_logged_tab(), "Double Logged")
        self.tabs.addTab(self._build_alerts_tab(), "Alerts")
        layout.addWidget(self.tabs, stretch=1)

        self._with_table_loading(
            lambda: (
                self._ensure_blank_last_row(self.errors_table, ERROR_COLUMNS),
                self._ensure_blank_last_row(self.double_table, DOUBLE_COLUMNS),
            )
        )

        self.setCentralWidget(root)
        self.statusBar().showMessage("Open a BuildingLink PDF to begin.")

    def _build_header(self) -> QHBoxLayout:
        title_box = QVBoxLayout()
        title_box.setSpacing(2)

        title = QLabel("Package Audit")
        title.setObjectName("appTitle")

        self.source_label = QLabel("No file loaded")
        self.source_label.setObjectName("appSubtitle")

        title_box.addWidget(title)
        title_box.addWidget(self.source_label)

        self.open_button = self._make_button("Open PDF", self.open_pdf, variant="primary")

        header = QHBoxLayout()
        header.addLayout(title_box)
        header.addStretch(1)
        header.addWidget(self.open_button)
        return header

    def _build_toolbar(self) -> QHBoxLayout:
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self._add_button(toolbar, "Export Audit TXT", self.export_audit_txt)
        self._add_button(toolbar, "Export CSV", self.export_csv)
        self._add_button(toolbar, "Export Highlighted PDF", self.export_highlighted_pdf)
        self._add_button(toolbar, "Highlight Color", self.choose_color)
        self.scanner_button = self._add_button(toolbar, "Start Phone Scanner", self.start_phone_scanner)
        toolbar.addWidget(self._make_vline())
        self._add_button(toolbar, "Clear Current Audit", self.clear_current_audit, "danger")
        self._add_button(toolbar, "Clear Manual Sections", self.clear_manual_sections, "danger")
        toolbar.addStretch(1)
        return toolbar

    def _build_stats_row(self) -> QHBoxLayout:
        self.audited_chip = self._make_chip()
        self.remaining_chip = self._make_chip()
        self.units_chip = self._make_chip()
        self.showing_chip = self._make_chip()
        self.alerts_chip = self._make_chip()

        row = QHBoxLayout()
        row.setSpacing(8)
        for chip in (
            self.audited_chip,
            self.remaining_chip,
            self.units_chip,
            self.showing_chip,
            self.alerts_chip,
        ):
            row.addWidget(chip)
        row.addStretch(1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFixedWidth(280)
        self.progress.setFormat("%p% audited")
        row.addWidget(self.progress)
        return row

    def _build_audit_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.search_box = QLineEdit()
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setPlaceholderText("Search unit, resident, package, tracking, tower...")
        self.search_box.textChanged.connect(self._refresh_table)

        self.unchecked_only = QCheckBox("Unchecked only")
        self.unchecked_only.stateChanged.connect(self._refresh_table)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(self.search_box, stretch=1)
        controls.addWidget(self.unchecked_only)
        self._add_button(controls, "Mark All Visible", self.mark_all_visible)
        self._add_button(controls, "Unmark All Visible", self.unmark_all_visible)

        self.table = QTableWidget(0, len(AUDIT_HEADERS))
        self.table.setHorizontalHeaderLabels(AUDIT_HEADERS)
        self.table.cellClicked.connect(self.on_audit_cell_clicked)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)

        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        for col, width in enumerate((70, 90, 80, 260, 570, 120, 170, 60)):
            self.table.setColumnWidth(col, width)
        header.setSectionResizeMode(4, QHeaderView.Stretch)  # Package column flexes.

        layout.addLayout(controls)
        layout.addWidget(self.table)
        return tab

    def _build_errors_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        help_label = QLabel(
            "Tab between cells to move across columns. A new blank row is added automatically "
            "as you type. Fields: Unit · Location · Carrier · Tracking · Last 4 · Note"
        )
        help_label.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._add_button(buttons, "Add Blank Row", self.add_blank_error_row)
        self._add_button(buttons, "Delete Selected Rows", self.delete_selected_errors, "danger")
        self._add_button(buttons, "Save Rows", self.save_error_rows)
        buttons.addStretch(1)

        self.errors_table = QTableWidget(0, ERROR_COLUMNS)
        self.errors_table.setHorizontalHeaderLabels(ERROR_HEADERS)
        self.errors_table.setAlternatingRowColors(True)
        self.errors_table.verticalHeader().setDefaultSectionSize(44)
        for col, width in enumerate((90, 105, 110, 260, 80, 500)):
            self.errors_table.setColumnWidth(col, width)
        self.errors_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.errors_table.setItemDelegateForColumn(1, ComboBoxDelegate(LOCATION_OPTIONS, self.errors_table))
        self.errors_table.setItemDelegateForColumn(2, ComboBoxDelegate(CARRIER_OPTIONS, self.errors_table))
        self.errors_table.itemChanged.connect(self.on_errors_table_changed)

        layout.addWidget(help_label)
        layout.addLayout(buttons)
        layout.addWidget(self.errors_table)
        return tab

    def _build_double_logged_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        help_label = QLabel(
            "Tab between cells to move across columns. A new blank row is added automatically "
            "as you type. Fields: Unit · Location · Carrier · Tracking · Last 4"
        )
        help_label.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._add_button(buttons, "Add Blank Row", self.add_blank_double_row)
        self._add_button(buttons, "Delete Selected Rows", self.delete_selected_double_logged, "danger")
        self._add_button(buttons, "Save Rows", self.save_double_rows)
        buttons.addStretch(1)

        self.double_table = QTableWidget(0, DOUBLE_COLUMNS)
        self.double_table.setHorizontalHeaderLabels(DOUBLE_HEADERS)
        self.double_table.setAlternatingRowColors(True)
        self.double_table.verticalHeader().setDefaultSectionSize(44)
        for col, width in enumerate((90, 110, 115, 300, 90)):
            self.double_table.setColumnWidth(col, width)
        self.double_table.horizontalHeader().setStretchLastSection(True)
        self.double_table.setItemDelegateForColumn(1, ComboBoxDelegate(LOCATION_OPTIONS, self.double_table))
        self.double_table.setItemDelegateForColumn(2, ComboBoxDelegate(CARRIER_OPTIONS, self.double_table))
        self.double_table.itemChanged.connect(self.on_double_table_changed)

        layout.addWidget(help_label)
        layout.addLayout(buttons)
        layout.addWidget(self.double_table)
        return tab

    def _build_alerts_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        buttons = QHBoxLayout()
        self._add_button(buttons, "Resolve Selected", self.resolve_selected_alerts)
        self._add_button(buttons, "Reopen Selected", self.reopen_selected_alerts)
        buttons.addStretch(1)

        self.alerts_table = QTableWidget(0, len(ALERT_HEADERS))
        self.alerts_table.setHorizontalHeaderLabels(ALERT_HEADERS)
        self.alerts_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.alerts_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.alerts_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.alerts_table.setAlternatingRowColors(True)
        self.alerts_table.verticalHeader().setVisible(False)
        self.alerts_table.verticalHeader().setDefaultSectionSize(36)
        for column, width in enumerate((80, 110, 80, 90, 210, 75, 500, 150)):
            self.alerts_table.setColumnWidth(column, width)
        self.alerts_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)

        layout.addLayout(buttons)
        layout.addWidget(self.alerts_table)
        return tab

    def _build_shortcuts(self) -> None:
        # Scoped to the audit table so the standard select-all behaviour keeps
        # working inside the search box, paste areas, and cell editors.
        for sequence, handler in (
            ("Ctrl+A", self.mark_all_visible),
            ("Ctrl+Shift+A", self.unmark_all_visible),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self.table)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)

    # ------------------------------------------------------------- widgets
    def _make_button(self, text: str, handler, variant: str = "default") -> QPushButton:
        button = QPushButton(text)
        button.setCursor(Qt.PointingHandCursor)
        if variant != "default":
            button.setProperty("variant", variant)
        button.clicked.connect(handler)
        return button

    def _add_button(self, layout, text: str, handler, variant: str = "default") -> QPushButton:
        button = self._make_button(text, handler, variant)
        layout.addWidget(button)
        return button

    def _make_chip(self) -> QLabel:
        chip = QLabel()
        chip.setProperty("chip", "true")
        chip.setTextFormat(Qt.RichText)
        return chip

    def _make_vline(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setProperty("role", "vline")
        return line

    # ----------------------------------------------------------- PDF / load
    def open_pdf(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open BuildingLink Event Log PDF", "", "PDF Files (*.pdf)"
        )
        if not path_str:
            return

        path = Path(path_str)
        try:
            entries = parse_buildinglink_pdf(path)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
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

        self.source_label.setText(f"{path.name}  •  {len(entries)} packages")
        self._load_manual_rows()
        self._configure_scanner_for_audit()
        self._refresh_table()
        self.statusBar().showMessage(f"Loaded {path.name}", 5000)

    def _load_manual_rows(self) -> None:
        if not self.pdf_hash:
            return
        self._populate_errors_table(self.db.load_package_errors(self.pdf_hash))
        self._populate_double_table(self.db.load_double_logged(self.pdf_hash))

    # ----------------------------------------------------------- audit table
    def _refresh_table(self) -> None:
        query = self.search_box.text().strip().lower() if hasattr(self, "search_box") else ""
        unchecked_only = self.unchecked_only.isChecked() if hasattr(self, "unchecked_only") else False

        self.filtered_indices = []
        for idx, entry in enumerate(self.entries):
            if query and query not in entry.search_text:
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
                if col == 0:
                    item.setTextAlignment(Qt.AlignCenter)
                    item.setData(ENTRY_INDEX_ROLE, entry_index)
                scanner_state = self.scanner_item_states.get(entry.item_id)
                if scanner_state in ALERT_COLORS:
                    item.setBackground(ALERT_COLORS[scanner_state])
                elif entry.audited:
                    item.setBackground(self.highlight_color)
                self.table.setItem(row, col, item)

        self.table.setSortingEnabled(True)
        self._update_summary()

    def _update_summary(self) -> None:
        total = len(self.entries)
        done = sum(1 for entry in self.entries if entry.audited)
        remaining = total - done
        unique_units = len({entry.unit for entry in self.entries})
        showing = len(self.filtered_indices)

        self.audited_chip.setText(self._chip_text(done, f"of {total} audited"))
        self.remaining_chip.setText(self._chip_text(remaining, "not closed out"))
        self.units_chip.setText(self._chip_text(unique_units, "unique units"))
        self.showing_chip.setText(self._chip_text(showing, "rows shown"))
        self.alerts_chip.setText(self._chip_text(self.open_alert_count, "open alerts"))
        self.progress.setValue(int(done / total * 100) if total else 0)

    @staticmethod
    def _chip_text(value: int, label: str) -> str:
        return (
            f'<span style="color:#1d2530;font-weight:700;">{value}</span> '
            f'<span style="color:#69727f;">{label}</span>'
        )

    def on_audit_cell_clicked(self, row: int, _column: int) -> None:
        # Read the entry index from the row itself so toggling is correct even
        # when the table has been re-sorted by the user.
        marker = self.table.item(row, 0)
        if marker is None:
            return
        entry_index = marker.data(ENTRY_INDEX_ROLE)
        if entry_index is None:
            return

        entry = self.entries[entry_index]
        entry.audited = not entry.audited
        if self.pdf_hash:
            self.db.set_state(self.pdf_hash, entry.item_id, entry.audited)
            self.scanner_coordinator.configure(self.pdf_hash, self.entries)
        self._refresh_table()

    def mark_all_visible(self) -> None:
        """Mark every currently visible audit row (respecting search/filter)."""
        self._set_visible_audited(True)

    def unmark_all_visible(self) -> None:
        """Unmark every currently visible audit row (respecting search/filter)."""
        self._set_visible_audited(False)

    def _set_visible_audited(self, audited: bool) -> None:
        if not self.pdf_hash or not self.filtered_indices:
            return
        changed = False
        for entry_index in self.filtered_indices:
            entry = self.entries[entry_index]
            if entry.audited != audited:
                entry.audited = audited
                self.db.set_state(self.pdf_hash, entry.item_id, audited)
                changed = True
        if changed:
            self.scanner_coordinator.configure(self.pdf_hash, self.entries)
            self._refresh_table()

    def choose_color(self) -> None:
        color = QColorDialog.getColor(self.highlight_color, self, "Choose audit highlight color")
        if color.isValid():
            color.setAlpha(DEFAULT_HIGHLIGHT_RGBA[3])
            self.highlight_color = color
            self._refresh_table()

    # --------------------------------------------------------- manual tables
    def _add_table_row(self, table: QTableWidget, values: list[str]) -> None:
        row = table.rowCount()
        table.insertRow(row)
        for col, value in enumerate(values):
            table.setItem(row, col, QTableWidgetItem(value))

    def add_blank_error_row(self) -> None:
        self._add_table_row(self.errors_table, [""] * ERROR_COLUMNS)

    def add_blank_double_row(self) -> None:
        self._add_table_row(self.double_table, [""] * DOUBLE_COLUMNS)

    def _ensure_blank_last_row(self, table: QTableWidget, column_count: int) -> None:
        if table.rowCount() == 0:
            self._add_table_row(table, [""] * column_count)
            return
        if self._row_has_values(table, table.rowCount() - 1, column_count):
            self._add_table_row(table, [""] * column_count)

    @staticmethod
    def _row_has_values(table: QTableWidget, row: int, column_count: int) -> bool:
        for col in range(column_count):
            item = table.item(row, col)
            if item and item.text().strip():
                return True
        return False

    def on_errors_table_changed(self, item: QTableWidgetItem) -> None:
        if self.loading_manual_tables:
            return
        self._normalize_manual_cell(self.errors_table, item)
        self._with_table_loading(lambda: self._ensure_blank_last_row(self.errors_table, ERROR_COLUMNS))
        self.save_error_rows()

    def on_double_table_changed(self, item: QTableWidgetItem) -> None:
        if self.loading_manual_tables:
            return
        self._normalize_manual_cell(self.double_table, item)
        self._with_table_loading(lambda: self._ensure_blank_last_row(self.double_table, DOUBLE_COLUMNS))
        self.save_double_rows()

    def _normalize_manual_cell(self, table: QTableWidget, item: QTableWidgetItem) -> None:
        value = item.text().strip()
        normalizers = {
            0: normalize_unit,
            1: normalize_location,
            2: normalize_carrier,
            3: normalize_tracking,
            4: lambda v: normalize_last4(v) if v else "",
        }
        normalizer = normalizers.get(item.column())
        new_value = normalizer(value) if normalizer else value
        if new_value != value:
            self._with_table_loading(lambda: item.setText(new_value))

    def delete_selected_errors(self) -> None:
        self._delete_selected_rows(self.errors_table)
        self.save_error_rows()

    def delete_selected_double_logged(self) -> None:
        self._delete_selected_rows(self.double_table)
        self.save_double_rows()

    def _remove_trailing_blank_rows(self, table: QTableWidget, column_count: int) -> None:
        for row in range(table.rowCount() - 1, -1, -1):
            if self._row_has_values(table, row, column_count):
                break
            table.removeRow(row)

    def _delete_selected_rows(self, table: QTableWidget) -> None:
        rows = sorted({index.row() for index in table.selectedIndexes()}, reverse=True)
        for row in rows:
            table.removeRow(row)

    def _collect_rows(self, table: QTableWidget, column_count: int) -> list[list[str]]:
        collected: list[list[str]] = []
        for row in range(table.rowCount()):
            values = [
                table.item(row, col).text().strip() if table.item(row, col) else ""
                for col in range(column_count)
            ]
            if any(values):
                collected.append(values)
        return collected

    def collect_error_rows(self) -> list[PackageError]:
        return [
            PackageError(
                unit=normalize_unit(unit),
                location=normalize_location(location),
                carrier=normalize_carrier(carrier),
                tracking=normalize_tracking(tracking),
                last4=normalize_last4(last4),
                note=note.strip(),
            )
            for unit, location, carrier, tracking, last4, note in self._collect_rows(
                self.errors_table, ERROR_COLUMNS
            )
        ]

    def collect_double_rows(self) -> list[DoubleLoggedPackage]:
        return [
            DoubleLoggedPackage(
                unit=normalize_unit(unit),
                location=normalize_location(location),
                carrier=normalize_carrier(carrier),
                tracking=normalize_tracking(tracking),
                last4=normalize_last4(last4),
            )
            for unit, location, carrier, tracking, last4 in self._collect_rows(
                self.double_table, DOUBLE_COLUMNS
            )
        ]

    def save_error_rows(self) -> None:
        if self.pdf_hash:
            self.db.replace_package_errors(self.pdf_hash, self.collect_error_rows())

    def save_double_rows(self) -> None:
        if self.pdf_hash:
            self.db.replace_double_logged(self.pdf_hash, self.collect_double_rows())

    def _populate_errors_table(self, rows: list[PackageError]) -> None:
        def populate() -> None:
            self.errors_table.setRowCount(0)
            for row in rows:
                self._add_table_row(
                    self.errors_table,
                    [row.unit, row.location, row.carrier, row.tracking, row.last4, row.note],
                )
                if row.note.casefold() == "not logged":
                    for column in range(ERROR_COLUMNS):
                        item = self.errors_table.item(self.errors_table.rowCount() - 1, column)
                        if item:
                            item.setBackground(ALERT_COLORS["error"])
            self._ensure_blank_last_row(self.errors_table, ERROR_COLUMNS)

        self._with_table_loading(populate)

    def _populate_double_table(self, rows: list[DoubleLoggedPackage]) -> None:
        def populate() -> None:
            self.double_table.setRowCount(0)
            for row in rows:
                self._add_table_row(
                    self.double_table,
                    [row.unit, row.location, row.carrier, row.tracking, row.last4],
                )
                for column in range(DOUBLE_COLUMNS):
                    item = self.double_table.item(self.double_table.rowCount() - 1, column)
                    if item:
                        item.setBackground(ALERT_COLORS["warning"])
            self._ensure_blank_last_row(self.double_table, DOUBLE_COLUMNS)

        self._with_table_loading(populate)

    def _with_table_loading(self, action) -> None:
        """Run *action* with manual-table change signals suppressed."""
        previous = self.loading_manual_tables
        self.loading_manual_tables = True
        try:
            action()
        finally:
            self.loading_manual_tables = previous

    # ---------------------------------------------------------- phone scanner
    def start_phone_scanner(self) -> None:
        if not self._require_entries() or not self.pdf_hash:
            return
        self.scanner_coordinator.configure(
            self.pdf_hash,
            self.entries,
            self.db.load_scanner_model(),
        )
        if self.scanner_server is None:
            self.scanner_server = ScannerServer(self.scanner_coordinator)
        try:
            self.scanner_server.start()
        except OSError as exc:
            QMessageBox.critical(self, "Scanner failed", f"Could not start the local scanner:\n{exc}")
            return

        if self.scanner_dialog is None:
            self.scanner_dialog = ScannerPairingDialog(
                self.scanner_server,
                scanner_capabilities(),
                self.stop_phone_scanner,
                self,
            )
        self.scanner_dialog.show()
        self.scanner_dialog.raise_()
        self.scanner_dialog.activateWindow()
        self.scanner_button.setText("Scanner Info")
        self.statusBar().showMessage(f"Phone scanner running at {self.scanner_server.url}")

    def stop_phone_scanner(self) -> None:
        dialog = self.scanner_dialog
        self.scanner_dialog = None
        if dialog:
            dialog.hide()
            dialog.stop_callback = lambda: None
            dialog.deleteLater()
        if self.scanner_server:
            self.scanner_server.stop()
        self.scanner_server = None
        if hasattr(self, "scanner_button"):
            self.scanner_button.setText("Start Phone Scanner")
        self.statusBar().showMessage("Phone scanner stopped.", 5000)

    @staticmethod
    def _entry_carrier(entry: AuditEntry) -> str:
        return normalize_carrier(entry.package.split(" - ", 1)[0]) or "PKG"

    def _configure_scanner_for_audit(self) -> None:
        if not self.pdf_hash:
            return
        self.scanner_undo.clear()
        self.scanner_coordinator.configure(
            self.pdf_hash,
            self.entries,
            self.db.load_scanner_model(),
        )
        entries_by_id = {entry.item_id: entry for entry in self.entries}
        for tracking, records in self.scanner_coordinator.duplicate_groups():
            item_ids = tuple(record.item_id for record in records)
            units = sorted({record.unit for record in records})
            for record in records:
                entry = entries_by_id[record.item_id]
                self.db.add_double_logged_if_missing(
                    self.pdf_hash,
                    DoubleLoggedPackage(
                        unit=entry.unit,
                        location="",
                        carrier=self._entry_carrier(entry),
                        last4=record.last4,
                        tracking=tracking,
                    ),
                )
            self.db.upsert_scanner_alert(
                self.pdf_hash,
                ScannerAlert(
                    alert_key=f"duplicate:{tracking}",
                    kind="duplicate",
                    severity="warning",
                    unit=" / ".join(units),
                    tracking=tracking,
                    last4=tracking[-4:],
                    message="The same tracking number appears multiple times in this audit.",
                    item_ids=item_ids,
                ),
            )
        self._populate_double_table(self.db.load_double_logged(self.pdf_hash))
        self._refresh_alerts()

    def _process_scanner_actions(self) -> None:
        actions = self.scanner_coordinator.drain_actions()
        if not actions or not self.pdf_hash:
            return
        for action in actions:
            self._apply_scanner_action(action)
        self.scanner_coordinator.configure(self.pdf_hash, self.entries)
        self._populate_errors_table(self.db.load_package_errors(self.pdf_hash))
        self._populate_double_table(self.db.load_double_logged(self.pdf_hash))
        self._refresh_alerts()
        self._refresh_table()

    def _history(self, scan_id: str) -> dict:
        return self.scanner_undo.setdefault(
            scan_id,
            {
                "audited": {},
                "alerts": [],
                "package_tracking": "",
                "double_rows": [],
            },
        )

    def _apply_scanner_action(self, action: ScannerAction) -> None:
        if not self.pdf_hash:
            return
        if action.kind == "undo":
            self._undo_scanner_action(action.scan_id)
            return
        if action.kind == "reject":
            self._undo_scanner_action(action.scan_id, save_event=False)
            if action.model:
                self.db.save_scanner_model(action.model)
            suggested_candidate = next(
                (
                    candidate
                    for candidate in action.decision.candidates
                    if candidate.item_id == action.suggested_item_id
                ),
                None,
            )
            self.db.record_scanner_feedback(
                self.pdf_hash,
                action.observation.scan_key,
                "rejected",
                action.suggested_item_id,
                "",
                suggested_candidate.features if suggested_candidate else {},
            )
            self._save_scanner_event(action, status="rejected")
            return

        history = self._history(action.scan_id)
        if action.kind == "match":
            entry = next((entry for entry in self.entries if entry.item_id == action.item_id), None)
            if entry:
                history["audited"].setdefault(entry.item_id, entry.audited)
                entry.audited = True
                self.db.set_state(self.pdf_hash, entry.item_id, True)
                review_key = f"review:{action.scan_id}"
                self.db.resolve_scanner_alert(self.pdf_hash, review_key)
                if action.model:
                    self.db.save_scanner_model(action.model)
                    selected_candidate = next(
                        (
                            candidate
                            for candidate in action.decision.candidates
                            if candidate.item_id == action.item_id
                        ),
                        None,
                    )
                    self.db.record_scanner_feedback(
                        self.pdf_hash,
                        action.observation.scan_key,
                        "corrected"
                        if action.suggested_item_id and action.suggested_item_id != action.item_id
                        else "accepted",
                        action.suggested_item_id,
                        action.item_id,
                        selected_candidate.features if selected_candidate else {},
                    )
            self._save_scanner_event(action)
            return

        if action.kind == "not_found":
            tracking = normalize_tracking(action.decision.tracking)
            alert_key = f"not_found:{tracking or action.scan_id}"
            existing_keys = {alert.alert_key for alert in self.db.load_scanner_alerts(self.pdf_hash)}
            inserted = self.db.add_package_error_if_missing(
                self.pdf_hash,
                PackageError(
                    unit=action.decision.unit,
                    location="",
                    carrier=action.decision.carrier,
                    last4=tracking[-4:] if tracking else "",
                    note="Not logged",
                    tracking=tracking,
                ),
            )
            self.db.upsert_scanner_alert(
                self.pdf_hash,
                ScannerAlert(
                    alert_key=alert_key,
                    kind="not_found",
                    severity="error",
                    unit=action.decision.unit,
                    carrier=action.decision.carrier,
                    tracking=tracking,
                    last4=tracking[-4:] if tracking else "",
                    message="Package is not present in the loaded audit. Logged as Not logged.",
                ),
            )
            if inserted:
                history["package_tracking"] = tracking
            if alert_key not in existing_keys:
                history["alerts"].append(alert_key)
            previous_units = {
                event.unit
                for event in self.db.load_scanner_events(self.pdf_hash)
                if event.tracking == tracking and event.unit and event.unit != action.decision.unit
            }
            if tracking and action.decision.unit and previous_units:
                duplicate_key = f"duplicate_scan:{tracking}"
                duplicate_units = previous_units | {action.decision.unit}
                for unit in duplicate_units:
                    inserted_double = self.db.add_double_logged_if_missing(
                        self.pdf_hash,
                        DoubleLoggedPackage(
                            unit=unit,
                            location="",
                            carrier=action.decision.carrier,
                            last4=tracking[-4:],
                            tracking=tracking,
                        ),
                    )
                    if inserted_double:
                        history["double_rows"].append((unit, tracking))
                self.db.upsert_scanner_alert(
                    self.pdf_hash,
                    ScannerAlert(
                        alert_key=duplicate_key,
                        kind="duplicate",
                        severity="warning",
                        unit=" / ".join(sorted(duplicate_units)),
                        carrier=action.decision.carrier,
                        tracking=tracking,
                        last4=tracking[-4:],
                        message="The same unlogged tracking was scanned for different units.",
                    ),
                )
                if duplicate_key not in existing_keys:
                    history["alerts"].append(duplicate_key)
            self.db.resolve_scanner_alert(self.pdf_hash, f"review:{action.scan_id}")
            self._save_scanner_event(action)
            return

        if action.kind == "duplicate":
            tracking = normalize_tracking(action.decision.tracking)
            alert_key = f"duplicate:{tracking or action.scan_id}"
            existing_keys = {alert.alert_key for alert in self.db.load_scanner_alerts(self.pdf_hash)}
            related = [entry for entry in self.entries if entry.item_id in action.decision.related_item_ids]
            related_units = {entry.unit for entry in related}
            for entry in related:
                inserted = self.db.add_double_logged_if_missing(
                    self.pdf_hash,
                    DoubleLoggedPackage(
                        unit=entry.unit,
                        location="",
                        carrier=self._entry_carrier(entry),
                        last4=tracking[-4:] if tracking else entry.last4,
                        tracking=tracking,
                    ),
                )
                if inserted:
                    history["double_rows"].append((entry.unit, tracking))
            if action.decision.unit and action.decision.unit not in related_units:
                inserted = self.db.add_double_logged_if_missing(
                    self.pdf_hash,
                    DoubleLoggedPackage(
                        unit=action.decision.unit,
                        location="",
                        carrier=action.decision.carrier,
                        last4=tracking[-4:] if tracking else "",
                        tracking=tracking,
                    ),
                )
                if inserted:
                    history["double_rows"].append((action.decision.unit, tracking))
            self.db.upsert_scanner_alert(
                self.pdf_hash,
                ScannerAlert(
                    alert_key=alert_key,
                    kind="duplicate",
                    severity="warning",
                    unit=" / ".join(sorted({*related_units, action.decision.unit} - {""})),
                    carrier=action.decision.carrier,
                    tracking=tracking,
                    last4=tracking[-4:] if tracking else "",
                    message="Duplicate tracking requires investigation.",
                    item_ids=action.decision.related_item_ids,
                ),
            )
            if alert_key not in existing_keys:
                history["alerts"].append(alert_key)
            self._save_scanner_event(action)
            return

        if action.kind == "review":
            alert_key = f"review:{action.scan_id}"
            self.db.upsert_scanner_alert(
                self.pdf_hash,
                ScannerAlert(
                    alert_key=alert_key,
                    kind="review",
                    severity="review",
                    unit=action.decision.unit,
                    carrier=action.decision.carrier,
                    tracking=action.decision.tracking,
                    last4=action.decision.tracking[-4:] if action.decision.tracking else "",
                    message="Phone confirmation is required.",
                    item_ids=action.decision.related_item_ids,
                ),
            )
            history["alerts"].append(alert_key)
            self._save_scanner_event(action)
            return

        self._save_scanner_event(action)

    def _save_scanner_event(self, action: ScannerAction, status: str | None = None) -> None:
        if not self.pdf_hash:
            return
        candidates = [candidate.to_dict() for candidate in action.decision.candidates]
        self.db.save_scanner_event(
            self.pdf_hash,
            ScannerEvent(
                scan_id=action.scan_id,
                status=status or action.decision.status,
                confidence=action.decision.confidence,
                unit=action.decision.unit,
                carrier=action.decision.carrier,
                tracking=action.decision.tracking,
                last4=action.decision.tracking[-4:] if action.decision.tracking else "",
                item_id=action.item_id,
                message=action.decision.message,
                details={"candidates": candidates},
            ),
        )

    def _undo_scanner_action(self, scan_id: str, *, save_event: bool = True) -> None:
        if not self.pdf_hash:
            return
        history = self.scanner_undo.pop(scan_id, None)
        if history:
            for item_id, previous in history["audited"].items():
                entry = next((entry for entry in self.entries if entry.item_id == item_id), None)
                if entry:
                    entry.audited = previous
                    self.db.set_state(self.pdf_hash, item_id, previous)
            if history["package_tracking"]:
                self.db.delete_package_error_by_tracking(self.pdf_hash, history["package_tracking"])
            for unit, tracking in history["double_rows"]:
                self.db.delete_double_logged_by_tracking(self.pdf_hash, unit, tracking)
            for alert_key in history["alerts"]:
                self.db.delete_scanner_alert(self.pdf_hash, alert_key)
        if save_event:
            previous_events = [
                event for event in self.db.load_scanner_events(self.pdf_hash) if event.scan_id == scan_id
            ]
            if previous_events:
                event = previous_events[0]
                event.status = "undone"
                event.message = "Scanner action undone."
                self.db.save_scanner_event(self.pdf_hash, event)

    def _refresh_alerts(self) -> None:
        if not self.pdf_hash:
            self.alerts_table.setRowCount(0)
            self.open_alert_count = 0
            self.scanner_item_states = {}
            return
        alerts = self.db.load_scanner_alerts(self.pdf_hash)
        self.alerts_table.setRowCount(len(alerts))
        item_states: dict[str, str] = {}
        priority = {"review": 1, "warning": 2, "error": 3}
        summary: Counter[str] = Counter()
        for row, alert in enumerate(alerts):
            values = [
                "Resolved" if alert.resolved else "Open",
                alert.kind.replace("_", " ").title(),
                alert.unit,
                alert.carrier,
                alert.tracking,
                alert.last4,
                alert.message,
                alert.created_at.replace("T", " ")[:19],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(ALERT_KEY_ROLE, alert.alert_key)
                if not alert.resolved and alert.severity in ALERT_COLORS:
                    item.setBackground(ALERT_COLORS[alert.severity])
                self.alerts_table.setItem(row, column, item)
            if not alert.resolved:
                summary[alert.kind] += 1
                for item_id in alert.item_ids:
                    current = item_states.get(item_id)
                    if current is None or priority[alert.severity] > priority[current]:
                        item_states[item_id] = alert.severity
        self.scanner_item_states = item_states
        self.open_alert_count = sum(summary.values())
        self.scanner_coordinator.set_alert_summary(dict(summary))
        self._update_summary()

    def _set_selected_alerts_resolved(self, resolved: bool) -> None:
        if not self.pdf_hash:
            return
        rows = {index.row() for index in self.alerts_table.selectedIndexes()}
        for row in rows:
            marker = self.alerts_table.item(row, 0)
            if marker:
                self.db.resolve_scanner_alert(
                    self.pdf_hash,
                    marker.data(ALERT_KEY_ROLE),
                    resolved,
                )
        self._refresh_alerts()
        self._refresh_table()

    def resolve_selected_alerts(self) -> None:
        self._set_selected_alerts_resolved(True)

    def reopen_selected_alerts(self) -> None:
        self._set_selected_alerts_resolved(False)

    # ---------------------------------------------------------------- clears
    def clear_current_audit(self) -> None:
        """Clear checked rows, package errors, and double logs for this PDF."""
        if not self._require_pdf():
            return
        if not self._confirm(
            "Clear current audit?",
            "This will clear checked rows, package errors, and double logged rows for the "
            "currently loaded PDF.\n\nIt will not delete the PDF. Continue?",
        ):
            return

        self.db.clear_all_for_pdf(self.pdf_hash)
        for entry in self.entries:
            entry.audited = False
        self._populate_errors_table([])
        self._populate_double_table([])
        self.scanner_undo.clear()
        self.scanner_coordinator.configure(self.pdf_hash, self.entries, reset_scans=True)
        self._refresh_alerts()
        self._refresh_table()
        self.statusBar().showMessage("Current audit data cleared.", 5000)

    def clear_manual_sections(self) -> None:
        """Clear only the Package Errors and Double Logged rows."""
        if not self._require_pdf():
            return
        if not self._confirm(
            "Clear manual sections?",
            "This will clear Package Errors and Double Logged rows only.\n\n"
            "Checked audit rows will stay as they are. Continue?",
        ):
            return

        self.db.clear_manual_rows(self.pdf_hash)
        self._populate_errors_table([])
        self._populate_double_table([])
        self.statusBar().showMessage("Manual report sections cleared.", 5000)

    # --------------------------------------------------------------- exports
    def export_audit_txt(self) -> None:
        if not self._require_entries():
            return
        self.save_error_rows()
        self.save_double_rows()

        default_name = (
            f"{self.pdf_path.stem}_audit_report.txt" if self.pdf_path else "package_audit_report.txt"
        )
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export Audit TXT", default_name, "Text Files (*.txt)"
        )
        if not path_str:
            return

        try:
            write_audit_report(
                output_path=Path(path_str),
                entries=self.entries,
                package_errors=self.collect_error_rows(),
                double_logged=self.collect_double_rows(),
                source_pdf_name=self.pdf_path.name if self.pdf_path else "",
            )
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", f"Could not write audit report:\n{exc}")
            return
        self._exported(path_str)

    def export_csv(self) -> None:
        if not self._require_entries():
            return

        default_name = f"{self.pdf_path.stem}_audit.csv" if self.pdf_path else "package_audit.csv"
        path_str, _ = QFileDialog.getSaveFileName(self, "Export Audit CSV", default_name, "CSV Files (*.csv)")
        if not path_str:
            return

        try:
            with open(path_str, "w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
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
        except (OSError, csv.Error) as exc:
            QMessageBox.critical(self, "Export failed", f"Could not write audit CSV:\n{exc}")
            return
        self._exported(path_str)

    def export_highlighted_pdf(self) -> None:
        if not self.pdf_path or not self._require_entries():
            return

        default_name = f"{self.pdf_path.stem}_highlighted.pdf"
        output_str, _ = QFileDialog.getSaveFileName(
            self, "Export Highlighted PDF", default_name, "PDF Files (*.pdf)"
        )
        if not output_str:
            return

        try:
            write_highlighted_pdf(
                input_pdf_path=self.pdf_path,
                output_pdf_path=Path(output_str),
                entries=self.entries,
                highlight_color=self.highlight_color,
                entry_colors={
                    item_id: ALERT_COLORS[state]
                    for item_id, state in self.scanner_item_states.items()
                    if state in ALERT_COLORS
                },
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            QMessageBox.critical(self, "Export failed", f"Could not write highlighted PDF:\n{exc}")
            return
        self._exported(output_str)

    # --------------------------------------------------------------- helpers
    def _require_pdf(self) -> bool:
        if not self.pdf_hash:
            QMessageBox.information(self, "No PDF loaded", "Open a PDF first.")
            return False
        return True

    def _require_entries(self) -> bool:
        if not self.entries:
            QMessageBox.information(self, "Nothing to export", "Open a PDF first.")
            return False
        return True

    def _confirm(self, title: str, message: str) -> bool:
        answer = QMessageBox.question(self, title, message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def _exported(self, path_str: str) -> None:
        QMessageBox.information(self, "Exported", f"Saved:\n{path_str}")
        self.statusBar().showMessage(f"Exported {Path(path_str).name}", 5000)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self.stop_phone_scanner()
        self.save_error_rows()
        self.save_double_rows()
        self.db.close()
        super().closeEvent(event)


def main() -> None:
    """Application entry point."""
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(build_stylesheet())

    window = PackageAuditApp()
    window.show()
    sys.exit(app.exec())
