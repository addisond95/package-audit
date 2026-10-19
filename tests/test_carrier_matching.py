"""Tests for deterministic and locally learned carrier formats."""

from app.carrier_matching import builtin_carrier, classify_carrier, tracking_signature
from app.scanner_vision import observation_from_barcodes


def test_documented_distinctive_carrier_formats_are_recognized():
    assert builtin_carrier("1Z999AA10123456784").carrier == "UPS"
    assert builtin_carrier("9400100000000000000000").carrier == "USPS"
    assert builtin_carrier("EC000000000US").carrier == "USPS"
    assert builtin_carrier("TBA123456789012").carrier == "AMZ"


def test_ambiguous_numeric_formats_are_not_guessed_without_evidence():
    assert builtin_carrier("123456789012").carrier == "PKG"


def test_signature_contains_structure_but_no_tracking_value():
    signature = tracking_signature("123456789012", "CODE_128")
    assert signature == "CODE_128:12:999999999999"
    assert "123456" not in signature


def test_learned_format_requires_three_consistent_confirmations():
    signature = tracking_signature("123456789012", "CODE_128")
    weak = {"carrier_patterns": {signature: {"FEDEX": 2}}}
    strong = {"carrier_patterns": {signature: {"FEDEX": 3}}}

    assert classify_carrier(("123456789012",), ("CODE_128",), weak).carrier == "PKG"
    guess = classify_carrier(("123456789012",), ("CODE_128",), strong)
    assert (guess.carrier, guess.source, guess.confidence) == ("FEDEX", "learned", 1.0)


def test_conflicting_local_examples_remain_unknown():
    signature = tracking_signature("123456789012", "CODE_128")
    model = {"carrier_patterns": {signature: {"FEDEX": 3, "UPS": 2}}}

    assert classify_carrier(("123456789012",), ("CODE_128",), model).carrier == "PKG"


def test_carrier_recognition_uses_tracking_extracted_from_structured_barcode():
    observation = observation_from_barcodes(
        ("SHIPMENT\nTRACKING: 1Z999AA10123456784",),
        ("QR_CODE",),
    )

    assert observation.carrier == "UPS"
    assert observation.barcode_trackings == ("1Z999AA10123456784",)
    assert observation.barcode_tracking_formats == ("QR_CODE",)
