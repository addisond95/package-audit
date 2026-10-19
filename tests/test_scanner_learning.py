"""Tests for local privacy-safe scanner outcome learning."""

from app.scanner_learning import learning_features, update_learning_model
from app.scanner_matching import ScanDecision, ScanObservation


def test_learning_features_never_retain_tracking_or_unit_values():
    observation = ScanObservation(
        barcodes=("1Z999AA10123456784",),
        barcode_formats=("CODE_128",),
        unit_candidates=("2803",),
        unit_confidences=(1.0,),
        unit_scan_attempted=True,
    )
    decision = ScanDecision(
        "confirm",
        1.0,
        "match",
        unit="2803S",
        tracking="1Z999AA10123456784",
        scanned_units=("2803",),
        unit_status="verified",
    )

    features = learning_features(observation, decision)
    encoded = repr(features)

    assert features["barcode_count"] == 1
    assert features["unit_status"] == "verified"
    assert "1Z999AA10123456784" not in encoded
    assert "2803" not in encoded


def test_confirmed_verified_scan_updates_bounded_local_profile():
    observation = ScanObservation(
        barcodes=("TRACKING0001",),
        barcode_formats=("CODE_128",),
        unit_candidates=("2803",),
        unit_confidences=(1.0,),
        unit_scan_attempted=True,
    )
    decision = ScanDecision(
        "matched",
        1.0,
        "saved",
        unit="2803S",
        scanned_units=("2803",),
        unit_status="verified",
    )

    model = update_learning_model({}, observation, decision, "accepted")

    assert model["examples"] == 1
    assert model["outcomes"] == {"accepted": 1}
    assert model["barcode_formats"] == {"CODE_128": 1}
    assert model["unit_results"] == {"verified": 1}
    assert model["unit_aliases"] == {"2803": "2803S"}
    assert model["carrier_patterns"] == {}


def test_confirmed_audit_carrier_teaches_only_a_structural_format():
    observation = ScanObservation(
        barcodes=("123456789012",),
        barcode_formats=("CODE_128",),
    )
    decision = ScanDecision(
        "matched",
        1.0,
        "saved",
        carrier="FEDEX",
        tracking="123456789012",
    )

    model = update_learning_model({}, observation, decision, "accepted")
    encoded = repr(model["carrier_patterns"])

    assert model["carrier_patterns"] == {"CODE_128:12:999999999999": {"FEDEX": 1}}
    assert "123456789012" not in encoded


def test_manual_confirmation_does_not_train_an_unverified_alias():
    observation = ScanObservation(unit_candidates=("90210",), unit_scan_attempted=True)
    decision = ScanDecision(
        "matched",
        1.0,
        "saved",
        unit="2803S",
        scanned_units=("90210",),
        unit_status="manually_confirmed",
    )

    model = update_learning_model({}, observation, decision, "accepted")

    assert model["unit_aliases"] == {}


def test_confirmation_does_not_learn_unrelated_numbers_as_unit_aliases():
    decision = ScanDecision(
        "matched", 1.0, "saved", unit="2803S", scanned_units=("2803", "90210"), unit_status="verified"
    )
    model = update_learning_model({}, ScanObservation(), decision, "accepted")
    assert model["unit_aliases"] == {"2803": "2803S"}
