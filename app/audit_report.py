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

_SECTION_RULE = "=" * 50


def _section(number: int, title: str) -> str:
    label = f"{number}. {title}"
    return f"{_SECTION_RULE}\n{label}\n{_SECTION_RULE}"


def _format_rows(rows: list[list[str]]) -> list[str]:
    """Render report records with visible delimiters and no embedded newlines."""
    if not rows:
        return ["None"]
    return [" | ".join(" ".join(cell.split()) for cell in row) for row in rows]


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
        _format_rows(
            [[normalize_unit(e.unit), normalize_last4(e.last4)] for e in unchecked],
        )
    )

    # ── Section 2 ──────────────────────────────────────────────────────────
    lines.append("")
    lines.append(_section(2, "PACKAGE ERRORS"))
    lines.append("")
    lines.extend(
        _format_rows(
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
        _format_rows(
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
