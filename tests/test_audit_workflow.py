"""Tests for physical-row deduplication and percentage-based audit scoring."""

from app.audit_workflow import assess_audit, canonical_audit_entries
from app.models import AuditEntry, DoubleLoggedPackage, PackageError


def _entry(item_id: str, tracking: str, audited: bool = False) -> AuditEntry:
    return AuditEntry(item_id, 0, "1701S", "Resident", f"UPS - #{item_id} - {tracking}", "South", "", audited)


def test_canonical_entries_suppress_overlapping_tracking_rows_only_once():
    entries = [
        _entry("first", "1Z999AA10123456784"),
        _entry("duplicate", "1Z999AA10123456784"),
        _entry("other", "9400000000000000000000"),
    ]

    canonical, suppressed = canonical_audit_entries(entries)

    assert [entry.item_id for entry in canonical] == ["first", "other"]
    assert suppressed == {"duplicate"}


def test_assessment_uses_physical_total_and_weighted_percentages():
    entries = [_entry(str(index), f"1Z999AA1012345{index:04d}", audited=True) for index in range(20)]
    result = assess_audit(
        entries,
        [PackageError("1701S", "BIN", "UPS", "1234", "Wrong unit")],
        [DoubleLoggedPackage("1701S", "BIN", "UPS", "5678")],
    )

    assert result.total == 20
    assert result.error_rate == 5.0
    assert result.duplicate_rate == 5.0
    assert result.score == 94.5
    assert result.rating == "Good audit"
