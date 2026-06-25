"""Shared, framework-free constants for the package audit application.

This module must stay free of any Qt or PyMuPDF imports so it can be used by
the data layer (models, database, reporting) and unit tests without pulling in
the GUI stack.
"""

from __future__ import annotations

from pathlib import Path

APP_NAME = "BuildingLink Package Audit"

#: Directory where audit state is persisted. Kept stable so existing audits are
#: not lost between versions.
APP_DIR = Path.home() / ".package_audit"
DB_FILENAME = "audit_state.sqlite3"

#: Canonical package storage locations used in the manual report tables.
LOCATIONS = ["SHELF", "BIN", "BB", "CG", "UG", "ALPHA", "FCR"]

#: Canonical carrier codes used in the manual report tables.
CARRIERS = ["USPS", "UPS", "FEDEX", "AMZ", "ONTRAC", "DHL", "PKG", "KEY", "FOOD", "PHARMACY"]

#: Dropdown option lists include a leading blank for "no selection".
LOCATION_OPTIONS = ["", *LOCATIONS]
CARRIER_OPTIONS = ["", *CARRIERS]

#: Common carrier spelling variants mapped to their canonical code. Canonical
#: codes are intentionally omitted because ``normalize_carrier`` falls back to
#: the input value when it is not found here.
CARRIER_ALIASES = {
    "FEDX": "FEDEX",
    "FDX": "FEDEX",
    "AMAZON": "AMZ",
    "PHARM": "PHARMACY",
    "RX": "PHARMACY",
}

#: Placeholder used when a tracking value cannot be determined.
MISSING_LAST4 = "NaN"

#: Default audit highlight color as an RGBA tuple (0-255 per channel).
DEFAULT_HIGHLIGHT_RGBA = (80, 200, 120, 95)
