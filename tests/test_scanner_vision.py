"""Tests for fast local barcode-only image processing."""

from __future__ import annotations

import io

import pytest
import qrcode
import zxingcpp
from PIL import Image

import app.scanner_vision as scanner_vision
from app.scanner_vision import ScanImageError, analyze_image, scanner_capabilities


def _qr_image(payload: str = "1Z999AA10123456784") -> bytes:
    qr = qrcode.make(payload).convert("RGB").resize((500, 500), Image.Resampling.NEAREST)
    canvas = Image.new("RGB", (700, 700), "white")
    canvas.paste(qr, (100, 100))
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


def test_analyze_image_decodes_qr_without_ocr_fields():
    observation = analyze_image(_qr_image())

    assert "1Z999AA10123456784" in observation.barcodes
    assert observation.carrier == "UPS"
    assert observation.barcode_formats
    assert not hasattr(observation, "ocr_text")
    assert not hasattr(observation, "ocr_confidence")


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


def test_scanner_capabilities_report_only_barcode_backend():
    capabilities = scanner_capabilities()
    assert capabilities.barcode is True
    assert capabilities.to_dict() == {"barcode": True}


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
