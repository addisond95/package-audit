"""Tests for local barcode and OCR image processing."""

from __future__ import annotations

import io
import subprocess

import pytest
import qrcode
import zxingcpp
from PIL import Image, ImageDraw, ImageFont

import app.scanner_vision as scanner_vision
from app.scanner_vision import (
    ScanImageError,
    analyze_image,
    find_tesseract,
    scanner_capabilities,
)


def _label_image(*, include_qr: bool = True) -> bytes:
    image = Image.new("RGB", (1500, 700), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=58)
    if include_qr:
        qr = qrcode.make("1Z999AA10123456784").convert("RGB").resize((330, 330))
        image.paste(qr, (30, 30))
    for index, text in enumerate(("JANE MATHIESEN", "UNIT 1701S", "UPS", "1Z999AA10123456784")):
        draw.text((410, 50 + index * 130), text, fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.mark.skipif(find_tesseract() is None, reason="Tesseract is not installed")
def test_analyze_image_decodes_qr_and_ocr_text():
    observation = analyze_image(_label_image())

    assert "1Z999AA10123456784" in observation.barcodes
    assert "MATHIESEN" in observation.ocr_text
    assert "1701S" in observation.ocr_text
    assert observation.ocr_confidence >= 60
    assert observation.carrier == "UPS"
    assert observation.barcode_formats


@pytest.mark.skipif(find_tesseract() is None, reason="Tesseract is not installed")
def test_analyze_image_uses_ocr_without_barcode():
    observation = analyze_image(_label_image(include_qr=False))

    assert observation.barcodes == ()
    assert "MATHIESEN" in observation.ocr_text
    assert "1Z999AA10123456784" in observation.ocr_trackings


@pytest.mark.skipif(find_tesseract() is None, reason="Tesseract is not installed")
def test_analyze_image_decodes_code_128_barcode():
    payload = "1Z999AA10123456784"
    barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.Code128)
    raster = zxingcpp.write_barcode_to_image(barcode)
    barcode_image = Image.frombytes("L", (raster.shape[1], raster.shape[0]), bytes(raster))
    barcode_image = barcode_image.resize((1254, 300), Image.Resampling.NEAREST).convert("RGB")
    canvas = Image.new("RGB", (1400, 500), "white")
    canvas.paste(barcode_image, (70, 80))
    output = io.BytesIO()
    canvas.save(output, format="PNG")

    observation = analyze_image(output.getvalue())

    assert payload in observation.barcodes
    assert any("Code 128" in barcode_format for barcode_format in observation.barcode_formats)


def test_scanner_capabilities_report_local_backends():
    capabilities = scanner_capabilities()
    assert capabilities.barcode is True
    assert capabilities.ocr is (find_tesseract() is not None)


def test_qr_scanning_works_when_ocr_is_unavailable(monkeypatch):
    monkeypatch.setattr(scanner_vision, "find_tesseract", lambda: None)

    observation = analyze_image(_label_image())

    assert "1Z999AA10123456784" in observation.barcodes
    assert observation.ocr_text == ""
    assert observation.ocr_confidence == 0.0


def test_ocr_rotation_attempts_have_a_bounded_total_failure_message(monkeypatch):
    timeouts = []
    monkeypatch.setattr(scanner_vision, "find_tesseract", lambda: "/trusted/tesseract")

    def time_out(_image, _executable, timeout):
        timeouts.append(timeout)
        raise subprocess.TimeoutExpired("tesseract", timeout)

    monkeypatch.setattr(scanner_vision, "_run_tesseract_once", time_out)

    with pytest.raises(ScanImageError, match="timed out"):
        scanner_vision._run_tesseract(Image.new("RGB", (200, 200)), timeout=20)

    assert len(timeouts) == 4
    assert all(0 < timeout <= 8 for timeout in timeouts)


@pytest.mark.parametrize("payload", [b"", b"not an image"])
def test_analyze_image_rejects_invalid_upload(payload):
    with pytest.raises(ScanImageError):
        analyze_image(payload)


def test_analyze_image_rejects_tiny_image():
    image = Image.new("RGB", (20, 20), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")

    with pytest.raises(ScanImageError, match="too small"):
        analyze_image(output.getvalue())


def test_oversized_dimensions_are_rejected_before_pixels_are_decoded(monkeypatch):
    class OversizedImage:
        width = 10_000
        height = 10_000

        def load(self):
            raise AssertionError("pixel data must not be decoded")

    monkeypatch.setattr(scanner_vision.Image, "open", lambda _stream: OversizedImage())

    with pytest.raises(ScanImageError, match="dimensions are too large"):
        scanner_vision._open_image(b"image header")


@pytest.mark.skipif(find_tesseract() is None, reason="Tesseract is not installed")
@pytest.mark.parametrize("angle", [90, 180, 270])
def test_analyze_image_recovers_rotated_ocr_without_exif(angle):
    image = Image.new("RGB", (1200, 500), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=54)
    draw.text((50, 60), "MATHIESEN UNIT 1701S", fill="black", font=font)
    draw.text((50, 190), "1Z999AA10123456784", fill="black", font=font)
    image = image.rotate(angle, expand=True, fillcolor="white")
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=88)

    observation = analyze_image(output.getvalue())

    assert "1701S" in observation.ocr_text
    assert "1Z999AA10123456784" in observation.ocr_trackings
    assert observation.ocr_confidence >= 60
