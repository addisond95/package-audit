"""Private local-network phone scanner service and desktop action queue."""

# ruff: noqa: E501

from __future__ import annotations

import ipaddress
import queue
import secrets
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.serving import BaseWSGIServer, WSGIRequestHandler, make_server

from app.models import AuditEntry
from app.scanner_matching import PackageMatcher, ScanDecision, ScanObservation
from app.scanner_tunnel import CloudflareQuickTunnel, TunnelStartupError
from app.scanner_vision import (
    MAX_IMAGE_BYTES,
    ScanImageError,
    analyze_image,
    observation_from_barcodes,
    scanner_capabilities,
)

SCAN_CACHE_TTL_SECONDS = 30 * 60
MAX_STORED_SCANS = 100
PAIRING_TTL_SECONDS = 15 * 60
PHONE_ACTIVE_SECONDS = 15
LIVE_STABILITY_HITS = 2
LIVE_STABILITY_SECONDS = 3.0
MAX_LIVE_BARCODES = 8
MAX_LIVE_BARCODE_LENGTH = 2048


class ScannerBusyError(RuntimeError):
    """Raised when all bounded image-processing slots are occupied."""


class ScannerSessionExpiredError(RuntimeError):
    """Raised when a scan finishes after the desktop audit has changed."""


class _QuietRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-") -> None:
        pass


@dataclass(frozen=True)
class ScannerAction:
    scan_id: str
    kind: str
    observation: ScanObservation
    decision: ScanDecision
    item_id: str = ""


@dataclass
class _StoredScan:
    scan_id: str
    observation: ScanObservation
    decision: ScanDecision
    created_at: float
    rejected: bool = False
    undo_queued: bool = False


