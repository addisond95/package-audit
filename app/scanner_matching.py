"""Exact tracking-barcode matching for the phone scanner."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.constants import MISSING_LAST4
from app.models import AuditEntry, normalize_unit

_CODE_RE = re.compile(r"[A-Z0-9]{7,40}", re.IGNORECASE)
_LOCATION_WORDS = {
    "ALPHA",
    "BB",
    "BIN",
    "CAGE",
    "CG",
    "FCR",
    "SHELF",
    "UG",
}
_TRACKING_LABELS = ("TRACKINGNUMBER", "TRACKINGNO", "TRACKING", "TRACKNUM", "TRACK", "TRK")


def normalize_code(value: str) -> str:
    """Return an upper-case alphanumeric identifier."""
    return "".join(
        character for character in value.upper() if "A" <= character <= "Z" or "0" <= character <= "9"
    )


def _plausible_tracking(value: str, *, trusted: bool = False) -> bool:
    if not 8 <= len(value) <= 40 or not any(character.isdigit() for character in value):
        return False
    if value.startswith(("HTTP", "UNIT", "ORDER", "PHONE", "ZIP")):
        return False
    if value.isdigit():
        return len(value) >= 10
    return trusted or len(value) >= 10


def extract_tracking_values(text: str, *, trusted: bool = False) -> tuple[str, ...]:
    """Extract plausible tracking identifiers from a decoded barcode value."""
    found: list[str] = []

    def add(value: str) -> None:
        normalized = normalize_code(value)
        for label in _TRACKING_LABELS:
            if normalized.startswith(label):
                normalized = normalized[len(label) :]
                break
        if _plausible_tracking(normalized, trusted=trusted) and normalized not in found:
            found.append(normalized)

    if trusted:
        add(text)

    for line in text.splitlines():
        if re.search(r"track(?:ing)?(?:\s*(?:no|number|num))?\s*[:#=?-]?", line, re.IGNORECASE):
            suffix = re.sub(
                r"^.*track(?:ing)?(?:\s*(?:no|number|num))?\s*[:#=?-]?",
                "",
                line,
                flags=re.IGNORECASE,
            )
            add(suffix)
            continue
        for match in _CODE_RE.finditer(line.upper()):
            add(match.group())

    return tuple(found)


def entry_tracking_values(package_text: str) -> tuple[str, ...]:
    """Extract external tracking values while ignoring the BuildingLink number."""
    parts = [part.strip() for part in re.split(r"\s+-\s+", package_text) if part.strip()]
    found: list[str] = []
    for part in reversed(parts):
        upper = part.upper().strip(",")
        if upper.startswith("#") or upper in _LOCATION_WORDS:
            continue
        for value in extract_tracking_values(part):
            if value not in found:
                found.append(value)
    return tuple(found)


@dataclass(frozen=True)
class ScanObservation:
    """Tracking barcodes decoded from one phone image."""

    barcodes: tuple[str, ...] = ()
    carrier: str = "PKG"
    barcode_formats: tuple[str, ...] = ()

    @property
    def barcode_trackings(self) -> tuple[str, ...]:
        values: list[str] = []
        for barcode in self.barcodes:
            for value in extract_tracking_values(barcode, trusted=True):
                if value not in values:
                    values.append(value)
        return tuple(values)

    @property
    def reliable_barcode_trackings(self) -> tuple[str, ...]:
        """Return values safe to log when no audit match exists.

        Retail EAN/UPC product codes may confirm an exact value already in the
        audit, but they must never create a new ``Not logged`` record.
        """
        values: list[str] = []
        for index, barcode in enumerate(self.barcodes):
            barcode_format = self.barcode_formats[index] if index < len(self.barcode_formats) else ""
            upper_format = barcode_format.upper()
            if "EAN" in upper_format or "UPC" in upper_format:
                continue
            for value in extract_tracking_values(barcode, trusted=True):
                if value not in values:
                    values.append(value)
        return tuple(values)

    @property
    def trackings(self) -> tuple[str, ...]:
        return self.barcode_trackings

    @property
    def scan_key(self) -> str:
        payload = json.dumps(sorted(set(self.barcodes)), separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class AuditMatchRecord:
    item_id: str
    unit: str
    resident: str
    trackings: tuple[str, ...]
    last4: str
    audited: bool

    @classmethod
    def from_entry(cls, entry: AuditEntry) -> AuditMatchRecord:
        return cls(
            item_id=entry.item_id,
            unit=normalize_unit(entry.unit),
            resident=entry.resident,
            trackings=entry_tracking_values(entry.package),
            last4=entry.last4,
            audited=entry.audited,
        )


@dataclass(frozen=True)
class MatchCandidate:
    item_id: str
    unit: str
    resident: str
    last4: str
    tracking: str
    confidence: float = 1.0
    audited: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "unit": self.unit,
            "resident": self.resident,
            "last4": self.last4,
            "tracking": self.tracking,
            "confidence": self.confidence,
            "audited": self.audited,
        }


@dataclass(frozen=True)
class ScanDecision:
    status: str
    confidence: float
    message: str
    candidates: tuple[MatchCandidate, ...] = ()
    tracking: str = ""
    unit: str = ""
    carrier: str = "PKG"
    scan_key: str = ""
    related_item_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "confidence": round(self.confidence, 4),
            "message": self.message,
            "tracking": self.tracking,
            "last4": self.tracking[-4:] if self.tracking else MISSING_LAST4,
            "unit": self.unit,
            "carrier": self.carrier,
            "scan_key": self.scan_key,
            "related_item_ids": list(self.related_item_ids),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


class PackageMatcher:
    """Look up decoded tracking numbers exactly; no OCR or fuzzy matching."""

    def __init__(self, entries: list[AuditEntry]) -> None:
        self.records = [AuditMatchRecord.from_entry(entry) for entry in entries]
        self.records_by_id = {record.item_id: record for record in self.records}
        self.tracking_index: dict[str, list[AuditMatchRecord]] = {}
        for record in self.records:
            for tracking in record.trackings:
                self.tracking_index.setdefault(tracking, []).append(record)

    @staticmethod
    def _candidate(record: AuditMatchRecord, tracking: str) -> MatchCandidate:
        return MatchCandidate(
            item_id=record.item_id,
            unit=record.unit,
            resident=record.resident,
            last4=tracking[-4:] if tracking else record.last4,
            tracking=tracking,
            audited=record.audited,
        )

    def decide(self, observation: ScanObservation) -> ScanDecision:
        trackings = observation.trackings
        if not trackings:
            return ScanDecision(
                status="poor_scan",
                confidence=0.0,
                message="No tracking barcode was found. Move closer and scan the tracking barcode only.",
                carrier=observation.carrier,
                scan_key=observation.scan_key,
            )

        duplicate_tracking = next(
            (tracking for tracking in trackings if len(self.tracking_index.get(tracking, ())) > 1),
            "",
        )
        if duplicate_tracking:
            records = self.tracking_index[duplicate_tracking]
            candidates = tuple(self._candidate(record, duplicate_tracking) for record in records)
            units = sorted({record.unit for record in records})
            return ScanDecision(
                status="duplicate",
                confidence=1.0,
                message=f"This tracking number is logged more than once: {', '.join(units)}.",
                candidates=candidates,
                tracking=duplicate_tracking,
                carrier=observation.carrier,
                scan_key=observation.scan_key,
                related_item_ids=tuple(record.item_id for record in records),
            )

        matched: dict[str, tuple[AuditMatchRecord, str]] = {}
        for tracking in trackings:
            for record in self.tracking_index.get(tracking, ()):
                matched.setdefault(record.item_id, (record, tracking))

        if len(matched) > 1:
            return ScanDecision(
                status="poor_scan",
                confidence=0.0,
                message="More than one audit tracking barcode was found. Isolate one label and scan again.",
                carrier=observation.carrier,
                scan_key=observation.scan_key,
                related_item_ids=tuple(matched),
            )

        if matched:
            record, tracking = next(iter(matched.values()))
            candidate = self._candidate(record, tracking)
            already_audited = record.audited
            return ScanDecision(
                status="already_matched" if already_audited else "confirm",
                confidence=1.0,
                message=(
                    f"Unit {record.unit} is already marked present."
                    if already_audited
                    else f"Audit lookup: unit {record.unit}. Confirm it against the label."
                ),
                candidates=(candidate,),
                tracking=tracking,
                unit=record.unit,
                carrier=observation.carrier,
                scan_key=observation.scan_key,
                related_item_ids=(record.item_id,),
            )

        reliable = observation.reliable_barcode_trackings
        if len(reliable) == 1:
            tracking = reliable[0]
            return ScanDecision(
                status="not_found",
                confidence=1.0,
                message="This tracking number is not in the loaded audit. It was logged as Not logged.",
                tracking=tracking,
                carrier=observation.carrier,
                scan_key=observation.scan_key,
            )

        message = (
            "More than one unknown barcode was found. Isolate the tracking barcode and scan again."
            if len(reliable) > 1
            else (
                "A product barcode was found, but no tracking barcode was identified. "
                "Scan the shipping label."
            )
        )
        return ScanDecision(
            status="poor_scan",
            confidence=0.0,
            message=message,
            carrier=observation.carrier,
            scan_key=observation.scan_key,
        )

    def duplicate_groups(self) -> tuple[tuple[str, tuple[AuditMatchRecord, ...]], ...]:
        return tuple(
            (tracking, tuple(records))
            for tracking, records in self.tracking_index.items()
            if len(records) > 1
        )
