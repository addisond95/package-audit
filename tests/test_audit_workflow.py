"""Tests for physical-row deduplication and unit-based audit scoring."""

from app.audit_workflow import assess_audit, canonical_audit_entries, triggered_audit_notes
from app.models import AuditEntry, AuditNoteRule, DoubleLoggedPackage, PackageError


def _entry(item_id: str, tracking: str, audited: bool = False, unit: str = "1701S") -> AuditEntry:
    return AuditEntry(item_id, 0, unit, "Resident", f"UPS - #{item_id} - {tracking}", "South", "", audited)


def test_canonical_entries_suppress_overlapping_tracking_rows_only_once():
    entries = [
        _entry("first", "1Z999AA10123456784"),
        _entry("duplicate", "1Z999AA10123456784"),
        _entry("other", "9400000000000000000000"),
    ]
    canonical, suppressed = canonical_audit_entries(entries)
    assert [entry.item_id for entry in canonical] == ["first", "other"]
    assert suppressed == {"duplicate"}


def test_assessment_uses_distinct_units_not_package_rows():
    entries = [
        _entry("a", "TRACKING00000001", True, "1701S"),
        _entry("b", "TRACKING00000002", False, "1701S"),
        _entry("c", "TRACKING00000003", False, "1701S"),
        _entry("d", "TRACKING00000004", True, "1802S"),
    ]
    errors = [
        PackageError("1701S", "BIN", "UPS", "0001", "Wrong unit"),
        PackageError("1701S", "BIN", "UPS", "0002", "Wrong unit"),
    ]
    result = assess_audit(entries, errors, [])
    assert result.total_units == 2
    assert result.not_closed_units == 1
    assert result.unclosed_packages == 2
    assert result.error_units == 1
    assert result.error_records == 2
    assert result.open_unit_rate == 50.0
    assert result.error_rate == 50.0
    assert result.affected_units == frozenset({"1701S"})


def test_duplicate_combined_units_are_counted_once_each():
    entries = [
        _entry("a", "TRACKING00000001", True, "1701S"),
        _entry("b", "TRACKING00000002", True, "1802S"),
    ]
    duplicates = [
        DoubleLoggedPackage("1701S / 1802S", "BIN", "UPS", "0001"),
        DoubleLoggedPackage("1701S", "BIN", "UPS", "0002"),
    ]
    result = assess_audit(entries, [], duplicates)
    assert result.duplicate_units == 2
    assert result.duplicate_records == 2
    assert result.duplicate_rate == 100.0


def test_assessment_weighting_scales_with_number_of_units():
    entries = [
        _entry(str(index), f"1Z999AA1012345{index:04d}", True, f"{1000 + index}S") for index in range(20)
    ]
    result = assess_audit(
        entries,
        [PackageError("1000S", "BIN", "UPS", "1234", "Wrong unit")],
        [DoubleLoggedPackage("1001S", "BIN", "UPS", "5678")],
    )
    assert result.total == 20
    assert result.error_rate == 5.0
    assert result.duplicate_rate == 5.0
    assert result.score == 94.5
    assert result.rating == "Good audit"


def test_custom_note_rules_use_unit_percentages_and_placeholders():
    entries = [
        _entry("a", "TRACKING00000001", False, "1701S"),
        _entry("b", "TRACKING00000002", False, "1701S"),
        _entry("c", "TRACKING00000003", True, "1802S"),
    ]
    assessment = assess_audit(entries, [], [])
    rules = [
        AuditNoteRule(
            "Coach staff: {affected_units} of {total_units} units affected ({open_unit_rate}%).",
            "open_unit_rate",
            ">=",
            50.0,
        ),
        AuditNoteRule("Should not appear", "error_unit_rate", ">", 0.0),
    ]
    assert triggered_audit_notes(assessment, rules) == ["Coach staff: 1 of 2 units affected (50.0%)."]


def test_invalid_or_disabled_custom_rules_are_ignored():
    result = assess_audit([_entry("a", "TRACKING00000001", True)], [], [])
    rules = [
        AuditNoteRule("disabled", "quality_score", ">=", 0.0, enabled=False),
        AuditNoteRule("bad metric", "made_up", ">=", 0.0),
        AuditNoteRule("bad threshold", "quality_score", ">=", 101.0),
    ]
    assert triggered_audit_notes(result, rules) == []


def test_custom_note_message_with_literal_or_unknown_braces_cannot_break_export():
    result = assess_audit([_entry("a", "TRACKING00000001", True)], [], [])
    rules = [AuditNoteRule("Use {total_units}; keep {unknown} and a literal {.", "quality_score", ">=", 0)]

    assert triggered_audit_notes(result, rules) == ["Use 1; keep {unknown} and a literal {."]
