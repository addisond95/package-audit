"""Offscreen interaction tests for the main audit window."""

from __future__ import annotations

import csv
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTableWidgetItem

import app.main_window as main_window
from app.models import AuditEntry


@pytest.fixture(scope="session")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def window(tmp_path, monkeypatch, application):
    monkeypatch.setattr(main_window, "APP_DIR", tmp_path)
    audit_window = main_window.PackageAuditApp()
    audit_window.pdf_hash = "ui-test"
    yield audit_window
    audit_window.close()
    application.processEvents()


def _entry(item_id: str, unit: str, resident: str) -> AuditEntry:
    return AuditEntry(
        item_id=item_id,
        page_index=0,
        unit=unit,
        resident=resident,
        package=f"UPS - #{item_id} - TRACK{unit}",
        tower="South",
        timestamp="06/15/2026 08:00:00 PM",
    )


def test_sorted_row_toggle_updates_the_displayed_entry_and_database(window):
    window.entries = [
        _entry("low", "0205S", "Low Resident"),
        _entry("high", "3904S", "High Resident"),
    ]
    window._refresh_table()
    window.table.sortItems(1, Qt.SortOrder.DescendingOrder)

    window.on_audit_cell_clicked(0, 1)

    assert [(entry.item_id, entry.audited) for entry in window.entries] == [
        ("low", False),
        ("high", True),
    ]
    assert window.db.load_state("ui-test") == {"high": True}


def test_search_scopes_bulk_marking_and_unchecked_filter(window):
    window.entries = [
        _entry("jane", "0205S", "Jane Doe"),
        _entry("john", "3904S", "John Doe"),
    ]
    window._refresh_table()
    window.search_box.setText("jane")

    window.mark_all_visible()

    assert [entry.audited for entry in window.entries] == [True, False]
    window.unchecked_only.setChecked(True)
    assert window.table.rowCount() == 0


def test_manual_package_type_normalizes_to_pkg_in_both_sections(window):
    window.errors_table.setItem(0, 0, QTableWidgetItem("708"))
    window.errors_table.setItem(0, 2, QTableWidgetItem("package"))
    window.double_table.setItem(0, 0, QTableWidgetItem("405"))
    window.double_table.setItem(0, 2, QTableWidgetItem("package"))

    assert window.collect_error_rows()[0].carrier == "PKG"
    assert window.collect_double_rows()[0].carrier == "PKG"
    assert window.db.load_package_errors("ui-test")[0].carrier == "PKG"
    assert window.db.load_double_logged("ui-test")[0].carrier == "PKG"


def test_csv_export_round_trips_commas_quotes_and_newlines(window, tmp_path, monkeypatch):
    output = tmp_path / "audit.csv"
    window.pdf_path = Path("event-log.pdf")
    window.entries = [_entry("quoted", "0205S", 'Jane, "JD"\nDoe')]
    monkeypatch.setattr(
        main_window.QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(output), "CSV Files (*.csv)"),
    )
    monkeypatch.setattr(window, "_exported", lambda _path: None)

    window.export_csv()

    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["resident"] == 'Jane, "JD"\nDoe'
    assert row["unit"] == "0205S"


@pytest.mark.parametrize("export_method", ["export_audit_txt", "export_csv"])
def test_text_exports_report_write_failures(window, tmp_path, monkeypatch, export_method):
    output = tmp_path / "missing" / "audit.txt"
    window.pdf_path = Path("event-log.pdf")
    window.entries = [_entry("one", "0205S", "Jane Doe")]
    messages = []
    monkeypatch.setattr(
        main_window.QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(output), ""),
    )
    monkeypatch.setattr(
        main_window.QMessageBox,
        "critical",
        lambda _parent, title, message: messages.append((title, message)),
    )

    getattr(window, export_method)()

    assert messages
    assert messages[0][0] == "Export failed"
