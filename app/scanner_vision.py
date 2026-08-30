"""Local barcode and OCR processing for phone-captured package labels."""

from __future__ import annotations

import csv
import io
import shutil
import subprocess  # nosec B404
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.models import normalize_carrier
from app.scanner_matching import ScanObservation

# Tesseract is resolved from PATH or fixed trusted paths and is never run through a shell.
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_OCR_DIMENSION = 2600
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
_OCR_SLOTS = threading.BoundedSemaphore(2)
_TESSERACT_LOCATIONS = (
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
    "C:/Program Files/Tesseract-OCR/tesseract.exe",
    "C:/Program Files (x86)/Tesseract-OCR/tesseract.exe",
)


class ScanImageError(ValueError):
    """Raised when an upload is not a safe, decodable image."""


@dataclass(frozen=True)
class ScannerCapabilities:
    barcode: bool
    ocr: bool
    ocr_engine: str = ""

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "barcode": self.barcode,
            "ocr": self.ocr,
            "ocr_engine": self.ocr_engine,
        }


def find_tesseract() -> str | None:
    discovered = shutil.which("tesseract")
    if discovered:
        return discovered
    return next((location for location in _TESSERACT_LOCATIONS if Path(location).is_file()), None)


def scanner_capabilities() -> ScannerCapabilities:
    try:
        import zxingcpp  # noqa: F401

        barcode = True
    except ImportError:
        barcode = False
    tesseract = find_tesseract()
    return ScannerCapabilities(
        barcode=barcode, ocr=bool(tesseract), ocr_engine="Tesseract" if tesseract else ""
    )


def _open_image(image_bytes: bytes) -> Image.Image:
    if not image_bytes:
        raise ScanImageError("No image was received.")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ScanImageError("The image is larger than 15 MB.")

    try:
        image = Image.open(io.BytesIO(image_bytes))
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise ScanImageError("The upload is not a supported image.") from exc

    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise ScanImageError("The image dimensions are too large.")
    try:
        image.load()
    except (Image.DecompressionBombError, OSError, ValueError) as exc:
        raise ScanImageError("The upload is not a supported image.") from exc

    if image.width < 80 or image.height < 80:
        raise ScanImageError("The image is too small to scan.")

    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((MAX_OCR_DIMENSION, MAX_OCR_DIMENSION), Image.Resampling.LANCZOS)
    return image


def _decode_barcodes(image: Image.Image) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        import zxingcpp
    except ImportError:
        return (), ()

    values: list[str] = []
    formats: list[str] = []
    for result in zxingcpp.read_barcodes(image):
        value = result.text.strip()
        if value and value not in values:
            values.append(value)
            formats.append(str(result.format))
    return tuple(values), tuple(formats)


def _prepare_ocr_image(image: Image.Image) -> bytes:
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    if gray.width < 1200:
        scale = min(3.0, 1200 / max(gray.width, 1))
        gray = gray.resize(
            (int(gray.width * scale), int(gray.height * scale)),
            Image.Resampling.LANCZOS,
        )
    output = io.BytesIO()
    gray.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _run_tesseract_once(
    image: Image.Image,
    executable: str,
    timeout: float,
) -> tuple[str, float]:
    result = subprocess.run(  # nosec B603
        [executable, "stdin", "stdout", "-l", "eng", "--psm", "6", "tsv"],
        input=_prepare_ocr_image(image),
        capture_output=True,
        check=True,
        timeout=timeout,
    )

    decoded = result.stdout.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(decoded), delimiter="\t")
    lines: dict[tuple[str, str, str, str], list[str]] = {}
    weighted_confidence = 0.0
    character_count = 0
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = max(0.0, float(row.get("conf") or 0.0))
        except ValueError:
            confidence = 0.0
        key = (
            row.get("page_num") or "0",
            row.get("block_num") or "0",
            row.get("par_num") or "0",
            row.get("line_num") or "0",
        )
        lines.setdefault(key, []).append(text)
        weighted_confidence += confidence * len(text)
        character_count += len(text)

    text = "\n".join(" ".join(words) for words in lines.values())
    confidence = weighted_confidence / character_count if character_count else 0.0
    return text, confidence


def _run_tesseract(image: Image.Image, timeout: float = 20.0) -> tuple[str, float]:
    executable = find_tesseract()
    if not executable:
        return "", 0.0

    try:
        with _OCR_SLOTS:
            deadline = time.monotonic() + timeout
            best = ("", 0.0)
            completed = False
            for angle in (0, 90, 180, 270):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                candidate_image = image if angle == 0 else image.rotate(angle, expand=True, fillcolor="white")
                try:
                    candidate = _run_tesseract_once(
                        candidate_image,
                        executable,
                        min(8.0, remaining),
                    )
                except subprocess.TimeoutExpired:
                    continue
                completed = True
                if candidate[1] > best[1]:
                    best = candidate
                if best[1] >= 60.0:
                    break
            if not completed:
                raise ScanImageError("Local OCR timed out. Retake a closer, sharper photo.")
            return best
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScanImageError(f"Local OCR failed: {exc}") from exc


def _detect_carrier(text: str, barcodes: tuple[str, ...]) -> str:
    upper = text.upper()
    aliases = (
        ("FEDEX", "FEDEX"),
        ("FEDX", "FEDEX"),
        ("USPS", "USPS"),
        ("ONTRAC", "ONTRAC"),
        ("AMAZON", "AMZ"),
        ("AMZ", "AMZ"),
        ("DHL", "DHL"),
        ("UPS", "UPS"),
    )
    for label, carrier in aliases:
        if label in upper:
            return normalize_carrier(carrier)

    normalized_barcodes = [
        "".join(character for character in value.upper() if character.isalnum()) for value in barcodes
    ]
    if any(value.startswith("1Z") for value in normalized_barcodes):
        return "UPS"
    if any(value.startswith("TBA") for value in normalized_barcodes):
        return "AMZ"
    return "PKG"


def analyze_image(image_bytes: bytes) -> ScanObservation:
    """Decode all local barcode and OCR evidence from one uploaded image."""
    image = _open_image(image_bytes)
    barcodes, formats = _decode_barcodes(image)
    ocr_text, confidence = _run_tesseract(image)
    return ScanObservation(
        barcodes=barcodes,
        ocr_text=ocr_text,
        ocr_confidence=confidence,
        carrier=_detect_carrier(ocr_text, barcodes),
        barcode_formats=formats,
    )
