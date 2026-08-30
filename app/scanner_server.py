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

from flask import Flask, abort, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.serving import BaseWSGIServer, WSGIRequestHandler, make_server

from app.models import AuditEntry
from app.scanner_matching import (
    AdaptiveMatchModel,
    PackageMatcher,
    ScanDecision,
    ScanObservation,
)
from app.scanner_vision import MAX_IMAGE_BYTES, ScanImageError, analyze_image, scanner_capabilities

SCAN_CACHE_TTL_SECONDS = 30 * 60
MAX_STORED_SCANS = 100
PAIRING_TTL_SECONDS = 15 * 60
PHONE_ACTIVE_SECONDS = 15


class ScannerBusyError(RuntimeError):
    """Raised when all bounded image-processing slots are occupied."""


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
    suggested_item_id: str = ""
    model: dict[str, Any] | None = None


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
        self._rejected_scan_keys: set[str] = set()
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
    def model(self) -> AdaptiveMatchModel:
        with self._lock:
            return self._matcher.model

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def configure(
        self,
        pdf_hash: str,
        entries: list[AuditEntry],
        model_data: dict[str, Any] | None = None,
        *,
        reset_scans: bool = False,
    ) -> None:
        with self._lock:
            model = (
                AdaptiveMatchModel.from_dict(model_data) if model_data is not None else self._matcher.model
            )
            self._matcher = PackageMatcher(entries, model)
            if pdf_hash != self._pdf_hash or reset_scans:
                self._scans.clear()
                self._scan_ids_by_key.clear()
                self._rejected_scan_keys.clear()
            if pdf_hash != self._pdf_hash or reset_scans:
                self._generation += 1
                self._phone_clients.clear()
                while True:
                    try:
                        self._actions.get_nowait()
                    except queue.Empty:
                        break
            self._pdf_hash = pdf_hash

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
                "model_examples": self._matcher.model.examples,
            }

    def process_image(self, image_bytes: bytes) -> dict[str, Any]:
        if not self._scan_slots.acquire(timeout=0.1):
            raise ScannerBusyError("The scanner is busy. Try again in a moment.")
        try:
            return self.process_observation(analyze_image(image_bytes))
        finally:
            self._scan_slots.release()

    def process_observation(self, observation: ScanObservation) -> dict[str, Any]:
        with self._lock:
            if not self._pdf_hash or not self._matcher.records:
                raise RuntimeError("Open an audit PDF on the desktop before scanning.")
            self._prune_scans()
            existing_scan_id = self._scan_ids_by_key.get(observation.scan_key)
            if existing_scan_id:
                return self._response(self._scans[existing_scan_id], repeated=True)

            scan_id = secrets.token_hex(8)
            decision = self._matcher.decide(observation)
            if observation.scan_key in self._rejected_scan_keys and decision.status in {
                "matched",
                "already_matched",
                "not_found",
                "duplicate",
            }:
                ranked = self._matcher.rank(observation)
                candidates = tuple(
                    candidate
                    for candidate in ranked[:3]
                    if candidate.confidence >= self._matcher.review_threshold
                )
                decision = ScanDecision(
                    status="review" if candidates else "poor_scan",
                    confidence=candidates[0].confidence if candidates else 0.0,
                    message=(
                        "This same scan was previously marked Wrong. Choose the correct package "
                        "or retake the photo."
                    ),
                    candidates=candidates,
                    tracking=decision.tracking,
                    unit=decision.unit,
                    carrier=observation.carrier,
                    scan_key=observation.scan_key,
                    related_item_ids=tuple(candidate.item_id for candidate in candidates),
                )
            stored = _StoredScan(scan_id, observation, decision, time.monotonic())
            self._scans[scan_id] = stored
            self._scan_ids_by_key[observation.scan_key] = scan_id
            self._prune_scans()

            if decision.status == "matched":
                item_id = decision.related_item_ids[0]
                self._actions.put(
                    ScannerAction(
                        scan_id,
                        "match",
                        observation,
                        decision,
                        item_id,
                    )
                )
            elif decision.status == "already_matched":
                self._actions.put(
                    ScannerAction(scan_id, "event", observation, decision, decision.related_item_ids[0])
                )
            elif decision.status == "not_found":
                self._actions.put(ScannerAction(scan_id, "not_found", observation, decision))
            elif decision.status == "duplicate":
                self._actions.put(ScannerAction(scan_id, "duplicate", observation, decision))
            elif decision.status == "review":
                self._actions.put(ScannerAction(scan_id, "review", observation, decision))
            else:
                self._actions.put(ScannerAction(scan_id, "event", observation, decision))

            return self._response(stored)

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
    def _observed_unit(stored: _StoredScan) -> str:
        units = sorted(stored.observation.unit_tokens)
        return units[0] if len(units) == 1 else ""

    @staticmethod
    def _not_found_tracking(stored: _StoredScan) -> str:
        reliable = stored.observation.reliable_barcode_trackings
        if stored.decision.tracking in reliable:
            return stored.decision.tracking
        return reliable[0] if len(reliable) == 1 else ""

    @classmethod
    def _can_mark_not_found(cls, stored: _StoredScan) -> bool:
        if stored.undo_queued:
            return False
        if stored.decision.status in {"matched", "already_matched", "duplicate"} and not stored.rejected:
            return False
        return bool(cls._not_found_tracking(stored) or cls._observed_unit(stored))

    @staticmethod
    def _can_undo(stored: _StoredScan) -> bool:
        return not stored.undo_queued and stored.decision.status in {
            "matched",
            "not_found",
            "duplicate",
            "review",
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
            candidate_ids = {candidate.item_id for candidate in stored.decision.candidates}
            if item_id not in candidate_ids:
                raise ValueError("That package is not a candidate for this scan.")
            suggested = stored.decision.candidates[0].item_id if stored.decision.candidates else ""
            self._matcher.learn_selection(stored.observation, item_id, suggested)
            self._rejected_scan_keys.discard(stored.observation.scan_key)
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
                    suggested,
                    self._matcher.model.to_dict(),
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
            suggested = stored.decision.candidates[0].item_id if stored.decision.candidates else ""
            if suggested:
                self._matcher.learn_rejection(stored.observation, suggested)
            self._rejected_scan_keys.add(stored.observation.scan_key)
            self._scan_ids_by_key.pop(stored.observation.scan_key, None)
            stored.rejected = True
            stored.undo_queued = False
            self._actions.put(
                ScannerAction(
                    scan_id,
                    "reject",
                    stored.observation,
                    stored.decision,
                    suggested_item_id=suggested,
                    model=self._matcher.model.to_dict(),
                )
            )
            response = self._response(stored)
            response.update(
                status="rejected",
                message="The suggestion was rejected. Retake the photo or mark it Not logged.",
            )
            return response

    def mark_not_found(self, scan_id: str) -> dict[str, Any]:
        with self._lock:
            stored = self._require_scan(scan_id)
            if stored.decision.status == "not_found" and not stored.rejected:
                return self._response(stored, repeated=True)
            if not self._can_mark_not_found(stored):
                raise ValueError("Not enough reliable label information was read. Retake the photo instead.")
            tracking = self._not_found_tracking(stored)
            decision = ScanDecision(
                status="not_found",
                confidence=stored.decision.confidence,
                message="Package marked Not logged for desk investigation.",
                tracking=tracking,
                unit=self._observed_unit(stored),
                carrier=stored.observation.carrier,
                scan_key=stored.observation.scan_key,
            )
            stored.decision = decision
            stored.rejected = False
            stored.undo_queued = False
            self._actions.put(ScannerAction(scan_id, "not_found", stored.observation, decision))
            return self._response(stored)

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
</style></head><body><main class="panel"><h1>Pair package scanner</h1><p>Enter the temporary six-digit code shown on the desktop. Both devices must be on the same trusted Wi-Fi.</p><form method="post" action="/pair"><label for="pair-code">Pairing code</label><input id="pair-code" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" value="{{ code }}" autofocus required><button type="submit">Connect to desktop</button></form>{% if error %}<div class="error" role="alert">{{ error }}</div>{% endif %}<p class="privacy">Photos are processed on the desktop and are not saved.</p></main></body></html>
"""

_SCANNER_TEMPLATE = """
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="{{ csrf }}"><title>Package Scanner</title><style>
:root{color-scheme:light;font-family:ui-rounded,"SF Pro Rounded",system-ui,sans-serif;background:#eef2f4;color:#17212b}*{box-sizing:border-box}body{margin:0}.shell{max-width:640px;margin:auto;min-height:100svh;background:white;box-shadow:0 0 30px #20352d14}.top{background:#143c32;color:white;padding:18px 18px 16px}.title-row{display:flex;align-items:center;justify-content:space-between;gap:12px}.top h1{font-size:22px;margin:0}.connection{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:700}.dot{width:9px;height:9px;border-radius:50%;background:#e1a928}.connection.online .dot{background:#60d394}.connection.offline .dot{background:#ff8f80}.progress-card{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-top:15px}.progress-card strong{font-size:18px}.progress-card span{font-size:13px;color:#dbe9e3}.progress-track{height:6px;background:#ffffff30;border-radius:99px;overflow:hidden;margin-top:8px}.progress-fill{height:100%;width:0;background:#73d6a2;transition:width .25s ease}.summary{display:flex;gap:7px;margin-top:12px;flex-wrap:wrap}.chip{font-size:12px;background:#ffffff20;padding:6px 8px;border-radius:99px}main{padding:18px}.notice{display:none;border-radius:9px;padding:11px 12px;margin-bottom:13px;font-size:13px;line-height:1.4}.notice.show{display:block}.notice.warning{background:#fff8e8;color:#76510a}.notice.error{background:#fff3f1;color:#9f261c}.notice button{margin-top:8px}.upload-actions{display:grid;grid-template-columns:1fr auto;gap:9px}.upload-actions button,.actions button,.candidate,.notice button{min-height:48px;border-radius:9px;font-size:16px;font-weight:750}.primary{border:0;background:#176b4d;color:white;padding:14px 18px}.secondary{border:1px solid #aeb7c2;background:white;color:#28343f;padding:12px 14px}.upload-actions input{display:none}.hint{text-align:center;color:#66717d;font-size:13px;line-height:1.4;margin:10px 0 17px}.busy{display:none;text-align:center;padding:25px 10px;color:#4f5d68;font-weight:700}.busy.show{display:block}.spinner{display:inline-block;width:18px;height:18px;border:3px solid #c7d2cd;border-top-color:#176b4d;border-radius:50%;animation:spin .8s linear infinite;vertical-align:-4px;margin-right:7px}@keyframes spin{to{transform:rotate(360deg)}}.result{display:none;border:1px solid #d4dae1;border-left:6px solid #64748b;border-radius:10px;padding:16px;outline:none}.result.show{display:block}.result.matched,.result.already_matched{border-left-color:#16845b;background:#effaf5}.result.not_found{border-left-color:#d92d20;background:#fff3f1}.result.duplicate{border-left-color:#e78b09;background:#fff8e8}.result.review,.result.poor_scan,.result.rejected{border-left-color:#d9a300;background:#fffbea}.result.undo_queued{border-left-color:#64748b;background:#f4f6f8}.result h2{font-size:19px;line-height:1.25;margin:0 0 8px}.result p{margin:5px 0;color:#4c5864}.meta{display:flex;gap:7px;flex-wrap:wrap;margin-top:9px}.meta span{background:#ffffffa8;border-radius:99px;padding:5px 8px;font-size:13px}.candidate{width:100%;text-align:left;border:1px solid #b9c2cc;background:white;padding:12px;margin-top:9px}.candidate strong,.candidate span{display:block}.candidate .reason{font-size:12px;color:#66717d;margin-top:4px;font-weight:500}.actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}.actions button{flex:1;border:1px solid #aeb7c2;background:white;padding:11px 13px;color:#28343f}.actions .danger{color:#b42318;border-color:#e8a49e}.privacy{text-align:center;color:#71808c;font-size:12px;margin:18px 0 0}@media(max-width:360px){.upload-actions{grid-template-columns:1fr}.progress-card{align-items:flex-start;flex-direction:column;gap:2px}}@media(prefers-reduced-motion:reduce){.spinner{animation:none}.progress-fill{transition:none}}
</style></head><body><div class="shell"><header class="top"><div class="title-row"><h1>Package Scanner</h1><div id="connection" class="connection"><span class="dot"></span><span id="connection-text">Checking desktop…</span></div></div><div class="progress-card"><strong id="progress-text">Loading audit…</strong><span id="remaining-text"></span></div><div class="progress-track"><div id="progress-fill" class="progress-fill"></div></div><div class="summary" id="summary"></div></header><main><div id="connection-notice" class="notice error" role="alert"></div><div id="capability-notice" class="notice warning"></div><div class="upload-actions"><button id="camera-button" class="primary" type="button">Take package photo</button><button id="library-button" class="secondary" type="button">Choose photo</button><input id="camera-image" type="file" accept="image/*" capture="environment"><input id="library-image" type="file" accept="image/*"></div><div class="hint">Keep the tracking barcode, unit, and resident name visible. Photos stay on this network and are not saved.</div><div id="busy" class="busy" role="status" aria-live="polite"><span class="spinner"></span><span id="busy-text">Reading label…</span></div><section id="result" class="result" aria-live="polite" tabindex="-1"></section><p class="privacy">Paired directly with your desktop · no internet service required</p></main></div><script>
const csrf=document.querySelector('meta[name=csrf-token]').content;
const maximumUploadBytes=15*1024*1024;const resizeEdge=2200;const preferredBytes=4*1024*1024;
const cameraInput=document.getElementById('camera-image');const libraryInput=document.getElementById('library-image');const cameraButton=document.getElementById('camera-button');const libraryButton=document.getElementById('library-button');const busyBox=document.getElementById('busy');const busyText=document.getElementById('busy-text');const resultBox=document.getElementById('result');
let current=null;let requestBusy=false;let desktopOnline=false;
function esc(value){const node=document.createElement('div');node.textContent=value??'';return node.innerHTML}
function setBusy(value,message='Reading label…'){requestBusy=value;busyText.textContent=message;busyBox.classList.toggle('show',value);document.querySelector('main').setAttribute('aria-busy',String(value));updateControls()}
function updateControls(){const disabled=requestBusy||!desktopOnline;cameraButton.disabled=disabled;libraryButton.disabled=disabled;cameraInput.disabled=disabled;libraryInput.disabled=disabled;resultBox.querySelectorAll('button').forEach(button=>button.disabled=disabled)}
function setConnection(online,message,authEnded=false){desktopOnline=online;const box=document.getElementById('connection');box.className=`connection ${online?'online':'offline'}`;document.getElementById('connection-text').textContent=message;const notice=document.getElementById('connection-notice');if(online){notice.classList.remove('show');notice.innerHTML=''}else{notice.classList.add('show');notice.innerHTML=`${esc(message)}${authEnded?'<br><button type="button" class="secondary" data-connect-action="reconnect">Pair again</button>':'<br><button type="button" class="secondary" data-connect-action="retry">Retry connection</button>'}`;const action=notice.querySelector('button');action.addEventListener('click',()=>authEnded?window.location.assign('/'):refresh())}updateControls()}
async function api(path,options={}){options.headers={...(options.headers||{}),'X-CSRF-Token':csrf};let response;try{response=await fetch(path,options)}catch(_error){const error=new Error('Desktop unavailable. Check that both devices are on the same Wi-Fi and the scanner is still running.');error.network=true;throw error}const data=await response.json().catch(()=>({}));if(!response.ok){const error=new Error(data.error||`Request failed (${response.status})`);error.status=response.status;throw error}return data}
function actionButton(label,action,cls=''){return `<button type="button" class="${cls}" data-action="${action}">${esc(label)}</button>`}
function bindResultActions(){resultBox.querySelectorAll('[data-candidate]').forEach(button=>button.addEventListener('click',()=>confirmMatch(Number(button.dataset.candidate))));resultBox.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>runAction(button.dataset.action)));updateControls()}
function render(data){current=data;const allowed=['matched','already_matched','not_found','duplicate','review','poor_scan','rejected','undo_queued'];const status=allowed.includes(data.status)?data.status:'poor_scan';resultBox.className=`result show ${status}`;let html=`<h2>${esc(data.message||'Scan complete')}</h2><div class="meta">`;if(data.tracking)html+=`<span>Tracking …${esc(data.tracking.slice(-4))}</span>`;if(data.unit)html+=`<span>Unit ${esc(data.unit)}</span>`;if(data.confidence)html+=`<span>${Math.round(data.confidence*100)}% confidence</span>`;html+='</div>';if(data.repeated)html+='<p>This photo was already processed; no duplicate action was added.</p>';
if(status==='review'){html+=(data.candidates||[]).map((candidate,index)=>`<button type="button" class="candidate" data-candidate="${index}"><strong>${esc(candidate.unit)} · ${esc(candidate.resident)}</strong><span>Tracking …${esc(candidate.last4)} · ${Math.round(candidate.confidence*100)}%</span>${candidate.reasons?.length?`<span class="reason">Matched on ${esc(candidate.reasons.join(', '))}${candidate.audited?' · already audited':''}</span>`:''}</button>`).join('');html+=`<div class="actions">${data.can_mark_not_found?actionButton('None — not logged','not-found','danger'):''}${actionButton('Wrong — retake','reject')}</div>`}
else if(status==='already_matched'){html+=`<div class="actions">${actionButton('Scan next package','camera')}${actionButton('Wrong — retake','reject','danger')}</div>`}
else if(['matched','not_found','duplicate'].includes(status)){html+=`<div class="actions">${data.can_undo?actionButton('Undo','undo'):''}${actionButton('Wrong — retake','reject','danger')}</div>`}
else if(status==='rejected'){html+=`<div class="actions">${data.can_mark_not_found?actionButton('Mark not logged','not-found','danger'):''}${actionButton('Take another photo','camera')}</div>`}
else if(status==='poor_scan'){html+=`<div class="actions">${actionButton('Retake photo','camera')}${data.can_mark_not_found?actionButton('Mark not logged','not-found','danger'):''}</div>`}
else{html+=`<div class="actions">${actionButton('Scan next package','camera')}</div>`}resultBox.innerHTML=html;cameraButton.textContent='Take next package photo';bindResultActions();resultBox.focus({preventScroll:true});resultBox.scrollIntoView({behavior:'smooth',block:'nearest'});if(navigator.vibrate)navigator.vibrate(status==='matched'?[35]:status==='duplicate'||status==='not_found'?[70,50,70]:[30,40,30])}
function renderFailure(error){if(error.status===401){setConnection(false,'Pairing ended because the desktop audit changed or the scanner restarted.',true);return}if(error.network){setConnection(false,error.message,false);return}render({status:'poor_scan',message:error.message,confidence:0,can_mark_not_found:false})}
async function prepareImage(file){if(!window.createImageBitmap)return file;try{const bitmap=await createImageBitmap(file,{imageOrientation:'from-image'});const scale=Math.min(1,resizeEdge/Math.max(bitmap.width,bitmap.height));if(scale===1&&file.size<=preferredBytes){bitmap.close?.();return file}const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(bitmap.width*scale));canvas.height=Math.max(1,Math.round(bitmap.height*scale));canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close?.();const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',0.86));return blob||file}catch(_error){return file}}
async function scan(file){if(requestBusy||!desktopOnline)return;resultBox.classList.remove('show');setBusy(true,'Preparing photo…');try{const prepared=await prepareImage(file);if(prepared.size>maximumUploadBytes)throw new Error('This photo is too large. Retake it closer to the label.');setBusy(true,'Reading barcode and label…');const form=new FormData();form.append('image',prepared,'package-label.jpg');render(await api('/api/scan',{method:'POST',body:form}));await refresh(true)}catch(error){renderFailure(error)}finally{cameraInput.value='';libraryInput.value='';setBusy(false)}}
async function requestAction(path,options={}){if(requestBusy||!current?.scan_id)return;setBusy(true,'Updating desktop…');try{render(await api(path,options));await refresh(true)}catch(error){renderFailure(error)}finally{setBusy(false)}}
function confirmMatch(index){const candidate=current?.candidates?.[index];if(!candidate)return;requestAction(`/api/scans/${current.scan_id}/confirm`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_id:candidate.item_id})})}
function runAction(action){if(action==='camera'){cameraInput.click();return}const paths={reject:'reject','not-found':'not-found',undo:'undo'};if(paths[action])requestAction(`/api/scans/${current.scan_id}/${paths[action]}`,{method:'POST'})}
function renderStatus(data){setConnection(true,'Desktop connected');const total=data.packages||0;const audited=data.audited||0;const remaining=data.remaining??Math.max(0,total-audited);document.getElementById('progress-text').textContent=`${audited} of ${total} audited`;document.getElementById('remaining-text').textContent=`${remaining} remaining`;document.getElementById('progress-fill').style.width=`${total?Math.round(audited/total*100):0}%`;const alerts=data.alerts||{};document.getElementById('summary').innerHTML=`<span class="chip">${alerts.not_found||0} not logged</span><span class="chip">${alerts.duplicate||0} duplicates</span><span class="chip">${alerts.review||0} review</span>`;const capabilities=data.capabilities||{};const notice=document.getElementById('capability-notice');const issues=[];if(!capabilities.barcode)issues.push('Barcode decoding is unavailable');if(!capabilities.ocr)issues.push('Text matching is unavailable; barcode scanning still works');notice.textContent=issues.join('. ');notice.classList.toggle('show',issues.length>0)}
async function refresh(force=false){if(requestBusy&&!force)return;try{renderStatus(await api('/api/status'))}catch(error){renderFailure(error)}}
cameraButton.addEventListener('click',()=>cameraInput.click());libraryButton.addEventListener('click',()=>libraryInput.click());cameraInput.addEventListener('change',event=>{if(event.target.files[0])scan(event.target.files[0])});libraryInput.addEventListener('change',event=>{if(event.target.files[0])scan(event.target.files[0])});window.addEventListener('online',refresh);window.addEventListener('offline',()=>setConnection(false,'Phone is offline. Reconnect to Wi-Fi and retry.'));document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')refresh()});refresh();setInterval(refresh,5000);
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
) -> Flask:
    app = Flask(__name__)
    app.secret_key = secret_key
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_IMAGE_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )
    attempts: dict[str, deque[float]] = {}
    global_attempts: deque[float] = deque()
    pairing_attempt_lock = threading.Lock()
    upload_slots = threading.BoundedSemaphore(2)
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
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; "
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

    @app.get("/")
    def index():
        if paired():
            return redirect(url_for("scanner"))
        return render_template_string(_PAIR_TEMPLATE, code=request.args.get("pair", ""), error="")

    @app.post("/pair")
    def pair():
        remote = request.remote_addr or "unknown"
        now = time.monotonic()
        if now > pairing_expires_at:
            return render_template_string(
                _PAIR_TEMPLATE,
                code="",
                error="The pairing code expired. Restart the phone scanner on the desktop.",
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
                return render_template_string(_PAIR_TEMPLATE, code="", error="Try again in one minute."), 429
            recent.append(now)
            global_attempts.append(now)
        if not secrets.compare_digest(request.form.get("code", ""), pairing_code):
            return render_template_string(
                _PAIR_TEMPLATE, code="", error="That pairing code is not valid."
            ), 403
        session.clear()
        session["paired"] = True
        session["generation"] = coordinator.generation
        session["csrf"] = secrets.token_urlsafe(24)
        session["client_id"] = secrets.token_hex(8)
        coordinator.note_phone_activity(session["client_id"])
        return redirect(url_for("scanner"))

    @app.get("/scanner")
    def scanner():
        require_paired()
        return render_template_string(_SCANNER_TEMPLATE, csrf=session["csrf"])

    @app.get("/api/status")
    def status():
        require_paired()
        return jsonify(coordinator.status())

    @app.post("/api/scan")
    def scan():
        require_csrf()
        uploaded = request.files.get("image")
        if uploaded is None:
            return jsonify(error="Choose a package-label image."), 400
        if not upload_slots.acquire(blocking=False):
            return jsonify(error="The scanner is busy. Try again in a moment."), 429
        try:
            return jsonify(coordinator.process_image(uploaded.read(MAX_IMAGE_BYTES + 1)))
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
    """Background WSGI server bound to the local network only."""

    def __init__(self, coordinator: ScannerCoordinator) -> None:
        self.coordinator = coordinator
        self.pairing_code = f"{secrets.randbelow(1_000_000):06d}"
        self._secret_key = secrets.token_urlsafe(32)
        self._pairing_expires_at = time.monotonic() + PAIRING_TTL_SECONDS
        self._server: BaseWSGIServer | None = None
        self._thread: threading.Thread | None = None
        self.host_address = local_ip_address()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def port(self) -> int:
        return int(self._server.server_port) if self._server else 0

    @property
    def url(self) -> str:
        return f"http://{self.host_address}:{self.port}/?pair={self.pairing_code}"

    @property
    def pairing_seconds_remaining(self) -> int:
        return max(0, int(self._pairing_expires_at - time.monotonic()))

    def start(self) -> None:
        if self.running:
            return
        if self._server:
            self.stop()
        app = create_scanner_app(
            self.coordinator,
            self.pairing_code,
            self._secret_key,
            self._pairing_expires_at,
        )
        # Phone access requires a LAN binding; before_request rejects non-local source addresses.
        self._server = make_server(
            "0.0.0.0",  # nosec B104
            0,
            app,
            threaded=True,
            request_handler=_QuietRequestHandler,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="package-scanner", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
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
