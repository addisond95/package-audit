"""Tests for building-agnostic unit identity and OCR verification."""

from app.unit_matching import UnitResolver, normalize_unit_reading, numeric_unit_identity


def test_numeric_identity_ignores_building_suffix_and_leading_zeroes():
    assert numeric_unit_identity("2803") == numeric_unit_identity("2803S")
    assert numeric_unit_identity("0205") == numeric_unit_identity("205N")
    assert numeric_unit_identity("305") != numeric_unit_identity("3005")


def test_unit_reading_normalization_is_bounded_and_ascii_safe():
    assert normalize_unit_reading(" Apt. 28-03s ") == "APT2803S"
    assert normalize_unit_reading("²８０３éS") == "S"
    assert len(normalize_unit_reading("1" * 100)) == 16


def test_matching_candidate_verifies_even_at_standalone_confidence():
    result = UnitResolver().verify("2803S", ("2803",), (0.75,))
    assert result.status == "verified"
    assert result.matched_reading == "2803"


def test_clear_conflicting_unit_is_a_mismatch():
    result = UnitResolver().verify("305S", ("3005",), (1.0,))
    assert result.status == "mismatch"
    assert result.scanned_units == ("3005",)


def test_weak_nonmatching_number_is_unreadable_not_an_accusation():
    result = UnitResolver().verify("305S", ("90210",), (0.75,))
    assert result.status == "unreadable"


def test_learned_alias_is_an_additional_matching_strategy():
    result = UnitResolver({"PENTHOUSE": "PH1"}).verify("PH1", ("penthouse",), (0.8,))
    assert result.status == "verified"
