"""Transport-independent Bluetooth authentication, replay, and audit-save guarantees."""

import hashlib
import json
import secrets

import pytest
from cryptography.exceptions import InvalidTag

from app.bluetooth_protocol import (
    MAX_TRAINING_CHUNK_BYTES,
    MAX_TRAINING_IMAGE_BYTES,
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


def test_reconnect_handshake_does_not_require_a_radio_disconnect_callback(session):
    client = handshake(session)
    request = {"id": "status-before", "op": "status"}
    send(session, client, request)
    assert session.authenticated
    nonce = secrets.token_bytes(32)
    welcome = json.loads(session.receive(compact({"hello": encode64(nonce)})))
    assert not session.authenticated
    fresh = SecureChannel(session.secret, nonce, decode64(welcome["welcome"]), server=False)
    result = send(session, fresh, {"id": "status-after", "op": "status"})
    assert result["ok"] and session.authenticated


def test_fresh_handshake_invalidates_previous_directional_keys(session):
    old = handshake(session)
    old_packet = compact(old.seal({"id": "old-status", "op": "status"}))
    session.receive(compact({"hello": encode64(secrets.token_bytes(32))}))
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        session.receive(old_packet)


@pytest.fixture
def session():
    coordinator = ScannerCoordinator()
    coordinator.configure(
        "audit",
        [AuditEntry("one", 0, "1701S", "Private Name", "UPS - #1 - BIN - 1Z999AA10123456784", "", "")],
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
    assert scan["location"] == "BIN"
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


def test_native_scan_sends_unit_candidates_and_verifies_both(session):
    client = handshake(session)
    scan = send(
        session,
        client,
        {
            "id": "unit-scan",
            "op": "scan",
            "barcodes": ["1Z999AA10123456784"],
            "formats": ["CODE_128"],
            "units": ["1701"],
            "unit_confidences": [0.75],
        },
    )["result"]

    assert scan["status"] == "confirm"
    assert scan["unit_status"] == "verified"
    assert scan["scanned_unit"] == "1701"


def test_native_unreadable_unit_can_be_confirmed_manually(session):
    client = handshake(session)
    scan = send(
        session,
        client,
        {
            "id": "unreadable",
            "op": "scan",
            "barcodes": ["1Z999AA10123456784"],
            "formats": ["CODE_128"],
            "units": [],
            "unit_confidences": [],
        },
    )["result"]
    session.coordinator.drain_actions()

    confirmed = send(
        session,
        client,
        {"id": "manual", "op": "confirm", "scan_id": scan["scan_id"], "item_id": "one"},
    )["result"]

    assert scan["status"] == "unit_not_read"
    assert confirmed["status"] == "matched"
    assert confirmed["unit_status"] == "manually_confirmed"


@pytest.mark.parametrize(
    "units,confidences",
    [(["1701"], []), (["1701"], [1.5]), ([""], [1.0]), (["1" * 17], [1.0])],
)
def test_invalid_unit_payload_is_rejected(session, units, confidences):
    client = handshake(session)
    response = send(
        session,
        client,
        {
            "id": "invalid-unit",
            "op": "scan",
            "barcodes": ["1Z999AA10123456784"],
            "formats": ["CODE_128"],
            "units": units,
            "unit_confidences": confidences,
        },
    )

    assert response["ok"] is False
    assert "scan again" in response["error"].lower()


def test_phone_can_flag_an_unmatched_tracking_for_desktop_review(session):
    client = handshake(session)
    scan = send(
        session,
        client,
        {"id": "scan", "op": "scan", "barcodes": ["1Z000ZZ00000000001"], "formats": ["CODE_128"]},
    )["result"]
    assert scan["status"] == "not_found"
    assert scan["can_mark_not_found"] is True
    assert session.coordinator.drain_actions()[0].kind == "not_found"

    flagged = send(
        session,
        client,
        {"id": "flag", "op": "not_found", "scan_id": scan["scan_id"]},
    )["result"]
    assert flagged["status"] == "flagged"
    assert flagged["saved"] is True
    assert session.coordinator.drain_actions()[0].kind == "flag_error"


def test_verified_phone_model_carrier_is_advisory_for_ambiguous_tracking(session):
    client = handshake(session)
    result = send(
        session,
        client,
        {
            "id": "model-carrier",
            "op": "scan",
            "barcodes": ["123456789012"],
            "formats": ["CODE_128"],
            "model_carrier": "FEDEX",
            "model_carrier_confidence": 0.91,
        },
    )["result"]

    assert result["status"] == "not_found"
    assert result["carrier"] == "FEDEX"


def test_low_confidence_phone_model_carrier_is_ignored(session):
    client = handshake(session)
    result = send(
        session,
        client,
        {
            "id": "weak-model-carrier",
            "op": "scan",
            "barcodes": ["123456789012"],
            "formats": ["CODE_128"],
            "model_carrier": "FEDEX",
            "model_carrier_confidence": 0.5,
        },
    )["result"]

    assert result["status"] == "not_found"
    assert result["carrier"] == "PKG"


def test_confirmed_label_upload_is_chunked_integrity_checked_and_saved(session):
    saved = []
    session.save_training_sample = lambda image, metadata: (
        saved.append((image, metadata)) or {"sample_id": "local-sample"}
    )
    client = handshake(session)
    image = b"local-jpeg-data" * 900
    upload_id = "upload-one"
    metadata = {"outcome": "correct", "carrier": "UPS", "app_version": "0.17.0"}

    ready = send(
        session,
        client,
        {
            "id": "begin",
            "op": "training_begin",
            "upload_id": upload_id,
            "size": len(image),
            "sha256": hashlib.sha256(image).hexdigest(),
            "metadata": metadata,
        },
    )["result"]
    assert ready["offset"] == 0

    offset = 0
    for index in range(0, len(image), MAX_TRAINING_CHUNK_BYTES):
        chunk = image[index : index + MAX_TRAINING_CHUNK_BYTES]
        received = send(
            session,
            client,
            {
                "id": f"chunk-{index}",
                "op": "training_chunk",
                "upload_id": upload_id,
                "offset": offset,
                "data": encode64(chunk),
            },
        )["result"]
        offset += len(chunk)
        assert received["offset"] == offset

    finished = send(
        session,
        client,
        {"id": "finish", "op": "training_finish", "upload_id": upload_id},
    )["result"]
    assert finished["status"] == "training_saved"
    assert finished["sample_id"] == "local-sample"
    assert saved == [(image, {**metadata, "consent": True, "source": "android_bluetooth_confirm"})]


def test_training_upload_rejects_oversize_out_of_order_and_bad_digest(session):
    session.save_training_sample = lambda _image, _metadata: {"sample_id": "never"}
    client = handshake(session)
    too_large = send(
        session,
        client,
        {
            "id": "large",
            "op": "training_begin",
            "upload_id": "large",
            "size": MAX_TRAINING_IMAGE_BYTES + 1,
            "sha256": "0" * 64,
            "metadata": {"outcome": "correct"},
        },
    )
    assert too_large["ok"] is False

    begin = {
        "id": "begin-valid",
        "op": "training_begin",
        "upload_id": "bad-digest",
        "size": 4,
        "sha256": "0" * 64,
        "metadata": {"outcome": "correct"},
    }
    assert send(session, client, begin)["ok"] is True
    out_of_order = send(
        session,
        client,
        {
            "id": "wrong-offset",
            "op": "training_chunk",
            "upload_id": "bad-digest",
            "offset": 1,
            "data": encode64(b"data"),
        },
    )
    assert out_of_order["ok"] is False
    assert (
        send(
            session,
            client,
            {
                "id": "right-offset",
                "op": "training_chunk",
                "upload_id": "bad-digest",
                "offset": 0,
                "data": encode64(b"data"),
            },
        )["ok"]
        is True
    )
    bad_finish = send(
        session,
        client,
        {"id": "bad-finish", "op": "training_finish", "upload_id": "bad-digest"},
    )
    assert bad_finish["ok"] is False


def test_phone_can_flag_a_known_tracking_unit_mismatch(session):
    client = handshake(session)
    scan = send(
        session,
        client,
        {"id": "scan", "op": "scan", "barcodes": ["1Z999AA10123456784"], "formats": ["CODE_128"]},
    )["result"]
    session.coordinator.drain_actions()

    flagged = send(
        session,
        client,
        {"id": "mismatch", "op": "flag_review", "scan_id": scan["scan_id"]},
    )["result"]

    assert flagged["status"] == "flagged"
    assert flagged["unit"] == "1701S"
    assert flagged["saved"] is True
    assert session.coordinator.drain_actions()[0].kind == "flag_review"


def test_phone_can_create_a_manual_alert_from_a_scanned_label_and_location(session):
    client = handshake(session)
    result = send(
        session,
        client,
        {
            "id": "manual-alert",
            "op": "manual_alert",
            "barcodes": ["1Z999AA10123456784"],
            "formats": ["CODE_128"],
            "units": ["1701"],
            "unit_confidences": [0.9],
            "location": "BB",
        },
    )["result"]

    assert result["status"] == "flagged"
    assert result["tracking"] == "1Z999AA10123456784"
    assert result["unit"] == "1701S"
    assert result["scanned_unit"] == "1701"
    assert result["location"] == "BB"
    action = session.coordinator.drain_actions()[0]
    assert (action.kind, action.location) == ("manual_alert", "BB")


def test_manual_alert_rejects_an_unknown_location(session):
    client = handshake(session)
    response = send(
        session,
        client,
        {
            "id": "bad-location",
            "op": "manual_alert",
            "barcodes": ["1Z999AA10123456784"],
            "formats": ["CODE_128"],
            "units": [],
            "unit_confidences": [],
            "location": "LOBBY DESK",
        },
    )

    assert response["ok"] is False
    assert session.coordinator.drain_actions() == []


def test_manual_alert_scan_offers_multiple_tracking_codes_then_saves_selected(session):
    client = handshake(session)
    prepared = send(
        session,
        client,
        {
            "id": "prepare-alert",
            "op": "prepare_alert",
            "barcodes": ["1Z000ZZ00000000001", "9400111899223856928499"],
            "formats": ["CODE_128", "CODE_128"],
            "units": ["3005"],
            "unit_confidences": [0.8],
        },
    )["result"]

    assert prepared["status"] == "alert_ready"
    assert prepared["tracking_options"] == ["1Z000ZZ00000000001", "9400111899223856928499"]
    assert session.coordinator.drain_actions() == []

    saved = send(
        session,
        client,
        {
            "id": "save-alert",
            "op": "save_manual_alert",
            "scan_id": prepared["scan_id"],
            "tracking": "9400111899223856928499",
            "location": "SHELF",
        },
    )["result"]

    assert saved["status"] == "flagged"
    assert saved["tracking"] == "9400111899223856928499"
    assert saved["location"] == "SHELF"
    assert session.coordinator.drain_actions()[0].kind == "manual_alert"


def test_aggregate_barcode_payload_is_bounded_without_expiring_session(session):
    client = handshake(session)
    response = send(
        session,
        client,
        {
            "id": "large-label",
            "op": "scan",
            "barcodes": ["A" * 600 + str(index) for index in range(8)],
            "formats": ["QR_CODE"] * 8,
        },
    )

    assert response["ok"] is False
    follow_up = send(session, client, {"id": "still-connected", "op": "status"})
    assert follow_up["ok"] is True


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
