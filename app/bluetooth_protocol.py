"""Offline scanner protocol. QR-provisioned PSK, fresh session keys, ordered AES-GCM records.

No secrets, packet contents, or package values are written to diagnostic logs.
See BLUETOOTH_PROTOCOL.md for the wire format shared with the Android app.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.models import normalize_carrier
from app.scanner_server import (
    MAX_LIVE_BARCODE_LENGTH,
    MAX_LIVE_BARCODE_PAYLOAD,
    ScannerCoordinator,
)
from app.scanner_vision import observation_from_barcodes

PROTOCOL = b"package-audit-ble-v1"
MAX_PACKET = 16384
MAX_TRAINING_IMAGE_BYTES = 512 * 1024
MAX_TRAINING_CHUNK_BYTES = 6144


def encode64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def decode64(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def compact(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


class SecureChannel:
    """Each direction uses its own key; sequences reject replays and reorderings."""

    def __init__(self, secret: bytes, client_nonce: bytes, server_nonce: bytes, *, server: bool):
        if len(secret) != 32 or len(client_nonce) != 32 or len(server_nonce) != 32:
            raise ValueError("Invalid pairing material")
        material = HKDF(
            algorithm=hashes.SHA256(), length=64, salt=client_nonce + server_nonce, info=PROTOCOL
        ).derive(secret)
        c2s, s2c = material[:32], material[32:]
        self._send = AESGCM(s2c if server else c2s)
        self._receive = AESGCM(c2s if server else s2c)
        self._sent = 0
        self._received = 0

    def seal(self, payload: dict) -> dict:
        self._sent += 1
        nonce = b"\0" * 4 + struct.pack(">Q", self._sent)
        return {"seq": self._sent, "data": encode64(self._send.encrypt(nonce, compact(payload), PROTOCOL))}

    def open(self, envelope: dict) -> dict:
        seq = envelope.get("seq")
        if type(seq) is not int or seq != self._received + 1:
            raise ValueError("Invalid Bluetooth sequence")
        nonce = b"\0" * 4 + struct.pack(">Q", seq)
        plaintext = self._receive.decrypt(nonce, decode64(envelope["data"]), PROTOCOL)
        payload = json.loads(plaintext)
        if not isinstance(payload, dict):
            raise ValueError("Invalid Bluetooth message")
        self._received = seq
        return payload


class ScannerBluetoothSession:
    """Runs on the Qt thread; acknowledge mutations only after persistence succeeds."""

    def __init__(
        self,
        coordinator: ScannerCoordinator,
        flush: Callable[[], None],
        verify_saved: Callable[[dict], bool],
        save_training_sample: Callable[[bytes, dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.coordinator = coordinator
        self.flush = flush
        self.verify_saved = verify_saved
        self.save_training_sample = save_training_sample
        self.secret = secrets.token_bytes(32)
        self.generation = coordinator.generation
        self.channel: SecureChannel | None = None
        self.authenticated = False
        self.responses: OrderedDict[str, tuple[bytes, dict]] = OrderedDict()
        self.training_upload: dict[str, Any] | None = None

    def disconnect(self) -> None:
        self.channel = None
        self.authenticated = False

    def receive(self, packet: bytes) -> bytes:
        if len(packet) > MAX_PACKET or self.generation != self.coordinator.generation:
            raise ValueError("Bluetooth session expired. Scan the new pairing QR.")
        envelope = json.loads(packet)
        if not isinstance(envelope, dict):
            raise ValueError("Invalid Bluetooth message")
        if "hello" in envelope:
            # CoreBluetooth may retain the subscription across an Android GATT
            # reconnect, so a disconnect delegate callback is not a prerequisite.
            # A fresh server nonce gives this handshake independent keys; only a
            # valid encrypted request can restore authentication.
            client_nonce = decode64(envelope["hello"])
            server_nonce = secrets.token_bytes(32)
            self.channel = SecureChannel(self.secret, client_nonce, server_nonce, server=True)
            self.authenticated = False
            proof = hmac.digest(self.secret, PROTOCOL + client_nonce + server_nonce, "sha256")
            return compact({"welcome": encode64(server_nonce), "proof": encode64(proof)})
        if self.channel is None:
            raise ValueError("Pair the phone first")
        request = self.channel.open(envelope)
        self.authenticated = True
        request_id = request.get("id", "")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
            raise ValueError("Invalid request identifier")
        fingerprint = hashlib.sha256(compact(request)).digest()
        previous = self.responses.get(request_id)
        if previous:
            if not hmac.compare_digest(previous[0], fingerprint):
                raise ValueError("Request identifier was reused")
            response = previous[1]
        else:
            try:
                result = self._dispatch(request)
                response = {"id": request_id, "ok": True, "result": result}
            except (ValueError, KeyError, RuntimeError) as exc:
                response = {"id": request_id, "ok": False, "error": str(exc)}
            self.responses[request_id] = (fingerprint, response)
            if len(self.responses) > 2048:
                self.responses.popitem(last=False)
        return compact(self.channel.seal(response))

    def _dispatch(self, request: dict[str, Any]) -> dict:
        op = request.get("op")
        if op == "status":
            return self.coordinator.status()
        if op == "training_begin":
            result = self._training_begin(request)
        elif op == "training_chunk":
            result = self._training_chunk(request)
        elif op == "training_finish":
            result = self._training_finish(request)
        elif op in {"scan", "prepare_alert", "manual_alert"} and "barcodes" in request:
            codes, formats = request.get("barcodes"), request.get("formats")
            units = request.get("units", [])
            unit_confidences = request.get("unit_confidences", [])
            if (
                not isinstance(codes, list)
                or not 1 <= len(codes) <= 8
                or any(
                    not isinstance(code, str) or not 1 <= len(code) <= MAX_LIVE_BARCODE_LENGTH
                    for code in codes
                )
                or sum(len(code.encode("utf-8")) for code in codes if isinstance(code, str))
                > MAX_LIVE_BARCODE_PAYLOAD
                or not isinstance(formats, list)
                or len(formats) != len(codes)
                or any(not isinstance(fmt, str) or not 1 <= len(fmt) <= 64 for fmt in formats)
                or not isinstance(units, list)
                or len(units) > 12
                or any(not isinstance(unit, str) or not 1 <= len(unit) <= 16 for unit in units)
                or not isinstance(unit_confidences, list)
                or len(unit_confidences) != len(units)
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not 0.0 <= float(value) <= 1.0
                    for value in unit_confidences
                )
            ):
                raise ValueError("Invalid tracking barcode. Scan again.")
            observation = observation_from_barcodes(
                tuple(codes),
                tuple(formats),
                tuple(units),
                tuple(float(value) for value in unit_confidences),
                unit_scan_attempted="units" in request,
            )
            model_carrier = request.get("model_carrier", "")
            model_confidence = request.get("model_carrier_confidence", 0.0)
            if (
                isinstance(model_carrier, str)
                and 1 <= len(model_carrier) <= 32
                and not isinstance(model_confidence, bool)
                and isinstance(model_confidence, (int, float))
                and 0.85 <= float(model_confidence) <= 1.0
                and observation.carrier == "PKG"
            ):
                observation = replace(observation, carrier=normalize_carrier(model_carrier))
            if op == "prepare_alert":
                result = self.coordinator.prepare_manual_alert(
                    observation,
                    expected_generation=self.generation,
                )
            elif op == "manual_alert":
                location = request.get("location")
                if not isinstance(location, str) or not 1 <= len(location) <= 32:
                    raise ValueError("Choose a package location from the phone list.")
                result = self.coordinator.create_manual_alert(
                    observation,
                    location,
                    expected_generation=self.generation,
                )
            else:
                result = self.coordinator.process_observation(
                    observation,
                    expected_generation=self.generation,
                )
        else:
            scan_id = request.get("scan_id")
            if not isinstance(scan_id, str) or len(scan_id) > 64:
                raise ValueError("Scan the package again.")
            if op == "confirm":
                item_id = request.get("item_id")
                if not isinstance(item_id, str) or len(item_id) > 256:
                    raise ValueError("Invalid audit item")
                result = self.coordinator.confirm(scan_id, item_id)
            elif op == "reject":
                result = self.coordinator.reject(scan_id)
            elif op == "not_found":
                result = self.coordinator.mark_not_found(scan_id)
            elif op == "flag_review":
                result = self.coordinator.flag_review(scan_id)
            elif op == "save_manual_alert":
                tracking = request.get("tracking")
                location = request.get("location")
                if not isinstance(tracking, str) or not 8 <= len(tracking) <= 40:
                    raise ValueError("Choose a tracking barcode read from this label.")
                if not isinstance(location, str) or not 1 <= len(location) <= 32:
                    raise ValueError("Choose a package location from the phone list.")
                result = self.coordinator.save_manual_alert(scan_id, tracking, location)
            elif op == "undo":
                result = self.coordinator.undo(scan_id)
            else:
                raise ValueError("Unsupported scanner action")
        # This callback applies queued actions and commits SQLite on the Qt thread.
        self.flush()
        if not self.verify_saved(result):
            raise RuntimeError("Mac could not verify the save. Check the desktop before continuing.")
        result = dict(result, saved=True, progress=self.coordinator.status())
        if result["status"] == "matched":
            result["message"] = f"Saved on Mac — unit {result['unit']}"
        return result

    @staticmethod
    def _valid_upload_id(value: Any) -> bool:
        return (
            isinstance(value, str)
            and 1 <= len(value) <= 64
            and all(character.isalnum() or character in "-_" for character in value)
        )

    def _training_begin(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.save_training_sample is None:
            raise RuntimeError("Training collection is unavailable on this Mac.")
        upload_id = request.get("upload_id")
        size = request.get("size")
        digest = request.get("sha256")
        metadata = request.get("metadata")
        if (
            not self._valid_upload_id(upload_id)
            or type(size) is not int
            or not 1 <= size <= MAX_TRAINING_IMAGE_BYTES
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(metadata, dict)
            or len(compact(metadata)) > 16 * 1024
        ):
            raise ValueError("Invalid training-example upload.")
        existing = self.training_upload
        if existing is not None and existing["upload_id"] == upload_id:
            if existing["size"] != size or existing["sha256"] != digest:
                raise ValueError("Training upload identifier was reused.")
            return {"status": "training_ready", "upload_id": upload_id, "offset": len(existing["data"])}
        self.training_upload = {
            "upload_id": upload_id,
            "size": size,
            "sha256": digest,
            "metadata": metadata,
            "data": bytearray(),
        }
        return {"status": "training_ready", "upload_id": upload_id, "offset": 0}

    def _training_chunk(self, request: dict[str, Any]) -> dict[str, Any]:
        upload = self.training_upload
        upload_id = request.get("upload_id")
        offset = request.get("offset")
        encoded = request.get("data")
        if upload is None or upload_id != upload["upload_id"]:
            raise ValueError("Training upload expired; confirm and collect the example again.")
        if type(offset) is not int or offset != len(upload["data"]) or not isinstance(encoded, str):
            raise ValueError("Training upload chunk is out of order.")
        try:
            chunk = decode64(encoded)
        except (ValueError, TypeError) as exc:
            raise ValueError("Training upload chunk is invalid.") from exc
        if not chunk or len(chunk) > MAX_TRAINING_CHUNK_BYTES:
            raise ValueError("Training upload chunk is invalid.")
        if len(upload["data"]) + len(chunk) > upload["size"]:
            raise ValueError("Training upload exceeds its declared size.")
        upload["data"].extend(chunk)
        return {"status": "training_receiving", "upload_id": upload_id, "offset": len(upload["data"])}

    def _training_finish(self, request: dict[str, Any]) -> dict[str, Any]:
        upload = self.training_upload
        upload_id = request.get("upload_id")
        if upload is None or upload_id != upload["upload_id"]:
            raise ValueError("Training upload expired; confirm and collect the example again.")
        image = bytes(upload["data"])
        if len(image) != upload["size"] or not hmac.compare_digest(
            hashlib.sha256(image).hexdigest(), upload["sha256"]
        ):
            raise ValueError("Training upload failed its integrity check.")
        metadata = dict(upload["metadata"])
        metadata["consent"] = True
        metadata["source"] = "android_bluetooth_confirm"
        try:
            saved = self.save_training_sample(image, metadata) if self.save_training_sample else None
        finally:
            self.training_upload = None
        if not isinstance(saved, dict) or not isinstance(saved.get("sample_id"), str):
            raise RuntimeError("Mac could not save the training example.")
        return {
            "status": "training_saved",
            "upload_id": upload_id,
            "sample_id": saved["sample_id"],
        }
