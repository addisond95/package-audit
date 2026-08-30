"""PDF parsing for BuildingLink event log exports.

The exporter renders each package event as a loosely structured block of text
lines. :func:`parse_buildinglink_pdf` reconstructs those blocks into structured
:class:`~app.models.AuditEntry` records.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pymupdf as fitz

from app.models import AuditEntry

UNIT_RE = re.compile(r"^\d{4}[A-Z]$")
LABELED_UNIT_RE = re.compile(r"^(?=.*\d)[A-Z0-9][A-Z0-9-]{0,15}$", re.IGNORECASE)
PAGE_FOOTER_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2},.*Event log \| BuildingLink$")
OPEN_EVENTS_RE = re.compile(r"^Open events\s*-\s*")
PACKAGE_MARKER_RE = re.compile(r"\s*-\s*#")
PACKAGE_START_RE = re.compile(r"(?<!\S)\S+\s*-\s*#")
TOWER_TIMESTAMP_RE = re.compile(
    r"(?P<tower>.+?)\s+"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<time>\d{1,2}:\d{2}:\d{2}\s+[AP]M)$"
)
MERGED_DIRECTIONAL_TOWER_TIMESTAMP_RE = re.compile(
    r"(?P<tower>(?:(?:North|South|East|West|Northeast|Northwest|Southeast|Southwest|"
    r"Central|Upper|Lower)\s+){1,2}Tower)\s+"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<time>\d{1,2}:\d{2}:\d{2}\s+[AP]M)$",
    re.IGNORECASE,
)
MERGED_NAMED_TOWER_TIMESTAMP_RE = re.compile(
    r"(?P<tower>\S+\s+Tower)\s+"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<time>\d{1,2}:\d{2}:\d{2}\s+[AP]M)$",
    re.IGNORECASE,
)


def file_hash(path: Path) -> str:
    hasher = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)

    return hasher.hexdigest()


def clean_lines(text: str) -> list[str]:
    lines: list[str] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if OPEN_EVENTS_RE.match(line):
            continue

        if PAGE_FOOTER_RE.match(line):
            continue

        if line.startswith("https://www.buildinglink.com/"):
            continue

        if re.fullmatch(r"\d+/\d+", line):
            continue

        lines.append(line)

    return lines


def parse_buildinglink_pdf(pdf_path: Path) -> list[AuditEntry]:
    """Parse a BuildingLink event log PDF into a list of audit entries."""
    entries: list[AuditEntry] = []

    with fitz.open(str(pdf_path)) as doc:
        for page_index, page in enumerate(doc):
            lines = clean_lines(page.get_text("text"))
            _parse_page(lines, page_index, entries)

    return entries


def _parse_page(lines: list[str], page_index: int, entries: list[AuditEntry]) -> None:
    """Parse a single cleaned page, appending entries in place."""
    i = 0
    legacy_entry_index = sum(UNIT_RE.fullmatch(entry.unit) is not None for entry in entries)
    labeled_occurrences: dict[tuple[str, str, str, str], int] = {}
    for entry in entries:
        if entry.page_index == page_index and UNIT_RE.fullmatch(entry.unit) is None:
            key = (entry.unit, entry.resident, entry.package, entry.timestamp)
            labeled_occurrences[key] = labeled_occurrences.get(key, 0) + 1

    while i < len(lines):
        if not _is_unit_line(lines, i):
            i += 1
            continue

        unit = lines[i]
        i += 1

        # BuildingLink sometimes emits a literal "Unit" label after the number.
        if i < len(lines) and lines[i].lower() == "unit":
            i += 1

        block: list[str] = []
        while i < len(lines) and not _is_unit_line(lines, i):
            block.append(lines[i])
            i += 1

        if not block:
            continue

        tower, timestamp, content = _extract_tower_and_content(block)
        resident, package = _split_resident_and_package(content)

        if UNIT_RE.fullmatch(unit):
            raw_id = f"{page_index}|{legacy_entry_index}|{unit}|{resident}|{package}|{timestamp}"
            legacy_entry_index += 1
        else:
            occurrence_key = (unit, resident, package, timestamp)
            duplicate_index = labeled_occurrences.get(occurrence_key, 0)
            labeled_occurrences[occurrence_key] = duplicate_index + 1
            raw_id = f"labeled|{page_index}|{duplicate_index}|{unit}|{resident}|{package}|{timestamp}"
        # This is a compact stable identifier, not a security digest.
        item_id = hashlib.sha1(raw_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]

        entries.append(
            AuditEntry(
                item_id=item_id,
                page_index=page_index,
                unit=unit,
                resident=resident,
                package=package,
                tower=tower,
                timestamp=timestamp,
            )
        )


def _is_unit_line(lines: list[str], index: int) -> bool:
    if UNIT_RE.fullmatch(lines[index]):
        return True
    return (
        LABELED_UNIT_RE.fullmatch(lines[index]) is not None
        and index + 1 < len(lines)
        and lines[index + 1].casefold() == "unit"
    )


def _extract_tower_and_content(block: list[str]) -> tuple[str, str, list[str]]:
    """Pull the trailing tower + timestamp out of a unit block.

    Returns the tower, the formatted timestamp, and the remaining content lines.
    """
    end_index: int | None = None
    tower = ""
    timestamp = ""

    for idx in range(len(block) - 1, -1, -1):
        patterns = [TOWER_TIMESTAMP_RE]
        if PACKAGE_MARKER_RE.search(block[idx]):
            patterns = [
                MERGED_DIRECTIONAL_TOWER_TIMESTAMP_RE,
                MERGED_NAMED_TOWER_TIMESTAMP_RE,
                TOWER_TIMESTAMP_RE,
            ]

        for pattern in patterns:
            match = pattern.search(block[idx])
            if match:
                break
        else:
            continue

        if match is not None:
            end_index = idx
            tower = match.group("tower").strip()
            timestamp = f"{match.group('date')} {match.group('time')}"
            block[idx] = pattern.sub("", block[idx]).strip()
            break

    if end_index is None:
        end_index = len(block) - 1

    content = [line for line in block[: end_index + 1] if line.strip()]
    return tower, timestamp, content


def _split_resident_and_package(content: list[str]) -> tuple[str, str]:
    """Split a unit block's content into resident text and package text."""
    package_start = None
    for idx, line in enumerate(content):
        if PACKAGE_MARKER_RE.search(line):
            package_start = idx
            break

    if package_start is None:
        resident_lines = content[:1]
        package_lines = content[1:]
    else:
        package_line = content[package_start]
        package_match = PACKAGE_START_RE.search(package_line)
        if package_match is None:
            resident_lines = content[:package_start]
            package_lines = content[package_start:]
        else:
            resident_prefix = package_line[: package_match.start()].strip()
            resident_lines = content[:package_start]
            if resident_prefix:
                resident_lines.append(resident_prefix)
            package_lines = [package_line[package_match.start() :], *content[package_start + 1 :]]

    return " ".join(resident_lines).strip(), " ".join(package_lines).strip()
