"""Offscreen tests for the human label-annotation workflow."""

from __future__ import annotations

import io
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

from app.label_annotation import LabelAnnotationDialog


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def label_image() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (640, 400), "white").save(output, "JPEG")
    return output.getvalue()


def test_annotation_dialog_loads_existing_regions_and_returns_valid_schema(application):
    sample = {
        "dataset_split": "validation",
        "metadata": {
            "carrier": "UPS",
            "capture_session": "audit-2026-09-23",
            "annotations": {
                "carrier": "UPS",
                "condition": "glare",
                "orientation": 90,
                "group_id": "audit-2026-09-23",
                "regions": [{"kind": "barcode", "box": [0.1, 0.2, 0.5, 0.2]}],
            },
        },
    }
    dialog = LabelAnnotationDialog(sample, label_image())

    assert dialog.dataset_split == "validation"
    assert dialog.annotations["carrier"] == "UPS"
    assert dialog.annotations["condition"] == "glare"
    assert dialog.annotations["orientation"] == 90
    assert dialog.annotations["regions"] == [{"kind": "barcode", "box": [0.1, 0.2, 0.5, 0.2]}]
    dialog.close()
    application.processEvents()
