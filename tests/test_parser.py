"""Tests for BuildingLink PDF text parsing."""

from __future__ import annotations

import hashlib

import fitz

from app.parser import _parse_page, _split_resident_and_package, clean_lines, parse_buildinglink_pdf


def test_parse_buildinglink_pdf_end_to_end(tmp_path):
    pdf_path = tmp_path / "event-log.pdf"
    lines = [
        "Open events - 1",
        "0205S",
        "Unit",
        "Jane Doe",
        "USPS - #2209361876 - 420981219261290357475302009983",
        "South Tower 06/15/2026 08:00:00 PM",
        "1/1",
    ]

    with fitz.open() as document:
        page = document.new_page()
        for line_number, line in enumerate(lines):
            page.insert_text((50, 50 + line_number * 20), line)
        document.save(pdf_path)

    entries = parse_buildinglink_pdf(pdf_path)

    assert len(entries) == 1
    assert entries[0].unit == "0205S"
    assert entries[0].resident == "Jane Doe"
    assert entries[0].last4 == "9983"


def test_parse_buildinglink_pdf_recovers_fields_when_columns_merge(tmp_path):
    pdf_path = tmp_path / "merged-columns.pdf"

    with fitz.open() as document:
        page = document.new_page(width=612, height=792)
        page.insert_text((36, 100), "1701S", fontsize=8)
        page.insert_text((36, 112), "Unit", fontsize=8)
        page.insert_text((71.2, 106), "Resident With A Long Name", fontsize=8)
        page.insert_text((195.8, 106), "UPS - #2209361938 - TRACK1234", fontsize=8)
        page.insert_text((430.2, 106), "South Tower 07/31/2026 08:00:00 PM", fontsize=8)
        document.save(pdf_path)

    entries = parse_buildinglink_pdf(pdf_path)

    assert len(entries) == 1
    assert entries[0].resident == "Resident With A Long Name"
    assert entries[0].package == "UPS - #2209361938 - TRACK1234"
    assert entries[0].tower == "South Tower"
    assert entries[0].timestamp == "07/31/2026 08:00:00 PM"


def test_parse_page_extracts_multiple_entries():
    lines = [
        "0205S",
        "Unit",
        "Jane Doe",
        "USPS - #2209361876 - 420981219261290357475302009983",
        "South Tower 06/15/2026 08:00:00 PM",
        "1708N",
        "John Doe",
        "UPS - #2209280242 - BIN - 1ZW828R0YW92807001",
        "North Tower 06/15/2026 09:15:00 PM",
    ]
    entries = []

    _parse_page(lines, page_index=2, entries=entries)

    assert [(entry.unit, entry.resident, entry.last4) for entry in entries] == [
        ("0205S", "Jane Doe", "9983"),
        ("1708N", "John Doe", "7001"),
    ]
    assert entries[0].page_index == 2
    assert entries[0].tower == "South Tower"
    assert entries[0].timestamp == "06/15/2026 08:00:00 PM"


def test_parse_page_splits_resident_package_and_tower_merged_on_one_line():
    entries = []
    _parse_page(
        [
            "1701S",
            "Unit",
            "Resident With A Long Name UPS - #2209361938 - TRACK1234 South Tower 07/31/2026 08:00:00 PM",
        ],
        page_index=0,
        entries=entries,
    )

    assert len(entries) == 1
    assert entries[0].resident == "Resident With A Long Name"
    assert entries[0].package == "UPS - #2209361938 - TRACK1234"
    assert entries[0].tower == "South Tower"
    assert entries[0].timestamp == "07/31/2026 08:00:00 PM"


def test_parse_page_keeps_multi_word_tower_name_out_of_merged_package():
    entries = []
    _parse_page(
        [
            "1701S",
            "Unit",
            "Resident Name UPS - #2209361938 - TRACK1234 North East Tower 07/31/2026 08:00:00 PM",
        ],
        page_index=0,
        entries=entries,
    )

    assert entries[0].package == "UPS - #2209361938 - TRACK1234"
    assert entries[0].tower == "North East Tower"


def test_parse_page_keeps_title_cased_package_suffix_out_of_merged_tower():
    entries = []
    _parse_page(
        [
            "1701S",
            "Unit",
            "Resident Name OTHER - #2209361938 - Left At Front Desk South Tower 07/31/2026 08:00:00 PM",
        ],
        page_index=0,
        entries=entries,
    )

    assert entries[0].package == "OTHER - #2209361938 - Left At Front Desk"
    assert entries[0].tower == "South Tower"


def test_parse_page_accepts_numeric_unit_with_explicit_unit_label():
    lines = [
        "708",
        "Unit",
        "Jane Doe",
        "AMZ - #2209361938 - TBA331958945193",
        "South Tower 06/15/2026 08:00:00 PM",
    ]
    entries = []

    _parse_page(lines, page_index=0, entries=entries)

    assert len(entries) == 1
    assert entries[0].unit == "708"
    assert entries[0].last4 == "5193"


def test_new_labeled_unit_does_not_renumber_legacy_entry_ids():
    numeric_package = "AMZ - #1 - TBA0000001111"
    legacy_package = "UPS - #2 - TRACK2222"
    timestamp = "06/15/2026 08:00:00 PM"
    lines = [
        "708",
        "Unit",
        "Numeric Resident",
        numeric_package,
        f"South Tower {timestamp}",
        "0205S",
        "Legacy Resident",
        legacy_package,
        f"South Tower {timestamp}",
    ]
    entries = []

    _parse_page(lines, page_index=0, entries=entries)

    legacy_raw_id = f"0|0|0205S|Legacy Resident|{legacy_package}|{timestamp}"
    expected_id = hashlib.sha1(legacy_raw_id.encode("utf-8")).hexdigest()[:16]
    assert entries[1].item_id == expected_id


def test_identical_labeled_entries_receive_unique_ids():
    block = [
        "708",
        "Unit",
        "Same Resident",
        "PKG - #1 - SAMEPACKAGE1234",
        "South Tower 06/15/2026 08:00:00 PM",
    ]
    entries = []

    _parse_page(block * 3, page_index=0, entries=entries)

    assert len(entries) == 3
    assert len({entry.item_id for entry in entries}) == 3


def test_split_resident_and_package_accepts_irregular_marker_spacing():
    resident, package = _split_resident_and_package(["Jane", "Doe", "AMZ -#2209361938 - TBA331958945193"])

    assert resident == "Jane Doe"
    assert package == "AMZ -#2209361938 - TBA331958945193"


def test_clean_lines_removes_buildinglink_page_noise():
    text = "\n".join(
        [
            "Open events - 42",
            "0205S",
            "https://www.buildinglink.com/tenant/EventLog",
            "1/3",
            "6/15/26, 8:00 PM Event log | BuildingLink",
        ]
    )

    assert clean_lines(text) == ["0205S"]
