"""Tests for scanner confidence scoring and adaptive matching."""

from __future__ import annotations

import pytest

from app.models import AuditEntry
from app.scanner_matching import (
    AdaptiveMatchModel,
    PackageMatcher,
    ScanObservation,
    entry_tracking_values,
    extract_tracking_values,
    normalize_code,
    resident_name_tokens,
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


def test_resident_tokens_support_slash_separated_surnames():
    assert resident_name_tokens("Abarr / Scott / (s) Mathiesen / Nguyen") == {
        "ABARR",
        "SCOTT",
        "MATHIESEN",
        "NGUYEN",
    }


def test_resident_tokens_use_only_surname_for_full_name():
    assert resident_name_tokens("Jane Mathiesen") == {"MATHIESEN"}
    assert resident_name_tokens("Mathiesen, Jane") == {"MATHIESEN"}


def test_entry_tracking_ignores_buildinglink_number_and_location():
    assert entry_tracking_values("UPS - #2209280242 - BIN - 1ZW828R0YW92807001") == ("1ZW828R0YW92807001",)


def test_exact_tracking_and_unit_is_automatic_match():
    entries = [_entry("one", "1701S", "Abarr / Mathiesen", "1Z999AA10123456784")]
    observation = ScanObservation(
        ocr_text="UNIT 1701S\nMATHIESEN\nTRACKING 1Z999AA10123456784",
        ocr_confidence=91.0,
    )

    decision = PackageMatcher(entries).decide(observation)

    assert decision.status == "matched"
    assert decision.related_item_ids == ("one",)
    assert decision.confidence >= 0.94


def test_tracking_can_be_extracted_from_qr_url_and_unicode_is_discarded():
    url = "https://www.ups.com/track?tracknum=1Z999AA10123456784"
    assert "1Z999AA10123456784" in extract_tracking_values(url, trusted=True)
    assert normalize_code("１Z-99é") == "Z99"


def test_tracking_split_by_ocr_whitespace_is_rejoined():
    assert extract_tracking_values("1Z999AA 10123456784") == ("1Z999AA10123456784",)


def test_unique_exact_barcode_is_automatic_match():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]
    observation = ScanObservation(barcodes=("1Z999AA10123456784",), barcode_formats=("Code 128",))

    decision = PackageMatcher(entries).decide(observation)

    assert decision.status == "matched"
    assert decision.related_item_ids == ("one",)


def test_exact_barcode_wins_when_same_unit_and_resident_have_multiple_packages():
    entries = [
        _entry("one", "1701S", "Mathiesen", "1Z999AA10123456784"),
        _entry("two", "1701S", "Mathiesen", "1Z999AA10123450000"),
    ]
    observation = ScanObservation(
        barcodes=("1Z999AA10123456784",),
        ocr_text="MATHIESEN UNIT 1701S",
    )

    decision = PackageMatcher(entries).decide(observation)

    assert decision.status == "matched"
    assert decision.related_item_ids == ("one",)


def test_distinct_known_barcodes_require_review_instead_of_arbitrary_auto_match():
    entries = [
        _entry("one", "1701S", "Mathiesen", "1Z999AA10123456784"),
        _entry("two", "1802S", "Nguyen", "1Z999AA10123450000"),
    ]

    decision = PackageMatcher(entries).decide(
        ScanObservation(barcodes=("1Z999AA10123456784", "1Z999AA10123450000"))
    )

    assert decision.status == "review"
    assert {candidate.item_id for candidate in decision.candidates} == {"one", "two"}


def test_exact_tracking_with_conflicting_unit_requires_duplicate_investigation():
    entries = [
        _entry("one", "1701S", "Mathiesen", "1Z999AA10123456784"),
        _entry("two", "1802S", "Nguyen", "1Z999AA10123450000"),
    ]
    observation = ScanObservation(
        barcodes=("1Z999AA10123456784",),
        ocr_text="NGUYEN UNIT 1802S",
    )

    decision = PackageMatcher(entries).decide(observation)

    assert decision.status == "duplicate"
    assert decision.unit == "1802S"
    assert set(decision.related_item_ids) == {"one", "two"}


