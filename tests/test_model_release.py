"""Model release gates, signing, and Android staging tests."""

import hashlib

import pytest

from app.model_release import (
    MANIFEST_FILENAME,
    MODEL_FILENAME,
    install_android_assets,
    release_gate,
    sign_release,
    verify_release,
)


def passing_metrics():
    return {
        "final_evaluation": True,
        "test_carrier_accuracy": 0.9,
        "test_carrier_macro_recall": 0.85,
        "test_carrier_macro_f1": 0.85,
        "test_condition_accuracy": 0.8,
        "test_condition_macro_recall": 0.7,
        "test_condition_macro_f1": 0.7,
        "test_region_presence_f1": 0.8,
        "test_region_mean_iou": 0.7,
        "onnx_max_abs_error": 0.00001,
    }


def test_release_gate_enforces_data_and_quality_thresholds():
    approved, failures = release_gate(passing_metrics(), {"train": 30, "validation": 10, "test": 10})
    assert approved
    assert failures == []

    approved, failures = release_gate(
        {**passing_metrics(), "test_carrier_accuracy": 0.5},
        {"train": 3, "validation": 1, "test": 1},
    )
    assert not approved
    assert any("train" in failure for failure in failures)
    assert any("carrier" in failure for failure in failures)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, None, "0.99", 1.1])
def test_release_gate_rejects_invalid_metrics(value):
    metrics = {**passing_metrics(), "test_carrier_accuracy": value}
    assert not release_gate(metrics, {"train": 30, "validation": 10, "test": 10})[0]


def test_release_gate_rejects_nan_parity_error():
    metrics = {**passing_metrics(), "onnx_max_abs_error": float("nan")}
    assert not release_gate(metrics, {"train": 30, "validation": 10, "test": 10})[0]


def test_signed_release_verifies_and_stages(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    model = b"valid fake onnx for signing test"
    (release / MODEL_FILENAME).write_bytes(model)
    manifest = {
        "release_id": "label-model-test",
        "model_sha256": hashlib.sha256(model).hexdigest(),
        "approved": True,
    }
    signing_key = tmp_path / "signing.pem"
    sign_release(release, manifest, signing_key)

    trusted_public = signing_key.with_suffix(".public.pem")
    assert verify_release(release, trusted_public)["release_id"] == "label-model-test"
    assets = tmp_path / "assets"
    install_android_assets(release, assets, trusted_public_key=trusted_public)
    assert (assets / MODEL_FILENAME).read_bytes() == model
    assert (assets / MANIFEST_FILENAME).is_file()


def test_tampered_or_unapproved_release_is_refused(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    model = b"model"
    (release / MODEL_FILENAME).write_bytes(model)
    sign_release(
        release,
        {
            "release_id": "candidate",
            "model_sha256": hashlib.sha256(model).hexdigest(),
            "approved": False,
        },
        tmp_path / "key.pem",
    )
    with pytest.raises(ValueError, match="did not pass"):
        install_android_assets(release, tmp_path / "assets")

    (release / MODEL_FILENAME).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="does not match"):
        verify_release(release)


def test_release_signed_by_an_untrusted_key_is_refused(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    model = b"model"
    (release / MODEL_FILENAME).write_bytes(model)
    sign_release(
        release,
        {
            "release_id": "candidate",
            "model_sha256": hashlib.sha256(model).hexdigest(),
            "approved": True,
        },
        tmp_path / "attacker.pem",
    )
    trusted_release = tmp_path / "trusted-release"
    trusted_release.mkdir()
    (trusted_release / MODEL_FILENAME).write_bytes(model)
    sign_release(
        trusted_release,
        {
            "release_id": "trusted",
            "model_sha256": hashlib.sha256(model).hexdigest(),
            "approved": True,
        },
        tmp_path / "trusted.pem",
    )

    with pytest.raises(ValueError, match="trusted"):
        verify_release(release, (tmp_path / "trusted.pem").with_suffix(".public.pem"))
