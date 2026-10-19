"""Security and lifecycle tests for the opt-in label training dataset API."""

import io
import json
import zipfile

import pytest
from PIL import Image

from app.label_training import TrainingDataset, create_training_api, validate_annotations


def reviewed_annotations(group_id="audit-1"):
    return {
        "carrier": "UPS",
        "condition": "good",
        "orientation": 0,
        "group_id": group_id,
        "regions": [{"kind": "barcode", "box": [0.1, 0.2, 0.6, 0.2]}],
    }


def jpeg_bytes(color="white") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (320, 200), color).save(output, "JPEG")
    return output.getvalue()


def sample_bundle(metadata=None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("image.jpg", jpeg_bytes())
        archive.writestr(
            "metadata.json",
            json.dumps(metadata or {"schema_version": 1, "consent": True, "outcome": "damaged_label"}),
        )
    return output.getvalue()


@pytest.fixture()
def dataset(tmp_path):
    value = TrainingDataset(tmp_path / "training")
    yield value
    value.close()


def test_sample_requires_explicit_consent_and_supported_outcome(dataset):
    with pytest.raises(ValueError, match="consent"):
        dataset.add_sample(jpeg_bytes(), {"outcome": "correct"})
    with pytest.raises(ValueError, match="outcome"):
        dataset.add_sample(jpeg_bytes(), {"consent": True, "outcome": "made_up"})


def test_images_and_private_metadata_are_encrypted_at_rest(dataset):
    raw = jpeg_bytes()
    sample = dataset.add_sample(
        raw,
        {
            "consent": True,
            "outcome": "damaged_label",
            "carrier": "UPS",
            "notes": "Resident Sample at 123 Private Street",
        },
    )

    blob = (dataset.blobs / f"{sample['sample_id']}.bin").read_bytes()
    database_bytes = dataset.db_path.read_bytes()
    assert raw[:20] not in blob
    assert b"Resident Sample" not in blob
    assert b"Resident Sample" not in database_bytes
    assert dataset.image_bytes(sample["sample_id"])
    assert dataset.get_sample(sample["sample_id"])["metadata"]["notes"].startswith("Resident")


def test_approved_sample_requires_human_annotations(dataset):
    sample = dataset.add_sample(jpeg_bytes(), {"consent": True, "outcome": "correct"})
    with pytest.raises(ValueError, match="annotations"):
        dataset.review_sample(
            sample["sample_id"],
            status="approved",
            annotations={},
            reviewer="Reviewer",
            dataset_split="train",
        )
    with pytest.raises(ValueError, match="reviewer"):
        dataset.review_sample(
            sample["sample_id"],
            status="approved",
            annotations=reviewed_annotations(),
            reviewer="",
            dataset_split="train",
        )
    with pytest.raises(ValueError, match="split"):
        dataset.review_sample(
            sample["sample_id"],
            status="approved",
            annotations=reviewed_annotations(),
            reviewer="Reviewer",
        )

    reviewed = dataset.review_sample(
        sample["sample_id"],
        status="approved",
        annotations=reviewed_annotations(),
        reviewer="Reviewer",
        dataset_split="test",
    )
    assert (reviewed["status"], reviewed["dataset_split"]) == ("approved", "test")


def test_api_requires_bearer_token_and_supports_deletion(dataset):
    token = "x" * 32
    client = create_training_api(dataset, token).test_client()
    unauthorized = client.get("/v1/samples")
    assert unauthorized.status_code == 401

    created = client.post(
        "/v1/samples",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "image": (io.BytesIO(jpeg_bytes()), "label.jpg"),
            "metadata": json.dumps({"consent": True, "outcome": "missed_barcode"}),
        },
    )
    assert created.status_code == 201
    sample_id = created.get_json()["sample_id"]
    deleted = client.delete(f"/v1/samples/{sample_id}", headers={"Authorization": f"Bearer {token}"})
    assert deleted.status_code == 204
    assert dataset.list_samples() == []


def test_api_supports_review_and_no_store_image_access(dataset):
    token = "x" * 32
    client = create_training_api(dataset, token).test_client()
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/v1/samples",
        headers=headers,
        data={
            "image": (io.BytesIO(jpeg_bytes()), "label.jpg"),
            "metadata": json.dumps({"consent": True, "outcome": "correct"}),
        },
    )
    sample_id = created.get_json()["sample_id"]

    reviewed = client.patch(
        f"/v1/samples/{sample_id}",
        headers=headers,
        json={
            "status": "approved",
            "annotations": reviewed_annotations(),
            "reviewer": "test-reviewer",
            "dataset_split": "validation",
        },
    )
    image = client.get(f"/v1/samples/{sample_id}/image", headers=headers)

    assert reviewed.status_code == 200
    assert reviewed.get_json()["dataset_split"] == "validation"
    assert image.status_code == 200
    assert image.content_type == "image/jpeg"
    assert image.headers["Cache-Control"] == "no-store"


def test_invalid_image_is_not_persisted(dataset):
    with pytest.raises(ValueError, match="supported image"):
        dataset.add_sample(b"not an image", {"consent": True, "outcome": "other"})
    assert dataset.list_samples() == []


