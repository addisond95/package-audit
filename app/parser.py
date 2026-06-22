from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

import fitz

from app.models import AuditEntry


UNIT_RE = re.compile(r"^\d{4}[A-Z]$")
PAGE_FOOTER_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2},.*Event log \| BuildingLink$")
OPEN_EVENTS_RE = re.compile(r"^Open events\s*-\s*")
TOWER_TIMESTAMP_RE = re.compile(
    r"(?P<tower>.+?)\s+"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<time>\d{1,2}:\d{2}:\d{2}\s+[AP]M)$"
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
    doc = fitz.open(str(pdf_path))
    entries: list[AuditEntry] = []

    for page_index, page in enumerate(doc):
        lines = clean_lines(page.get_text("text"))
        i = 0

        while i < len(lines):
            if not UNIT_RE.match(lines[i]):
                i += 1
                continue

            unit = lines[i]
            i += 1

            if i < len(lines) and lines[i].lower() == "unit":
                i += 1

            block: list[str] = []

            while i < len(lines) and not UNIT_RE.match(lines[i]):
                block.append(lines[i])
                i += 1

            if not block:
                continue

            end_index: Optional[int] = None
            tower = ""
            timestamp = ""

            for idx in range(len(block) - 1, -1, -1):
                match = TOWER_TIMESTAMP_RE.search(block[idx])
                if match:
                    end_index = idx
                    tower = match.group("tower").strip()
                    timestamp = f"{match.group('date')} {match.group('time')}"
                    block[idx] = TOWER_TIMESTAMP_RE.sub("", block[idx]).strip()
                    break

            if end_index is None:
                end_index = len(block) - 1

            content = [x for x in block[: end_index + 1] if x.strip()]

            package_start = None
            for idx, line in enumerate(content):
                if " - #" in line:
                    package_start = idx
                    break

            if package_start is None:
                resident_lines = content[:1]
                package_lines = content[1:]
            else:
                resident_lines = content[:package_start]
                package_lines = content[package_start:]

            resident = " ".join(resident_lines).strip()
            package = " ".join(package_lines).strip()

            raw_id = f"{page_index}|{len(entries)}|{unit}|{resident}|{package}|{timestamp}"
            item_id = hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:16]

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

    doc.close()
    return entries
