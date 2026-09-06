"""Transport-independent Bluetooth authentication, replay, and audit-save guarantees."""

import json
import secrets

import pytest
from cryptography.exceptions import InvalidTag

from app.bluetooth_protocol import (
    PROTOCOL,
    ScannerBluetoothSession,
    SecureChannel,
    compact,
    decode64,
    encode64,
)
from app.models import AuditEntry
from app.scanner_server import ScannerCoordinator


def handshake(session, secret=None):
    session.disconnect()
    client_nonce = secrets.token_bytes(32)
    welcome = json.loads(session.receive(compact({"hello": encode64(client_nonce)})))
    return SecureChannel(secret or session.secret, client_nonce, decode64(welcome["welcome"]), server=False)


def send(session, client, request):
    return client.open(json.loads(session.receive(compact(client.seal(request)))))


@pytest.fixture
def session():
    coordinator = ScannerCoordinator()
    coordinator.configure(
        "audit", [AuditEntry("one", 0, "1701S", "Private Name", "UPS - #1 - 1Z999AA10123456784", "", "")]
    )
    return ScannerBluetoothSession(coordinator, lambda: None, lambda _result: True)


def test_qr_key_authenticates_before_any_audit_access(session):
    attacker = handshake(session, b"x" * 32)
    with pytest.raises(InvalidTag):
        send(session, attacker, {"id": "1", "op": "status"})
    assert not session.authenticated
    assert session.coordinator.drain_actions() == []


def test_ciphertext_tampering_and_replay_are_rejected(session):
    client = handshake(session)
    packet = compact(client.seal({"id": "1", "op": "status"}))
    response = session.receive(packet)
    assert b"1701S" not in response and b"Private Name" not in response
    with pytest.raises(ValueError, match="sequence"):
        session.receive(packet)
    payload = client.seal({"id": "2", "op": "status"})
    ciphertext = bytearray(decode64(payload["data"]))
    ciphertext[-1] ^= 1
    payload["data"] = encode64(bytes(ciphertext))
    with pytest.raises(InvalidTag):
        session.receive(compact(payload))


def test_confirmation_retry_after_disconnect_does_not_repeat_mutation(session):
    client = handshake(session)
    scan = send(
        session,
        client,
        {"id": "scan", "op": "scan", "barcodes": ["1Z999AA10123456784"], "formats": ["CODE_128"]},
    )["result"]
    assert scan["status"] == "confirm" and scan["unit"] == "1701S"
    assert b"Private Name" not in compact(scan)
    assert all(action.kind != "match" for action in session.coordinator.drain_actions())
    request = {"id": "confirm", "op": "confirm", "scan_id": scan["scan_id"], "item_id": "one"}
    response = send(session, client, request)
    assert response["result"]["saved"] is True
    assert [action.kind for action in session.coordinator.drain_actions()] == ["match"]
    reconnected = handshake(session)
    replay = send(session, reconnected, request)
    assert replay == response
    assert session.coordinator.drain_actions() == []


def test_failed_persistence_is_never_acknowledged_as_saved(session):
    session.verify_saved = lambda _result: False
    client = handshake(session)
    reply = send(
        session,
        client,
        {"id": "scan", "op": "scan", "barcodes": ["1Z000ZZ00000000001"], "formats": ["CODE_128"]},
    )
    assert reply["ok"] is False and "save" in reply["error"]
    assert "result" not in reply


def test_new_audit_revokes_old_bluetooth_pairing(session):
    client = handshake(session)
    session.coordinator.invalidate_sessions()
    with pytest.raises(ValueError, match="expired"):
        send(session, client, {"id": "1", "op": "status"})


def test_reusing_request_id_for_another_operation_is_rejected(session):
    client = handshake(session)
    send(session, client, {"id": "1", "op": "status"})
    with pytest.raises(ValueError, match="reused"):
        send(session, client, {"id": "1", "op": "undo", "scan_id": "anything"})


@pytest.mark.parametrize(
    "codes,formats", [([], []), ([123], ["CODE_128"]), (["x" * 2049], ["CODE_128"]), (["123456789012"], [])]
)
def test_invalid_barcodes_cannot_mutate_audit(session, codes, formats):
    client = handshake(session)
    reply = send(session, client, {"id": "1", "op": "scan", "barcodes": codes, "formats": formats})
    assert reply["ok"] is False
    assert session.coordinator.drain_actions() == []


def test_cross_language_protocol_vectors():
    secret, client, server = bytes(range(32)), bytes(range(32, 64)), bytes(range(64, 96))
    sender = SecureChannel(secret, client, server, server=False)
    assert sender.seal({"id": "vector-1", "op": "status"}) == {
        "seq": 1,
        "data": "F51ZQSCdIxkY6Xh+0+kfY8Pr6N6eW1CTlSnQLbg1LZbHndGAjRVCWUr6mqr1zVM=",
    }
    assert PROTOCOL == b"package-audit-ble-v1"
