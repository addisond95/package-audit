"""Highlighted PDF export.

Re-renders the original BuildingLink PDF with a translucent band drawn across
each audited package row.
"""

from __future__ import annotations

from pathlib import Path

import fitz
from PySide6.QtGui import QColor

from app.models import AuditEntry


def write_highlighted_pdf(
    input_pdf_path: Path,
    output_pdf_path: Path,
    entries: list[AuditEntry],
    highlight_color: QColor,
) -> None:
    """Write a copy of the source PDF with audited rows highlighted."""
    rgb = (
        highlight_color.red() / 255,
        highlight_color.green() / 255,
        highlight_color.blue() / 255,
    )
    opacity = max(0.15, min(0.65, highlight_color.alpha() / 255))

    # Collect the unique audited units per page so a unit shared by several
    # audited packages is highlighted once instead of stacking opacity.
    units_by_page: dict[int, set[str]] = {}
    for entry in entries:
        if entry.audited:
            units_by_page.setdefault(entry.page_index, set()).add(entry.unit)

    with fitz.open(str(input_pdf_path)) as doc:
        for page_index, units in units_by_page.items():
            page = doc[page_index]
            page_rect = page.rect

            for unit in units:
                for match in page.search_for(unit):
                    y0 = max(0, match.y0 - 3)
                    y1 = min(page_rect.height, match.y1 + 22)
                    rect = fitz.Rect(20, y0, page_rect.width - 20, y1)

                    annot = page.add_rect_annot(rect)
                    annot.set_colors(stroke=rgb, fill=rgb)
                    annot.set_opacity(opacity)
                    annot.update()

        doc.save(str(output_pdf_path))
