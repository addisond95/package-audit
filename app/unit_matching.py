"""Building-agnostic unit-number identity and verification rules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

from app.models import normalize_unit


def normalize_unit_reading(value: str) -> str:
    """Keep a compact OCR-safe unit rendering without assuming a building suffix."""
    return "".join(
        character for character in value.upper() if "A" <= character <= "Z" or "0" <= character <= "9"
    )[:16]


def numeric_unit_identity(value: str) -> str:
    """Return the numeric identity so ``2803`` matches audit unit ``2803S``."""
    digits = "".join(character for character in normalize_unit_reading(value) if character.isdigit())
    if not digits:
        return ""
    return str(int(digits))


@dataclass(frozen=True)
class UnitVerification:
    status: str
    scanned_units: tuple[str, ...]
    matched_reading: str = ""


class UnitResolver:
    """Resolve label readings through a replaceable identity/alias layer.

    The numeric strategy handles current buildings. Confirmed rendering aliases
    are accepted as an additional, bounded strategy so future building formats
    can be learned without weakening exact tracking matching.
    """

    def __init__(self, aliases: Mapping[str, str] | None = None):
        self.aliases = {
            normalize_unit_reading(label): normalize_unit(audit_unit)
            for label, audit_unit in (aliases or {}).items()
            if normalize_unit_reading(label) and normalize_unit(audit_unit)
        }

    def matches(self, audit_unit: str, reading: str) -> bool:
        normalized_reading = normalize_unit_reading(reading)
        if self.aliases.get(normalized_reading) == normalize_unit(audit_unit):
            return True
        expected = numeric_unit_identity(audit_unit)
        return bool(expected and expected == numeric_unit_identity(normalized_reading))

    def verify(
        self,
        audit_unit: str,
        readings: tuple[str, ...],
        confidences: tuple[float, ...],
    ) -> UnitVerification:
        candidates: list[tuple[str, float]] = []
        for index, raw in enumerate(readings[:12]):
            reading = normalize_unit_reading(raw)
            confidence = confidences[index] if index < len(confidences) else 0.0
            if (
                reading
                and (numeric_unit_identity(reading) or reading in self.aliases)
                and isfinite(confidence)
                and 0.0 <= confidence <= 1.0
                and reading not in {item[0] for item in candidates}
            ):
                candidates.append((reading, confidence))
        scanned = tuple(reading for reading, _confidence in candidates)
        matched = next((reading for reading, _ in candidates if self.matches(audit_unit, reading)), "")
        if matched:
            return UnitVerification("verified", scanned, matched)
        if any(confidence >= 0.9 for _reading, confidence in candidates):
            return UnitVerification("mismatch", scanned)
        return UnitVerification("unreadable", scanned)
