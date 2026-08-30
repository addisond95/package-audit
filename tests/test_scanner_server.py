"""Tests for the local phone scanner coordinator and HTTP security."""

from __future__ import annotations

import io
import re
import time
import urllib.request
from dataclasses import replace

import pytest

import app.scanner_server as scanner_server
from app.models import AuditEntry
from app.scanner_matching import ScanObservation
from app.scanner_server import ScannerCoordinator, ScannerServer, create_scanner_app


def _entry(
    item_id: str,
    unit: str,
    resident: str,
    tracking: str,
    *,
    audited: bool = False,
) -> AuditEntry:
    return AuditEntry(
        item_id=item_id,
        page_index=0,
        unit=unit,
        resident=resident,
        package=f"UPS - #{item_id} - {tracking}",
        tower="South Tower",
        timestamp="07/31/2026 08:00:00 PM",
        audited=audited,
    )


@pytest.fixture()
def entries():
    return [
        _entry("one", "1701S", "Mathiesen", "1Z999AA10123456784"),
        _entry("two", "1802S", "Nguyen", "1Z999AA10123450000"),
    ]


def test_coordinator_auto_match_and_repeated_scan_are_idempotent(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    observation = ScanObservation(barcodes=("1Z999AA10123456784",))

    first = coordinator.process_observation(observation)
    actions = coordinator.drain_actions()
    repeated = coordinator.process_observation(observation)

    assert first["status"] == "matched"
    assert len(actions) == 1 and actions[0].kind == "match"
    assert actions[0].model is None
    assert coordinator.model.examples == 0
    assert repeated["scan_id"] == first["scan_id"]
    assert repeated["repeated"] is True
    assert coordinator.drain_actions() == []


def test_coordinator_review_confirmation_updates_model(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    result = coordinator.process_observation(
        ScanObservation(ocr_text="MATHIESEN UNIT 1701S", ocr_confidence=90)
    )
    assert result["status"] == "review"
    assert coordinator.drain_actions()[0].kind == "review"
    before = coordinator.model.examples

    confirmed = coordinator.confirm(result["scan_id"], "one")
    action = coordinator.drain_actions()[0]

    assert confirmed["status"] == "matched"
    assert action.kind == "match" and action.item_id == "one"
    assert action.model is not None
    assert coordinator.model.examples > before


def test_confirmation_replay_does_not_retrain_or_queue_another_action(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    result = coordinator.process_observation(
        ScanObservation(ocr_text="MATHIESEN UNIT 1701S", ocr_confidence=90)
    )
    coordinator.drain_actions()
    coordinator.confirm(result["scan_id"], "one")
    coordinator.drain_actions()
    examples = coordinator.model.examples

    repeated = coordinator.confirm(result["scan_id"], "one")

    assert repeated["repeated"] is True
    assert coordinator.model.examples == examples
    assert coordinator.drain_actions() == []


def test_multiple_known_barcodes_preserve_selected_tracking_on_confirmation(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    result = coordinator.process_observation(
        ScanObservation(barcodes=("1Z999AA10123456784", "1Z999AA10123450000"))
    )
    coordinator.drain_actions()

    confirmed = coordinator.confirm(result["scan_id"], "two")
    action = coordinator.drain_actions()[0]

    assert confirmed["tracking"] == "1Z999AA10123450000"
    assert action.decision.tracking == "1Z999AA10123450000"


def test_coordinator_reject_not_found_and_undo(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    result = coordinator.process_observation(
        ScanObservation(ocr_text="MATHIESEN UNIT 1701S", ocr_confidence=90)
    )
    scan_id = result["scan_id"]
    assert coordinator.drain_actions()[0].kind == "review"

    rejected = coordinator.reject(scan_id)
    not_found = coordinator.mark_not_found(scan_id)
    undone = coordinator.undo(scan_id)
    assert rejected["status"] == "rejected" and rejected["can_mark_not_found"] is True
    assert not_found["status"] == "not_found" and not_found["unit"] == "1701S"
    assert undone["status"] == "undo_queued" and undone["can_mark_not_found"] is False
    assert [action.kind for action in coordinator.drain_actions()] == ["reject", "not_found", "undo"]


def test_undone_scan_rejects_late_follow_up_actions(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    result = coordinator.process_observation(
        ScanObservation(ocr_text="MATHIESEN UNIT 1701S", ocr_confidence=90)
    )
    coordinator.drain_actions()

    coordinator.undo(result["scan_id"])

    with pytest.raises(ValueError, match="undone"):
        coordinator.confirm(result["scan_id"], "one")
    with pytest.raises(ValueError, match="undone"):
        coordinator.reject(result["scan_id"])


def test_already_audited_scan_does_not_offer_or_accept_undo(entries):
    entries[0].audited = True
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)

    result = coordinator.process_observation(ScanObservation(barcodes=("1Z999AA10123456784",)))

    assert result["status"] == "already_matched"
    assert result["can_undo"] is False
    with pytest.raises(ValueError, match="nothing to undo"):
        coordinator.undo(result["scan_id"])


def test_not_found_requires_observed_identifier_and_never_inherits_suggested_unit(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    poor = coordinator.process_observation(ScanObservation(ocr_text="blurred shipping label"))
    assert poor["status"] == "poor_scan"
    assert poor["can_mark_not_found"] is False
    with pytest.raises(ValueError, match="Retake"):
        coordinator.mark_not_found(poor["scan_id"])

    review = coordinator.process_observation(
        ScanObservation(ocr_text="MATHIESEN UNIT 1701S", ocr_confidence=90)
    )
    stored = coordinator._scans[review["scan_id"]]
    stored.decision = replace(stored.decision, unit="9999S")
    not_found = coordinator.mark_not_found(review["scan_id"])

    assert not_found["unit"] == "1701S"


def test_known_match_cannot_be_changed_to_not_found_without_rejection(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    result = coordinator.process_observation(ScanObservation(barcodes=("1Z999AA10123456784",)))

    assert result["status"] == "matched"
    assert result["can_mark_not_found"] is False
    with pytest.raises(ValueError, match="Retake"):
        coordinator.mark_not_found(result["scan_id"])


def test_rejected_identical_photo_is_reevaluated(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    observation = ScanObservation(ocr_text="MATHIESEN UNIT 1701S", ocr_confidence=90)
    first = coordinator.process_observation(observation)
    coordinator.drain_actions()
    coordinator.reject(first["scan_id"])
    coordinator.drain_actions()

    second = coordinator.process_observation(observation)

    assert second["scan_id"] != first["scan_id"]
    assert second["repeated"] is False
    assert second["status"] == "review"


def test_switching_pdf_discards_queued_actions(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("old-hash", entries)
    coordinator.process_observation(ScanObservation(barcodes=("1Z999AA10123456784",)))

    coordinator.configure("new-hash", entries)

    assert coordinator.drain_actions() == []


def test_explicit_scan_reset_allows_same_image_again(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    observation = ScanObservation(barcodes=("1Z999AA10123456784",))
    first = coordinator.process_observation(observation)
    coordinator.drain_actions()

    coordinator.configure("hash", entries, reset_scans=True)
    second = coordinator.process_observation(observation)

    assert second["scan_id"] != first["scan_id"]


def test_explicit_scan_reset_discards_same_audit_queued_actions(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    coordinator.process_observation(ScanObservation(barcodes=("1Z999AA10123456784",)))

    coordinator.configure("hash", entries, reset_scans=True)

    assert coordinator.drain_actions() == []


def test_coordinator_rejects_invalid_or_expired_candidate(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    result = coordinator.process_observation(ScanObservation(ocr_text="MATHIESEN UNIT 1701S"))

    with pytest.raises(ValueError):
        coordinator.confirm(result["scan_id"], "not-a-candidate")
    with pytest.raises(KeyError):
        coordinator.undo("expired")


def _paired_client(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    app = create_scanner_app(coordinator, "123456", "test-secret")
    app.config["TESTING"] = True
    client = app.test_client()
    response = client.post("/pair", data={"code": "123456"})
    assert response.status_code == 302
    scanner = client.get("/scanner")
    csrf = re.search(rb'name="csrf-token" content="([^"]+)"', scanner.data).group(1).decode()
    assert coordinator.active_phone_count() == 1
    return coordinator, client, csrf


def test_http_requires_pairing_and_rejects_public_addresses(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    app = create_scanner_app(coordinator, "123456", "secret")
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.get("/api/status").status_code == 401
    assert client.get("/", environ_base={"REMOTE_ADDR": "8.8.8.8"}).status_code == 403
    assert client.post("/pair", data={"code": "wrong"}).status_code == 403


def test_pairing_page_escapes_untrusted_prefill(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    app = create_scanner_app(coordinator, "123456", "secret")
    app.config["TESTING"] = True

    response = app.test_client().get("/?pair=%3Cscript%3Ealert(1)%3C/script%3E")

    assert b"<script>alert(1)</script>" not in response.data
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.data
    assert response.headers["Cache-Control"].startswith("no-store")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_http_pairing_rate_limit(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    app = create_scanner_app(coordinator, "123456", "secret")
    app.config["TESTING"] = True
    client = app.test_client()

    responses = [client.post("/pair", data={"code": "wrong"}) for _ in range(9)]
    assert responses[-1].status_code == 429


def test_http_global_pairing_rate_limit_and_expiration(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    app = create_scanner_app(coordinator, "123456", "secret")
    app.config["TESTING"] = True
    client = app.test_client()

    responses = [
        client.post(
            "/pair",
            data={"code": "wrong"},
            environ_base={"REMOTE_ADDR": f"10.0.0.{index + 1}"},
        )
        for index in range(31)
    ]
    assert responses[-1].status_code == 429

    expired_app = create_scanner_app(
        coordinator,
        "123456",
        "secret",
        pairing_expires_at=time.monotonic() - 1,
    )
    expired_app.config["TESTING"] = True
    assert expired_app.test_client().post("/pair", data={"code": "123456"}).status_code == 410


def test_paired_session_is_invalidated_when_audit_changes(entries):
    coordinator, client, _csrf = _paired_client(entries)

    coordinator.configure("new-hash", entries)

    assert client.get("/api/status").status_code == 401


def test_http_status_csrf_scan_and_confirmation(entries, monkeypatch):
    coordinator, client, csrf = _paired_client(entries)
    monkeypatch.setattr(
        scanner_server,
        "analyze_image",
        lambda _image: ScanObservation(ocr_text="MATHIESEN UNIT 1701S", ocr_confidence=90),
    )

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.get_json()["packages"] == 2
    assert status.get_json()["audited"] == 0
    assert status.get_json()["remaining"] == 2
    assert client.post("/api/scan", data={"image": (io.BytesIO(b"image"), "label.jpg")}).status_code == 403

    response = client.post(
        "/api/scan",
        data={"image": (io.BytesIO(b"image"), "label.jpg")},
        headers={"X-CSRF-Token": csrf},
    )
    result = response.get_json()
    assert response.status_code == 200 and result["status"] == "review"

    confirmed = client.post(
        f"/api/scans/{result['scan_id']}/confirm",
        json={"item_id": "one"},
        headers={"X-CSRF-Token": csrf},
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json()["status"] == "matched"


def test_phone_page_exposes_progress_connection_and_fast_upload_controls(entries):
    _coordinator, client, _csrf = _paired_client(entries)

    scanner = client.get("/scanner")

    assert b'id="connection"' in scanner.data
    assert b"Take package photo" in scanner.data
    assert b"Choose photo" in scanner.data
    assert b"prepareImage" in scanner.data
    assert b"can_mark_not_found" in scanner.data
    assert b"maximum-scale" not in scanner.data
    assert b"button.disabled=disabled" in scanner.data


def test_http_not_found_rejects_unusable_scan(entries, monkeypatch):
    _coordinator, client, csrf = _paired_client(entries)
    monkeypatch.setattr(
        scanner_server,
        "analyze_image",
        lambda _image: ScanObservation(ocr_text="blurred shipping label"),
    )
    scanned = client.post(
        "/api/scan",
        data={"image": (io.BytesIO(b"image"), "label.jpg")},
        headers={"X-CSRF-Token": csrf},
    ).get_json()

    response = client.post(
        f"/api/scans/{scanned['scan_id']}/not-found",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert "Retake" in response.get_json()["error"]


def test_status_reports_audited_progress_and_phone_heartbeats(entries):
    entries[0].audited = True
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    coordinator.note_phone_activity("phone-one")

    assert coordinator.status()["audited"] == 1
    assert coordinator.status()["remaining"] == 1
    assert coordinator.active_phone_count() == 1

    coordinator._phone_clients["phone-one"] -= scanner_server.PHONE_ACTIVE_SECONDS + 1
    assert coordinator.active_phone_count() == 0


def test_http_scan_rejects_missing_and_invalid_images(entries):
    _coordinator, client, csrf = _paired_client(entries)
    assert client.post("/api/scan", headers={"X-CSRF-Token": csrf}).status_code == 400
    response = client.post(
        "/api/scan",
        data={"image": (io.BytesIO(b"not an image"), "bad.jpg")},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422


def test_http_scan_returns_busy_when_processing_slots_are_full(entries):
    coordinator, client, csrf = _paired_client(entries)
    assert coordinator._scan_slots.acquire()
    assert coordinator._scan_slots.acquire()
    try:
        response = client.post(
            "/api/scan",
            data={"image": (io.BytesIO(b"image"), "label.jpg")},
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        coordinator._scan_slots.release()
        coordinator._scan_slots.release()
    assert response.status_code == 429


def test_scan_cache_expires_and_stays_bounded(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    first = coordinator.process_observation(ScanObservation(ocr_text="first label"))
    coordinator.drain_actions()
    coordinator._scans[first["scan_id"]].created_at -= scanner_server.SCAN_CACHE_TTL_SECONDS + 1

    second = coordinator.process_observation(ScanObservation(ocr_text="first label"))
    assert second["scan_id"] != first["scan_id"]
    for index in range(scanner_server.MAX_STORED_SCANS + 25):
        coordinator.process_observation(ScanObservation(ocr_text=f"unique label {index}"))
    coordinator.status()
    assert len(coordinator._scans) <= scanner_server.MAX_STORED_SCANS


def test_background_server_starts_and_stops(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    server = ScannerServer(coordinator)
    server.host_address = "127.0.0.1"

    server.start()
    try:
        assert server.running
        assert server.port > 0
        with urllib.request.urlopen(server.url, timeout=3) as response:
            assert response.status == 200
            assert b"Pair package scanner" in response.read()
    finally:
        server.stop()
        server.stop()
    assert not server.running
