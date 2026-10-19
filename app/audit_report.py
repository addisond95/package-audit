"""Plain-text audit report generation.

Produces the audit's open-package, error, duplicate, and unit-percentage note
sections.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path

from app.audit_workflow import assess_audit, canonical_audit_entries, triggered_audit_notes
from app.export_utils import atomic_output_path
from app.models import (
    AuditEntry,
    AuditNoteRule,
    DoubleLoggedPackage,
    PackageError,
    ShiftNote,
    normalize_carrier,
    normalize_location,
    normalize_tracking,
    normalize_unit,
    resolve_last4,
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
    audit_note_rules: list[AuditNoteRule] | None = None,
    shift_notes: list[ShiftNote] | None = None,
) -> str:
    canonical_entries, _ = canonical_audit_entries(entries)
    unchecked = sorted(
        (e for e in canonical_entries if not e.audited),
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
            [
                [
                    normalize_unit(e.unit),
                    e.report_carrier,
                    e.report_location,
                    e.open_report_reference,
                ]
                for e in unchecked
            ],
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
                    normalize_tracking(r.tracking),
                    resolve_last4(r.last4, r.tracking),
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
                    normalize_tracking(r.tracking),
                    resolve_last4(r.last4, r.tracking),
                ]
                for r in double_logged
            ],
        )
    )

    # ── Section 4 ──────────────────────────────────────────────────────────
    assessment = assess_audit(canonical_entries, package_errors, double_logged)
    reviewed = assessment.total_units - assessment.not_closed_units
    audit_notes: list[list[str]] = [
        [f"Overall result: {assessment.rating} ({assessment.score:.1f}% quality score)."],
        [
            f"Completion: {reviewed} of {assessment.total_units} units have all packages accounted for "
            f"({assessment.completion_rate:.1f}%)."
        ],
        [
            f"Package errors: {assessment.error_units} affected units, {assessment.error_records} records "
            f"({assessment.error_rate:.1f}% of audited units)."
        ],
        [
            f"Duplicate logs: {assessment.duplicate_units} affected units, "
            f"{assessment.duplicate_records} records ({assessment.duplicate_rate:.1f}% of audited units)."
        ],
    ]
    if package_errors:
        reasons = Counter(row.note.strip() or "Unspecified" for row in package_errors)
        audit_notes.append(
            [
                "Error breakdown: "
                + "; ".join(f"{reason} ({count})" for reason, count in reasons.most_common())
                + "."
            ]
        )
    if assessment.not_closed_units:
        audit_notes.append(
            [
                f"Follow-up: {assessment.not_closed_units} unit"
                f"{'s have' if assessment.not_closed_units != 1 else ' has'} "
                f"{assessment.unclosed_packages} unclosed physical package"
                f"{'s' if assessment.unclosed_packages != 1 else ''}; reinforce pickup close-out procedure."
            ]
        )
    if assessment.error_units:
        audit_notes.append(
            ["Follow-up: review the categorized package errors below with staff and address repeated causes."]
        )
    if assessment.duplicate_units:
        audit_notes.append(
            ["Follow-up: search for an existing tracking entry before relogging incoming packages."]
        )
    if not (assessment.not_closed_units or assessment.error_units or assessment.duplicate_units):
        audit_notes.append(
            ["No package exceptions were recorded. Current receiving and close-out practices were effective."]
        )
    for custom_note in triggered_audit_notes(assessment, audit_note_rules or []):
        audit_notes.append([f"Custom rule: {custom_note}"])

    lines.append("")
    lines.append(_section(4, "AUDIT NOTES"))
    lines.append("")
    lines.extend(_format_rows(audit_notes))

    # Shift notes remain separate from formula-driven audit findings so the
    # operator's contemporaneous record is easy to locate and quote.
    lines.append("")
    lines.append(_section(5, "SHIFT NOTES"))
    lines.append("")
    note_rows: list[list[str]] = []
    for note in shift_notes or []:
        try:
            timestamp = datetime.fromisoformat(note.created_at).astimezone().strftime("%m/%d/%Y %I:%M:%S %p")
        except (TypeError, ValueError):
            timestamp = note.created_at.replace("T", " ")[:19]
        note_rows.append([timestamp, note.text])
    lines.extend(_format_rows(note_rows))

    lines.append("")
    return "\n".join(lines)


def write_audit_report(
    output_path: Path,
    entries: list[AuditEntry],
    package_errors: list[PackageError],
    double_logged: list[DoubleLoggedPackage],
    source_pdf_name: str = "",
    audit_note_rules: list[AuditNoteRule] | None = None,
    shift_notes: list[ShiftNote] | None = None,
) -> None:
    report = make_audit_report(
        entries=entries,
        package_errors=package_errors,
        double_logged=double_logged,
        source_pdf_name=source_pdf_name,
        audit_note_rules=audit_note_rules,
        shift_notes=shift_notes,
    )
    with atomic_output_path(output_path) as temporary_path:
        temporary_path.write_text(report, encoding="utf-8")