def test_exact_tracking_with_unknown_conflicting_unit_is_still_duplicate():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]

    decision = PackageMatcher(entries).decide(
        ScanObservation(barcodes=("1Z999AA10123456784",), ocr_text="UNIT 9999S")
    )

    assert decision.status == "duplicate"
    assert decision.unit == "9999S"
    assert decision.related_item_ids == ("one",)


def test_unlabeled_zip_code_does_not_create_unit_conflict():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]

    decision = PackageMatcher(entries).decide(
        ScanObservation(
            barcodes=("1Z999AA10123456784",),
            ocr_text="MATHIESEN UNIT 1701S NEW YORK NY 10001",
        )
    )

    assert decision.status == "matched"


def test_multiple_unseen_barcodes_without_ocr_confirmation_are_not_auto_logged():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]

    decision = PackageMatcher(entries).decide(
        ScanObservation(barcodes=("1Z000ZZ00000000001", "9400000000000000000000"))
    )

    assert decision.status == "poor_scan"


def test_ocr_confirms_one_of_multiple_unseen_barcodes_for_not_found():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]

    decision = PackageMatcher(entries).decide(
        ScanObservation(
            barcodes=("1Z000ZZ00000000001", "9400000000000000000000"),
            ocr_text="TRACKING 1Z000ZZ00000000001",
        )
    )

    assert decision.status == "not_found"
    assert decision.tracking == "1Z000ZZ00000000001"


def test_unit_and_surname_without_tracking_requires_review():
    entries = [
        _entry("one", "1701S", "Mathiesen", "1Z999AA10123456784"),
        _entry("two", "1802S", "Nguyen", "1Z999AA10123450000"),
    ]
    observation = ScanObservation(ocr_text="MATHIESEN\nUNIT 1701S", ocr_confidence=90.0)

    decision = PackageMatcher(entries).decide(observation)

    assert decision.status == "review"
    assert decision.candidates[0].item_id == "one"


def test_low_confidence_ocr_cannot_automatically_match_without_a_barcode():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]
    observation = ScanObservation(
        ocr_text="MATHIESEN UNIT 1701S TRACKING 1Z999AA10123456784",
        ocr_confidence=15.0,
    )

    decision = PackageMatcher(entries).decide(observation)

    assert decision.status == "review"


def test_extreme_learned_weights_cannot_auto_match_without_tracking():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]
    model = AdaptiveMatchModel()
    model.weights["unit_exact"] = 12.0
    model.weights["surname_exact"] = 12.0
    model.bias = 2.0

    decision = PackageMatcher(entries, model).decide(ScanObservation(ocr_text="MATHIESEN UNIT 1701S"))

    assert decision.status == "review"


def test_carrier_never_changes_candidate_scores():
    entries = [
        _entry("one", "1701S", "Mathiesen", "1Z999AA10123456784"),
        _entry("two", "1802S", "Nguyen", "1Z999AA10123450000"),
    ]
    matcher = PackageMatcher(entries)
    base = ScanObservation(ocr_text="MATHIESEN UNIT 1701S", carrier="UPS")
    wrong_carrier = ScanObservation(ocr_text="MATHIESEN UNIT 1701S", carrier="FEDEX")

    assert [candidate.confidence for candidate in matcher.rank(base)] == [
        candidate.confidence for candidate in matcher.rank(wrong_carrier)
    ]


def test_reliable_unmatched_barcode_is_logged_as_not_found():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]
    observation = ScanObservation(
        barcodes=("1Z999AA10123450000",),
        ocr_text="UNIT 9901S UNKNOWN",
        carrier="UPS",
    )

    decision = PackageMatcher(entries).decide(observation)

    assert decision.status == "not_found"
    assert decision.tracking == "1Z999AA10123450000"
    assert decision.carrier == "UPS"


