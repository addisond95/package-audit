"""Tests for the plain-text audit report generation."""

from __future__ import annotations

from app.audit_report import _format_table, make_audit_report
from app.models import AuditEntry, DoubleLoggedPackage, PackageError


def _entry(unit: str, package: str, audited: bool) -> AuditEntry:
    return AuditEntry(
        item_id=unit,
        page_index=0,
        unit=unit,
        resident="Resident",
        package=package,
        tower="North",
        timestamp="06/15/2026 08:00:00 PM",
        audited=audited,
    )


# ── _format_table ────────────────────────────────────────────────────────────

def test_format_table_empty_returns_none():
    result = _format_table(["UNIT", "LAST 4"], [])
    assert result == ["  None"]


def test_format_table_aligns_columns():
    rows = _format_table(["UNIT", "LAST 4"], [["0207S", "5193"], ["3804S", "9823"]])
    # Header and divider are present.
    assert "UNIT" in rows[0] and "LAST 4" in rows[0]
    assert rows[1].strip().startswith("----")
    # Data rows are present and contain the correct values.
    assert "0207S" in rows[2] and "5193" in rows[2]
    assert "3804S" in rows[3] and "9823" in rows[3]
    # Both data rows have the same width (columns are aligned).
    assert len(rows[2]) == len(rows[3])


def test_format_table_last_column_not_padded():
    rows = _format_table(["A", "B", "NOTE"], [["x", "y", "short"], ["x", "y", "a much longer note"]])
    # The note in the first row must not be padded out to the longer note's length.
    assert rows[2].endswith("short")


# ── make_audit_report ────────────────────────────────────────────────────────

def test_report_lists_unchecked_entries_sorted():
    entries = [
        _entry("3904S", "AMZ - #1 - TBA000002395", audited=False),
        _entry("0207S", "USPS - #2 - 000000005193", audited=False),
        _entry("1010S", "UPS - #3 - 000000009999", audited=True),
    ]
    report = make_audit_report(entries, [], [], source_pdf_name="Event log.pdf")

    assert "1. PICKED UP BUT NOT CLOSED OUT" in report
    assert "Source: Event log.pdf" in report
    # Audited unit must not appear in section 1.
    s1 = report.split("2. PACKAGE ERRORS")[0]
    assert "1010S" not in s1
    # Unchecked entries are ordered numerically by unit.
    assert report.index("0207S") < report.index("3904S")


def test_report_formats_manual_sections_as_table():
    errors = [PackageError("1701S", "BIN", "ONTRAC", "6651", "wrong unit")]
    doubles = [DoubleLoggedPackage("0201S", "BIN", "AMZ", "5561")]
    report = make_audit_report([], errors, doubles)

    # Values appear in their columns (not raw pipe strings).
    assert "1701S" in report
    assert "ONTRAC" in report
    assert "wrong unit" in report
    assert "0201S" in report
    # Headers present.
    assert "NOTE" in report
    assert "LOCATION" in report


def test_report_shows_none_for_empty_sections():
    report = make_audit_report([], [], [])
    assert report.count("None") == 3
    assert "PACKAGE AUDIT REPORT" in report

