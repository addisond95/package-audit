"""Tests for value normalization and parsing helpers in :mod:`app.models`."""

from __future__ import annotations

from app.models import (
    AuditEntry,
    extract_last4,
    normalize_carrier,
    normalize_last4,
    normalize_location,
    normalize_unit,
    unit_sort_key,
)


def test_normalize_unit_and_location_uppercase_and_strip():
    assert normalize_unit("  1708s ") == "1708S"
    assert normalize_location(" bin ") == "BIN"


def test_normalize_carrier_resolves_aliases():
    assert normalize_carrier("fedx") == "FEDEX"
    assert normalize_carrier("fdx") == "FEDEX"
    assert normalize_carrier("amazon") == "AMZ"
    assert normalize_carrier("rx") == "PHARMACY"
    assert normalize_carrier("ups") == "UPS"


def test_normalize_carrier_passthrough_for_unknown():
    assert normalize_carrier("ontrac") == "ONTRAC"
    assert normalize_carrier("whoknows") == "WHOKNOWS"


def test_normalize_last4_handles_missing_values():
    assert normalize_last4("") == "NaN"
    assert normalize_last4("   ") == "NaN"
    assert normalize_last4("nan") == "NaN"


def test_normalize_last4_keeps_last_four_uppercased():
    assert normalize_last4("8572") == "8572"
    assert normalize_last4("tba1968") == "1968"
    assert normalize_last4("ab12") == "AB12"


def test_extract_last4_from_buildinglink_examples():
    assert extract_last4("USPS - #2209361876 - 420981219261290357475302009983") == "9983"
    assert extract_last4("UPS - #2209280242 - BIN - 1ZW828R0YW92807001") == "7001"
    assert extract_last4("AMZ - #2209361938 - TBA331958945193") == "5193"


def test_extract_last4_returns_nan_when_no_tracking():
    assert extract_last4("KEY - #2209130287 - Keys for Pelin, Alpha P") == "NaN"
    assert extract_last4("") == "NaN"


def test_unit_sort_key_orders_numerically():
    units = ["3207S", "0205S", "1708S", "1708N"]
    assert sorted(units, key=unit_sort_key) == ["0205S", "1708N", "1708S", "3207S"]


def test_audit_entry_last4_and_search_text():
    entry = AuditEntry(
        item_id="abc",
        page_index=0,
        unit="1708S",
        resident="Jane Doe",
        package="AMZ - #2209361938 - TBA331958945193",
        tower="North",
        timestamp="06/15/2026 08:00:00 PM",
    )
    assert entry.last4 == "5193"
    haystack = entry.search_text
    assert "1708s" in haystack
    assert "jane doe" in haystack
    assert "5193" in haystack