def test_unmatched_retail_product_barcode_is_not_auto_logged():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]
    observation = ScanObservation(
        barcodes=("012345678905",),
        barcode_formats=("UPC-A",),
    )

    decision = PackageMatcher(entries).decide(observation)

    assert decision.status == "poor_scan"


def test_unmatched_barcode_is_not_overridden_by_matching_unit_and_name():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]
    observation = ScanObservation(
        barcodes=("1Z999AA10123450000",),
        ocr_text="MATHIESEN UNIT 1701S",
    )

    decision = PackageMatcher(entries).decide(observation)

    assert decision.status == "not_found"
    assert decision.tracking == "1Z999AA10123450000"


def test_weak_ocr_is_not_silently_logged_as_not_found():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]

    decision = PackageMatcher(entries).decide(ScanObservation(ocr_text="blurry label", ocr_confidence=20))

    assert decision.status == "poor_scan"


def test_duplicate_tracking_across_units_is_duplicate_decision():
    entries = [
        _entry("one", "1701S", "Mathiesen", "1Z999AA10123456784"),
        _entry("two", "1802S", "Nguyen", "1Z999AA10123456784"),
    ]

    decision = PackageMatcher(entries).decide(
        ScanObservation(barcodes=("1Z999AA10123456784",), carrier="UPS")
    )

    assert decision.status == "duplicate"
    assert set(decision.related_item_ids) == {"one", "two"}


def test_already_audited_match_is_idempotent():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784", audited=True)]

    decision = PackageMatcher(entries).decide(ScanObservation(barcodes=("1Z999AA10123456784",)))

    assert decision.status == "already_matched"


def test_model_learns_repeated_ocr_name_and_unit_corrections():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]
    model = AdaptiveMatchModel()
    matcher = PackageMatcher(entries, model)
    observation = ScanObservation(ocr_text="MATHIESCN UNIT I701S", ocr_confidence=82.0)

    before = matcher.rank(observation)[0].confidence
    matcher.learn_selection(observation, "one")
    matcher.learn_selection(observation, "one")
    after = matcher.rank(observation)[0]

    assert after.confidence > before
    assert after.features["learned_unit_alias"] == 1.0
    assert after.features["learned_surname_alias"] == 1.0
    restored = AdaptiveMatchModel.from_dict(model.to_dict())
    assert restored.to_dict() == model.to_dict()


def test_malformed_adaptive_model_values_fall_back_safely():
    restored = AdaptiveMatchModel.from_dict(
        {
            "bias": "not-a-number",
            "weights": {"unit_exact": "bad", "tracking_exact": 6},
            "examples": "bad",
            "unit_aliases": [],
            "surname_aliases": {"MATHIESCN": {"MATHIESEN": 2, "BAD": "many"}},
        }
    )

    assert restored.bias == -4.0
    assert restored.weights["unit_exact"] == 3.0
    assert restored.weights["tracking_exact"] == 6.0
    assert restored.examples == 0
    assert restored.unit_aliases == {}
    assert restored.surname_aliases == {"MATHIESCN": {"MATHIESEN": 2}}


def test_rejection_lowers_suggested_candidate_probability():
    entries = [_entry("one", "1701S", "Mathiesen", "1Z999AA10123456784")]
    matcher = PackageMatcher(entries)
    observation = ScanObservation(ocr_text="MATHIESIEN UNIT 1701S", ocr_confidence=70.0)
    before = matcher.rank(observation)[0].confidence

    for _ in range(5):
        matcher.learn_rejection(observation, "one")

    assert matcher.rank(observation)[0].confidence < before


@pytest.mark.parametrize("carrier", ["UPS", "FEDEX", "USPS", "AMZ", "PKG"])
def test_scan_key_does_not_depend_on_carrier(carrier):
    observation = ScanObservation(barcodes=("1Z999AA10123456784",), ocr_text="UNIT 1701S", carrier=carrier)
    assert (
        observation.scan_key
        == ScanObservation(barcodes=("1Z999AA10123456784",), ocr_text="UNIT 1701S", carrier="OTHER").scan_key
    )
