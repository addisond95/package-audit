"""Privacy-safe scanner outcome learning and bounded unit-rendering aliases."""

from __future__ import annotations

from typing import Any

from app.carrier_matching import tracking_signature
from app.models import normalize_unit
from app.scanner_matching import ScanDecision, ScanObservation
from app.unit_matching import UnitResolver, normalize_unit_reading


def learning_features(observation: ScanObservation, decision: ScanDecision) -> dict[str, Any]:
    """Return non-label, non-resident features suitable for local feedback storage."""
    return {
        "barcode_count": min(len(observation.barcodes), 8),
        "barcode_formats": sorted({value.upper()[:32] for value in observation.barcode_formats if value}),
        "unit_candidate_count": min(len(observation.unit_candidates), 12),
        "unit_scan_attempted": observation.unit_scan_attempted,
        "unit_status": decision.unit_status,
        "decision_status": decision.status,
    }


def update_learning_model(
    model: dict[str, Any] | None,
    observation: ScanObservation,
    decision: ScanDecision,
    outcome: str,
) -> dict[str, Any]:
    """Update aggregate results and learn aliases only from accepted verified scans."""
    current = dict(model or {})
    current["version"] = 2
    current["examples"] = min(int(current.get("examples", 0)) + 1, 10_000_000)
    outcomes = dict(current.get("outcomes", {})) if isinstance(current.get("outcomes"), dict) else {}
    outcomes[outcome] = min(int(outcomes.get(outcome, 0)) + 1, 10_000_000)
    current["outcomes"] = outcomes
    formats = (
        dict(current.get("barcode_formats", {})) if isinstance(current.get("barcode_formats"), dict) else {}
    )
    for barcode_format in learning_features(observation, decision)["barcode_formats"]:
        formats[barcode_format] = min(int(formats.get(barcode_format, 0)) + 1, 10_000_000)
    current["barcode_formats"] = formats
    unit_results = (
        dict(current.get("unit_results", {})) if isinstance(current.get("unit_results"), dict) else {}
    )
    unit_results[decision.unit_status] = min(int(unit_results.get(decision.unit_status, 0)) + 1, 10_000_000)
    current["unit_results"] = unit_results

    aliases = dict(current.get("unit_aliases", {})) if isinstance(current.get("unit_aliases"), dict) else {}
    resolver = UnitResolver(aliases)
    if outcome == "accepted" and decision.unit_status == "verified" and decision.unit:
        for raw in decision.scanned_units[:12]:
            reading = normalize_unit_reading(raw)
            if reading and resolver.matches(decision.unit, reading):
                aliases[reading] = normalize_unit(decision.unit)
    current["unit_aliases"] = dict(list(aliases.items())[-500:])

    carrier_patterns = (
        dict(current.get("carrier_patterns", {})) if isinstance(current.get("carrier_patterns"), dict) else {}
    )
    trusted_carrier = decision.carrier if decision.carrier not in {"", "PKG"} else ""
    if outcome == "accepted" and trusted_carrier and decision.tracking:
        for index, tracking in enumerate(observation.barcode_trackings):
            if tracking != decision.tracking:
                continue
            barcode_format = (
                observation.barcode_tracking_formats[index]
                if index < len(observation.barcode_tracking_formats)
                else ""
            )
            signature = tracking_signature(tracking, barcode_format)
            counts = (
                dict(carrier_patterns.get(signature, {}))
                if isinstance(carrier_patterns.get(signature), dict)
                else {}
            )
            counts[trusted_carrier] = min(int(counts.get(trusted_carrier, 0)) + 1, 10_000_000)
            carrier_patterns[signature] = counts
    current["carrier_patterns"] = dict(list(carrier_patterns.items())[-1000:])
    return current