class ScannerCoordinator:
    """Own matching snapshots and pass mutations safely back to the Qt thread."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._actions: queue.Queue[ScannerAction] = queue.Queue()
        self._scans: dict[str, _StoredScan] = {}
        self._scan_ids_by_key: dict[str, str] = {}
        self._matcher = PackageMatcher([])
        self._pdf_hash = ""
        self._alert_summary: dict[str, int] = {}
        self._phone_clients: dict[str, float] = {}
        self._generation = 0
        self._scan_slots = threading.BoundedSemaphore(2)

    @property
    def pdf_hash(self) -> str:
        with self._lock:
            return self._pdf_hash

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def configure(
        self,
        pdf_hash: str,
        entries: list[AuditEntry],
        *,
        reset_scans: bool = False,
    ) -> None:
        with self._lock:
            self._matcher = PackageMatcher(entries)
            if pdf_hash != self._pdf_hash or reset_scans:
                self._invalidate_sessions_locked()
            self._pdf_hash = pdf_hash

    def invalidate_sessions(self) -> None:
        """Invalidate paired phones and discard actions that have not reached Qt."""
        with self._lock:
            self._invalidate_sessions_locked()

    def _invalidate_sessions_locked(self) -> None:
        self._scans.clear()
        self._scan_ids_by_key.clear()
        self._generation += 1
        self._phone_clients.clear()
        while True:
            try:
                self._actions.get_nowait()
            except queue.Empty:
                break

    def set_alert_summary(self, summary: dict[str, int]) -> None:
        with self._lock:
            self._alert_summary = dict(summary)

    def duplicate_groups(self):
        with self._lock:
            return self._matcher.duplicate_groups()

    def note_phone_activity(self, client_id: str) -> None:
        """Record a paired phone heartbeat for the desktop connection indicator."""
        if not client_id:
            return
        with self._lock:
            self._phone_clients[client_id] = time.monotonic()
            self._prune_phone_clients()

    def active_phone_count(self) -> int:
        with self._lock:
            self._prune_phone_clients()
            return len(self._phone_clients)

    def _prune_phone_clients(self) -> None:
        cutoff = time.monotonic() - PHONE_ACTIVE_SECONDS
        self._phone_clients = {
            client_id: last_seen
            for client_id, last_seen in self._phone_clients.items()
            if last_seen >= cutoff
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._prune_scans()
            audited = sum(record.audited for record in self._matcher.records)
            return {
                "audit_loaded": bool(self._pdf_hash and self._matcher.records),
                "packages": len(self._matcher.records),
                "audited": audited,
                "remaining": len(self._matcher.records) - audited,
                "alerts": dict(self._alert_summary),
                "capabilities": scanner_capabilities().to_dict(),
            }

    def process_image(
        self,
        image_bytes: bytes,
        *,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        observation = self.observe_image(
            image_bytes,
            expected_generation=expected_generation,
        )
        return self.process_observation(
            observation,
            expected_generation=expected_generation,
        )

    def observe_image(
        self,
        image_bytes: bytes,
        *,
        expected_generation: int | None = None,
    ) -> ScanObservation:
        """Decode one image without creating an audit event yet."""
        with self._lock:
            self._require_generation(expected_generation)
        if not self._scan_slots.acquire(timeout=0.1):
            raise ScannerBusyError("The scanner is busy. Try again in a moment.")
        try:
            observation = analyze_image(image_bytes)
        finally:
            self._scan_slots.release()
        with self._lock:
            self._require_generation(expected_generation)
        return observation

    def process_observation(
        self,
        observation: ScanObservation,
        *,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_generation(expected_generation)
            if not self._pdf_hash or not self._matcher.records:
                raise RuntimeError("Open an audit PDF on the desktop before scanning.")
            self._prune_scans()
            # A failed decode has an empty barcode set and therefore no stable
            # package identity. Never cache it: the very next photo may be a
            # clearer image of a completely different box.
            cacheable = bool(observation.barcodes)
            existing_scan_id = self._scan_ids_by_key.get(observation.scan_key) if cacheable else None
            if existing_scan_id:
                return self._response(self._scans[existing_scan_id], repeated=True)

            scan_id = secrets.token_hex(8)
            decision = self._matcher.decide(observation)
            stored = _StoredScan(scan_id, observation, decision, time.monotonic())
            self._scans[scan_id] = stored
            if cacheable:
                self._scan_ids_by_key[observation.scan_key] = scan_id
            self._prune_scans()

            if decision.status == "already_matched":
                self._actions.put(
                    ScannerAction(scan_id, "event", observation, decision, decision.related_item_ids[0])
                )
            elif decision.status == "not_found":
                self._actions.put(ScannerAction(scan_id, "not_found", observation, decision))
            elif decision.status == "duplicate":
                self._actions.put(ScannerAction(scan_id, "duplicate", observation, decision))
            else:
                self._actions.put(ScannerAction(scan_id, "event", observation, decision))

            return self._response(stored)

    def _require_generation(self, expected_generation: int | None) -> None:
        if expected_generation is not None and expected_generation != self._generation:
            raise ScannerSessionExpiredError(
                "The desktop audit changed while this photo was being processed. Pair again and rescan it."
            )

    def _prune_scans(self) -> None:
        cutoff = time.monotonic() - SCAN_CACHE_TTL_SECONDS
        expired = [stored for stored in self._scans.values() if stored.created_at < cutoff]
        overflow = max(0, len(self._scans) - MAX_STORED_SCANS)
        oldest = sorted(self._scans.values(), key=lambda value: value.created_at)[:overflow]
        removals = {stored.scan_id: stored for stored in (*expired, *oldest)}
        for stored in removals.values():
            self._scans.pop(stored.scan_id, None)
            self._scan_ids_by_key.pop(stored.observation.scan_key, None)

    @staticmethod
    def _can_mark_not_found(_stored: _StoredScan) -> bool:
        return False

    @staticmethod
    def _can_undo(stored: _StoredScan) -> bool:
        return not stored.undo_queued and stored.decision.status in {
            "matched",
            "not_found",
            "duplicate",
        }

    @classmethod
    def _response(cls, stored: _StoredScan, *, repeated: bool = False) -> dict[str, Any]:
        response = stored.decision.to_dict()
        response["scan_id"] = stored.scan_id
        response["repeated"] = repeated
        response["can_mark_not_found"] = cls._can_mark_not_found(stored)
        response["can_undo"] = cls._can_undo(stored)
        return response

    def confirm(self, scan_id: str, item_id: str) -> dict[str, Any]:
        with self._lock:
            stored = self._require_scan(scan_id)
            if stored.undo_queued:
                raise ValueError("This scan was undone. Scan the label again before choosing a package.")
            if stored.decision.status == "matched":
                if item_id in stored.decision.related_item_ids:
                    return self._response(stored, repeated=True)
                raise ValueError("That package is not a candidate for this scan.")
            if stored.decision.status != "confirm":
                raise ValueError("This scan is not waiting for package confirmation.")
            candidate_ids = {candidate.item_id for candidate in stored.decision.candidates}
            if item_id not in candidate_ids:
                raise ValueError("That package is not a candidate for this scan.")
            selected = self._matcher.records_by_id[item_id]
            tracking = next(
                (value for value in stored.observation.trackings if value in selected.trackings),
                stored.decision.tracking,
            )
            decision = ScanDecision(
                status="matched",
                confidence=next(
                    candidate.confidence
                    for candidate in stored.decision.candidates
                    if candidate.item_id == item_id
                ),
                message="Package confirmed and marked here.",
                candidates=stored.decision.candidates,
                tracking=tracking,
                unit=selected.unit,
                carrier=stored.decision.carrier,
                scan_key=stored.decision.scan_key,
                related_item_ids=(item_id,),
            )
            stored.decision = decision
            stored.rejected = False
            stored.undo_queued = False
            self._actions.put(
                ScannerAction(
                    scan_id,
                    "match",
                    stored.observation,
                    decision,
                    item_id,
                )
            )
            return self._response(stored)

    def reject(self, scan_id: str) -> dict[str, Any]:
        with self._lock:
            stored = self._require_scan(scan_id)
            if stored.undo_queued:
                raise ValueError("This scan was undone. Scan the label again before rejecting it.")
            if stored.rejected:
                response = self._response(stored, repeated=True)
                response.update(
                    status="rejected",
                    message="The suggestion was already rejected.",
                )
                return response
            self._scan_ids_by_key.pop(stored.observation.scan_key, None)
            stored.rejected = True
            stored.undo_queued = False
            self._actions.put(ScannerAction(scan_id, "reject", stored.observation, stored.decision))
            response = self._response(stored)
            response.update(
                status="rejected",
                message="Nothing was marked. Isolate the tracking barcode and scan again.",
            )
            return response

    def mark_not_found(self, scan_id: str) -> dict[str, Any]:
        with self._lock:
            stored = self._require_scan(scan_id)
            if stored.decision.status == "not_found" and not stored.rejected:
                return self._response(stored, repeated=True)
            raise ValueError("Only a scanned tracking barcode can be marked not logged.")

    def undo(self, scan_id: str) -> dict[str, Any]:
        with self._lock:
            stored = self._require_scan(scan_id)
            if stored.undo_queued:
                return {
                    "scan_id": scan_id,
                    "status": "undo_queued",
                    "message": "Undo was already sent to desktop.",
                    "can_mark_not_found": False,
                    "can_undo": False,
                }
            if not self._can_undo(stored):
                raise ValueError("This scan did not change desktop audit data, so there is nothing to undo.")
            self._scan_ids_by_key.pop(stored.observation.scan_key, None)
            stored.undo_queued = True
            self._actions.put(ScannerAction(scan_id, "undo", stored.observation, stored.decision))
            return {
                "scan_id": scan_id,
                "status": "undo_queued",
                "message": "Undo sent to desktop.",
                "can_mark_not_found": False,
                "can_undo": False,
            }

    def _require_scan(self, scan_id: str) -> _StoredScan:
        stored = self._scans.get(scan_id)
        if stored is None:
            raise KeyError("The scan has expired. Scan the label again.")
        return stored

    def drain_actions(self, limit: int = 100) -> list[ScannerAction]:
        actions: list[ScannerAction] = []
        while len(actions) < limit:
            try:
                actions.append(self._actions.get_nowait())
            except queue.Empty:
                break
        return actions


_PAIR_TEMPLATE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair Package Scanner</title><style>
:root{color-scheme:light;font-family:ui-rounded,"SF Pro Rounded",system-ui,sans-serif;background:#eef2f4;color:#17212b}
*{box-sizing:border-box}body{margin:0;min-height:100svh;display:grid;place-items:center;padding:20px}.panel{width:min(440px,100%);background:white;border:1px solid #d9dee5;border-radius:14px;padding:26px;box-shadow:0 10px 30px #1a2b2520}h1{font-size:25px;margin:0 0 8px}p{color:#5a6572;line-height:1.45;margin:0 0 20px}label{display:block;font-size:14px;font-weight:700;margin-bottom:8px}input{width:100%;font-size:28px;letter-spacing:8px;text-align:center;padding:14px;border:2px solid #aeb7c2;border-radius:9px}input:focus{border-color:#176b4d;outline:3px solid #176b4d22}button{width:100%;min-height:50px;margin-top:13px;padding:14px;border:0;border-radius:9px;background:#176b4d;color:white;font-size:17px;font-weight:750}.error{color:#b42318;background:#fff3f1;border-radius:8px;margin-top:14px;padding:11px}.privacy{font-size:12px;margin:16px 0 0;text-align:center}
</style></head><body><main class="panel"><h1>Pair package scanner</h1><p>{{ connection_help }}</p><form method="post" action="/pair"><label for="pair-code">Pairing code</label><input id="pair-code" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" value="{{ code }}" autofocus required><button type="submit">Connect to desktop</button></form>{% if error %}<div class="error" role="alert">{{ error }}</div>{% endif %}<p class="privacy">{{ privacy_message }}</p></main><script>
const fragment=new URLSearchParams(window.location.hash.slice(1));const fragmentCode=fragment.get('pair');const pairInput=document.getElementById('pair-code');if(fragmentCode&&/^[0-9]{6}$/.test(fragmentCode)){pairInput.value=fragmentCode;history.replaceState(null,'',window.location.pathname)}
</script></body></html>
"""

