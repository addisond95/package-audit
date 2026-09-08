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
#: ``CG`` remains for existing data while ``CAGE`` is the clearer new label.
LOCATIONS = ["SHELF", "BIN", "BB", "CG", "CAGE", "UG", "ALPHA", "FCR", "PERISHABLES", "ODDITIES"]

#: Physical areas that must be confirmed before a final audit export.
AUDIT_AREAS = (
    ("SHELF", "Shelf"),
    ("BB", "BB"),
    ("ALPHA", "Alpha bin"),
    ("BIN", "Bin"),
    ("PERISHABLES", "Perishables"),
    ("CAGE", "Cage / CG"),
    ("UG", "UG"),
    ("FCR", "FCR"),
    ("ODDITIES", "Oddities / other storage"),
)

#: Standard resolutions for scanner alerts. The desktop dialog keeps this
#: list editable and stores additional property-specific templates locally.
DEFAULT_ERROR_TEMPLATES = (
    "Not logged — entered",
    "Logged for one unit but belongs to another",
    "Relogged / duplicate entry",
    "Closed in error — reopened",
    "Wrong unit",
    "Wrong storage location",
    "Wrong carrier",
    "Wrong tracking number",
    "Unit number unreadable — investigated",
    "Package damaged",
    "Other — see notes",
)

#: Percentage/score metrics available to user-defined final audit note rules.
AUDIT_NOTE_METRICS = (
    ("quality_score", "Quality score"),
    ("completion_rate", "Completed-unit rate"),
    ("open_unit_rate", "Units with unclosed packages"),
    ("error_unit_rate", "Units with package errors"),
    ("duplicate_unit_rate", "Units with duplicate logs"),
    ("affected_unit_rate", "Units affected by any issue"),
)
AUDIT_NOTE_OPERATORS = (">=", ">", "<=", "<", "==")

#: Canonical carrier codes used in the manual report tables.
CARRIERS = ["USPS", "UPS", "FEDEX", "AMZ", "ONTRAC", "DHL", "PKG", "KEY", "FOOD", "RX"]

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
    "PACKAGE": "PKG",
    "PHARM": "RX",
    "PHARMACY": "RX",
    "RX": "RX",
}

#: Placeholder used when a tracking value cannot be determined.
MISSING_LAST4 = "NaN"

#: Default audit highlight color as an RGBA tuple (0-255 per channel).
DEFAULT_HIGHLIGHT_RGBA = (80, 200, 120, 95)
