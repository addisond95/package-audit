"""Local package-label matching and adaptive confidence scoring."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from app.constants import MISSING_LAST4
from app.models import AuditEntry

_CODE_RE = re.compile(r"[A-Z0-9]{7,40}", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Z][A-Z'-]{1,}", re.IGNORECASE)
_OCR_TOKEN_RE = re.compile(r"[A-Z0-9][A-Z0-9'-]*", re.IGNORECASE)
_UNIT_TOKEN_RE = re.compile(r"^\d{3,5}[A-Z]?$", re.IGNORECASE)
_LABELED_UNIT_RE = re.compile(
    r"(?:UNIT|APT|APARTMENT|SUITE)\s*[:#-]?\s*(\d{3,5}[A-Z]?)\b",
    re.IGNORECASE,
)
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
_NAME_NOISE = {
    "AND",
    "APT",
    "ATTN",
    "DELIVER",
    "DESK",
    "FOR",
    "FRONT",
    "MR",
    "MRS",
    "MS",
    "PACKAGE",
    "RESIDENT",
    "SHIP",
    "TO",
    "UNIT",
}
_TRACKING_LABELS = ("TRACKINGNUMBER", "TRACKINGNO", "TRACKING", "TRACK", "TRK")


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
    """Extract plausible full tracking identifiers from barcode or OCR text."""
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

        # OCR often inserts spaces inside a printed tracking number. Rejoin a
        # line only when every fragment contains a digit, which avoids turning
        # ordinary names and address labels into identifiers.
        fragments = _OCR_TOKEN_RE.findall(line.upper())
        if len(fragments) > 1 and all(any(character.isdigit() for character in part) for part in fragments):
            add("".join(fragments))
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


def resident_name_tokens(resident: str) -> frozenset[str]:
    """Return matchable resident-name tokens, including slash-separated surnames."""
    cleaned = re.sub(r"\([A-Z]+\)", " ", resident.upper())
    segments = [segment.strip() for segment in cleaned.split("/") if segment.strip()]
    tokens: set[str] = set()
    for segment in segments:
        words = [
            normalize_code(match.group())
            for match in _WORD_RE.finditer(segment)
            if normalize_code(match.group()) not in _NAME_NOISE and len(normalize_code(match.group())) >= 3
        ]
        if not words:
            continue
        if "," in segment:
            tokens.add(words[0])
        else:
            tokens.add(words[-1])
    return frozenset(tokens)


@dataclass(frozen=True)
class ScanObservation:
    """Barcode and OCR evidence extracted from one phone image."""

    barcodes: tuple[str, ...] = ()
    ocr_text: str = ""
    ocr_confidence: float = 0.0
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
        """Return barcode values safe to auto-log when no audit match exists.

        Retail EAN/UPC product codes are common on package contents but are not
        shipping identifiers. They may still confirm an exact value already in
        the audit, but they must never create a new ``Not logged`` record.
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
    def ocr_trackings(self) -> tuple[str, ...]:
        return extract_tracking_values(self.ocr_text)

    @property
    def trackings(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.barcode_trackings, *self.ocr_trackings)))

    @property
    def ocr_tokens(self) -> frozenset[str]:
        return frozenset(
            normalize_code(match.group())
            for match in _OCR_TOKEN_RE.finditer(self.ocr_text.upper())
            if normalize_code(match.group())
        )

    @property
    def unit_tokens(self) -> frozenset[str]:
        return frozenset(
            normalize_code(match.group(1))
            for match in _LABELED_UNIT_RE.finditer(self.ocr_text)
            if _UNIT_TOKEN_RE.fullmatch(normalize_code(match.group(1)))
        )

    @property
    def scan_key(self) -> str:
        payload = json.dumps(
            {
                "barcodes": self.barcodes,
                "ocr": " ".join(self.ocr_text.upper().split()),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class AuditMatchRecord:
    item_id: str
    unit: str
    resident: str
    resident_tokens: frozenset[str]
    trackings: tuple[str, ...]
    last4: str
    audited: bool

    @classmethod
    def from_entry(cls, entry: AuditEntry) -> AuditMatchRecord:
        return cls(
            item_id=entry.item_id,
            unit=normalize_code(entry.unit),
            resident=entry.resident,
            resident_tokens=resident_name_tokens(entry.resident),
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
    confidence: float
    features: dict[str, float]
    reasons: tuple[str, ...]
    audited: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "unit": self.unit,
            "resident": self.resident,
            "last4": self.last4,
            "confidence": round(self.confidence, 4),
            "reasons": list(self.reasons),
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


_DEFAULT_WEIGHTS = {
    "barcode_tracking_exact": 7.0,
    "tracking_exact": 5.5,
    "tracking_last4": 1.8,
    "unit_exact": 3.0,
    "unit_fuzzy": 0.8,
    "surname_exact": 2.2,
    "surname_fuzzy": 0.7,
    "learned_unit_alias": 1.8,
    "learned_surname_alias": 1.5,
}


@dataclass
class AdaptiveMatchModel:
    """Small online logistic model trained by scan confirmations and corrections."""

    bias: float = -4.0
    weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    examples: int = 0
    unit_aliases: dict[str, dict[str, int]] = field(default_factory=dict)
    surname_aliases: dict[str, dict[str, int]] = field(default_factory=dict)

    def probability(self, features: dict[str, float]) -> float:
        value = self.bias + sum(self.weights.get(name, 0.0) * amount for name, amount in features.items())
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, value))))

    def learn(self, features: dict[str, float], accepted: bool, learning_rate: float = 0.08) -> None:
        expected = 1.0 if accepted else 0.0
        error = expected - self.probability(features)
        self.bias = max(-8.0, min(2.0, self.bias + learning_rate * error))
        for name, amount in features.items():
            current = self.weights.get(name, 0.0)
            self.weights[name] = max(-3.0, min(12.0, current + learning_rate * error * amount))
        self.examples += 1

    @staticmethod
    def _record_alias(store: dict[str, dict[str, int]], observed: str, actual: str) -> None:
        if not observed or not actual or observed == actual:
            return
        choices = store.setdefault(observed, {})
        choices[actual] = choices.get(actual, 0) + 1

    def learn_correction(self, observation: ScanObservation, record: AuditMatchRecord) -> None:
        tokens = observation.ocr_tokens
        if record.unit not in tokens:
            similar_units = [token for token in tokens if abs(len(token) - len(record.unit)) <= 1]
            if similar_units:
                observed = max(
                    similar_units, key=lambda token: SequenceMatcher(None, token, record.unit).ratio()
                )
                if SequenceMatcher(None, observed, record.unit).ratio() >= 0.55:
                    self._record_alias(self.unit_aliases, observed, record.unit)

        for actual in record.resident_tokens:
            if actual in tokens:
                continue
            name_tokens = [token for token in tokens if token.isalpha() and len(token) >= 3]
            if name_tokens:
                observed = max(name_tokens, key=lambda token: SequenceMatcher(None, token, actual).ratio())
                if SequenceMatcher(None, observed, actual).ratio() >= 0.65:
                    self._record_alias(self.surname_aliases, observed, actual)

    @staticmethod
    def _has_alias(store: dict[str, dict[str, int]], observed: str, actual: str) -> bool:
        return store.get(observed, {}).get(actual, 0) >= 2

    def unit_alias_matches(self, observed_tokens: frozenset[str], actual: str) -> bool:
        return any(self._has_alias(self.unit_aliases, token, actual) for token in observed_tokens)

    def surname_alias_matches(self, observed_tokens: frozenset[str], actual_names: frozenset[str]) -> bool:
        return any(
            self._has_alias(self.surname_aliases, token, actual)
            for token in observed_tokens
            for actual in actual_names
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bias": self.bias,
            "weights": self.weights,
            "examples": self.examples,
            "unit_aliases": self.unit_aliases,
            "surname_aliases": self.surname_aliases,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> AdaptiveMatchModel:
        if not isinstance(value, dict) or not value:
            return cls()

        def number(name: str, default: float) -> float:
            try:
                return float(value.get(name, default))
            except (TypeError, ValueError):
                return default

        def aliases(name: str) -> dict[str, dict[str, int]]:
            raw = value.get(name, {})
            if not isinstance(raw, dict):
                return {}
            cleaned: dict[str, dict[str, int]] = {}
            for observed, choices in raw.items():
                if not isinstance(observed, str) or not isinstance(choices, dict):
                    continue
                cleaned[observed] = {
                    actual: count
                    for actual, count in choices.items()
                    if isinstance(actual, str) and isinstance(count, int) and count >= 0
                }
            return cleaned

        weights = dict(_DEFAULT_WEIGHTS)
        raw_weights = value.get("weights", {})
        if isinstance(raw_weights, dict):
            for name, weight in raw_weights.items():
                if not isinstance(name, str):
                    continue
                try:
                    weights[name] = float(weight)
                except (TypeError, ValueError):
                    continue
        return cls(
            bias=number("bias", -4.0),
            weights=weights,
            examples=max(0, int(number("examples", 0.0))),
            unit_aliases=aliases("unit_aliases"),
            surname_aliases=aliases("surname_aliases"),
        )


class PackageMatcher:
    """Rank loaded audit entries without ever using carrier as a match signal."""

    def __init__(
        self,
        entries: list[AuditEntry],
        model: AdaptiveMatchModel | None = None,
        *,
        high_threshold: float = 0.94,
        review_threshold: float = 0.55,
    ) -> None:
        self.records = [AuditMatchRecord.from_entry(entry) for entry in entries]
        self.records_by_id = {record.item_id: record for record in self.records}
        self.model = model or AdaptiveMatchModel()
        self.high_threshold = high_threshold
        self.review_threshold = review_threshold
        self.tracking_index: dict[str, list[AuditMatchRecord]] = {}
        for record in self.records:
            for tracking in record.trackings:
                self.tracking_index.setdefault(tracking, []).append(record)

    @staticmethod
    def _similarity(value: str, candidates: frozenset[str]) -> float:
        if not value or not candidates:
            return 0.0
        return max(SequenceMatcher(None, value, candidate).ratio() for candidate in candidates)

    def _features(self, observation: ScanObservation, record: AuditMatchRecord) -> dict[str, float]:
        tokens = observation.ocr_tokens
        barcode_trackings = set(observation.barcode_trackings)
        all_trackings = set(observation.trackings)
        record_trackings = set(record.trackings)
        tracking_exact = bool(all_trackings & record_trackings)
        barcode_exact = bool(barcode_trackings & record_trackings)
        tracking_last4 = bool(
            record.last4 != MISSING_LAST4
            and any(tracking.endswith(record.last4) for tracking in all_trackings)
        )
        unit_exact = record.unit in tokens
        unit_fuzzy = max(
            (
                SequenceMatcher(None, token, record.unit).ratio()
                for token in tokens
                if abs(len(token) - len(record.unit)) <= 1 and any(character.isdigit() for character in token)
            ),
            default=0.0,
        )
        surname_exact = bool(tokens & record.resident_tokens)
        surname_fuzzy = max(
            (self._similarity(token, record.resident_tokens) for token in tokens if token.isalpha()),
            default=0.0,
        )
        return {
            "barcode_tracking_exact": float(barcode_exact),
            "tracking_exact": float(tracking_exact),
            "tracking_last4": float(tracking_last4),
            "unit_exact": float(unit_exact),
            "unit_fuzzy": unit_fuzzy if unit_fuzzy >= 0.72 else 0.0,
            "surname_exact": float(surname_exact),
            "surname_fuzzy": surname_fuzzy if surname_fuzzy >= 0.78 else 0.0,
            "learned_unit_alias": float(self.model.unit_alias_matches(tokens, record.unit)),
            "learned_surname_alias": float(self.model.surname_alias_matches(tokens, record.resident_tokens)),
        }

    @staticmethod
    def _reasons(features: dict[str, float]) -> tuple[str, ...]:
        labels = {
            "barcode_tracking_exact": "barcode tracking",
            "tracking_exact": "tracking",
            "tracking_last4": "tracking last four",
            "unit_exact": "unit",
            "unit_fuzzy": "similar unit",
            "surname_exact": "resident name",
            "surname_fuzzy": "similar resident name",
            "learned_unit_alias": "learned unit correction",
            "learned_surname_alias": "learned name correction",
        }
        return tuple(labels[name] for name, value in features.items() if value)

    def rank(self, observation: ScanObservation) -> tuple[MatchCandidate, ...]:
        candidates = []
        for record in self.records:
            features = self._features(observation, record)
            confidence = self.model.probability(features)
            candidates.append(
                MatchCandidate(
                    item_id=record.item_id,
                    unit=record.unit,
                    resident=record.resident,
                    last4=record.last4,
                    confidence=confidence,
                    features=features,
                    reasons=self._reasons(features),
                    audited=record.audited,
                )
            )
        return tuple(sorted(candidates, key=lambda candidate: candidate.confidence, reverse=True))

    def decide(self, observation: ScanObservation) -> ScanDecision:
        duplicate_tracking = next(
            (
                tracking
                for tracking in observation.trackings
                if len(self.tracking_index.get(tracking, [])) > 1
            ),
            "",
        )
        duplicate_records = self.tracking_index.get(duplicate_tracking, [])
        if duplicate_records:
            units = sorted({record.unit for record in duplicate_records})
            return ScanDecision(
                status="duplicate",
                confidence=1.0,
                message=(
                    f"Tracking appears {len(duplicate_records)} times in this audit ({', '.join(units)})."
                ),
                tracking=duplicate_tracking,
                unit=units[0] if len(units) == 1 else "",
                carrier=observation.carrier,
                scan_key=observation.scan_key,
                related_item_ids=tuple(record.item_id for record in duplicate_records),
            )

        matched_record_ids = {
            record.item_id
            for tracking in observation.trackings
            for record in self.tracking_index.get(tracking, [])
        }
        if len(matched_record_ids) > 1:
            ranked = self.rank(observation)
            candidates = tuple(candidate for candidate in ranked if candidate.item_id in matched_record_ids)
            return ScanDecision(
                status="review",
                confidence=candidates[0].confidence if candidates else 0.0,
                message="Multiple barcodes match different packages. Choose the correct package.",
                candidates=candidates,
                carrier=observation.carrier,
                scan_key=observation.scan_key,
                related_item_ids=tuple(candidate.item_id for candidate in candidates),
            )

        exact_tracking_record = next(
            (
                record
                for tracking in observation.trackings
                for record in self.tracking_index.get(tracking, [])
            ),
            None,
        )
        conflicting_units = observation.unit_tokens - (
            {exact_tracking_record.unit} if exact_tracking_record else set()
        )
        if exact_tracking_record and conflicting_units:
            scanned_unit = sorted(conflicting_units)[0]
            related = [exact_tracking_record]
            related.extend(
                record
                for record in self.records
                if record.unit == scanned_unit and record.item_id != exact_tracking_record.item_id
            )
            tracking = next(
                value for value in observation.trackings if value in exact_tracking_record.trackings
            )
            return ScanDecision(
                status="duplicate",
                confidence=1.0,
                message=(
                    f"Tracking belongs to unit {exact_tracking_record.unit}, but the label reads "
                    f"unit {scanned_unit}. Investigate this conflict."
                ),
                tracking=tracking,
                unit=scanned_unit,
                carrier=observation.carrier,
                scan_key=observation.scan_key,
                related_item_ids=tuple(record.item_id for record in related),
            )

        ranked = self.rank(observation)
        best = ranked[0] if ranked else None
        best_record = self.records_by_id.get(best.item_id) if best else None
        matching_tracking = next(
            (
                tracking
                for tracking in observation.trackings
                if best_record and tracking in best_record.trackings
            ),
            observation.trackings[0] if observation.trackings else "",
        )
        second_score = ranked[1].confidence if len(ranked) > 1 else 0.0
        exact_barcode_match = bool(best and best.features["barcode_tracking_exact"])
        reliable_ocr = observation.ocr_confidence >= 50.0
        exact_tracking_confirmed = bool(
            best
            and reliable_ocr
            and best.features["tracking_exact"]
            and (best.features["unit_exact"] or best.features["surname_exact"])
        )
        last4_confirmed = bool(
            best
            and reliable_ocr
            and best.features["tracking_last4"]
            and best.features["unit_exact"]
            and best.features["surname_exact"]
        )
        unambiguous = bool(best and best.confidence - second_score >= 0.08)

        exact_full_match = exact_barcode_match or exact_tracking_confirmed
        if (
            best
            and best.confidence >= self.high_threshold
            and (exact_full_match or (unambiguous and last4_confirmed))
        ):
            return ScanDecision(
                status="already_matched" if best.audited else "matched",
                confidence=best.confidence,
                message="This package is already marked here."
                if best.audited
                else "Package matched and marked here.",
                candidates=(best,),
                tracking=matching_tracking,
                unit=best.unit,
                carrier=observation.carrier,
                scan_key=observation.scan_key,
                related_item_ids=(best.item_id,),
            )

        reliable_barcodes = observation.reliable_barcode_trackings
        all_barcodes_unmatched = bool(reliable_barcodes) and not any(
            value in self.tracking_index for value in reliable_barcodes
        )
        ocr_confirmed_barcode = next(
            (value for value in reliable_barcodes if value in observation.ocr_trackings),
            "",
        )
        reliable_unmatched_tracking = ocr_confirmed_barcode or (
            reliable_barcodes[0] if len(reliable_barcodes) == 1 else ""
        )
        if all_barcodes_unmatched and reliable_unmatched_tracking:
            observed_unit = next(iter(sorted(observation.unit_tokens)), "")
            return ScanDecision(
                status="not_found",
                confidence=max(0.95, 1.0 - (best.confidence if best else 0.0)),
                message="This barcode is not present in the loaded audit and was logged as Not logged.",
                tracking=reliable_unmatched_tracking,
                unit=observed_unit,
                carrier=observation.carrier,
                scan_key=observation.scan_key,
            )

        review_candidates = tuple(
            candidate for candidate in ranked[:3] if candidate.confidence >= self.review_threshold
        )
        if review_candidates:
            return ScanDecision(
                status="review",
                confidence=review_candidates[0].confidence,
                message="Do you mean one of these packages?",
                candidates=review_candidates,
                tracking=matching_tracking,
                unit=review_candidates[0].unit,
                carrier=observation.carrier,
                scan_key=observation.scan_key,
                related_item_ids=tuple(candidate.item_id for candidate in review_candidates),
            )

        return ScanDecision(
            status="poor_scan",
            confidence=best.confidence if best else 0.0,
            message="The label could not be matched safely. Retake the photo or investigate at the desk.",
            tracking=matching_tracking,
            carrier=observation.carrier,
            scan_key=observation.scan_key,
        )

    def learn_selection(
        self,
        observation: ScanObservation,
        selected_item_id: str,
        suggested_item_id: str | None = None,
    ) -> None:
        selected = self.records_by_id[selected_item_id]
        if suggested_item_id and suggested_item_id != selected_item_id:
            suggested = self.records_by_id.get(suggested_item_id)
            if suggested:
                self.model.learn(self._features(observation, suggested), False)
        self.model.learn(self._features(observation, selected), True)
        self.model.learn_correction(observation, selected)

    def learn_rejection(self, observation: ScanObservation, rejected_item_id: str) -> None:
        rejected = self.records_by_id.get(rejected_item_id)
        if rejected:
            self.model.learn(self._features(observation, rejected), False)

    def duplicate_groups(self) -> tuple[tuple[str, tuple[AuditMatchRecord, ...]], ...]:
        return tuple(
            (tracking, tuple(records))
            for tracking, records in self.tracking_index.items()
            if len(records) > 1
        )
