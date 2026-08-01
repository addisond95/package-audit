"""Highlighted PDF export.

Re-renders the original BuildingLink PDF with a translucent band drawn across
each audited package row.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz
from PySide6.QtGui import QColor

from app.models import AuditEntry

_PACKAGE_TOKEN_RE = re.compile(r"[A-Z0-9]{4,}", re.IGNORECASE)


def _resolve_entry_matches(
    page: fitz.Page,
    entries: list[AuditEntry],
    textpage: fitz.TextPage | None = None,
) -> list[tuple[AuditEntry, fitz.Rect]]:
    if textpage is None:
        textpage = page.get_textpage()

    token_index: dict[str, list[fitz.Rect]] = {}
    for word in page.get_text("words", textpage=textpage):
        rect = fitz.Rect(word[:4])
        text = word[4]
        keys = {
            text.casefold(),
            *(token.casefold() for token in _PACKAGE_TOKEN_RE.findall(text)),
        }
        for key in keys:
            token_index.setdefault(key, []).append(rect)

    matches_by_unit = {
        unit: token_index.get(unit.casefold(), []) for unit in {entry.unit for entry in entries}
    }
    used_by_unit: dict[str, set[int]] = {}
    resolved: list[tuple[AuditEntry, fitz.Rect]] = []

    for entry in entries:
        unit_matches = matches_by_unit[entry.unit]
        used = used_by_unit.setdefault(entry.unit, set())
        available = [index for index in range(len(unit_matches)) if index not in used]
        if not available:
            continue

        anchor_matches: list[fitz.Rect] = []
        fallback_matches: list[fitz.Rect] = []
        for token in sorted(set(_PACKAGE_TOKEN_RE.findall(entry.package)), key=len, reverse=True):
            if token.casefold() == entry.unit.casefold():
                continue
            token_matches = token_index.get(token.casefold(), [])
            if not fallback_matches and token_matches:
                fallback_matches = token_matches
            if any(
                abs(unit_matches[index].y0 - anchor.y0) <= 20
                for index in available
                for anchor in token_matches
            ):
                anchor_matches = token_matches
                break
        if not anchor_matches:
            anchor_matches = fallback_matches

        if anchor_matches:
            match_index = min(
                available,
                key=lambda index: min(
                    (
                        unit_matches[index].y0 > anchor.y0 + 2,
                        abs(unit_matches[index].y0 - anchor.y0),
                    )
                    for anchor in anchor_matches
                ),
            )
        else:
            match_index = available[0]

        used.add(match_index)
        resolved.append((entry, unit_matches[match_index]))

    return resolved


def write_highlighted_pdf(
    input_pdf_path: Path,
    output_pdf_path: Path,
    entries: list[AuditEntry],
    highlight_color: QColor,
    entry_colors: dict[str, QColor] | None = None,
) -> None:
    """Write a copy of the source PDF with audited rows highlighted."""
    entry_colors = entry_colors or {}

    entries_by_page: dict[int, list[AuditEntry]] = {}
    for entry in entries:
        entries_by_page.setdefault(entry.page_index, []).append(entry)

    with fitz.open(str(input_pdf_path)) as doc:
        for page_index, page_entries in entries_by_page.items():
            if page_index < 0 or page_index >= len(doc):
                continue
            page = doc[page_index]
            page_rect = page.rect
            resolved_entries = _resolve_entry_matches(page, page_entries, page.get_textpage())
            row_tops = sorted({match.y0 for _, match in resolved_entries})

            for entry, match in resolved_entries:
                color = entry_colors.get(entry.item_id)
                if color is None and entry.audited:
                    color = highlight_color
                if color is None:
                    continue

                rgb = (color.red() / 255, color.green() / 255, color.blue() / 255)
                opacity = max(0.15, min(0.65, color.alpha() / 255))

                next_row_top = next((top for top in row_tops if top > match.y0 + 1), None)
                y0 = max(0, match.y0 - 3)
                y1 = min(page_rect.height, match.y1 + 22)
                if next_row_top is not None:
                    y1 = min(y1, next_row_top - 2)
                rect = fitz.Rect(20, y0, page_rect.width - 20, y1)

                annot = page.add_rect_annot(rect)
                annot.set_colors(stroke=rgb, fill=rgb)
                annot.set_opacity(opacity)
                annot.update()

        doc.save(str(output_pdf_path))
