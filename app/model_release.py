"""Signing, verification, approval gates, and deployment helpers for label models."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import shutil
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MODEL_FILENAME = "package_audit_label.onnx"
MANIFEST_FILENAME = "package_audit_model_manifest.json"
SIGNATURE_FILENAME = "package_audit_model_manifest.sig"
PUBLIC_KEY_FILENAME = "package_audit_model_public.pem"
MODEL_CARD_FILENAME = "MODEL_CARD.md"


def canonical_manifest(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def release_gate(metrics: dict[str, Any], split_counts: dict[str, int]) -> tuple[bool, list[str]]:
    """Apply conservative minimum data and held-out performance requirements."""
    failures: list[str] = []
    if metrics.get("final_evaluation") is not True:
        failures.append("a one-time final held-out evaluation is required")
    minimums = {"train": 30, "validation": 10, "test": 10}
    for split, minimum in minimums.items():
        count = split_counts.get(split, 0)
        if type(count) is not int or count < minimum:
            failures.append(f"{split} requires at least {minimum} samples")
    thresholds = {
        "test_carrier_accuracy": 0.85,
        "test_carrier_macro_recall": 0.80,
        "test_carrier_macro_f1": 0.80,
        "test_condition_accuracy": 0.70,
        "test_condition_macro_recall": 0.60,
        "test_condition_macro_f1": 0.60,
        "test_region_presence_f1": 0.75,
        "test_region_mean_iou": 0.60,
        "onnx_max_abs_error": 0.001,
    }
    for metric, threshold in thresholds.items():
        value = metrics.get(metric)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            failures.append(f"{metric} must be a finite number")
            continue
        if metric == "onnx_max_abs_error":
            if value < 0 or value > threshold:
                failures.append(f"{metric} must be at most {threshold}")
        elif not threshold <= value <= 1.0:
            failures.append(f"{metric} must be between {threshold} and 1.0")
    return not failures, failures


def _load_or_create_signing_key(key_path: Path) -> Ed25519PrivateKey:
    if key_path.exists():
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("Model signing key is not Ed25519.")
        return key
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = Ed25519PrivateKey.generate()
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return key


def sign_release(release_dir: Path, manifest: dict[str, Any], key_path: Path) -> None:
    key = _load_or_create_signing_key(key_path)
    encoded = canonical_manifest(manifest)
    public_bytes = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    trusted_public_path = key_path.with_suffix(".public.pem")
    if trusted_public_path.exists() and trusted_public_path.read_bytes() != public_bytes:
        raise ValueError("Stored model-signing public key does not match the private key.")
    if not trusted_public_path.exists():
        trusted_public_path.write_bytes(public_bytes)
        trusted_public_path.chmod(0o600)
    (release_dir / MANIFEST_FILENAME).write_bytes(encoded)
    (release_dir / SIGNATURE_FILENAME).write_bytes(key.sign(encoded))
    (release_dir / PUBLIC_KEY_FILENAME).write_bytes(public_bytes)
    for filename in (MANIFEST_FILENAME, SIGNATURE_FILENAME, PUBLIC_KEY_FILENAME):
        (release_dir / filename).chmod(0o600)


def verify_release(release_dir: Path, trusted_public_key: Path | None = None) -> dict[str, Any]:
    manifest_bytes = (release_dir / MANIFEST_FILENAME).read_bytes()
    signature = (release_dir / SIGNATURE_FILENAME).read_bytes()
    release_public_bytes = (release_dir / PUBLIC_KEY_FILENAME).read_bytes()
    if trusted_public_key is not None:
        trusted_bytes = trusted_public_key.read_bytes()
        if not secrets.compare_digest(release_public_bytes, trusted_bytes):
            raise ValueError("Model release was not signed by the trusted local model key.")
    public_key = serialization.load_pem_public_key(release_public_bytes)
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("Model release public key is not Ed25519.")
    public_key.verify(signature, manifest_bytes)
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict):
        raise ValueError("Model manifest is invalid.")
    model_path = release_dir / MODEL_FILENAME
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if digest != manifest.get("model_sha256"):
        raise ValueError("Model file does not match its signed manifest.")
    return manifest


def install_android_assets(
    release_dir: Path,
    assets_dir: Path,
    *,
    allow_unapproved: bool = False,
    trusted_public_key: Path | None = None,
) -> dict[str, Any]:
    """Install a verified release as Android build assets with atomic replacement."""
    manifest = verify_release(release_dir, trusted_public_key)
    if not manifest.get("approved") and not allow_unapproved:
        raise ValueError("Refusing to deploy a model that did not pass its release gate.")
    assets_dir.mkdir(parents=True, exist_ok=True)
    mapping = {
        MODEL_FILENAME: MODEL_FILENAME,
        MANIFEST_FILENAME: MANIFEST_FILENAME,
        SIGNATURE_FILENAME: SIGNATURE_FILENAME,
        PUBLIC_KEY_FILENAME: PUBLIC_KEY_FILENAME,
    }
    staged: list[tuple[Path, Path]] = []
    try:
        for source_name, target_name in mapping.items():
            target = assets_dir / target_name
            temporary = target.with_suffix(target.suffix + ".tmp")
            shutil.copyfile(release_dir / source_name, temporary)
            staged.append((temporary, target))
        for temporary, target in staged:
            os.replace(temporary, target)
    finally:
        for temporary, _target in staged:
            temporary.unlink(missing_ok=True)
    return manifest
