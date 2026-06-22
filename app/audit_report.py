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


def format_section_header(title: str) -> str:
    bar = "=" * 50
    return f"{bar}\n{title}\n{bar}"


def make_audit_report(
    entries: list[AuditEntry],
    package_errors: list[PackageError],
    double_logged: list[DoubleLoggedPackage],
    source_pdf_name: str = "",
) -> str:
    unchecked = [entry for entry in entries if not entry.audited]
    unchecked.sort(key=lambda row: (unit_sort_key(row.unit), row.last4))

    package_errors = sorted(
        package_errors,
        key=lambda row: (unit_sort_key(row.unit), row.location, row.carrier, row.last4),
    )

    double_logged = sorted(
        double_logged,
        key=lambda row: (unit_sort_key(row.unit), row.location, row.carrier, row.last4),
    )

    lines: list[str] = []
    lines.append("PACKAGE AUDIT REPORT")
    lines.append(datetime.now().strftime("%m/%d/%Y %I:%M %p"))

    if source_pdf_name:
        lines.append(f"Source: {source_pdf_name}")

    lines.append("")
    lines.append(format_section_header("1. PICKED UP BUT NOT CLOSED OUT"))
    lines.append("")

    if unchecked:
        for entry in unchecked:
            lines.append(f"{normalize_unit(entry.unit)} | {normalize_last4(entry.last4)}")
    else:
        lines.append("None")

    lines.append("")
    lines.append(format_section_header("2. PACKAGE ERRORS"))
    lines.append("")

    if package_errors:
        for row in package_errors:
            lines.append(
                " | ".join(
                    [
                        normalize_unit(row.unit),
                        normalize_location(row.location),
                        normalize_carrier(row.carrier),
                        normalize_last4(row.last4),
                        row.note.strip(),
                    ]
                )
            )
    else:
        lines.append("None")

    lines.append("")
    lines.append(format_section_header("3. DOUBLE LOGGED PACKAGES"))
    lines.append("")

    if double_logged:
        for row in double_logged:
            lines.append(
                " | ".join(
                    [
                        normalize_unit(row.unit),
                        normalize_location(row.location),
                        normalize_carrier(row.carrier),
                        normalize_last4(row.last4),
                    ]
                )
            )
    else:
        lines.append("None")

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
