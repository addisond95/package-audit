"""Conservative carrier classification and privacy-safe format learning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.models import normalize_carrier

_BUILTIN_PATTERNS = (
    ("UPS", "ups_1z", re.compile(r"1Z[0-9A-Z]{16}"), 1.0),
    ("UPS", "ups_t", re.compile(r"T\d{10}"), 0.98),
    ("USPS", "usps_domestic", re.compile(r"9[2345]\d{18,32}"), 0.96),
    ("USPS", "usps_international", re.compile(r"[A-Z]{2}\d{9}US"), 0.99),
    ("AMZ", "amazon_tba", re.compile(r"TBA\d{10,14}"), 0.99),
)


@dataclass(frozen=True)
class CarrierGuess:
    carrier: str = "PKG"
    confidence: float = 0.0
    source: str = "unknown"
    pattern: str = ""


def tracking_signature(tracking: str, barcode_format: str = "") -> str:
    """Describe identifier structure without retaining its letters or digits."""
    mask = "".join("9" if char.isdigit() else "A" if char.isalpha() else "-" for char in tracking)
    safe_format = "".join(char for char in barcode_format.upper() if char.isalnum() or char == "_")[:32]
    return f"{safe_format or 'UNKNOWN'}:{len(tracking)}:{mask}"


def builtin_carrier(tracking: str) -> CarrierGuess:
    value = tracking.strip().upper()
    for carrier, pattern_id, pattern, confidence in _BUILTIN_PATTERNS:
        if pattern.fullmatch(value):
            return CarrierGuess(carrier, confidence, "builtin", pattern_id)
    return CarrierGuess()


def classify_carrier(
    trackings: tuple[str, ...],
    barcode_formats: tuple[str, ...] = (),
    model: dict[str, Any] | None = None,
) -> CarrierGuess:
    """Use documented formats first, then locally confirmed structural evidence."""
    builtin = [builtin_carrier(value) for value in trackings]
    recognized = [guess for guess in builtin if guess.carrier != "PKG"]
    if recognized:
        recognized.sort(key=lambda guess: guess.confidence, reverse=True)
        return recognized[0]

    patterns = (model or {}).get("carrier_patterns", {})
    if not isinstance(patterns, dict):
        return CarrierGuess()
    best = CarrierGuess()
    for index, tracking in enumerate(trackings):
        barcode_format = barcode_formats[index] if index < len(barcode_formats) else ""
        counts = patterns.get(tracking_signature(tracking, barcode_format), {})
        if not isinstance(counts, dict):
            continue
        clean = {
            normalize_carrier(carrier): int(count)
            for carrier, count in counts.items()
            if isinstance(carrier, str)
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
            and normalize_carrier(carrier) != "PKG"
        }
        total = sum(clean.values())
        if total < 3 or not clean:
            continue
        carrier, count = max(clean.items(), key=lambda item: item[1])
        confidence = count / total
        if confidence >= 0.8 and confidence > best.confidence:
            best = CarrierGuess(carrier, confidence, "learned", "local_confirmed_shape")
    return best
