"""Exact tracking-barcode matching for the phone scanner."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.constants import MISSING_LAST4
from app.models import AuditEntry, normalize_unit
from app.unit_matching import UnitResolver

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
    """Tracking barcodes and privacy-safe on-device unit readings."""

    barcodes: tuple[str, ...] = ()
    carrier: str = "PKG"
    barcode_formats: tuple[str, ...] = ()
    unit_candidates: tuple[str, ...] = ()
    unit_confidences: tuple[float, ...] = ()
    unit_scan_attempted: bool = False

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
        payload = json.dumps(
            {
                "barcodes": sorted(set(self.barcodes)),
                "units": sorted(set(self.unit_candidates)),
                "unit_scan": self.unit_scan_attempted,
            },
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class AuditMatchRecord:
    item_id: str
    unit: str
    trackings: tuple[str, ...]
    last4: str
    audited: bool

    @classmethod
    def from_entry(cls, entry: AuditEntry) -> AuditMatchRecord:
        return cls(
            item_id=entry.item_id,
            unit=normalize_unit(entry.unit),
            trackings=entry_tracking_values(entry.package),
            last4=entry.last4,
            audited=entry.audited,
        )


@dataclass(frozen=True)
class MatchCandidate:
    item_id: str
    unit: str
    last4: str
    tracking: str
    confidence: float = 1.0
    audited: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "unit": self.unit,
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
    scanned_units: tuple[str, ...] = ()
    unit_status: str = "not_attempted"

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
            "scanned_units": list(self.scanned_units),
            "scanned_unit": self.scanned_units[0] if self.scanned_units else "",
            "unit_status": self.unit_status,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


class PackageMatcher:
    """Require exact tracking and, for native scans, compatible unit identity."""

    def __init__(self, entries: list[AuditEntry], unit_aliases: dict[str, str] | None = None) -> None:
        self.unit_resolver = UnitResolver(unit_aliases)
        self.records = [AuditMatchRecord.from_entry(entry) for entry in entries]
        self.records_by_id = {record.item_id: record for record in self.records}
        self.physical_records: list[AuditMatchRecord] = []
        claimed_trackings: set[str] = set()
        for record in self.records:
            trackings = set(record.trackings)
            if trackings and trackings & claimed_trackings:
                continue
            self.physical_records.append(record)
            claimed_trackings.update(trackings)
        self.tracking_index: dict[str, list[AuditMatchRecord]] = {}
        for record in self.records:
            for tracking in record.trackings:
                self.tracking_index.setdefault(tracking, []).append(record)

    @staticmethod
    def _candidate(record: AuditMatchRecord, tracking: str) -> MatchCandidate:
        return MatchCandidate(
            item_id=record.item_id,
            unit=record.unit,
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
                scanned_units=observation.unit_candidates,
                unit_status="duplicate",
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
            if observation.unit_scan_attempted:
                verification = self.unit_resolver.verify(
                    record.unit,
                    observation.unit_candidates,
                    observation.unit_confidences,
                )
                if verification.status == "mismatch":
                    label_units = ", ".join(verification.scanned_units)
                    return ScanDecision(
                        status="unit_mismatch",
                        confidence=1.0,
                        message=(
                            f"Investigate this package: tracking is logged for unit {record.unit}, "
                            f"but the label appears to show {label_units}."
                        ),
                        candidates=(candidate,),
                        tracking=tracking,
                        unit=record.unit,
                        carrier=observation.carrier,
                        scan_key=observation.scan_key,
                        related_item_ids=(record.item_id,),
                        scanned_units=verification.scanned_units,
                        unit_status="mismatch",
                    )
                if verification.status == "unreadable":
                    return ScanDecision(
                        status="unit_not_read",
                        confidence=0.0,
                        message=(
                            f"Tracking matches unit {record.unit}, but the label unit could not be "
                            "verified. Rescan the address area or flag it for review."
                        ),
                        candidates=(candidate,),
                        tracking=tracking,
                        unit=record.unit,
                        carrier=observation.carrier,
                        scan_key=observation.scan_key,
                        related_item_ids=(record.item_id,),
                        scanned_units=verification.scanned_units,
                        unit_status="unreadable",
                    )
                scanned_units = verification.scanned_units
                unit_status = "verified"
                unit_message = (
                    f"Tracking and label unit {verification.matched_reading} match audit unit {record.unit}."
                )
            else:
                scanned_units = ()
                unit_status = "not_attempted"
                unit_message = f"Audit lookup: unit {record.unit}. Confirm it against the label."
            return ScanDecision(
                status="already_matched" if already_audited else "confirm",
                confidence=1.0,
                message=(
                    f"Unit {record.unit} is already marked present." if already_audited else unit_message
                ),
                candidates=(candidate,),
                tracking=tracking,
                unit=record.unit,
                carrier=observation.carrier,
                scan_key=observation.scan_key,
                related_item_ids=(record.item_id,),
                scanned_units=scanned_units,
                unit_status=unit_status,
            )

        reliable = observation.reliable_barcode_trackings
        if len(reliable) == 1:
            tracking = reliable[0]
            return ScanDecision(
                status="not_found",
                confidence=1.0,
                message=(
                    "This tracking number is not in the loaded audit. Rescan if the label may be wrong, "
                    "or flag it for end-of-audit review."
                ),
                tracking=tracking,
                carrier=observation.carrier,
                scan_key=observation.scan_key,
                scanned_units=observation.unit_candidates,
                unit_status=("unmatched" if observation.unit_scan_attempted else "not_attempted"),
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
