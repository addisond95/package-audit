"""Domain models and value normalization helpers.

This module is deliberately free of Qt and PyMuPDF imports so it can be reused
by the database, reporting, and test layers without the GUI stack.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.constants import CARRIER_ALIASES, MISSING_LAST4


@dataclass
class AuditEntry:
    """A single package event parsed from a BuildingLink event log."""

    item_id: str
    page_index: int
    unit: str
    resident: str
    package: str
    tower: str
    timestamp: str
    audited: bool = False

    @property
    def last4(self) -> str:
        """Best-effort last four characters of the tracking number."""
        return extract_last4(self.package)

    @property
    def search_text(self) -> str:
        """Lower-cased haystack used for the audit table search filter."""
        return " ".join(
            [
                self.unit,
                self.last4,
                self.resident,
                self.package,
                self.tower,
                self.timestamp,
                str(self.page_index + 1),
            ]
        ).lower()


@dataclass
class PackageError:
    """A manually recorded package discrepancy (report section 2)."""

    unit: str
    location: str
    carrier: str
    last4: str
    note: str


@dataclass
class DoubleLoggedPackage:
    """A manually recorded duplicate package log (report section 3)."""

    unit: str
    location: str
    carrier: str
    last4: str


def normalize_unit(unit: str) -> str:
    return unit.strip().upper()


def normalize_location(location: str) -> str:
    return location.strip().upper()


def normalize_carrier(carrier: str) -> str:
    """Upper-case a carrier code and resolve common spelling variants."""
    carrier = carrier.strip().upper()
    return CARRIER_ALIASES.get(carrier, carrier)


def normalize_last4(last4: str) -> str:
    """Normalize a tracking value to its last four characters.

    Returns :data:`~app.constants.MISSING_LAST4` when the value is blank or an
    explicit ``nan``. Tracking numbers may contain letters, so this is not
    restricted to digits.
    """
    value = last4.strip()
    if not value or value.lower() == "nan":
        return MISSING_LAST4
    return value[-4:].upper()


def unit_sort_key(unit: str) -> tuple[int, str]:
    unit = normalize_unit(unit)
    digits = ""
    suffix = ""

    for char in unit:
        if char.isdigit():
            digits += char
        else:
            suffix += char

    number = int(digits) if digits else 999999
    return number, suffix


def extract_last4(package_text: str) -> str:
    """
    Try to extract the last four characters of the tracking number.

    BuildingLink examples:
    USPS - #2209361876 - 420981219261290357475302009983
    UPS - #2209280242 - BIN - 1ZW828R0YW92807001
    AMZ - #2209361938 - TBA331958945193
    KEY - #2209130287 - Keys for Pelin, Alpha P

    We ignore the BuildingLink internal # number and look after the final dash.
    If there is no plausible tracking value, return the missing-value placeholder.
    """
    text = " ".join(package_text.split())
    if not text:
        return MISSING_LAST4

    parts = [part.strip() for part in text.split(" - ") if part.strip()]
    candidates = list(reversed(parts))

    location_words = {
        "BIN", "BB", "CG", "CAGE", "UG", "ALPHA", "FCR",
        "SHELF", "FRONT DESK", "LEFT AT THE DESK"
    }

    for candidate in candidates:
        upper = candidate.upper().strip(",")
        if upper in location_words:
            continue
        if upper.startswith("#"):
            continue

        tokens = [token.strip(" ,.;:()[]") for token in candidate.split()]
        tokens = [token for token in tokens if token]

        for token in reversed(tokens):
            token_upper = token.upper()
            if token_upper in location_words:
                continue
            if token_upper.startswith("#"):
                continue

            # Plausible tracking identifiers usually contain at least four alphanumeric chars
            # and at least one digit.
            alnum = "".join(ch for ch in token_upper if ch.isalnum())
            if len(alnum) >= 4 and any(ch.isdigit() for ch in alnum):
                return alnum[-4:]

    return MISSING_LAST4
