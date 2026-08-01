"""Tests for highlighted PDF export."""

from __future__ import annotations

import fitz
import pytest
from PySide6.QtGui import QColor

from app.export_pdf import write_highlighted_pdf
from app.models import AuditEntry


def _entry(item_id: str, package: str, audited: bool) -> AuditEntry:
    return AuditEntry(
        item_id=item_id,
        page_index=0,
        unit="1701S",
        resident=f"Resident {item_id}",
        package=package,
        tower="North",
        timestamp="06/15/2026 08:00:00 PM",
        audited=audited,
    )


def test_only_audited_duplicate_unit_row_is_highlighted(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "highlighted.pdf"

    with fitz.open() as document:
        page = document.new_page(width=600, height=800)
        page.insert_text((50, 100), "1701S  Resident one  UPS - #1 - TRACK1111")
        page.insert_text((50, 200), "1701S  Resident two  UPS - #2 - TRACK2222")
        document.save(source)

    entries = [
        _entry("one", "UPS - #1 - TRACK1111", audited=False),
        _entry("two", "UPS - #2 - TRACK2222", audited=True),
    ]
    write_highlighted_pdf(source, output, entries, QColor(80, 200, 120, 95))

    with fitz.open(output) as document:
        page = document[0]
        unit_matches = page.search_for("1701S")
        annotation_rects = [fitz.Rect(annotation.rect) for annotation in page.annots() or []]

    assert len(annotation_rects) == 1
    annotation_y = annotation_rects[0].y0
    assert abs(annotation_y - unit_matches[1].y0) < abs(annotation_y - unit_matches[0].y0)


def test_highlight_stops_before_the_next_package_row(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "highlighted.pdf"

    with fitz.open() as document:
        page = document.new_page(width=600, height=800)
        page.insert_text((50, 100), "1701S  Resident one  UPS - #1 - TRACK1111")
        page.insert_text((50, 125), "1702S  Resident two  UPS - #2 - TRACK2222")
        document.save(source)

    entries = [
        _entry("one", "UPS - #1 - TRACK1111", audited=True),
        AuditEntry(
            item_id="two",
            page_index=0,
            unit="1702S",
            resident="Resident two",
            package="UPS - #2 - TRACK2222",
            tower="North",
            timestamp="06/15/2026 08:00:00 PM",
            audited=False,
        ),
    ]
    write_highlighted_pdf(source, output, entries, QColor(80, 200, 120, 95))

    with fitz.open(output) as document:
        page = document[0]
        next_row = page.search_for("1702S")[0]
        annotation_rect = fitz.Rect(next(page.annots()).rect)

    assert annotation_rect.y1 < next_row.y0


def test_unrelated_unit_text_does_not_shift_highlight_mapping(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "highlighted.pdf"

    with fitz.open() as document:
        page = document.new_page(width=600, height=800)
        page.insert_text((50, 50), "Audit summary for unit 1701S")
        page.insert_text((50, 100), "1701S  Resident one  UPS - #1 - TRACK1111")
        page.insert_text((50, 200), "1701S  Resident two  UPS - #2 - TRACK2222")
        document.save(source)

    entries = [
        _entry("one", "UPS - #1 - TRACK1111", audited=False),
        _entry("two", "UPS - #2 - TRACK2222", audited=True),
    ]
    write_highlighted_pdf(source, output, entries, QColor(80, 200, 120, 95))

    with fitz.open(output) as document:
        page = document[0]
        unit_matches = page.search_for("1701S")
        annotation_rect = fitz.Rect(next(page.annots()).rect)

    assert len(unit_matches) == 3
    assert abs(annotation_rect.y0 - unit_matches[2].y0) < abs(annotation_rect.y0 - unit_matches[1].y0)


def test_no_audited_entries_produces_unannotated_copy(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "highlighted.pdf"

    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((50, 100), "1701S  Resident one  UPS - #1 - TRACK1111")
        document.save(source)

    write_highlighted_pdf(
        source,
        output,
        [_entry("one", "UPS - #1 - TRACK1111", audited=False)],
        QColor(80, 200, 120, 95),
    )

    with fitz.open(output) as document:
        assert len(document) == 1
        assert list(document[0].annots() or []) == []


def test_all_duplicate_unit_entries_receive_separate_highlights(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "highlighted.pdf"

    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((50, 100), "1701S  Resident one  UPS - #1 - TRACK1111")
        page.insert_text((50, 200), "1701S  Resident two  UPS - #2 - TRACK2222")
        document.save(source)

    write_highlighted_pdf(
        source,
        output,
        [
            _entry("one", "UPS - #1 - TRACK1111", audited=True),
            _entry("two", "UPS - #2 - TRACK2222", audited=True),
        ],
        QColor(10, 20, 30, 255),
    )

    with fitz.open(output) as document:
        annotations = list(document[0].annots() or [])
        assert len(annotations) == 2
        assert annotations[0].opacity == pytest.approx(0.65)
        assert annotations[0].colors["fill"] == pytest.approx([10 / 255, 20 / 255, 30 / 255], abs=0.01)


def test_stale_page_index_is_ignored(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "highlighted.pdf"

    with fitz.open() as document:
        document.new_page()
        document.save(source)

    stale_entry = _entry("stale", "UPS - #1 - TRACK1111", audited=True)
    stale_entry.page_index = 99

    write_highlighted_pdf(source, output, [stale_entry], QColor(80, 200, 120, 95))

    with fitz.open(output) as document:
        assert len(document) == 1
        assert list(document[0].annots() or []) == []


def test_numeric_unit_does_not_match_tracking_substring(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "highlighted.pdf"

    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((50, 100), "405  Resident one  UPS - #1 - TRACK7081111")
        page.insert_text((50, 200), "708  Resident two  UPS - #2 - TRACK2222")
        document.save(source)

    entries = [
        AuditEntry("one", 0, "405", "Resident one", "UPS - #1 - TRACK7081111", "", "", False),
        AuditEntry("two", 0, "708", "Resident two", "UPS - #2 - TRACK2222", "", "", True),
    ]
    write_highlighted_pdf(source, output, entries, QColor(80, 200, 120, 95))

    with fitz.open(output) as document:
        page = document[0]
        target_row = page.search_for("Resident two")[0]
        annotation_rect = fitz.Rect(next(page.annots()).rect)

    assert abs(annotation_rect.y0 - target_row.y0) < 5
