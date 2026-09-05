"""Tests for exact tracking-only scanner matching."""

from __future__ import annotations

import pytest

from app.models import AuditEntry
from app.scanner_matching import (
    PackageMatcher,
    ScanObservation,
    entry_tracking_values,
    extract_tracking_values,
    normalize_code,
)


def _entry(
    item_id: str,
    unit: str,
    resident: str,
    tracking: str,
    *,
    audited: bool = False,
) -> AuditEntry:
    return AuditEntry(
        item_id=item_id,
        page_index=0,
        unit=unit,
        resident=resident,
        package=f"UPS - #{item_id} - BIN - {tracking}",
        tower="South Tower",
        timestamp="07/31/2026 08:00:00 PM",
        audited=audited,
    )


def test_entry_tracking_ignores_buildinglink_number_and_location():
    assert entry_tracking_values("UPS - #2209280242 - BIN - 1ZW828R0YW92807001") == ("1ZW828R0YW92807001",)


def test_tracking_can_be_extracted_from_qr_url_and_unicode_is_discarded():
    url = "https://www.ups.com/track?tracknum=1Z999AA10123456784"
    assert "1Z999AA10123456784" in extract_tracking_values(url, trusted=True)
    assert normalize_code("１Z-99é") == "Z99"


def test_unique_exact_barcode_requires_unit_confirmation():
    entries = [_entry("one", "1701S", "Jane Mathiesen", "1Z999AA10123456784")]

    decision = PackageMatcher(entries).decide(
        ScanObservation(barcodes=("1Z999AA10123456784",), barcode_formats=("Code 128",))
    )

    assert decision.status == "confirm"
    assert decision.related_item_ids == ("one",)
    assert decision.unit == "1701S"
    assert decision.tracking == "1Z999AA10123456784"
    assert decision.candidates[0].resident == "Jane Mathiesen"
    assert decision.confidence == 1.0


def test_last_four_alone_never_matches_a_full_tracking_number():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]

    decision = PackageMatcher(entries).decide(ScanObservation(barcodes=("6784",)))

    assert decision.status == "poor_scan"
    assert decision.related_item_ids == ()


def test_distinct_known_barcodes_require_isolating_one_label():
    entries = [
        _entry("one", "1701S", "Mathiesen", "1Z999AA10123456784"),
        _entry("two", "1802S", "Nguyen", "1Z999AA10123450000"),
    ]

    decision = PackageMatcher(entries).decide(
        ScanObservation(barcodes=("1Z999AA10123456784", "1Z999AA10123450000"))
    )

    assert decision.status == "poor_scan"
    assert set(decision.related_item_ids) == {"one", "two"}


def test_reliable_unmatched_tracking_is_logged_not_found_without_a_unit():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]

    decision = PackageMatcher(entries).decide(
        ScanObservation(barcodes=("1Z000ZZ00000000001",), carrier="UPS")
    )

    assert decision.status == "not_found"
    assert decision.tracking == "1Z000ZZ00000000001"
    assert decision.unit == ""
    assert decision.carrier == "UPS"


def test_multiple_unseen_barcodes_are_not_auto_logged():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]

    decision = PackageMatcher(entries).decide(
        ScanObservation(barcodes=("1Z000ZZ00000000001", "9400000000000000000000"))
    )

    assert decision.status == "poor_scan"


def test_unmatched_retail_product_barcode_is_not_auto_logged():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]

    decision = PackageMatcher(entries).decide(
        ScanObservation(barcodes=("012345678905",), barcode_formats=("UPC-A",))
    )

    assert decision.status == "poor_scan"


def test_known_retail_format_can_still_confirm_an_exact_audit_value():
    entries = [_entry("one", "1701S", "Mathiesen", "012345678905")]

    decision = PackageMatcher(entries).decide(
        ScanObservation(barcodes=("012345678905",), barcode_formats=("UPC-A",))
    )

    assert decision.status == "confirm"
    assert decision.related_item_ids == ("one",)


def test_duplicate_tracking_across_units_is_a_duplicate_decision():
    entries = [
        _entry("one", "1701S", "Mathiesen", "1Z999AA10123456784"),
        _entry("two", "1802S", "Nguyen", "1Z999AA10123456784"),
    ]

    matcher = PackageMatcher(entries)
    decision = matcher.decide(ScanObservation(barcodes=("1Z999AA10123456784",)))

    assert decision.status == "duplicate"
    assert set(decision.related_item_ids) == {"one", "two"}
    assert matcher.duplicate_groups()[0][0] == "1Z999AA10123456784"


def test_already_audited_exact_match_is_idempotent():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784", audited=True)]

    decision = PackageMatcher(entries).decide(ScanObservation(barcodes=("1Z999AA10123456784",)))

    assert decision.status == "already_matched"


def test_no_barcode_is_a_poor_scan():
    matcher = PackageMatcher([_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")])

    decision = matcher.decide(ScanObservation())

    assert decision.status == "poor_scan"
    assert "tracking barcode" in decision.message.lower()


@pytest.mark.parametrize("carrier", ["UPS", "FEDEX", "USPS", "AMZ", "PKG"])
def test_scan_key_depends_only_on_decoded_barcodes(carrier):
    observation = ScanObservation(barcodes=("1Z999AA10123456784",), carrier=carrier)
    other = ScanObservation(barcodes=("1Z999AA10123456784",), carrier="OTHER")
    assert observation.scan_key == other.scan_key