_SCANNER_TEMPLATE = """
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="csrf-token" content="{{ csrf }}"><title>Package Scanner</title><style>
:root{color-scheme:light;font-family:ui-rounded,"SF Pro Rounded",system-ui,sans-serif;background:#eef2f4;color:#17212b}
*{box-sizing:border-box}body{margin:0}.shell{max-width:640px;margin:auto;min-height:100svh;background:white;box-shadow:0 0 30px #20352d14}
.top{background:#143c32;color:white;padding:18px 18px 16px}.title-row{display:flex;align-items:center;justify-content:space-between;gap:12px}.top h1{font-size:22px;margin:0}.connection{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:700}.dot{width:9px;height:9px;border-radius:50%;background:#e1a928}.connection.online .dot{background:#60d394}.connection.offline .dot{background:#ff8f80}
.progress-card{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-top:15px}.progress-card strong{font-size:18px}.progress-card span{font-size:13px;color:#dbe9e3}.progress-track{height:6px;background:#ffffff30;border-radius:99px;overflow:hidden;margin-top:8px}.progress-fill{height:100%;width:0;background:#73d6a2;transition:width .25s ease}.summary{display:flex;gap:7px;margin-top:12px;flex-wrap:wrap}.chip{font-size:12px;background:#ffffff20;padding:6px 8px;border-radius:99px}
main{padding:18px}.notice{display:none;border-radius:9px;padding:11px 12px;margin-bottom:13px;font-size:13px;line-height:1.4}.notice.show{display:block}.notice.warning{background:#fff8e8;color:#76510a}.notice.error{background:#fff3f1;color:#9f261c}.notice button{margin-top:8px}
.upload-actions{display:grid;grid-template-columns:1fr auto;gap:9px}.upload-actions button,.actions button,.candidate,.notice button{min-height:52px;border-radius:9px;font-size:17px;font-weight:750}.primary{border:0;background:#176b4d;color:white;padding:15px 18px}.secondary{border:1px solid #aeb7c2;background:white;color:#28343f;padding:12px 14px}button:disabled{opacity:.5}.upload-actions input{display:none}
.live-view{display:none;position:relative;margin:13px 0 9px;border-radius:12px;overflow:hidden;background:#101816;aspect-ratio:4/3}.live-view.show{display:block}.live-view video{width:100%;height:100%;display:block;object-fit:cover}.scan-shade{position:absolute;inset:0;background:linear-gradient(#0008 0 20%,transparent 20% 70%,#0008 70%)}.scan-guide{position:absolute;left:5%;right:5%;top:27%;height:38%;border:3px solid #73d6a2;border-radius:10px;box-shadow:0 0 0 999px #00000018}.scan-line{position:absolute;left:9%;right:9%;top:46%;height:2px;background:#73d6a2;box-shadow:0 0 8px #73d6a2}.live-label{position:absolute;left:10px;right:10px;bottom:10px;padding:8px 10px;border-radius:8px;background:#101816d9;color:white;text-align:center;font-size:13px;font-weight:750}
.hint{text-align:center;color:#66717d;font-size:13px;line-height:1.4;margin:10px 0 17px}.busy{display:none;text-align:center;padding:25px 10px;color:#4f5d68;font-weight:700}.busy.show{display:block}.spinner{display:inline-block;width:18px;height:18px;border:3px solid #c7d2cd;border-top-color:#176b4d;border-radius:50%;animation:spin .8s linear infinite;vertical-align:-4px;margin-right:7px}@keyframes spin{to{transform:rotate(360deg)}}
.result{display:none;border:1px solid #d4dae1;border-left:6px solid #64748b;border-radius:10px;padding:16px;outline:none}.result.show{display:block}.result.confirm{border-left-color:#176b4d;background:#effaf5}.result.matched,.result.already_matched{border-left-color:#16845b;background:#effaf5}.result.not_found{border-left-color:#d92d20;background:#fff3f1}.result.duplicate{border-left-color:#e78b09;background:#fff8e8}.result.poor_scan,.result.rejected{border-left-color:#d9a300;background:#fffbea}.result.undo_queued{border-left-color:#64748b;background:#f4f6f8}.result h2{font-size:19px;line-height:1.25;margin:0 0 8px}.result p{margin:5px 0;color:#4c5864}.unit-number{font-size:42px;font-weight:850;line-height:1;margin:12px 0 8px}.meta{display:flex;gap:7px;flex-wrap:wrap;margin-top:9px}.meta span{background:#ffffffa8;border-radius:99px;padding:5px 8px;font-size:13px}.candidate{width:100%;text-align:left;border:1px solid #b9c2cc;background:white;padding:12px;margin-top:9px}.candidate strong,.candidate span{display:block}.actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}.actions button{flex:1;border:1px solid #aeb7c2;background:white;padding:12px 13px;color:#28343f}.actions .confirm-action{border:0;background:#176b4d;color:white}.actions .danger{color:#b42318;border-color:#e8a49e}.privacy{text-align:center;color:#71808c;font-size:12px;margin:18px 0 0}
@media(max-width:360px){.upload-actions{grid-template-columns:1fr}.progress-card{align-items:flex-start;flex-direction:column;gap:2px}}@media(prefers-reduced-motion:reduce){.spinner{animation:none}.progress-fill{transition:none}.scan-line{display:none}}
</style></head><body><div class="shell"><header class="top"><div class="title-row"><h1>Package Scanner</h1><div id="connection" class="connection"><span class="dot"></span><span id="connection-text">Checking desktop…</span></div></div><div class="progress-card"><strong id="progress-text">Loading audit…</strong><span id="remaining-text"></span></div><div class="progress-track"><div id="progress-fill" class="progress-fill"></div></div><div class="summary" id="summary"></div></header><main><div id="connection-notice" class="notice error" role="alert"></div><div id="capability-notice" class="notice warning"></div><div class="upload-actions"><button id="camera-button" class="primary" type="button">Scan package</button><button id="library-button" class="secondary" type="button">Existing photo</button><input id="camera-image" type="file" accept="image/*" capture="environment"><input id="library-image" type="file" accept="image/*"></div><section id="live-view" class="live-view" aria-label="Live tracking barcode camera"><video id="live-video" autoplay muted playsinline></video><div class="scan-shade"></div><div class="scan-guide"></div><div class="scan-line"></div><div id="live-label" class="live-label">Point the guide at one tracking barcode</div></section><div id="scan-hint" class="hint">Tap Scan package once. The live camera reads the tracking barcode automatically—no picture or Submit step.</div><div id="busy" class="busy" role="status" aria-live="polite"><span class="spinner"></span><span id="busy-text">Reading tracking barcode…</span></div><section id="result" class="result" aria-live="polite" tabindex="-1"></section><p class="privacy">{{ scanner_privacy }}</p></main></div><script>
const csrf=document.querySelector('meta[name=csrf-token]').content;
const maximumUploadBytes=15*1024*1024;const resizeEdge=2200;const preferredBytes=3*1024*1024;const requestTimeoutMs=20000;const liveRequestTimeoutMs=7000;const liveIntervalMs=320;const liveFrameWidth=1280;
const cameraInput=document.getElementById('camera-image');const libraryInput=document.getElementById('library-image');const cameraButton=document.getElementById('camera-button');const libraryButton=document.getElementById('library-button');const busyBox=document.getElementById('busy');const busyText=document.getElementById('busy-text');const resultBox=document.getElementById('result');const liveView=document.getElementById('live-view');const liveVideo=document.getElementById('live-video');const liveLabel=document.getElementById('live-label');const scanHint=document.getElementById('scan-hint');
let current=null;let requestBusy=false;let desktopOnline=false;let desktopCapabilities={};let liveStream=null;let liveActive=false;let livePaused=false;let liveTimer=null;let liveRequestBusy=false;let liveUnavailable=!(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia);let liveFailure='';let nativeDetector=null;let nativeDetectorFailures=0;let nativeEmptyFrames=0;let awaitingClearTracking='';let clearFrames=0;
function esc(value){const node=document.createElement('div');node.textContent=value??'';return node.innerHTML}
function setBusy(value,message='Reading tracking barcode…'){requestBusy=value;busyText.textContent=message;busyBox.classList.toggle('show',value);document.querySelector('main').setAttribute('aria-busy',String(value));updateControls()}
function updateControls(){const disabled=requestBusy||!desktopOnline;cameraButton.disabled=disabled;libraryButton.disabled=disabled;cameraInput.disabled=disabled;libraryInput.disabled=disabled;cameraButton.textContent=liveActive?'Stop live scanner':'Scan package';resultBox.querySelectorAll('button').forEach(button=>button.disabled=disabled)}
function setConnection(online,message,authEnded=false){desktopOnline=online;const box=document.getElementById('connection');box.className=`connection ${online?'online':'offline'}`;document.getElementById('connection-text').textContent=message;const notice=document.getElementById('connection-notice');if(online){notice.classList.remove('show');notice.innerHTML=''}else{notice.classList.add('show');notice.innerHTML=`${esc(message)}${authEnded?'<br><button type="button" class="secondary" data-connect-action="reconnect">Pair again</button>':'<br><button type="button" class="secondary" data-connect-action="retry">Retry connection</button>'}`;const action=notice.querySelector('button');action.addEventListener('click',()=>authEnded?window.location.assign('/'):refresh())}updateControls()}
async function api(path,options={},timeoutMs=requestTimeoutMs){options.headers={...(options.headers||{}),'X-CSRF-Token':csrf};const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),timeoutMs);let response;try{response=await fetch(path,{...options,signal:controller.signal})}catch(_error){const error=new Error('Desktop did not respond. Check the phone connection, keep the scanner running, and try again.');error.network=true;throw error}finally{clearTimeout(timeout)}const data=await response.json().catch(()=>({}));if(!response.ok){const error=new Error(data.error||`Request failed (${response.status})`);error.status=response.status;throw error}return data}
function actionButton(label,action,cls=''){return `<button type="button" class="${cls}" data-action="${action}">${esc(label)}</button>`}
function bindResultActions(){resultBox.querySelectorAll('[data-candidate]').forEach(button=>button.addEventListener('click',()=>confirmMatch(Number(button.dataset.candidate))));resultBox.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>runAction(button.dataset.action)));updateControls()}
function render(data){current=data;if(liveActive)livePaused=true;const allowed=['confirm','matched','already_matched','not_found','duplicate','poor_scan','rejected','undo_queued'];const status=allowed.includes(data.status)?data.status:'poor_scan';const tracking=String(data.tracking||'');resultBox.className=`result show ${status}`;let html=`<h2>${esc(data.message||'Scan complete')}</h2>`;if(status==='confirm'){html+=`<div class="unit-number">${esc(data.unit)}</div><div class="meta"><span>Tracking …${esc(tracking.slice(-4))}</span></div><div class="actions">${actionButton(`Confirm unit ${data.unit}`, 'confirm','confirm-action')}${actionButton('Wrong barcode — rescan','reject','danger')}</div>`}
else{html+='<div class="meta">';if(data.tracking)html+=`<span>Tracking …${esc(data.tracking.slice(-4))}</span>`;if(data.unit)html+=`<span>Unit ${esc(data.unit)}</span>`;html+='</div>';if(data.repeated)html+='<p>This barcode was already processed; no duplicate action was added.</p>';
if(status==='already_matched'){html+=`<div class="actions">${actionButton('Scan next package','camera')}${actionButton('Wrong — retake','reject','danger')}</div>`}
else if(['matched','not_found','duplicate'].includes(status)){html+=`<div class="actions">${actionButton('Scan next package','camera')}${data.can_undo?actionButton('Undo','undo'):''}${actionButton('Wrong — retake','reject','danger')}</div>`}
else if(status==='rejected'){html+=`<div class="actions">${actionButton('Scan again','camera')}</div>`}
else if(status==='poor_scan'){html+=`<div class="actions">${actionButton('Scan again','camera')}</div>`}
else{html+=`<div class="actions">${actionButton('Scan next package','camera')}</div>`}}resultBox.innerHTML=html;bindResultActions();resultBox.focus({preventScroll:true});resultBox.scrollIntoView({behavior:'smooth',block:'nearest'});if(navigator.vibrate)navigator.vibrate(status==='matched'?[35]:status==='duplicate'||status==='not_found'?[70,50,70]:[30,40,30])}
function renderFailure(error){if(error.status===401){setConnection(false,'Pairing ended because the desktop audit changed or the scanner restarted.',true);return}if(error.network){setConnection(false,error.message,false);return}render({status:'poor_scan',message:error.message,confidence:0,can_mark_not_found:false})}
async function prepareImage(file){if(!window.createImageBitmap)return file;try{const bitmap=await createImageBitmap(file,{imageOrientation:'from-image'});const scale=Math.min(1,resizeEdge/Math.max(bitmap.width,bitmap.height));if(scale===1&&file.size<=preferredBytes){bitmap.close?.();return file}const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(bitmap.width*scale));canvas.height=Math.max(1,Math.round(bitmap.height*scale));canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close?.();const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',0.86));return blob||file}catch(_error){return file}}
async function scan(file){if(requestBusy||!desktopOnline)return;livePaused=liveActive;resultBox.classList.remove('show');setBusy(true,'Preparing scan…');try{const prepared=await prepareImage(file);if(prepared.size>maximumUploadBytes)throw new Error('This photo is too large. Move closer to the tracking barcode and try again.');setBusy(true,'Looking up tracking…');const form=new FormData();form.append('image',prepared,'tracking-barcode.jpg');render(await api('/api/scan',{method:'POST',body:form}));await refresh(true)}catch(error){renderFailure(error)}finally{cameraInput.value='';libraryInput.value='';setBusy(false)}}
async function requestAction(path,options={}){if(requestBusy||!current?.scan_id)return;setBusy(true,'Updating desktop…');try{render(await api(path,options));await refresh(true)}catch(error){renderFailure(error)}finally{setBusy(false)}}
function confirmMatch(index){const candidate=current?.candidates?.[index];if(!candidate)return;requestAction(`/api/scans/${current.scan_id}/confirm`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_id:candidate.item_id})})}
function runAction(action){if(action==='camera'){resumeLiveScanner();return}if(action==='confirm'){confirmMatch(0);return}const paths={reject:'reject',undo:'undo'};if(paths[action])requestAction(`/api/scans/${current.scan_id}/${paths[action]}`,{method:'POST'})}
function renderCapabilityNotice(){const messages=[];if(desktopCapabilities.barcode===false)messages.push('Tracking barcode decoding is unavailable on the desktop.');if(liveUnavailable)messages.push(liveFailure||'This page cannot open a live camera. Scan package will use the phone camera-photo fallback.');const notice=document.getElementById('capability-notice');notice.textContent=messages.join(' ');notice.classList.toggle('show',messages.length>0)}
function renderStatus(data){setConnection(true,'Desktop connected');const total=data.packages||0;const audited=data.audited||0;const remaining=data.remaining??Math.max(0,total-audited);document.getElementById('progress-text').textContent=`${audited} of ${total} audited`;document.getElementById('remaining-text').textContent=`${remaining} remaining`;document.getElementById('progress-fill').style.width=`${total?Math.round(audited/total*100):0}%`;const alerts=data.alerts||{};document.getElementById('summary').innerHTML=`<span class="chip">${alerts.not_found||0} not logged</span><span class="chip">${alerts.duplicate||0} duplicates</span><span class="chip">${alerts.review||0} review</span>`;desktopCapabilities=data.capabilities||{};renderCapabilityNotice()}
async function refresh(force=false){if(requestBusy&&!force)return;try{renderStatus(await api('/api/status'))}catch(error){renderFailure(error)}}
function normalizedTracking(value){return String(value||'').toUpperCase().replace(/[^A-Z0-9]/g,'')}
function setLiveLabel(message){liveLabel.textContent=message;scanHint.textContent=message}
function markClearFrame(){if(!awaitingClearTracking)return;clearFrames+=1;if(clearFrames>=2){awaitingClearTracking='';clearFrames=0}}
function scheduleLiveScan(delay=liveIntervalMs){clearTimeout(liveTimer);if(liveActive&&!livePaused)liveTimer=setTimeout(runLiveScan,delay)}
async function makeNativeDetector(){if(!window.BarcodeDetector)return null;try{const preferred=['code_128','qr_code','data_matrix','pdf417','aztec','maxi_code','code_39','code_93','codabar','itf'];const supported=BarcodeDetector.getSupportedFormats?await BarcodeDetector.getSupportedFormats():preferred;const formats=preferred.filter(format=>supported.includes(format));return formats.length?new BarcodeDetector({formats}):new BarcodeDetector()}catch(_error){return null}}
function captureLiveFrame(){if(liveVideo.readyState<2||!liveVideo.videoWidth)return Promise.resolve(null);const sourceWidth=Math.round(liveVideo.videoWidth*.94);const sourceHeight=Math.round(Math.min(liveVideo.videoHeight*.52,sourceWidth*.55));const sourceX=Math.round((liveVideo.videoWidth-sourceWidth)/2);const sourceY=Math.round((liveVideo.videoHeight-sourceHeight)/2);const scale=Math.min(1,liveFrameWidth/sourceWidth);const canvas=document.createElement('canvas');canvas.width=Math.max(320,Math.round(sourceWidth*scale));canvas.height=Math.max(120,Math.round(sourceHeight*scale));canvas.getContext('2d',{alpha:false}).drawImage(liveVideo,sourceX,sourceY,sourceWidth,sourceHeight,0,0,canvas.width,canvas.height);return new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.78))}
async function sendNativeDetections(detections){const barcodes=[];const formats=[];for(const detection of detections){const value=String(detection.rawValue||'').trim();if(value&&!barcodes.includes(value)){barcodes.push(value);formats.push(String(detection.format||''))}}if(!barcodes.length)return null;return api('/api/live/trackings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({barcodes,formats})},liveRequestTimeoutMs)}
async function sendLiveFrame(){const frame=await captureLiveFrame();if(!frame)return null;const form=new FormData();form.append('image',frame,'live-barcode.jpg');return api('/api/live/frame',{method:'POST',body:form},liveRequestTimeoutMs)}
function handleLiveResponse(data){if(!data)return;if(data.status==='searching'){if(data.detected){setLiveLabel('Barcode seen — hold steady…')}else{markClearFrame();setLiveLabel(awaitingClearTracking?'Move to the next package':'Point the guide at one tracking barcode')}return}const tracking=normalizedTracking(data.tracking);if(awaitingClearTracking&&tracking===awaitingClearTracking){setLiveLabel('Move to the next package');return}awaitingClearTracking='';clearFrames=0;livePaused=true;setLiveLabel('Barcode found');render(data);refresh(true)}
async function runLiveScan(){if(!liveActive||livePaused||liveRequestBusy||!desktopOnline||document.visibilityState==='hidden'){scheduleLiveScan(600);return}liveRequestBusy=true;try{let data=null;if(nativeDetector){try{const detections=await nativeDetector.detect(liveVideo);nativeDetectorFailures=0;if(detections.length){nativeEmptyFrames=0;data=await sendNativeDetections(detections)}else{nativeEmptyFrames+=1;markClearFrame()}}catch(_error){nativeDetectorFailures+=1;if(nativeDetectorFailures>=3)nativeDetector=null}}if((!nativeDetector||nativeEmptyFrames>=3)&&!data){nativeEmptyFrames=0;data=await sendLiveFrame()}handleLiveResponse(data)}catch(error){if(error.status===429){setLiveLabel('Desktop is busy — hold steady…')}else{livePaused=true;renderFailure(error)}}finally{liveRequestBusy=false;scheduleLiveScan()}}
async function startLiveScanner(){if(liveUnavailable){cameraInput.click();return}if(requestBusy||!desktopOnline)return;resultBox.classList.remove('show');setBusy(true,'Opening live camera…');try{liveStream=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:{ideal:'environment'},width:{ideal:1920},height:{ideal:1080}}});liveVideo.srcObject=liveStream;await liveVideo.play();nativeDetector=await makeNativeDetector();nativeDetectorFailures=0;nativeEmptyFrames=0;liveActive=true;livePaused=false;liveView.classList.add('show');setLiveLabel('Point the guide at one tracking barcode');scheduleLiveScan(100)}catch(_error){stopLiveScanner();liveUnavailable=true;liveFailure='Live camera access was blocked. Tap Scan package again for the phone camera-photo fallback, or allow camera access and reload this page.';renderCapabilityNotice()}finally{setBusy(false)}}
function stopLiveScanner(){clearTimeout(liveTimer);liveTimer=null;liveActive=false;livePaused=false;liveRequestBusy=false;if(liveStream)liveStream.getTracks().forEach(track=>track.stop());liveStream=null;liveVideo.srcObject=null;liveView.classList.remove('show');updateControls()}
function resumeLiveScanner(){const previousTracking=normalizedTracking(current?.tracking);if(previousTracking){awaitingClearTracking=previousTracking;clearFrames=0}current=null;resultBox.classList.remove('show');if(!liveActive){startLiveScanner();return}livePaused=false;setLiveLabel(awaitingClearTracking?'Move to the next package':'Point the guide at one tracking barcode');scheduleLiveScan(100)}
cameraButton.addEventListener('click',()=>liveActive?stopLiveScanner():startLiveScanner());libraryButton.addEventListener('click',()=>libraryInput.click());cameraInput.addEventListener('change',event=>{if(event.target.files[0])scan(event.target.files[0])});libraryInput.addEventListener('change',event=>{if(event.target.files[0])scan(event.target.files[0])});window.addEventListener('online',refresh);window.addEventListener('offline',()=>setConnection(false,'Phone is offline. Reconnect to the internet or Wi-Fi and retry.'));window.addEventListener('pagehide',stopLiveScanner);document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'){refresh();scheduleLiveScan(100)}});renderCapabilityNotice();refresh();setInterval(refresh,5000);
</script></body></html>
"""


