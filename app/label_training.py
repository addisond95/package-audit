"""Local, authenticated collection and review API for opt-in label examples.

Routine audit scans never call this module. Samples enter only through an
explicit diagnostic workflow and remain encrypted at rest until an operator
exports an approved dataset version.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import secrets
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Flask, Response, jsonify, request

from app.scanner_vision import MAX_IMAGE_BYTES, ScanImageError, _open_image

SCHEMA_VERSION = 1
ALLOWED_OUTCOMES = {
    "correct",
    "missed_barcode",
    "wrong_tracking",
    "wrong_carrier",
    "unit_read_error",
    "address_layout",
    "name_layout",
    "damaged_label",
    "other",
}
ALLOWED_STATUSES = {"pending", "approved", "rejected"}
ALLOWED_SPLITS = {"", "train", "validation", "test"}
REGION_KINDS = ("barcode", "tracking_text", "unit", "address", "last_name")
LABEL_CONDITIONS = (
    "good",
    "blur",
    "glare",
    "skew",
    "wrinkle",
    "occlusion",
    "low_contrast",
    "torn",
    "other",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def validate_annotations(value: Any) -> dict[str, Any]:
    """Validate the PII-minimized annotation schema used by model training."""
    if not isinstance(value, dict):
        raise ValueError("Reviewed annotations must be a JSON object.")
    carrier = _clean_text(value.get("carrier"), 32).upper()
    if not carrier:
        raise ValueError("Annotations require a carrier or label-family class.")
    condition = _clean_text(value.get("condition"), 32).lower()
    if condition not in LABEL_CONDITIONS:
        raise ValueError("Annotations require a supported label condition.")
    group_id = _clean_text(value.get("group_id"), 100)
    if not group_id:
        raise ValueError("Annotations require a capture group ID for leakage-safe splitting.")
    orientation = value.get("orientation", 0)
    if type(orientation) is not int or orientation not in {0, 90, 180, 270}:
        raise ValueError("Orientation must be 0, 90, 180, or 270 degrees.")
    raw_regions = value.get("regions", [])
    if not isinstance(raw_regions, list):
        raise ValueError("Annotation regions must be a list.")
    regions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_regions:
        if not isinstance(raw, dict) or raw.get("kind") not in REGION_KINDS:
            raise ValueError("Every region requires a supported kind.")
        kind = raw["kind"]
        if kind in seen:
            raise ValueError(f"Only one {kind} region is supported per sample.")
        box = raw.get("box")
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(
                isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
                for item in box
            )
        ):
            raise ValueError("Every region requires a normalized [x, y, width, height] box.")
        normalized = [round(float(item), 6) for item in box]
        x, y, width, height = normalized
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
            raise ValueError("Annotation boxes must stay within the image.")
        regions.append({"kind": kind, "box": normalized})
        seen.add(kind)
    if "barcode" not in seen and "tracking_text" not in seen:
        raise ValueError("Mark at least a barcode or tracking-text region.")
    return {
        "carrier": carrier,
        "condition": condition,
        "group_id": group_id,
        "orientation": orientation,
        "regions": regions,
    }


class TrainingDataset:
    """Encrypted image store with reviewable, versioned metadata."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.blobs = self.root / "blobs"
        self.exports = self.root / "exports"
        self.versions = self.root / "versions"
        self.key_path = self.root / "dataset.key"
        self.db_path = self.root / "samples.sqlite3"
        for directory in (self.root, self.blobs, self.exports, self.versions):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        self.key = self._load_key()
        self.conn = sqlite3.connect(self.db_path)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS label_samples (
                    sample_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    image_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    carrier TEXT NOT NULL,
                    dataset_split TEXT NOT NULL DEFAULT '',
                    encrypted_metadata BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def _load_key(self) -> bytes:
        if not self.key_path.exists() and (self.db_path.exists() or any(self.blobs.glob("*.bin"))):
            raise RuntimeError("Training dataset key is missing. Restore dataset.key from backup.")
        if not self.key_path.exists():
            key = AESGCM.generate_key(bit_length=256)
            try:
                descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(key)
        if self.key_path.exists():
            key = self.key_path.read_bytes()
            if len(key) != 32:
                raise RuntimeError("Training dataset key is invalid.")
            return key
        raise RuntimeError("Training dataset key could not be created.")

    def _seal(self, sample_id: str, data: bytes, purpose: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        aad = b"package-audit-label-v1:" + purpose + b":" + sample_id.encode("ascii")
        return nonce + AESGCM(self.key).encrypt(nonce, data, aad)

    def _open(self, sample_id: str, data: bytes, purpose: bytes) -> bytes:
        if len(data) < 29:
            raise ValueError("Encrypted sample is corrupt.")
        aad = b"package-audit-label-v1:" + purpose + b":" + sample_id.encode("ascii")
        return AESGCM(self.key).decrypt(data[:12], data[12:], aad)

    @staticmethod
    def _safe_jpeg(image_bytes: bytes) -> bytes:
        image = _open_image(image_bytes)
        output = io.BytesIO()
        image.save(output, "JPEG", quality=92, optimize=True)
        return output.getvalue()

    def add_sample(self, image_bytes: bytes, metadata: dict[str, Any]) -> dict[str, Any]:
        if metadata.get("consent") is not True:
            raise ValueError("Explicit diagnostic-sample consent is required.")
        outcome = _clean_text(metadata.get("outcome"), 40).lower()
        if outcome not in ALLOWED_OUTCOMES:
            raise ValueError("Choose a supported diagnostic outcome.")
        safe_image = self._safe_jpeg(image_bytes)
        sample_id = secrets.token_hex(16)
        created_at = _now()
        private_metadata = {
            "schema_version": SCHEMA_VERSION,
            "outcome": outcome,
            "carrier": _clean_text(metadata.get("carrier"), 32).upper(),
            "notes": _clean_text(metadata.get("notes"), 2000),
            "source": _clean_text(metadata.get("source"), 64) or "operator_opt_in",
            "annotations": metadata.get("annotations", {}),
            "device": _clean_text(metadata.get("device"), 100),
            "app_version": _clean_text(metadata.get("app_version"), 32),
            "capture_session": _clean_text(metadata.get("capture_session"), 100),
            "created_at": created_at,
        }
        if not isinstance(private_metadata["annotations"], dict):
            raise ValueError("Annotations must be a JSON object.")
        encoded_metadata = json.dumps(private_metadata, sort_keys=True).encode("utf-8")
        if len(encoded_metadata) > 64 * 1024:
            raise ValueError("Sample metadata is too large.")
        blob_path = self.blobs / f"{sample_id}.bin"
        blob_path.write_bytes(self._seal(sample_id, safe_image, b"image"))
        try:
            blob_path.chmod(0o600)
        except OSError:
            pass
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO label_samples (
                        sample_id, schema_version, image_sha256, status, outcome, carrier,
                        encrypted_metadata, created_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        sample_id,
                        SCHEMA_VERSION,
                        hashlib.sha256(safe_image).hexdigest(),
                        outcome,
                        private_metadata["carrier"],
                        self._seal(sample_id, encoded_metadata, b"metadata"),
                        created_at,
                    ),
                )
        except Exception:
            blob_path.unlink(missing_ok=True)
            raise
        return self.get_sample(sample_id, include_private=False)

    def import_bundle(self, bundle_bytes: bytes) -> dict[str, Any]:
        """Validate an offline Android ``.pasample`` bundle and encrypt its contents."""
        if len(bundle_bytes) > MAX_IMAGE_BYTES + 128 * 1024:
            raise ValueError("Training bundle is too large.")
        try:
            with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as archive:
                names = archive.namelist()
                if len(names) != 2 or set(names) != {"image.jpg", "metadata.json"}:
                    raise ValueError("Training bundle has an invalid structure.")
                image_info = archive.getinfo("image.jpg")
                metadata_info = archive.getinfo("metadata.json")
                if image_info.file_size > MAX_IMAGE_BYTES or metadata_info.file_size > 64 * 1024:
                    raise ValueError("Training bundle contents are too large.")
                with archive.open(image_info) as image_file:
                    image_bytes = image_file.read(MAX_IMAGE_BYTES + 1)
                with archive.open(metadata_info) as metadata_file:
                    metadata_bytes = metadata_file.read(64 * 1024 + 1)
                if len(image_bytes) > MAX_IMAGE_BYTES or len(metadata_bytes) > 64 * 1024:
                    raise ValueError("Training bundle contents are too large.")
                metadata = json.loads(metadata_bytes)
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Training bundle is invalid.") from exc
        if not isinstance(metadata, dict) or metadata.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Training bundle schema is unsupported.")
        return self.add_sample(image_bytes, metadata)

    def list_samples(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in ALLOWED_STATUSES:
            raise ValueError("Invalid review status.")
        if status:
            rows = self.conn.execute(
                """
                SELECT sample_id, schema_version, image_sha256, status, outcome, carrier,
                       dataset_split, created_at, reviewed_at
                FROM label_samples WHERE status = ? ORDER BY created_at, sample_id
                """,
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT sample_id, schema_version, image_sha256, status, outcome, carrier,
                       dataset_split, created_at, reviewed_at
                FROM label_samples ORDER BY created_at, sample_id
                """
            ).fetchall()
        return [
            {
                "sample_id": row[0],
                "schema_version": row[1],
                "image_sha256": row[2],
                "status": row[3],
                "outcome": row[4],
                "carrier": row[5],
                "dataset_split": row[6],
                "created_at": row[7],
                "reviewed_at": row[8],
            }
            for row in rows
        ]

    def get_sample(self, sample_id: str, *, include_private: bool = True) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT sample_id, schema_version, image_sha256, status, outcome, carrier,
                   dataset_split, encrypted_metadata, created_at, reviewed_at
            FROM label_samples WHERE sample_id = ?
            """,
            (sample_id,),
        ).fetchone()
        if not row:
            raise KeyError("Training sample not found.")
        result = {
            "sample_id": row[0],
            "schema_version": row[1],
            "image_sha256": row[2],
            "status": row[3],
            "outcome": row[4],
            "carrier": row[5],
            "dataset_split": row[6],
            "created_at": row[8],
            "reviewed_at": row[9],
        }
        if include_private:
            result["metadata"] = json.loads(self._open(sample_id, row[7], b"metadata"))
        return result

    def image_bytes(self, sample_id: str) -> bytes:
        self.get_sample(sample_id, include_private=False)
        return self._open(sample_id, (self.blobs / f"{sample_id}.bin").read_bytes(), b"image")

    def review_sample(
        self,
        sample_id: str,
        *,
        status: str,
        annotations: dict[str, Any],
        reviewer: str,
        dataset_split: str = "",
    ) -> dict[str, Any]:
        if not isinstance(status, str) or status not in {"approved", "rejected"}:
            raise ValueError("Review status must be approved or rejected.")
        if not isinstance(dataset_split, str) or dataset_split not in ALLOWED_SPLITS:
            raise ValueError("Invalid dataset split.")
        if status == "approved" and not annotations:
            raise ValueError("Approved samples require reviewed annotations.")
        clean_reviewer = _clean_text(reviewer, 100)
        if not clean_reviewer:
            raise ValueError("A reviewer name or identifier is required.")
        if status == "approved" and dataset_split not in {"train", "validation", "test"}:
            raise ValueError("Approved samples require a dataset split.")
        if status == "rejected":
            dataset_split = ""
            clean_annotations = annotations if isinstance(annotations, dict) else {}
        else:
            clean_annotations = validate_annotations(annotations)
        current = self.get_sample(sample_id)
        metadata = dict(current["metadata"])
        metadata["annotations"] = clean_annotations
        metadata["reviewer"] = clean_reviewer
        metadata["reviewed_at"] = _now()
        encoded = json.dumps(metadata, sort_keys=True).encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise ValueError("Reviewed annotations are too large.")
        with self.conn:
            self.conn.execute(
                """
                UPDATE label_samples
                SET status = ?, dataset_split = ?, encrypted_metadata = ?, reviewed_at = ?, carrier = ?
                WHERE sample_id = ?
                """,
                (
                    status,
                    dataset_split,
                    self._seal(sample_id, encoded, b"metadata"),
                    metadata["reviewed_at"],
                    clean_annotations.get("carrier", current["carrier"]),
                    sample_id,
                ),
            )
        return self.get_sample(sample_id, include_private=False)

    def create_version(self, name: str = "") -> dict[str, Any]:
        """Freeze all approved samples into a reproducible, PII-minimized manifest."""
        samples = self.list_samples("approved")
        if not samples:
            raise ValueError("Approve annotated samples before creating a dataset version.")
        members: list[dict[str, Any]] = []
        group_splits: dict[str, str] = {}
        image_splits: dict[str, str] = {}
        split_counts = {"train": 0, "validation": 0, "test": 0}
        for sample in samples:
            full = self.get_sample(sample["sample_id"])
            annotations = validate_annotations(full["metadata"].get("annotations"))
            split = sample["dataset_split"]
            if split not in split_counts:
                raise ValueError("Every approved sample requires a train, validation, or test split.")
            group_id = annotations["group_id"]
            previous = group_splits.setdefault(group_id, split)
            if previous != split:
                raise ValueError(f"Capture group {group_id!r} crosses dataset splits.")
            previous_image_split = image_splits.setdefault(sample["image_sha256"], split)
            if previous_image_split != split:
                raise ValueError("An identical label image crosses dataset splits.")
            split_counts[split] += 1
            members.append(
                {
                    "sample_id": sample["sample_id"],
                    "image_sha256": sample["image_sha256"],
                    "split": split,
                    "annotations": annotations,
                }
            )
        missing = [split for split, count in split_counts.items() if count == 0]
        if missing:
            raise ValueError("Dataset versions require train, validation, and test samples.")
        created_at = _now()
        body = {
            "schema_version": 1,
            "created_at": created_at,
            "name": _clean_text(name, 100),
            "split_counts": split_counts,
            "region_kinds": list(REGION_KINDS),
            "samples": sorted(members, key=lambda item: item["sample_id"]),
        }
        dataset_identity = {
            "schema_version": body["schema_version"],
            "region_kinds": body["region_kinds"],
            "samples": body["samples"],
        }
        body["dataset_sha256"] = hashlib.sha256(
            json.dumps(dataset_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        body["content_sha256"] = hashlib.sha256(canonical).hexdigest()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        version_id = f"dataset-{stamp}-{body['content_sha256'][:12]}"
        body["version_id"] = version_id
        destination = self.versions / f"{version_id}.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(destination)
        return body

    def list_versions(self) -> list[dict[str, Any]]:
        versions: list[dict[str, Any]] = []
        for path in sorted(self.versions.glob("dataset-*.json"), reverse=True):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                versions.append(value)
        return versions

    def get_version(self, version_id: str) -> dict[str, Any]:
        safe_characters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        if not version_id.startswith("dataset-") or any(char not in safe_characters for char in version_id):
            raise ValueError("Invalid dataset version ID.")
        path = self.versions / f"{version_id}.json"
        if not path.is_file():
            raise KeyError("Dataset version not found.")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version_id") != version_id:
            raise ValueError("Dataset version manifest is invalid.")
        expected = value.get("content_sha256")
        content = dict(value)
        content.pop("content_sha256", None)
        content.pop("version_id", None)
        actual = hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if not isinstance(expected, str) or not secrets.compare_digest(expected, actual):
            raise ValueError("Dataset version manifest failed its integrity check.")
        return value

    def delete_sample(self, sample_id: str) -> None:
        self.get_sample(sample_id, include_private=False)
        with self.conn:
            self.conn.execute("DELETE FROM label_samples WHERE sample_id = ?", (sample_id,))
        (self.blobs / f"{sample_id}.bin").unlink(missing_ok=True)

    def close(self) -> None:
        self.conn.close()


def create_training_api(dataset: TrainingDataset, token: str) -> Flask:
    """Create the local API. The caller must bind it to loopback or authenticated TLS."""
    if len(token) < 32:
        raise ValueError("Training API token must contain at least 32 characters.")
    app = Flask("package_audit_training")
    app.config.update(MAX_CONTENT_LENGTH=MAX_IMAGE_BYTES + 128 * 1024)

    @app.before_request
    def authenticate():
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if not secrets.compare_digest(supplied, expected):
            return jsonify(error="Unauthorized"), 401
        return None

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="Training sample is too large."), 413

    @app.after_request
    def private_response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.post("/v1/samples")
    def add_sample():
        uploaded = request.files.get("image")
        if uploaded is None:
            return jsonify(error="An image file is required."), 400
        try:
            metadata = json.loads(request.form.get("metadata", "{}"))
            if not isinstance(metadata, dict):
                raise ValueError("Metadata must be a JSON object.")
            result = dataset.add_sample(uploaded.read(MAX_IMAGE_BYTES + 1), metadata)
            return jsonify(result), 201
        except (ValueError, ScanImageError, json.JSONDecodeError) as exc:
            return jsonify(error=str(exc)), 400

    @app.post("/v1/import")
    def import_bundle():
        uploaded = request.files.get("bundle")
        if uploaded is None:
            return jsonify(error="A .pasample bundle is required."), 400
        try:
            return jsonify(dataset.import_bundle(uploaded.read(MAX_IMAGE_BYTES + 128 * 1024 + 1))), 201
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.get("/v1/samples")
    def list_samples():
        try:
            return jsonify(samples=dataset.list_samples(request.args.get("status")))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.get("/v1/samples/<sample_id>")
    def get_sample(sample_id: str):
        try:
            return jsonify(dataset.get_sample(sample_id))
        except KeyError as exc:
            return jsonify(error=str(exc)), 404

    @app.get("/v1/samples/<sample_id>/image")
    def get_sample_image(sample_id: str):
        try:
            response = Response(dataset.image_bytes(sample_id), mimetype="image/jpeg")
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        except KeyError as exc:
            return jsonify(error=str(exc)), 404

    @app.patch("/v1/samples/<sample_id>")
    def review_sample(sample_id: str):
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify(error="Send a JSON object."), 400
        try:
            return jsonify(
                dataset.review_sample(
                    sample_id,
                    status=payload.get("status", ""),
                    annotations=payload.get("annotations", {}),
                    reviewer=payload.get("reviewer", ""),
                    dataset_split=payload.get("dataset_split", ""),
                )
            )
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.delete("/v1/samples/<sample_id>")
    def delete_sample(sample_id: str):
        try:
            dataset.delete_sample(sample_id)
            return "", 204
        except KeyError as exc:
            return jsonify(error=str(exc)), 404

    @app.get("/v1/dataset-versions")
    def list_versions():
        return jsonify(versions=dataset.list_versions())

    @app.post("/v1/dataset-versions")
    def create_version():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify(error="Send a JSON object."), 400
        try:
            return jsonify(dataset.create_version(payload.get("name", ""))), 201
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.get("/v1/dataset-versions/<version_id>")
    def get_version(version_id: str):
        try:
            return jsonify(dataset.get_version(version_id))
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    return app
