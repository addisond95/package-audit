"""Tests for the SQLite persistence layer."""

from __future__ import annotations

import pytest

from app.database import AuditDatabase
from app.models import DoubleLoggedPackage, PackageError


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
