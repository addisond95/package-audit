"""Tests for the local phone scanner coordinator and HTTP security."""

from __future__ import annotations

import io
import re
import time
import urllib.request

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


def test_exact_scan_waits_for_confirmation_and_repeats_idempotently(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    observation = ScanObservation(barcodes=("1Z999AA10123456784",))

    first = coordinator.process_observation(observation)
    actions = coordinator.drain_actions()
    repeated = coordinator.process_observation(observation)

    assert first["status"] == "confirm"
    assert first["unit"] == "1701S"
    assert first["candidates"][0]["resident"] == "Mathiesen"
    assert len(actions) == 1 and actions[0].kind == "event"
    assert repeated["scan_id"] == first["scan_id"]
    assert repeated["repeated"] is True
    assert coordinator.drain_actions() == []


def test_confirmation_queues_match_and_replay_is_idempotent(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    result = coordinator.process_observation(ScanObservation(barcodes=("1Z999AA10123456784",)))
    coordinator.drain_actions()

    confirmed = coordinator.confirm(result["scan_id"], "one")
    action = coordinator.drain_actions()[0]
    repeated = coordinator.confirm(result["scan_id"], "one")

    assert confirmed["status"] == "matched"
    assert confirmed["can_undo"] is True
    assert action.kind == "match" and action.item_id == "one"
    assert repeated["repeated"] is True
    assert coordinator.drain_actions() == []


def test_confirmation_rejects_other_items_and_multi_label_scan(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    result = coordinator.process_observation(
        ScanObservation(barcodes=("1Z999AA10123456784", "1Z999AA10123450000"))
    )

    assert result["status"] == "poor_scan"
    with pytest.raises(ValueError, match="not waiting"):
        coordinator.confirm(result["scan_id"], "two")

    exact = coordinator.process_observation(ScanObservation(barcodes=("1Z999AA10123456784",)))
    with pytest.raises(ValueError, match="not a candidate"):
        coordinator.confirm(exact["scan_id"], "two")


def test_reject_allows_a_fresh_scan_of_the_same_tracking(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    observation = ScanObservation(barcodes=("1Z999AA10123456784",))
    first = coordinator.process_observation(observation)
    coordinator.drain_actions()

    rejected = coordinator.reject(first["scan_id"])
    coordinator.drain_actions()
    second = coordinator.process_observation(observation)

    assert rejected["status"] == "rejected"
    assert rejected["can_mark_not_found"] is False
    assert second["status"] == "confirm"
    assert second["scan_id"] != first["scan_id"]


def test_unknown_tracking_is_logged_not_found_and_can_be_undone(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)

    result = coordinator.process_observation(ScanObservation(barcodes=("1Z000ZZ00000000001",)))
    action = coordinator.drain_actions()[0]
    undone = coordinator.undo(result["scan_id"])

    assert result["status"] == "not_found"
    assert result["unit"] == ""
    assert action.kind == "not_found"
    assert undone["status"] == "undo_queued"
    assert coordinator.drain_actions()[0].kind == "undo"


def test_undone_match_rejects_late_follow_up_actions(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    result = coordinator.process_observation(ScanObservation(barcodes=("1Z999AA10123456784",)))
    coordinator.drain_actions()
    coordinator.confirm(result["scan_id"], "one")
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


def test_failed_decode_cannot_be_marked_not_found_or_cached(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    first = coordinator.process_observation(ScanObservation())
    second = coordinator.process_observation(ScanObservation())

    assert first["status"] == "poor_scan"
    assert first["can_mark_not_found"] is False
    assert second["scan_id"] != first["scan_id"]
    with pytest.raises(ValueError, match="tracking barcode"):
        coordinator.mark_not_found(first["scan_id"])


def test_switching_pdf_discards_queued_actions_and_invalidates_generation(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("old-hash", entries)
    coordinator.process_observation(ScanObservation(barcodes=("1Z999AA10123456784",)))
    generation = coordinator.generation

    coordinator.configure("new-hash", entries)

    assert coordinator.generation == generation + 1
    assert coordinator.drain_actions() == []


def test_explicit_scan_reset_allows_same_image_again_and_discards_actions(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    observation = ScanObservation(barcodes=("1Z999AA10123456784",))
    first = coordinator.process_observation(observation)

    coordinator.configure("hash", entries, reset_scans=True)
    second = coordinator.process_observation(observation)

    assert second["scan_id"] != first["scan_id"]
    assert [action.scan_id for action in coordinator.drain_actions()] == [second["scan_id"]]


def test_session_invalidation_discards_pending_actions_and_phone_activity(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    coordinator.note_phone_activity("phone-one")
    coordinator.process_observation(ScanObservation(barcodes=("1Z999AA10123456784",)))
    generation = coordinator.generation

    coordinator.invalidate_sessions()

    assert coordinator.generation == generation + 1
    assert coordinator.active_phone_count() == 0
    assert coordinator.drain_actions() == []


def test_expired_scan_is_rejected(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)

    with pytest.raises(KeyError, match="expired"):
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


def test_pairing_page_escapes_prefill_and_sets_private_headers(entries):
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


def test_http_pairing_rate_limits_and_expires(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    app = create_scanner_app(coordinator, "123456", "secret")
    app.config["TESTING"] = True
    client = app.test_client()

    responses = [client.post("/pair", data={"code": "wrong"}) for _ in range(9)]
    assert responses[-1].status_code == 429

    expired_app = create_scanner_app(
        coordinator,
        "123456",
        "secret",
        pairing_expires_at=time.monotonic() - 1,
    )
    expired_app.config["TESTING"] = True
    assert expired_app.test_client().post("/pair", data={"code": "123456"}).status_code == 410


def test_http_global_pairing_rate_limit(entries):
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


def test_paired_session_is_invalidated_when_audit_changes(entries):
    coordinator, client, _csrf = _paired_client(entries)

    coordinator.configure("new-hash", entries)

    assert client.get("/api/status").status_code == 401


def test_scan_is_discarded_when_audit_changes_during_processing(entries, monkeypatch):
    coordinator, client, csrf = _paired_client(entries)

    def analyze_after_audit_change(_image):
        coordinator.configure("new-hash", entries)
        return ScanObservation(barcodes=("1Z999AA10123456784",))

    monkeypatch.setattr(scanner_server, "analyze_image", analyze_after_audit_change)

    response = client.post(
        "/api/scan",
        data={"image": (io.BytesIO(b"image"), "label.jpg")},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 401
    assert "audit changed" in response.get_json()["error"]
    assert coordinator.drain_actions() == []


def test_http_status_scan_and_confirmation(entries, monkeypatch):
    coordinator, client, csrf = _paired_client(entries)
    monkeypatch.setattr(
        scanner_server,
        "analyze_image",
        lambda _image: ScanObservation(barcodes=("1Z999AA10123456784",)),
    )

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.get_json()["packages"] == 2
    assert status.get_json()["remaining"] == 2
    assert client.post("/api/scan", data={"image": (io.BytesIO(b"image"), "label.jpg")}).status_code == 403

    response = client.post(
        "/api/scan",
        data={"image": (io.BytesIO(b"image"), "label.jpg")},
        headers={"X-CSRF-Token": csrf},
    )
    result = response.get_json()
    assert response.status_code == 200 and result["status"] == "confirm"
    assert result["unit"] == "1701S"

    confirmed = client.post(
        f"/api/scans/{result['scan_id']}/confirm",
        json={"item_id": "one"},
        headers={"X-CSRF-Token": csrf},
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json()["status"] == "matched"


def test_phone_page_exposes_one_tap_auto_upload_and_confirmation(entries):
    _coordinator, client, _csrf = _paired_client(entries)

    scanner = client.get("/scanner")

    assert b'id="connection"' in scanner.data
    assert b"Scan package" in scanner.data
    assert b"Existing photo" in scanner.data
    assert b"tracking barcode only" in scanner.data
    assert b"prepareImage" in scanner.data
    assert b"Confirm unit" in scanner.data
    assert b"requestTimeoutMs" in scanner.data
    assert b"Take package photo" not in scanner.data
    assert b"maximum-scale" not in scanner.data


def test_http_not_found_rejects_unusable_scan(entries, monkeypatch):
    _coordinator, client, csrf = _paired_client(entries)
    monkeypatch.setattr(scanner_server, "analyze_image", lambda _image: ScanObservation())
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
    assert "tracking barcode" in response.get_json()["error"]


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


def test_http_scan_rejects_missing_invalid_and_busy_images(entries):
    coordinator, client, csrf = _paired_client(entries)
    assert client.post("/api/scan", headers={"X-CSRF-Token": csrf}).status_code == 400
    invalid = client.post(
        "/api/scan",
        data={"image": (io.BytesIO(b"not an image"), "bad.jpg")},
        headers={"X-CSRF-Token": csrf},
    )
    assert invalid.status_code == 422

    assert coordinator._scan_slots.acquire()
    assert coordinator._scan_slots.acquire()
    try:
        busy = client.post(
            "/api/scan",
            data={"image": (io.BytesIO(b"image"), "label.jpg")},
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        coordinator._scan_slots.release()
        coordinator._scan_slots.release()
    assert busy.status_code == 429


def test_scan_cache_expires_and_stays_bounded(entries):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    first_observation = ScanObservation(barcodes=("1Z000ZZ00000000001",))
    first = coordinator.process_observation(first_observation)
    coordinator.drain_actions()
    coordinator._scans[first["scan_id"]].created_at -= scanner_server.SCAN_CACHE_TTL_SECONDS + 1

    second = coordinator.process_observation(first_observation)
    assert second["scan_id"] != first["scan_id"]
    for index in range(scanner_server.MAX_STORED_SCANS + 25):
        coordinator.process_observation(ScanObservation(barcodes=(f"TRACKING{index:010d}",)))
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


def test_background_server_reports_bind_exit_as_oserror(entries, monkeypatch):
    coordinator = ScannerCoordinator()
    coordinator.configure("hash", entries)
    server = ScannerServer(coordinator)
    monkeypatch.setattr(
        scanner_server,
        "make_server",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit(1)),
    )

    with pytest.raises(OSError, match="local network port"):
        server.start()

    assert not server.running
