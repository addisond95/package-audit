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
            if pdf_hash != self._pdf_hash:
                self._generation += 1
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

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._prune_scans()
            return {
                "audit_loaded": bool(self._pdf_hash and self._matcher.records),
                "packages": len(self._matcher.records),
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
            stored = _StoredScan(scan_id, observation, decision, time.time())
            self._scans[scan_id] = stored
            self._scan_ids_by_key[observation.scan_key] = scan_id
            self._prune_scans()

            if decision.status == "matched":
                item_id = decision.related_item_ids[0]
                self._matcher.learn_selection(observation, item_id)
                self._actions.put(
                    ScannerAction(
                        scan_id,
                        "match",
                        observation,
                        decision,
                        item_id,
                        model=self._matcher.model.to_dict(),
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
        cutoff = time.time() - SCAN_CACHE_TTL_SECONDS
        expired = [stored for stored in self._scans.values() if stored.created_at < cutoff]
        overflow = max(0, len(self._scans) - MAX_STORED_SCANS)
        oldest = sorted(self._scans.values(), key=lambda value: value.created_at)[:overflow]
        removals = {stored.scan_id: stored for stored in (*expired, *oldest)}
        for stored in removals.values():
            self._scans.pop(stored.scan_id, None)
            self._scan_ids_by_key.pop(stored.observation.scan_key, None)

    @staticmethod
    def _response(stored: _StoredScan, *, repeated: bool = False) -> dict[str, Any]:
        response = stored.decision.to_dict()
        response["scan_id"] = stored.scan_id
        response["repeated"] = repeated
        return response

    def confirm(self, scan_id: str, item_id: str) -> dict[str, Any]:
        with self._lock:
            stored = self._require_scan(scan_id)
            candidate_ids = {candidate.item_id for candidate in stored.decision.candidates}
            if item_id not in candidate_ids:
                raise ValueError("That package is not a candidate for this scan.")
            suggested = stored.decision.candidates[0].item_id if stored.decision.candidates else ""
            self._matcher.learn_selection(stored.observation, item_id, suggested)
            self._rejected_scan_keys.discard(stored.observation.scan_key)
            selected = self._matcher.records_by_id[item_id]
            decision = ScanDecision(
                status="matched",
                confidence=next(
                    candidate.confidence
                    for candidate in stored.decision.candidates
                    if candidate.item_id == item_id
                ),
                message="Package confirmed and marked here.",
                candidates=stored.decision.candidates,
                tracking=stored.decision.tracking,
                unit=selected.unit,
                carrier=stored.decision.carrier,
                scan_key=stored.decision.scan_key,
                related_item_ids=(item_id,),
            )
            stored.decision = decision
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
            suggested = stored.decision.candidates[0].item_id if stored.decision.candidates else ""
            if suggested:
                self._matcher.learn_rejection(stored.observation, suggested)
            self._rejected_scan_keys.add(stored.observation.scan_key)
            self._scan_ids_by_key.pop(stored.observation.scan_key, None)
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
            return {
                "scan_id": scan_id,
                "status": "rejected",
                "message": "The suggestion was rejected. Retake the photo or mark it Not logged.",
            }

    def mark_not_found(self, scan_id: str) -> dict[str, Any]:
        with self._lock:
            stored = self._require_scan(scan_id)
            tracking = stored.decision.tracking or (
                stored.observation.trackings[0] if stored.observation.trackings else ""
            )
            decision = ScanDecision(
                status="not_found",
                confidence=stored.decision.confidence,
                message="Package marked Not logged for desk investigation.",
                tracking=tracking,
                unit=stored.decision.unit,
                carrier=stored.observation.carrier,
                scan_key=stored.observation.scan_key,
            )
            stored.decision = decision
            self._actions.put(ScannerAction(scan_id, "not_found", stored.observation, decision))
            return self._response(stored)

    def undo(self, scan_id: str) -> dict[str, str]:
        with self._lock:
            stored = self._require_scan(scan_id)
            self._scan_ids_by_key.pop(stored.observation.scan_key, None)
            self._actions.put(ScannerAction(scan_id, "undo", stored.observation, stored.decision))
            return {"scan_id": scan_id, "status": "undo_queued", "message": "Undo sent to desktop."}

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
<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair Package Scanner</title><style>
:root{color-scheme:light;font-family:ui-rounded,"SF Pro Rounded",sans-serif;background:#f3f5f7;color:#17212b}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px}.panel{width:min(440px,100%);background:white;border:1px solid #d9dee5;border-radius:8px;padding:24px;box-sizing:border-box}
h1{font-size:24px;margin:0 0 8px}p{color:#5a6572;margin:0 0 18px}input{width:100%;box-sizing:border-box;font-size:28px;letter-spacing:8px;text-align:center;padding:14px;border:1px solid #aeb7c2;border-radius:6px}button{width:100%;margin-top:12px;padding:14px;border:0;border-radius:6px;background:#176b4d;color:white;font-size:17px;font-weight:700}.error{color:#b42318;margin-top:12px}
</style><body><main class="panel"><h1>Pair scanner</h1><p>Enter the temporary code shown on the desktop.</p><form method="post" action="/pair"><input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" value="{{ code }}" autofocus><button>Connect</button></form>{% if error %}<div class="error">{{ error }}</div>{% endif %}</main></body></html>
"""

_SCANNER_TEMPLATE = """
<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="csrf-token" content="{{ csrf }}"><title>Package Scanner</title><style>
:root{font-family:ui-rounded,"SF Pro Rounded",sans-serif;background:#eef2f4;color:#17212b}*{box-sizing:border-box}body{margin:0}.shell{max-width:640px;margin:auto;min-height:100vh;background:white}.top{background:#143c32;color:white;padding:18px 20px}.top h1{font-size:22px;margin:0}.summary{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}.chip{font-size:13px;background:#ffffff24;padding:6px 9px;border-radius:5px}main{padding:18px}.capture{display:block;text-align:center;background:#176b4d;color:white;padding:18px;border-radius:7px;font-size:19px;font-weight:750}.capture input{display:none}.hint{text-align:center;color:#66717d;font-size:13px;margin:10px 0 18px}.result{display:none;border:1px solid #d4dae1;border-left:6px solid #64748b;border-radius:6px;padding:16px}.result.show{display:block}.result.matched{border-left-color:#16845b;background:#effaf5}.result.not_found{border-left-color:#d92d20;background:#fff3f1}.result.duplicate{border-left-color:#e78b09;background:#fff8e8}.result.review,.result.poor_scan{border-left-color:#d9a300;background:#fffbea}.result h2{font-size:20px;margin:0 0 7px}.result p{margin:5px 0;color:#4c5864}.candidate{width:100%;text-align:left;border:1px solid #b9c2cc;background:white;padding:13px;margin-top:9px;border-radius:6px}.candidate strong{display:block;font-size:16px}.actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}.actions button{border:1px solid #aeb7c2;background:white;padding:11px 13px;border-radius:6px;font-weight:650}.actions .danger{color:#b42318;border-color:#e8a49e}.busy{display:none;text-align:center;padding:28px;color:#5c6874}.busy.show{display:block}
</style><body><div class="shell"><header class="top"><h1>Package Scanner</h1><div class="summary" id="summary"></div></header><main><label class="capture">Scan package<input id="image" type="file" accept="image/*" capture="environment"></label><div class="hint">Hold the label flat and fill the frame.</div><div id="busy" class="busy">Reading label...</div><section id="result" class="result"></section></main></div><script>
const csrf=document.querySelector('meta[name=csrf-token]').content;let current=null;
async function api(path,options={}){options.headers={...(options.headers||{}),'X-CSRF-Token':csrf};const response=await fetch(path,options);const data=await response.json();if(!response.ok)throw new Error(data.error||'Request failed');return data}
function esc(value){const node=document.createElement('div');node.textContent=value||'';return node.innerHTML}
function button(label,action,cls=''){return `<button class="${cls}" onclick="${action}">${esc(label)}</button>`}
function render(data){current=data;const box=document.getElementById('result');box.className=`result show ${data.status}`;let html=`<h2>${esc(data.message)}</h2>`;if(data.tracking)html+=`<p>Tracking: ...${esc(data.tracking.slice(-4))}</p>`;if(data.unit)html+=`<p>Unit: ${esc(data.unit)}</p>`;if(data.confidence)html+=`<p>Confidence: ${Math.round(data.confidence*100)}%</p>`;
if(data.status==='review'){html+=data.candidates.map(c=>`<button class="candidate" onclick="confirmMatch('${c.item_id}')"><strong>${esc(c.unit)} - ${esc(c.resident)}</strong>...${esc(c.last4)} - ${Math.round(c.confidence*100)}%</button>`).join('');html+=`<div class="actions">${button('None - Not logged','markNotFound()','danger')}${button('Wrong / rescan','reject()')}</div>`}
else if(['matched','already_matched','not_found','duplicate'].includes(data.status)){html+=`<div class="actions">${button('Undo','undo()')}${button('Wrong','reject()','danger')}</div>`}
else{html+=`<div class="actions">${button('Not logged','markNotFound()','danger')}</div>`}box.innerHTML=html}
async function scan(file){document.getElementById('busy').classList.add('show');document.getElementById('result').classList.remove('show');const form=new FormData();form.append('image',file);try{render(await api('/api/scan',{method:'POST',body:form}))}catch(error){render({status:'poor_scan',message:error.message,confidence:0})}finally{document.getElementById('busy').classList.remove('show');document.getElementById('image').value=''}}
async function confirmMatch(id){render(await api(`/api/scans/${current.scan_id}/confirm`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_id:id})}))}
async function reject(){render(await api(`/api/scans/${current.scan_id}/reject`,{method:'POST'}))}
async function markNotFound(){render(await api(`/api/scans/${current.scan_id}/not-found`,{method:'POST'}))}
async function undo(){render(await api(`/api/scans/${current.scan_id}/undo`,{method:'POST'}))}
async function refresh(){try{const data=await api('/api/status');const alerts=data.alerts||{};document.getElementById('summary').innerHTML=`<span class="chip">${data.packages||0} packages</span><span class="chip">${alerts.not_found||0} not logged</span><span class="chip">${alerts.duplicate||0} duplicates</span><span class="chip">${alerts.review||0} review</span>`}catch(_error){}}
document.getElementById('image').addEventListener('change',event=>{if(event.target.files[0])scan(event.target.files[0])});refresh();setInterval(refresh,5000);
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
        recent = attempts.setdefault(remote, deque())
        while recent and now - recent[0] > 60:
            recent.popleft()
        while global_attempts and now - global_attempts[0] > 60:
            global_attempts.popleft()
        if now > pairing_expires_at:
            return render_template_string(
                _PAIR_TEMPLATE,
                code="",
                error="The pairing code expired. Restart the phone scanner on the desktop.",
            ), 410
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

    @app.post("/api/scans/<scan_id>/not-found")
    def not_found(scan_id: str):
        require_csrf()
        try:
            return jsonify(coordinator.mark_not_found(scan_id))
        except KeyError as exc:
            return jsonify(error=str(exc)), 404

    @app.post("/api/scans/<scan_id>/undo")
    def undo(scan_id: str):
        require_csrf()
        try:
            return jsonify(coordinator.undo(scan_id))
        except KeyError as exc:
            return jsonify(error=str(exc)), 404

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
        self._server = make_server(
            "0.0.0.0",
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