def _is_local_address(value: str | None) -> bool:
    if not value:
        return False
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_private or address.is_loopback or address.is_link_local


def local_ip_address() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def create_scanner_app(
    coordinator: ScannerCoordinator,
    pairing_code: str,
    secret_key: str,
    pairing_expires_at: float | None = None,
    *,
    remote: bool = False,
) -> Flask:
    app = Flask(__name__)
    app.secret_key = secret_key
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_IMAGE_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=remote,
    )
    attempts: dict[str, deque[float]] = {}
    global_attempts: deque[float] = deque()
    pairing_attempt_lock = threading.Lock()
    upload_slots = threading.BoundedSemaphore(2)
    live_candidate_lock = threading.Lock()
    live_candidates: dict[str, tuple[str, int, float]] = {}
    pairing_expires_at = pairing_expires_at or (time.monotonic() + PAIRING_TTL_SECONDS)

    @app.before_request
    def local_network_only():
        if not _is_local_address(request.remote_addr):
            abort(403)

    @app.after_request
    def private_response_headers(response):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(self), microphone=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def paired() -> bool:
        return session.get("paired") is True and session.get("generation") == coordinator.generation

    def require_paired() -> None:
        if not paired():
            abort(401)
        coordinator.note_phone_activity(str(session.get("client_id", "")))

    def require_csrf() -> None:
        require_paired()
        if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf", "")):
            abort(403)

    def render_pair(*, code: str = "", error: str = ""):
        if remote:
            connection_help = (
                "Enter the temporary six-digit code shown on the desktop. "
                "This secure remote connection works across public Wi-Fi and VPNs."
            )
            privacy_message = (
                "Decoded tracking values or small camera crops travel through Cloudflare over HTTPS, "
                "are processed on the desktop, and are not saved by Package Audit."
            )
        else:
            connection_help = (
                "Enter the temporary six-digit code shown on the desktop. "
                "Both devices must be on the same trusted Wi-Fi."
            )
            privacy_message = (
                "Tracking values and camera images are processed on the desktop and are not saved."
            )
        return render_template_string(
            _PAIR_TEMPLATE,
            code=code,
            error=error,
            connection_help=connection_help,
            privacy_message=privacy_message,
        )

    @app.get("/")
    def index():
        if paired():
            return redirect(url_for("scanner"))
        return render_pair(code=request.args.get("pair", ""))

    @app.post("/pair")
    def pair():
        remote = request.remote_addr or "unknown"
        now = time.monotonic()
        if now > pairing_expires_at:
            return render_pair(
                error="The pairing code expired. Restart the phone scanner on the desktop."
            ), 410
        with pairing_attempt_lock:
            for address, history in list(attempts.items()):
                while history and now - history[0] > 60:
                    history.popleft()
                if not history:
                    attempts.pop(address, None)
            while global_attempts and now - global_attempts[0] > 60:
                global_attempts.popleft()
            recent = attempts.setdefault(remote, deque())
            if len(recent) >= 8 or len(global_attempts) >= 30:
                return render_pair(error="Try again in one minute."), 429
            recent.append(now)
            global_attempts.append(now)
        if not secrets.compare_digest(request.form.get("code", ""), pairing_code):
            return render_pair(error="That pairing code is not valid."), 403
        session.clear()
        session["paired"] = True
        session["generation"] = coordinator.generation
        session["csrf"] = secrets.token_urlsafe(24)
        session["client_id"] = secrets.token_hex(8)
        coordinator.note_phone_activity(session["client_id"])
        return redirect(url_for("scanner"))

    def live_searching(*, detected: bool) -> dict[str, Any]:
        return {
            "status": "searching",
            "detected": detected,
            "message": "Hold steady…" if detected else "Point the guide at one tracking barcode.",
        }

    def process_stable_live_observation(observation: ScanObservation) -> dict[str, Any]:
        """Require two matching live frames before creating an audit scan."""
        client_id = str(session.get("client_id", ""))
        now = time.monotonic()
        with live_candidate_lock:
            for candidate_client, (_key, _hits, seen_at) in list(live_candidates.items()):
                if now - seen_at > LIVE_STABILITY_SECONDS:
                    live_candidates.pop(candidate_client, None)
            if not observation.barcodes:
                live_candidates.pop(client_id, None)
                return live_searching(detected=False)
            previous_key, previous_hits, seen_at = live_candidates.get(client_id, ("", 0, 0.0))
            hits = (
                previous_hits + 1
                if previous_key == observation.scan_key and now - seen_at <= LIVE_STABILITY_SECONDS
                else 1
            )
            live_candidates[client_id] = (observation.scan_key, hits, now)
            if hits < LIVE_STABILITY_HITS:
                return live_searching(detected=True)
            live_candidates.pop(client_id, None)
        return coordinator.process_observation(
            observation,
            expected_generation=session["generation"],
        )

    @app.get("/scanner")
    def scanner():
        require_paired()
        privacy = (
            "Tracking-only lookup · encrypted Cloudflare route to your desktop"
            if remote
            else "Tracking-only lookup · paired directly with your desktop"
        )
        return render_template_string(
            _SCANNER_TEMPLATE,
            csrf=session["csrf"],
            scanner_privacy=privacy,
        )

    @app.get("/api/status")
    def status():
        require_paired()
        return jsonify(coordinator.status())

    @app.post("/api/scan")
    def scan():
        require_csrf()
        request_generation = session["generation"]
        uploaded = request.files.get("image")
        if uploaded is None:
            return jsonify(error="Choose a package-label image."), 400
        if not upload_slots.acquire(blocking=False):
            return jsonify(error="The scanner is busy. Try again in a moment."), 429
        try:
            return jsonify(
                coordinator.process_image(
                    uploaded.read(MAX_IMAGE_BYTES + 1),
                    expected_generation=request_generation,
                )
            )
        except ScannerSessionExpiredError as exc:
            return jsonify(error=str(exc)), 401
        except ScannerBusyError as exc:
            return jsonify(error=str(exc)), 429
        except (RuntimeError, ScanImageError) as exc:
            return jsonify(error=str(exc)), 422
        finally:
            upload_slots.release()

    @app.post("/api/live/trackings")
    def live_trackings():
        require_csrf()
        payload = request.get_json(silent=True) or {}
        raw_barcodes = payload.get("barcodes")
        raw_formats = payload.get("formats", [])
        if not isinstance(raw_barcodes, list) or not 1 <= len(raw_barcodes) <= MAX_LIVE_BARCODES:
            return jsonify(error="Send one live tracking-barcode detection."), 400
        if not isinstance(raw_formats, list) or len(raw_formats) > MAX_LIVE_BARCODES:
            return jsonify(error="The live barcode formats were invalid."), 400
        if any(
            not isinstance(value, str) or not value or len(value) > MAX_LIVE_BARCODE_LENGTH
            for value in raw_barcodes
        ):
            return jsonify(error="The live barcode value was invalid."), 400
        if any(not isinstance(value, str) or len(value) > 64 for value in raw_formats):
            return jsonify(error="The live barcode format was invalid."), 400
        observation = observation_from_barcodes(tuple(raw_barcodes), tuple(raw_formats))
        try:
            return jsonify(process_stable_live_observation(observation))
        except ScannerSessionExpiredError as exc:
            return jsonify(error=str(exc)), 401
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 422

    @app.post("/api/live/frame")
    def live_frame():
        require_csrf()
        uploaded = request.files.get("image")
        if uploaded is None:
            return jsonify(error="No live camera frame was received."), 400
        if not upload_slots.acquire(blocking=False):
            return jsonify(error="The scanner is busy. Keep the barcode steady."), 429
        try:
            observation = coordinator.observe_image(
                uploaded.read(MAX_IMAGE_BYTES + 1),
                expected_generation=session["generation"],
            )
            return jsonify(process_stable_live_observation(observation))
        except ScannerSessionExpiredError as exc:
            return jsonify(error=str(exc)), 401
        except ScannerBusyError as exc:
            return jsonify(error=str(exc)), 429
        except (RuntimeError, ScanImageError) as exc:
            return jsonify(error=str(exc)), 422
        finally:
            upload_slots.release()

    @app.post("/api/scans/<scan_id>/confirm")
    def confirm(scan_id: str):
        require_csrf()
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(coordinator.confirm(scan_id, str(payload.get("item_id", ""))))
        except (KeyError, ValueError) as exc:
            return jsonify(error=str(exc)), 400

    @app.post("/api/scans/<scan_id>/reject")
    def reject(scan_id: str):
        require_csrf()
        try:
            return jsonify(coordinator.reject(scan_id))
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.post("/api/scans/<scan_id>/not-found")
    def not_found(scan_id: str):
        require_csrf()
        try:
            return jsonify(coordinator.mark_not_found(scan_id))
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.post("/api/scans/<scan_id>/undo")
    def undo(scan_id: str):
        require_csrf()
        try:
            return jsonify(coordinator.undo(scan_id))
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.errorhandler(401)
    def unauthorized(_error):
        return jsonify(error="Pair this phone with the desktop first."), 401

    @app.errorhandler(403)
    def forbidden(_error):
        return jsonify(error="This scanner request was not authorized."), 403

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="The image is larger than 15 MB."), 413

    return app


