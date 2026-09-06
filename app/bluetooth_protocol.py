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
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.scanner_server import ScannerCoordinator
from app.scanner_vision import observation_from_barcodes

PROTOCOL = b"package-audit-ble-v1"
MAX_PACKET = 16384


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
    ):
        self.coordinator = coordinator
        self.flush = flush
        self.verify_saved = verify_saved
        self.secret = secrets.token_bytes(32)
        self.generation = coordinator.generation
        self.channel: SecureChannel | None = None
        self.authenticated = False
        self.responses: OrderedDict[str, tuple[bytes, dict]] = OrderedDict()

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
            if self.channel is not None:
                raise ValueError("Reconnect before starting a new handshake")
            client_nonce = decode64(envelope["hello"])
            server_nonce = secrets.token_bytes(32)
            self.channel = SecureChannel(self.secret, client_nonce, server_nonce, server=True)
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
        if op == "scan":
            codes, formats = request.get("barcodes"), request.get("formats")
            if (
                not isinstance(codes, list)
                or not 1 <= len(codes) <= 8
                or any(not isinstance(code, str) or not 1 <= len(code) <= 2048 for code in codes)
                or not isinstance(formats, list)
                or len(formats) != len(codes)
                or any(not isinstance(fmt, str) or not 1 <= len(fmt) <= 64 for fmt in formats)
            ):
                raise ValueError("Invalid tracking barcode. Scan again.")
            result = self.coordinator.process_observation(
                observation_from_barcodes(tuple(codes), tuple(formats)),
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
