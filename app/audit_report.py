"""Plain-text audit report generation.

Produces the three-section report: packages still open, manually recorded
package errors, and manually recorded double-logged packages.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.models import (
    AuditEntry,
    DoubleLoggedPackage,
    PackageError,
    normalize_carrier,
    normalize_last4,
    normalize_location,
    normalize_unit,
    unit_sort_key,
)

_INDENT = "  "


def _format_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Render *rows* as a fixed-width text table under *headers*.

    Column widths are derived from the widest value in each column so every
    row lines up cleanly.  The last column is never padded — it holds free-text
    like notes that should flow naturally.
    """
    if not rows:
        return [f"{_INDENT}None"]

    col_count = len(headers)
    # Seed widths from the header labels.
    widths = [len(h) for h in headers]
    for row in rows:
        for col, cell in enumerate(row):
            widths[col] = max(widths[col], len(cell))

    def render_row(cells: list[str], pad: bool = True) -> str:
        parts: list[str] = []
        for col, cell in enumerate(cells):
            is_last = col == col_count - 1
            parts.append(cell if (is_last or not pad) else cell.ljust(widths[col]))
        return _INDENT + "  ".join(parts).rstrip()

    dividers = ["-" * widths[col] for col in range(col_count)]

    out: list[str] = []
    out.append(render_row(headers))
    out.append(render_row(dividers))
    for row in rows:
        out.append(render_row(row))
    return out


def _section(number: int, title: str) -> str:
    label = f"{number}. {title}"
    return f"{label}\n{'─' * len(label)}"


def make_audit_report(
    entries: list[AuditEntry],
    package_errors: list[PackageError],
    double_logged: list[DoubleLoggedPackage],
    source_pdf_name: str = "",
) -> str:
    unchecked = sorted(
        (e for e in entries if not e.audited),
        key=lambda e: (unit_sort_key(e.unit), e.last4),
    )
    package_errors = sorted(
        package_errors,
        key=lambda r: (unit_sort_key(r.unit), r.location, r.carrier, r.last4),
    )
    double_logged = sorted(
        double_logged,
        key=lambda r: (unit_sort_key(r.unit), r.location, r.carrier, r.last4),
    )

    lines: list[str] = []
    lines.append("PACKAGE AUDIT REPORT")
    lines.append(datetime.now().strftime("%m/%d/%Y %I:%M %p"))
    if source_pdf_name:
        lines.append(f"Source: {source_pdf_name}")

    # ── Section 1 ──────────────────────────────────────────────────────────
    lines.append("")
    lines.append(_section(1, "PICKED UP BUT NOT CLOSED OUT"))
    lines.append("")
    lines.extend(
        _format_table(
            ["UNIT", "LAST 4"],
            [
                [normalize_unit(e.unit), normalize_last4(e.last4)]
                for e in unchecked
            ],
        )
    )

    # ── Section 2 ──────────────────────────────────────────────────────────
    lines.append("")
    lines.append(_section(2, "PACKAGE ERRORS"))
    lines.append("")
    lines.extend(
        _format_table(
            ["UNIT", "LOCATION", "CARRIER", "LAST 4", "NOTE"],
            [
                [
                    normalize_unit(r.unit),
                    normalize_location(r.location),
                    normalize_carrier(r.carrier),
                    normalize_last4(r.last4),
                    r.note.strip(),
                ]
                for r in package_errors
            ],
        )
    )

    # ── Section 3 ──────────────────────────────────────────────────────────
    lines.append("")
    lines.append(_section(3, "DOUBLE LOGGED PACKAGES"))
    lines.append("")
    lines.extend(
        _format_table(
            ["UNIT", "LOCATION", "CARRIER", "LAST 4"],
            [
                [
                    normalize_unit(r.unit),
                    normalize_location(r.location),
                    normalize_carrier(r.carrier),
                    normalize_last4(r.last4),
                ]
                for r in double_logged
            ],
        )
    )

    lines.append("")
    return "\n".join(lines)


def write_audit_report(
    output_path: Path,
    entries: list[AuditEntry],
    package_errors: list[PackageError],
    double_logged: list[DoubleLoggedPackage],
    source_pdf_name: str = "",
) -> None:
    report = make_audit_report(
        entries=entries,
        package_errors=package_errors,
        double_logged=double_logged,
        source_pdf_name=source_pdf_name,
    )
    output_path.write_text(report, encoding="utf-8")
