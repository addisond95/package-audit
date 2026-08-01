"""Tests for the SQLite persistence layer."""

from __future__ import annotations

import sqlite3

import pytest

from app.database import AuditDatabase
from app.models import DoubleLoggedPackage, PackageError, ScannerAlert, ScannerEvent


@pytest.fixture()
def db(tmp_path):
    database = AuditDatabase(tmp_path / "audit_state.sqlite3")
    yield database
    database.close()


def test_audit_state_round_trip(db):
    db.set_state("hash-a", "item-1", True)
    db.set_state("hash-a", "item-2", False)

    state = db.load_state("hash-a")
    assert state == {"item-1": True, "item-2": False}


def test_audit_state_update_overwrites(db):
    db.set_state("hash-a", "item-1", True)
    db.set_state("hash-a", "item-1", False)
    assert db.load_state("hash-a") == {"item-1": False}


def test_state_is_isolated_per_pdf_hash(db):
    db.set_state("hash-a", "item-1", True)
    db.set_state("hash-b", "item-1", True)
    db.clear_audit_state("hash-a")

    assert db.load_state("hash-a") == {}
    assert db.load_state("hash-b") == {"item-1": True}


def test_package_errors_round_trip_with_normalization(db):
    db.replace_package_errors(
        "hash-a",
        [PackageError("1701s", "bin", "fedx", "tba6651", " wrong unit ")],
    )
    rows = db.load_package_errors("hash-a")

    assert len(rows) == 1
    row = rows[0]
    assert (row.unit, row.location, row.carrier, row.last4, row.note) == (
        "1701S",
        "BIN",
        "FEDEX",
        "6651",
        "wrong unit",
    )


def test_double_logged_round_trip(db):
    db.replace_double_logged(
        "hash-a",
        [DoubleLoggedPackage("0201s", "bin", "amazon", "5561")],
    )
    rows = db.load_double_logged("hash-a")

    assert len(rows) == 1
    assert (rows[0].unit, rows[0].carrier, rows[0].last4) == ("0201S", "AMZ", "5561")


def test_legacy_package_type_loads_as_pkg(db):
    with db.conn:
        db.conn.execute(
            """
            INSERT INTO package_errors (pdf_hash, unit, location, carrier, last4, note)
            VALUES ('hash-a', '708', 'bin', 'PACKAGE', '1234', 'legacy')
            """
        )
        db.conn.execute(
            """
            INSERT INTO double_logged (pdf_hash, unit, location, carrier, last4)
            VALUES ('hash-a', '405', 'bb', 'package', '5678')
            """
        )

    assert db.load_package_errors("hash-a")[0].carrier == "PKG"
    assert db.load_double_logged("hash-a")[0].carrier == "PKG"


def test_clear_all_for_pdf_removes_everything(db):
    db.set_state("hash-a", "item-1", True)
    db.replace_package_errors("hash-a", [PackageError("1701S", "BIN", "USPS", "6651", "note")])
    db.replace_double_logged("hash-a", [DoubleLoggedPackage("0201S", "BIN", "AMZ", "5561")])

    db.clear_all_for_pdf("hash-a")

    assert db.load_state("hash-a") == {}
    assert db.load_package_errors("hash-a") == []
    assert db.load_double_logged("hash-a") == []