def test_offline_phone_bundle_imports_into_encrypted_store(dataset):
    sample = dataset.import_bundle(sample_bundle())

    assert sample["outcome"] == "damaged_label"
    assert dataset.image_bytes(sample["sample_id"])


def test_bundle_rejects_extra_files_and_unsupported_schema(dataset):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("image.jpg", jpeg_bytes())
        archive.writestr(
            "metadata.json",
            json.dumps({"schema_version": 2, "consent": True, "outcome": "correct"}),
        )
        archive.writestr("unexpected.txt", "no")
    with pytest.raises(ValueError, match="structure"):
        dataset.import_bundle(output.getvalue())


def test_dataset_version_requires_all_splits_and_prevents_group_leakage(dataset):
    samples = [
        dataset.add_sample(jpeg_bytes(color), {"consent": True, "outcome": "correct"})
        for color in ("white", "red", "blue")
    ]
    for sample, split in zip(samples, ("train", "validation", "test"), strict=True):
        dataset.review_sample(
            sample["sample_id"],
            status="approved",
            annotations=reviewed_annotations(f"group-{split}"),
            reviewer="reviewer",
            dataset_split=split,
        )

    version = dataset.create_version("first labeled set")

    assert version["split_counts"] == {"train": 1, "validation": 1, "test": 1}
    assert dataset.get_version(version["version_id"])["content_sha256"] == version["content_sha256"]
    repeated = dataset.create_version("different note, identical data")
    assert repeated["dataset_sha256"] == version["dataset_sha256"]
    assert repeated["content_sha256"] != version["content_sha256"]


def test_dataset_version_rejects_capture_group_crossing_splits(dataset):
    for split in ("train", "validation", "test"):
        sample = dataset.add_sample(jpeg_bytes(), {"consent": True, "outcome": "correct"})
        dataset.review_sample(
            sample["sample_id"],
            status="approved",
            annotations=reviewed_annotations("same-audit" if split != "test" else "test-audit"),
            reviewer="reviewer",
            dataset_split=split,
        )

    with pytest.raises(ValueError, match="crosses dataset splits"):
        dataset.create_version()


def test_dataset_version_detects_manifest_tampering(dataset):
    samples = [
        dataset.add_sample(jpeg_bytes(color), {"consent": True, "outcome": "correct"})
        for color in ("white", "red", "blue")
    ]
    for sample, split in zip(samples, ("train", "validation", "test"), strict=True):
        dataset.review_sample(
            sample["sample_id"],
            status="approved",
            annotations=reviewed_annotations(f"group-{split}"),
            reviewer="reviewer",
            dataset_split=split,
        )
    version = dataset.create_version()
    path = dataset.versions / f"{version['version_id']}.json"
    altered = json.loads(path.read_text())
    altered["name"] = "tampered"
    path.write_text(json.dumps(altered))

    with pytest.raises(ValueError, match="integrity"):
        dataset.get_version(version["version_id"])


def test_missing_dataset_key_does_not_silently_create_a_replacement(tmp_path):
    dataset = TrainingDataset(tmp_path / "training")
    dataset.add_sample(jpeg_bytes(), {"consent": True, "outcome": "correct"})
    dataset.close()
    dataset.key_path.unlink()
    with pytest.raises(RuntimeError, match="key is missing"):
        TrainingDataset(dataset.root)
    assert not dataset.key_path.exists()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_annotation_coordinates_are_refused(value):
    annotations = reviewed_annotations()
    annotations["regions"][0]["box"][0] = value
    with pytest.raises(ValueError, match="box"):
        validate_annotations(annotations)


def test_identical_images_cannot_cross_splits_with_different_group_ids(dataset):
    for split in ("train", "validation", "test"):
        sample = dataset.add_sample(jpeg_bytes(), {"consent": True, "outcome": "correct"})
        dataset.review_sample(
            sample["sample_id"],
            status="approved",
            annotations=reviewed_annotations(split),
            reviewer="reviewer",
            dataset_split=split,
        )
    with pytest.raises(ValueError, match="identical label image"):
        dataset.create_version()


def test_reviewed_carrier_updates_the_sample_summary(dataset):
    sample = dataset.add_sample(jpeg_bytes(), {"consent": True, "outcome": "wrong_carrier", "carrier": "PKG"})
    dataset.review_sample(
        sample["sample_id"],
        status="approved",
        annotations=reviewed_annotations(),
        reviewer="reviewer",
        dataset_split="train",
    )
    assert dataset.list_samples()[0]["carrier"] == "UPS"


@pytest.mark.parametrize("path", ["/v1/samples/missing", "/v1/dataset-versions"])
def test_nonobject_api_payload_returns_bad_request(dataset, path):
    client = create_training_api(dataset, "x" * 32).test_client()
    response = client.open(
        path,
        method="PATCH" if "samples" in path else "POST",
        headers={"Authorization": "Bearer " + "x" * 32},
        json=["invalid"],
    )
    assert response.status_code == 400
    assert response.headers["Cache-Control"] == "no-store"
