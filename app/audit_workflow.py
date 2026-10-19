"""Framework-free audit completion, deduplication, scoring, and note rules."""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass

from app.constants import AUDIT_NOTE_METRICS, AUDIT_NOTE_OPERATORS
from app.models import AuditEntry, AuditNoteRule, DoubleLoggedPackage, PackageError, normalize_unit
from app.scanner_matching import entry_tracking_values, tracking_components


def canonical_audit_entries(entries: list[AuditEntry]) -> tuple[list[AuditEntry], set[str]]:
    """Return one physical audit row per overlapping full tracking identity."""
    components = tracking_components([entry_tracking_values(entry.package) for entry in entries])
    canonical = [entries[indices[0]] for indices in components]
    suppressed = {entries[index].item_id for indices in components for index in indices[1:]}
    return canonical, suppressed


def _row_units(value: str) -> set[str]:
    """Split combined duplicate rows such as ``1701S / 1802S`` safely."""
    return {
        normalized for part in re.split(r"\s*/\s*|\s*,\s*", value) if (normalized := normalize_unit(part))
    }


@dataclass(frozen=True)
class AuditAssessment:
    """Unit-based audit quality result used in the final report."""

    score: float
    rating: str
    total_units: int
    not_closed_units: int
    error_units: int
    duplicate_units: int
    affected_units: frozenset[str]
    physical_packages: int
    unclosed_packages: int
    error_records: int
    duplicate_records: int

    @property
    def completion_rate(self) -> float:
        return (
            100.0
            if not self.total_units
            else (self.total_units - self.not_closed_units) / self.total_units * 100.0
        )

    @property
    def open_unit_rate(self) -> float:
        return 0.0 if not self.total_units else self.not_closed_units / self.total_units * 100.0

    @property
    def error_rate(self) -> float:
        return 0.0 if not self.total_units else min(100.0, self.error_units / self.total_units * 100.0)

    @property
    def duplicate_rate(self) -> float:
        return 0.0 if not self.total_units else min(100.0, self.duplicate_units / self.total_units * 100.0)

    @property
    def affected_rate(self) -> float:
        return (
            0.0 if not self.total_units else min(100.0, len(self.affected_units) / self.total_units * 100.0)
        )

    def metric_value(self, metric: str) -> float:
        values = {
            "quality_score": self.score,
            "completion_rate": self.completion_rate,
            "open_unit_rate": self.open_unit_rate,
            "error_unit_rate": self.error_rate,
            "duplicate_unit_rate": self.duplicate_rate,
            "affected_unit_rate": self.affected_rate,
        }
        if metric not in values:
            raise ValueError(f"Unsupported audit-note metric: {metric}")
        return values[metric]


def assess_audit(
    entries: list[AuditEntry],
    package_errors: list[PackageError],
    double_logged: list[DoubleLoggedPackage],
) -> AuditAssessment:
    """Score issues by affected unit rather than raw package/error counts."""
    canonical, _ = canonical_audit_entries(entries)
    audit_units = {normalize_unit(entry.unit) for entry in canonical if normalize_unit(entry.unit)}
    not_closed = {
        normalize_unit(entry.unit) for entry in canonical if not entry.audited and normalize_unit(entry.unit)
    }
    error_units = {normalize_unit(row.unit) for row in package_errors if normalize_unit(row.unit)}
    duplicate_units: set[str] = set()
    for row in double_logged:
        duplicate_units.update(_row_units(row.unit))

    total_units = len(audit_units)
    denominator = max(total_units, 1)
    penalty = (
        (len(not_closed) * 1.0 + len(error_units) * 0.75 + len(duplicate_units) * 0.35) / denominator * 100.0
    )
    score = max(0.0, min(100.0, 100.0 - penalty))
    if score >= 98.0:
        rating = "Excellent audit"
    elif score >= 94.0:
        rating = "Good audit"
    elif score >= 88.0:
        rating = "Generally sound — review noted issues"
    elif score >= 75.0:
        rating = "Needs improvement"
    else:
        rating = "Significant follow-up required"
    return AuditAssessment(
        score=score,
        rating=rating,
        total_units=total_units,
        not_closed_units=len(not_closed),
        error_units=len(error_units),
        duplicate_units=len(duplicate_units),
        affected_units=frozenset(not_closed | error_units | duplicate_units),
        physical_packages=len(canonical),
        unclosed_packages=sum(not entry.audited for entry in canonical),
        error_records=len(package_errors),
        duplicate_records=len(double_logged),
    )


_COMPARISONS = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
}
_METRIC_LABELS = dict(AUDIT_NOTE_METRICS)


_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def triggered_audit_notes(assessment: AuditAssessment, rules: list[AuditNoteRule]) -> list[str]:
    """Evaluate enabled custom rules and render their documented placeholders."""
    notes: list[str] = []
    values = {
        "score": f"{assessment.score:.1f}",
        "completion_rate": f"{assessment.completion_rate:.1f}",
        "open_unit_rate": f"{assessment.open_unit_rate:.1f}",
        "error_unit_rate": f"{assessment.error_rate:.1f}",
        "duplicate_unit_rate": f"{assessment.duplicate_rate:.1f}",
        "affected_unit_rate": f"{assessment.affected_rate:.1f}",
        "affected_units": str(len(assessment.affected_units)),
        "total_units": str(assessment.total_units),
    }
    for rule in rules:
        if (
            not rule.enabled
            or rule.metric not in _METRIC_LABELS
            or rule.operator not in AUDIT_NOTE_OPERATORS
            or not 0.0 <= rule.threshold <= 100.0
        ):
            continue
        if _COMPARISONS[rule.operator](assessment.metric_value(rule.metric), rule.threshold):
            notes.append(
                _PLACEHOLDER_RE.sub(
                    lambda match: values.get(match.group(1), match.group(0)),
                    rule.message.strip(),
                )
            )
    return [note for note in notes if note]
