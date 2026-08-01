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
from app.models import AuditEntry, DoubleLoggedPackage, PackageError
from app.scanner_matching import ScanDecision, ScanObservation
from app.scanner_server import ScannerAction


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


def test_scanner_match_marks_entry_persists_event_and_undoes(window):
    window.entries = [_entry("one", "0205S", "Jane Doe")]
    observation = ScanObservation(barcodes=("TRACK0205S",))
    decision = ScanDecision(
        status="matched",
        confidence=0.99,
        message="Matched",
        tracking="TRACK0205S",
        unit="0205S",
        scan_key=observation.scan_key,
        related_item_ids=("one",),
    )
    action = ScannerAction("scan-1", "match", observation, decision, "one")

    window._apply_scanner_action(action)

    assert window.entries[0].audited is True
    assert window.db.load_state("ui-test") == {"one": True}
    assert window.db.load_scanner_events("ui-test")[0].status == "matched"

    window._undo_scanner_action("scan-1")
    assert window.entries[0].audited is False
    assert window.db.load_state("ui-test") == {"one": False}
    assert window.db.load_scanner_events("ui-test")[0].status == "undone"


def test_scanner_not_found_is_idempotent_red_alert_and_undo(window):
    observation = ScanObservation(barcodes=("1Z999AA10123456784",), carrier="UPS")
    decision = ScanDecision(
        status="not_found",
        confidence=0.98,
        message="Not found",
        tracking="1Z999AA10123456784",
        unit="9901S",
        carrier="UPS",
        scan_key=observation.scan_key,
    )
    action = ScannerAction("scan-missing", "not_found", observation, decision)

    window._apply_scanner_action(action)
    window._apply_scanner_action(action)
    window._populate_errors_table(window.db.load_package_errors("ui-test"))
    window._refresh_alerts()

    errors = window.db.load_package_errors("ui-test")
    alerts = window.db.load_scanner_alerts("ui-test", include_resolved=False)
    assert len(errors) == 1
    assert errors[0].tracking == "1Z999AA10123456784"
    assert errors[0].note == "Not logged"
    assert len(alerts) == 1 and alerts[0].severity == "error"

    window._undo_scanner_action("scan-missing")
    assert window.db.load_package_errors("ui-test") == []
    assert window.db.load_scanner_alerts("ui-test") == []


def test_scanner_duplicate_logs_each_unit_orange_and_undoes(window):
    tracking = "1Z999AA10123456784"
    window.entries = [
        AuditEntry("one", 0, "0205S", "Jane", f"UPS - #1 - {tracking}", "South", "", False),
        AuditEntry("two", 0, "3904S", "John", f"UPS - #2 - {tracking}", "South", "", False),
    ]
    observation = ScanObservation(barcodes=(tracking,), carrier="UPS")
    decision = ScanDecision(
        status="duplicate",
        confidence=1.0,
        message="Duplicate",
        tracking=tracking,
        carrier="UPS",
        scan_key=observation.scan_key,
        related_item_ids=("one", "two"),
    )

    window._apply_scanner_action(ScannerAction("scan-duplicate", "duplicate", observation, decision))
    window._refresh_alerts()

    assert len(window.db.load_double_logged("ui-test")) == 2
    assert window.scanner_item_states == {"one": "warning", "two": "warning"}
    assert len(window.db.load_scanner_alerts("ui-test", include_resolved=False)) == 1

    window._undo_scanner_action("scan-duplicate")
    assert window.db.load_double_logged("ui-test") == []
    assert window.db.load_scanner_alerts("ui-test") == []


def test_scanner_duplicate_logs_unknown_conflicting_unit(window):
    tracking = "1Z999AA10123456784"
    window.entries = [AuditEntry("one", 0, "0205S", "Jane", f"UPS - #1 - {tracking}", "South", "", False)]
    observation = ScanObservation(barcodes=(tracking,), ocr_text="UNIT 9999S", carrier="UPS")
    decision = window.scanner_coordinator.__class__()
    decision.configure("ui-test", window.entries)
    result = decision.process_observation(observation)
    action = decision.drain_actions()[0]

    window._apply_scanner_action(action)

    assert result["status"] == "duplicate"
    assert {row.unit for row in window.db.load_double_logged("ui-test")} == {"0205S", "9999S"}


def test_same_unseen_tracking_scanned_for_two_units_becomes_duplicate(window):
    tracking = "1Z000ZZ00000000001"
    for scan_id, unit in (("first", "9901S"), ("second", "9902S")):
        observation = ScanObservation(barcodes=(tracking,), ocr_text=f"UNIT {unit}", carrier="UPS")
        decision = ScanDecision(
            status="not_found",
            confidence=0.98,
            message="Not found",
            tracking=tracking,
            unit=unit,
            carrier="UPS",
            scan_key=observation.scan_key,
        )
        window._apply_scanner_action(ScannerAction(scan_id, "not_found", observation, decision))

    assert {row.unit for row in window.db.load_double_logged("ui-test")} == {"9901S", "9902S"}
    alerts = window.db.load_scanner_alerts("ui-test", include_resolved=False)
    assert any(alert.alert_key == f"duplicate_scan:{tracking}" for alert in alerts)


def test_automatic_manual_rows_use_alert_colors(window):
    window._populate_errors_table(
        [PackageError("9901S", "", "PKG", "0001", "Not logged", "1Z000ZZ00000000001")]
    )
    window._populate_double_table([DoubleLoggedPackage("1701S", "", "UPS", "6784", "1Z999AA10123456784")])

    error_color = window.errors_table.item(0, 0).background().color()
    duplicate_color = window.double_table.item(0, 0).background().color()
    assert error_color.red() == main_window.ALERT_COLORS["error"].red()
    assert duplicate_color.red() == main_window.ALERT_COLORS["warning"].red()


def test_review_alert_can_be_resolved_and_reopened(window):
    observation = ScanObservation(ocr_text="JANE UNIT 0205S")
    decision = ScanDecision(
        status="review",
        confidence=0.75,
        message="Review",
        unit="0205S",
        scan_key=observation.scan_key,
        related_item_ids=("one",),
    )
    window.entries = [_entry("one", "0205S", "Jane Doe")]
    window._apply_scanner_action(ScannerAction("scan-review", "review", observation, decision))
    window._refresh_alerts()
    window.alerts_table.selectRow(0)

    window.resolve_selected_alerts()
    assert window.db.load_scanner_alerts("ui-test")[0].resolved is True
    window.alerts_table.selectRow(0)
    window.reopen_selected_alerts()
    assert window.db.load_scanner_alerts("ui-test")[0].resolved is False


def test_configure_scanner_detects_existing_duplicate_tracking(window):
    tracking = "1Z999AA10123456784"
    window.entries = [
        AuditEntry("one", 0, "0205S", "Jane", f"UPS - #1 - {tracking}", "South", "", False),
        AuditEntry("two", 0, "3904S", "John", f"UPS - #2 - {tracking}", "South", "", False),
    ]

    window._configure_scanner_for_audit()

    assert len(window.db.load_double_logged("ui-test")) == 2
    assert len(window.db.load_scanner_alerts("ui-test", include_resolved=False)) == 1


def test_phone_scanner_starts_and_stops_with_loaded_entries(window):
    window.entries = [_entry("one", "0205S", "Jane Doe")]

    window.start_phone_scanner()
    assert window.scanner_server is not None and window.scanner_server.running
    assert window.scanner_dialog is not None

    window.stop_phone_scanner()
    assert window.scanner_server is None
