"""Framework-independent model training math tests."""

import pytest

from app.label_training import TrainingDataset
from app.model_training import box_iou, reserve_test_evaluation, rotate_box


def test_box_iou_handles_identical_disjoint_and_partial_boxes():
    assert box_iou([0.1, 0.1, 0.4, 0.4], [0.1, 0.1, 0.4, 0.4]) == 1.0
    assert box_iou([0.0, 0.0, 0.1, 0.1], [0.5, 0.5, 0.1, 0.1]) == 0.0
    assert 0.0 < box_iou([0.0, 0.0, 0.5, 0.5], [0.25, 0.25, 0.5, 0.5]) < 1.0


def test_rotate_box_keeps_annotations_aligned_with_oriented_images():
    box = [0.1, 0.2, 0.3, 0.4]
    assert rotate_box(box, 0) == box
    assert rotate_box(box, 90) == pytest.approx([0.4, 0.1, 0.4, 0.3])
    assert rotate_box(box, 180) == pytest.approx([0.6, 0.4, 0.3, 0.4])
    assert rotate_box(box, 270) == pytest.approx([0.2, 0.6, 0.4, 0.3])


def test_test_images_cannot_be_reused_by_renaming_or_changing_a_dataset(tmp_path):
    dataset = TrainingDataset(tmp_path)
    try:
        manifest = {
            "version_id": "dataset-first",
            "samples": [{"split": "test", "image_sha256": "same-image"}],
        }
        reserve_test_evaluation(dataset, manifest)
        manifest["version_id"] = "dataset-second"
        with pytest.raises(ValueError, match="already been evaluated"):
            reserve_test_evaluation(dataset, manifest)
        manifest["samples"][0]["image_sha256"] = "fresh-image"
        reserve_test_evaluation(dataset, manifest)
    finally:
        dataset.close()