def test_legacy_database_migrates_tracking_columns(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE package_errors (
            id INTEGER PRIMARY KEY, pdf_hash TEXT, unit TEXT, location TEXT,
            carrier TEXT, last4 TEXT, note TEXT
        );
        CREATE TABLE double_logged (
            id INTEGER PRIMARY KEY, pdf_hash TEXT, unit TEXT, location TEXT,
            carrier TEXT, last4 TEXT
        );
        """
    )
    connection.close()

    database = AuditDatabase(path)
    package_columns = {row[1] for row in database.conn.execute("PRAGMA table_info(package_errors)")}
    double_columns = {row[1] for row in database.conn.execute("PRAGMA table_info(double_logged)")}
    database.close()

    assert "tracking" in package_columns
    assert "tracking" in double_columns


def test_full_tracking_round_trip_and_idempotent_auto_rows(db):
    error = PackageError("1701s", "", "package", "", "Not logged", "1z999aa10123456784")
    duplicate = DoubleLoggedPackage("1802s", "", "ups", "", "1z999aa10123456784")

    assert db.add_package_error_if_missing("hash-a", error) is True
    assert db.add_package_error_if_missing("hash-a", error) is False
    assert db.add_double_logged_if_missing("hash-a", duplicate) is True
    assert db.add_double_logged_if_missing("hash-a", duplicate) is False

    loaded_error = db.load_package_errors("hash-a")[0]
    loaded_duplicate = db.load_double_logged("hash-a")[0]
    assert (loaded_error.unit, loaded_error.carrier, loaded_error.tracking, loaded_error.last4) == (
        "1701S",
        "PKG",
        "1Z999AA10123456784",
        "6784",
    )
    assert (loaded_duplicate.unit, loaded_duplicate.tracking, loaded_duplicate.last4) == (
        "1802S",
        "1Z999AA10123456784",
        "6784",
    )


def test_double_log_dedupes_full_tracking_against_last_four_only(db):
    full = DoubleLoggedPackage("1701S", "", "UPS", "6784", "1Z999AA10123456784")
    last_four_only = DoubleLoggedPackage("1701S", "", "PKG", "6784")

    assert db.add_double_logged_if_missing("hash-a", full) is True
    assert db.add_double_logged_if_missing("hash-a", last_four_only) is False
    assert len(db.load_double_logged("hash-a")) == 1


def test_scanner_alert_upsert_resolution_and_clear(db):
    alert = ScannerAlert(
        alert_key="duplicate:TRACK1234",
        kind="duplicate",
        severity="warning",
        unit="1701s",
        carrier="ups",
        tracking="track1234",
        message="Duplicate tracking",
        item_ids=("one", "two"),
    )
    db.upsert_scanner_alert("hash-a", alert)
    db.upsert_scanner_alert("hash-a", alert)

    alerts = db.load_scanner_alerts("hash-a", include_resolved=False)
    assert len(alerts) == 1
    assert alerts[0].item_ids == ("one", "two")
    db.resolve_scanner_alert("hash-a", alert.alert_key)
    assert db.load_scanner_alerts("hash-a", include_resolved=False) == []
    assert db.load_scanner_alerts("hash-a")[0].resolved is True

    db.clear_all_for_pdf("hash-a")
    assert db.load_scanner_alerts("hash-a") == []


def test_scanner_event_feedback_and_model_round_trip(db):
    event = ScannerEvent(
        scan_id="scan-1",
        status="matched",
        confidence=0.98,
        unit="1701S",
        carrier="FEDEX",
        tracking="1Z999AA10123456784",
        item_id="one",
        message="Matched",
        details={"reasons": ["tracking", "unit"]},
    )
    db.save_scanner_event("hash-a", event)
    db.save_scanner_event("hash-a", ScannerEvent(**{**event.__dict__, "status": "corrected"}))
    loaded = db.load_scanner_events("hash-a")
    assert len(loaded) == 1
    assert loaded[0].status == "corrected"
    assert loaded[0].details == {"reasons": ["tracking", "unit"]}

    db.record_scanner_feedback("hash-a", "key", "accepted", "one", "one", {"unit": 1})
    assert db.scanner_feedback_count() == 1
    feedback_json = db.conn.execute("SELECT features FROM scanner_feedback").fetchone()[0]
    assert feedback_json == '{"unit": 1}'
    model = {"bias": -3.5, "weights": {"unit": 2.0}, "examples": 1}
    db.save_scanner_model(model)
    assert db.load_scanner_model() == model
