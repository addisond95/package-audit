"""Framework-free audit completion, deduplication, and scoring rules."""

from __future__ import annotations

from dataclasses import dataclass

from app.models import AuditEntry, DoubleLoggedPackage, PackageError
from app.scanner_matching import entry_tracking_values


def canonical_audit_entries(entries: list[AuditEntry]) -> tuple[list[AuditEntry], set[str]]:
    """Return one physical audit row per overlapping full tracking identity."""
    canonical: list[AuditEntry] = []
    claimed_trackings: set[str] = set()
    suppressed: set[str] = set()
    for entry in entries:
        trackings = set(entry_tracking_values(entry.package))
        if trackings and trackings & claimed_trackings:
            suppressed.add(entry.item_id)
            continue
        canonical.append(entry)
        claimed_trackings.update(trackings)
    return canonical, suppressed


@dataclass(frozen=True)
class AuditAssessment:
    """Percentage-based audit quality result used in the final report."""

    score: float
    rating: str
    total: int
    not_closed: int
    errors: int
    double_logs: int

    @property
    def completion_rate(self) -> float:
        return 100.0 if not self.total else (self.total - self.not_closed) / self.total * 100.0

    @property
    def error_rate(self) -> float:
        return 0.0 if not self.total else self.errors / self.total * 100.0

    @property
    def duplicate_rate(self) -> float:
        return 0.0 if not self.total else self.double_logs / self.total * 100.0


def assess_audit(
    entries: list[AuditEntry],
    package_errors: list[PackageError],
    double_logged: list[DoubleLoggedPackage],
) -> AuditAssessment:
    """Score results relative to the number of physical packages audited.

    An unclosed package carries the largest operational cost, an error carries
    a substantial cost, and a duplicate log a smaller but visible cost. Rates
    are capped by the 0–100 score rather than by arbitrary raw-count thresholds.
    """
    canonical, _ = canonical_audit_entries(entries)
    total = len(canonical)
    not_closed = sum(not entry.audited for entry in canonical)
    errors = len(package_errors)
    double_logs = len(double_logged)
    denominator = max(total, 1)
    penalty = (not_closed * 1.0 + errors * 0.75 + double_logs * 0.35) / denominator * 100.0
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
    return AuditAssessment(score, rating, total, not_closed, errors, double_logs)
