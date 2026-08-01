"""Tests for the plain-text audit report generation."""

from __future__ import annotations

from app.audit_report import _format_rows, make_audit_report
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

    assert "1701S | BIN | ONTRAC | 6651 | wrong unit" in report
    assert "0201S | BIN | AMZ | 5561" in report


def test_report_uses_ascii_section_rules():
    errors = [PackageError("1701S", "BIN", "ONTRAC", "6651", "wrong unit")]
    doubles = [DoubleLoggedPackage("0201S", "BIN", "AMZ", "5561")]

    report = make_audit_report([], errors, doubles)

    assert "=" * 50 in report
    assert "─" not in report


def test_report_shows_none_for_empty_sections():
    report = make_audit_report([], [], [])
    assert report.count("None") == 3
    assert "PACKAGE AUDIT REPORT" in report
