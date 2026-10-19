"""Fast local tracking-barcode decoding for phone-captured labels."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from app.carrier_matching import classify_carrier
from app.scanner_matching import ScanObservation

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_SCAN_DIMENSION = 2600
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class ScanImageError(ValueError):
    """Raised when an upload is not a safe, decodable image."""


@dataclass(frozen=True)
class ScannerCapabilities:
    barcode: bool

    def to_dict(self) -> dict[str, bool]:
        return {"barcode": self.barcode}


def scanner_capabilities() -> ScannerCapabilities:
    try:
        import zxingcpp  # noqa: F401

        barcode = True
    except ImportError:
        barcode = False
    return ScannerCapabilities(barcode=barcode)


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
    image.thumbnail((MAX_SCAN_DIMENSION, MAX_SCAN_DIMENSION), Image.Resampling.LANCZOS)
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


def _detect_carrier(barcodes: tuple[str, ...], barcode_formats: tuple[str, ...] = ()) -> str:
    observation = ScanObservation(barcodes=barcodes, barcode_formats=barcode_formats)
    return classify_carrier(
        observation.barcode_trackings,
        observation.barcode_tracking_formats,
    ).carrier


def observation_from_barcodes(
    barcodes: tuple[str, ...],
    barcode_formats: tuple[str, ...] = (),
    unit_candidates: tuple[str, ...] = (),
    unit_confidences: tuple[float, ...] = (),
    *,
    unit_scan_attempted: bool = False,
) -> ScanObservation:
    """Build a normalized observation from browser- or image-decoded barcodes."""
    values: list[str] = []
    formats: list[str] = []
    for index, raw_value in enumerate(barcodes):
        value = raw_value.strip()
        if not value or value in values:
            continue
        values.append(value)
        formats.append(barcode_formats[index] if index < len(barcode_formats) else "")
    normalized_values = tuple(values)
    return ScanObservation(
        barcodes=normalized_values,
        carrier=_detect_carrier(normalized_values, tuple(formats)),
        barcode_formats=tuple(formats),
        unit_candidates=unit_candidates,
        unit_confidences=unit_confidences,
        unit_scan_attempted=unit_scan_attempted,
    )


def analyze_image(image_bytes: bytes) -> ScanObservation:
    """Decode tracking barcodes without running slow text OCR."""
    image = _open_image(image_bytes)
    barcodes, formats = _decode_barcodes(image)
    return observation_from_barcodes(barcodes, formats)