class ScannerServer:
    """Background scanner server using either a LAN or temporary HTTPS address."""

    def __init__(
        self,
        coordinator: ScannerCoordinator,
        *,
        remote: bool = False,
        tunnel_factory=CloudflareQuickTunnel,
    ) -> None:
        self.coordinator = coordinator
        self.remote = remote
        self.pairing_code: str
        self._secret_key: str
        self._pairing_expires_at: float
        self._server: BaseWSGIServer | None = None
        self._thread: threading.Thread | None = None
        self._app: Flask | None = None
        self._tunnel_factory = tunnel_factory
        self._tunnel: CloudflareQuickTunnel | None = None
        self._public_url: str | None = None
        self.host_address = "127.0.0.1" if remote else local_ip_address()
        self._renew_pairing()

    def _renew_pairing(self) -> None:
        self.pairing_code = f"{secrets.randbelow(1_000_000):06d}"
        self._secret_key = secrets.token_urlsafe(32)
        self._pairing_expires_at = time.monotonic() + PAIRING_TTL_SECONDS

    @property
    def running(self) -> bool:
        local_running = bool(self._thread and self._thread.is_alive())
        return local_running and (not self.remote or self.tunnel_running)

    @property
    def tunnel_running(self) -> bool:
        return bool(self._tunnel and self._tunnel.running)

    @property
    def port(self) -> int:
        return int(self._server.server_port) if self._server else 0

    @property
    def url(self) -> str:
        if self.remote:
            if not self._public_url:
                raise RuntimeError("The remote scanner tunnel has not started.")
            # The fragment is filled into the pairing form by the browser and
            # is never sent to Cloudflare in the initial HTTP request.
            return f"{self._public_url}/#pair={self.pairing_code}"
        return f"http://{self.host_address}:{self.port}/?pair={self.pairing_code}"

    @property
    def pairing_seconds_remaining(self) -> int:
        return max(0, int(self._pairing_expires_at - time.monotonic()))

    def start(self) -> None:
        if self.running:
            return
        if self._server:
            self.stop()
        self._renew_pairing()
        self._app = create_scanner_app(
            self.coordinator,
            self.pairing_code,
            self._secret_key,
            self._pairing_expires_at,
            remote=self.remote,
        )
        # LAN mode accepts private-network traffic. Remote mode binds only to
        # loopback so the origin is reachable exclusively through cloudflared.
        # LAN binding is intentional and guarded by local_network_only().
        bind_address = "127.0.0.1" if self.remote else str(ipaddress.IPv4Address(0))
        try:
            self._server = make_server(
                bind_address,
                0,
                self._app,
                threaded=True,
                request_handler=_QuietRequestHandler,
            )
        except SystemExit as exc:
            # Werkzeug converts bind failures to SystemExit. A windowed app
            # must surface that as an ordinary startup error instead of
            # unexpectedly terminating the entire desktop process.
            self._server = None
            port_kind = "loopback" if self.remote else "local network"
            raise OSError(f"Could not open a {port_kind} port for the phone scanner.") from exc
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="package-scanner", daemon=True
        )
        self._thread.start()
        if self.remote:
            try:
                last_tunnel_error: TunnelStartupError | None = None
                for _attempt in range(2):
                    self._tunnel = self._tunnel_factory()
                    try:
                        self._public_url = self._tunnel.start(f"http://127.0.0.1:{self.port}")
                        break
                    except TunnelStartupError as exc:
                        last_tunnel_error = exc
                        self._tunnel.stop()
                        self._tunnel = None
                if not self._public_url:
                    if last_tunnel_error:
                        raise last_tunnel_error
                    raise TunnelStartupError("cloudflared did not provide a remote scanner address.")
                public_host = urlparse(self._public_url).hostname
                if public_host:
                    self._app.config["TRUSTED_HOSTS"] = [
                        public_host,
                        "127.0.0.1",
                        "localhost",
                    ]
            except Exception:
                self.stop()
                raise

    def stop(self) -> None:
        tunnel = self._tunnel
        self._tunnel = None
        self._public_url = None
        if tunnel:
            tunnel.stop()
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server:
            try:
                server.shutdown()
            except OSError:
                pass
            finally:
                try:
                    server.server_close()
                except OSError:
                    pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._app = None
