"""Tests for the plain-text audit report generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.audit_report import _format_rows, make_audit_report, write_audit_report
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


# ── _format_rows ─────────────────────────────────────────────────────────────


def test_format_rows_empty_returns_none():
    assert _format_rows([]) == ["None"]


def test_format_rows_uses_visible_delimiters():
    rows = _format_rows([["0207S", "5193"], ["3804S", "9823"]])
    assert rows == ["0207S | 5193", "3804S | 9823"]


def test_format_rows_collapses_embedded_whitespace():
    rows = _format_rows([["1701S", "BIN", "wrong\nunit\t entered"]])
    assert rows == ["1701S | BIN | wrong unit entered"]


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


def test_report_formats_manual_sections_as_delimited_rows():
    errors = [PackageError("1701S", "BIN", "ONTRAC", "6651", "wrong unit")]
    doubles = [DoubleLoggedPackage("0201S", "BIN", "AMZ", "5561")]
    report = make_audit_report([], errors, doubles)

    assert "1701S | BIN | ONTRAC |  | 6651 | wrong unit" in report
    assert "0201S | BIN | AMZ |  | 5561" in report


def test_report_includes_full_tracking_for_scanner_rows():
    errors = [
        PackageError(
            "1701S",
            "",
            "PKG",
            "6784",
            "Not logged",
            "1Z999AA10123456784",
        )
    ]
    doubles = [DoubleLoggedPackage("1802S", "", "UPS", "6784", "1Z999AA10123456784")]

    report = make_audit_report([], errors, doubles)

    assert "1701S |  | PKG | 1Z999AA10123456784 | 6784 | Not logged" in report
    assert "1802S |  | UPS | 1Z999AA10123456784 | 6784" in report


def test_report_derives_last_four_from_tracking_immediately():
    errors = [PackageError("1701S", "", "UPS", "", "Not logged", "1Z999AA10123456784")]

    report = make_audit_report([], errors, [])

    assert "1701S |  | UPS | 1Z999AA10123456784 | 6784 | Not logged" in report


def test_report_uses_ascii_section_rules():
    errors = [PackageError("1701S", "BIN", "ONTRAC", "6651", "wrong unit")]
    doubles = [DoubleLoggedPackage("0201S", "BIN", "AMZ", "5561")]

    report = make_audit_report([], errors, doubles)

    assert "=" * 50 in report
    assert "─" not in report


def test_report_shows_none_for_empty_sections():
    report = make_audit_report([], [], [])
    assert report.count("None") == 4
    assert "PACKAGE AUDIT REPORT" in report


def test_report_uses_special_package_type_instead_of_nan_for_open_items():
    entries = [
        _entry("0101S", "FOOD - #1 - Perishable", audited=False),
        _entry("0102S", "KEY - #2 - Keys for resident, Alpha P", audited=False),
        _entry("0103S", "PKG - #3 - Envelope", audited=False),
        _entry("0104S", "PKG - #4 - Alfa shelf", audited=False),
    ]

    report = make_audit_report(entries, [], [])
    open_section = report.split("2. PACKAGE ERRORS")[0]

    assert "0101S | Perishable" in open_section
    assert "0102S | Keys" in open_section
    assert "0103S | Envelope" in open_section
    assert "0104S | Alpha" in open_section
    assert "NaN" not in open_section


def test_audit_notes_appear_at_threshold_with_actionable_counts():
    entries = [_entry(f"100{index}S", f"UPS - #{index} - TRACK000{index}", False) for index in range(3)]
    errors = [PackageError(f"200{index}S", "", "PKG", "1234", "error") for index in range(3)]
    doubles = [DoubleLoggedPackage(f"300{index}S", "", "UPS", "5678") for index in range(3)]

    report = make_audit_report(entries, errors, doubles)
    notes = report.split("4. AUDIT NOTES")[1]

    assert "3 package errors recorded" in notes
    assert "3 double-logged packages recorded" in notes
    assert "3 packages were picked up but not closed out" in notes
    assert notes.count("Staff should take extra care") == 3


def test_audit_notes_stay_empty_below_threshold():
    entries = [_entry("1001S", "UPS - #1 - TRACK0001", False)]
    report = make_audit_report(
        entries,
        [PackageError("2001S", "", "PKG", "1234", "error")],
        [DoubleLoggedPackage("3001S", "", "UPS", "5678")],
    )

    assert report.split("4. AUDIT NOTES")[1].strip("\n=") == "None"


def test_failed_report_export_preserves_existing_file(tmp_path, monkeypatch):
    output = tmp_path / "report.txt"
    output.write_text("existing report", encoding="utf-8")

    def fail_write(_self, *_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", fail_write)

    with pytest.raises(OSError, match="disk full"):
        write_audit_report(output, [], [], [])

    with output.open(encoding="utf-8") as handle:
        assert handle.read() == "existing report"
