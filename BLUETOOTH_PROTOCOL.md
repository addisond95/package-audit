# Offline scanner protocol v1

## Components and data boundaries

Android CameraX supplies in-memory frames to bundled ML Kit barcode/text recognizers, with ZXing as an
independent barcode fallback. Stable decoded barcode sets plus normalized unit candidates/confidences go to
the Mac. No image is sent for normal confirmation; one label image is sent only when the operator chooses the
separate training-confirmation button. The Java BLE central connects to a Swift CoreBluetooth peripheral. The helper
only frames bytes and sends newline-delimited JSON over private QProcess stdin/stdout. Python authenticates,
decrypts, calls the existing tracking matcher, and persists actions on the Qt thread before acknowledging saves.
No web server starts in Bluetooth mode.

The app requests Camera, Nearby devices (BLE scan/connect), and vibration permissions. It has no INTERNET,
storage, location, microphone, analytics, or remotely downloaded vision-model dependency. Routine camera frames,
pairing credentials, and pending messages are held in RAM. A selected training capture briefly uses private app
cache, is compressed into memory, and is deleted after preparation; the memory buffer is overwritten after the
upload finishes or fails. Android backup is disabled and data extraction
rules exclude app data; screenshots/recents previews are blocked with FLAG_SECURE. Audit records remain in
the existing Mac SQLite database. Bluetooth mode does not log packet contents or pairing keys.

## Pairing and transport

The Mac creates a random 128-bit service UUID and random 32-byte pre-shared key for each scanner session.
The QR is `packageaudit:` followed by compact JSON containing `v:1`, `service:<UUID>`, and `key:<base64>`.
Treat the QR as an access credential. It disappears while a phone is authenticated and is revoked when the
scanner stops or the audit changes. No OS-level Bluetooth bond is required.

RX characteristic `690c9e36-68e1-4367-aad4-98b4e78d0001` accepts writes with response.
TX characteristic `690c9e36-68e1-4367-aad4-98b4e78d0002` provides notifications. Android subscribes to TX
before sending anything. A single subscribed central is served at a time. UTF-8 JSON records end in LF;
records are split to the negotiated ATT MTU/central notification limit, and reassembled with a 16 KiB
record limit. A routine scan contains no image. When the operator explicitly chooses **Confirm + training**, one
compressed label JPEG of at most 512 KiB is divided into authenticated 6 KiB application chunks and saved into
the Mac's encrypted Pending training dataset after the audit confirmation succeeds. Android serializes
characteristic writes, capped at 512 bytes even when Android negotiates MTU 517; Swift handles notification
backpressure. The Mac advertises just the session UUID so it fits the primary advertisement; Apple's overflow
service area is not discoverable by Android ([Apple advertising documentation](https://developer.apple.com/documentation/corebluetooth/cbperipheralmanager/startadvertising(_:))).

## Application-layer authenticated encryption

All binary strings use standard padded Base64. `P` is the UTF-8 byte string `package-audit-ble-v1`.

1. Android sends plaintext `{"hello":base64(C)}` with fresh random 32-byte nonce C.
2. Mac generates fresh random 32-byte nonce S and replies with plaintext
   `{"welcome":base64(S),"proof":base64(HMAC-SHA256(PSK,P || C || S))}`.
3. Android verifies the proof with a constant-time comparison. Both derive 64 bytes with HKDF-SHA256:
   IKM=PSK, salt=C||S, info=P. Bytes 0–31 are the client-to-server AES-256 key; bytes 32–63 the reverse key.
4. Subsequent envelopes are `{"seq":n,"data":base64(ciphertext || 16-byte GCM tag)}`. Each direction starts
   n=1 and accepts only the next integer. AES-GCM nonce is four zero bytes followed by n as unsigned 64-bit
   big-endian; AAD=P. The plaintext is a JSON object. Fresh handshakes mean fresh directional keys on reconnect.
5. The first valid encrypted Android request proves possession of the PSK to the Mac. No audit information
   or actions are available before this verification. Invalid authentication, sequence, framing, or generation
   resets the connection; it never produces a saved acknowledgment.

The Mac accepts a new hello even if CoreBluetooth retained the subscription and did not report a disconnect.
Each hello replaces the directional keys using a fresh server nonce and clears authenticated status until a
valid encrypted request arrives. Old ciphertext cannot authenticate under the replacement keys. Android can
reconnect directly to the peripheral discovered for the current pairing session, then falls back to discovery.

This is a small custom PSK protocol using standard cryptographic primitives, not a claim of a formal security
audit. It has no forward secrecy: later PSK compromise can expose recorded traffic for that scanner session.
Radio identifiers, timing, traffic length, and presence remain observable. Radio interference or an unwanted
subscriber can deny service. OS/device compromise and someone photographing the pairing QR are outside the
protection provided by the encrypted link. Stop/restart the scanner if the QR may have leaked.

## Requests, saves, and retries

Encrypted requests include `id` (a UUID, maximum 64 characters) and `op`. Operations are `status`, `scan`
(aligned `barcodes`/`formats`, at most eight, each barcode at most 1,024 characters and all barcode data at
most 4,096 UTF-8 bytes, plus optional aligned `units`/`unit_confidences`, at most twelve, and optional signed-model
advisory fields `model_carrier`, `model_carrier_confidence`, `label_condition`,
`label_condition_confidence`, and `model_release`), `prepare_alert`
(the same decoded fields), `save_manual_alert` (`scan_id`, selected `tracking`, approved package `location`),
`confirm` (`scan_id`, `item_id`), `not_found`, `flag_review`, `reject`, `undo` (`scan_id`), and the bounded
`training_begin`/`training_chunk`/`training_finish` sequence. Training uploads declare their size and SHA-256,
must arrive in order, and are integrity-checked before the normal encrypted training store accepts them. Omitting `units`
is an intentional tracking-only scan. The two flag operations preserve unmatched-tracking and unit-review
alerts without marking the audit row present. Native unit outcomes include `verified`, `unit_not_read`, and
`unit_mismatch`; an unreadable unit can still be confirmed manually by the operator. Only a model carrier
prediction at or above 0.85 is accepted, and only when deterministic carrier recognition remains unknown.
Model fields never select an audit row: an exact tracking match remains mandatory. The model release is verified
inside Android from assets protected by the APK update signature; the Mac still treats received model output as
untrusted advisory input and validates its type, size, range, and use.
The two-stage alert operation lets the phone present explicit tracking choices when a physical label contains
multiple usable shipping codes. Oversized or excessive decoded label data is rejected without expiring the
authenticated Bluetooth session.
Responses contain the same id, `ok`, and either `result` or `error`. Audit results reuse the existing scanner
schema, including logged/scanned units, package location, and exact tracking candidates, with `saved` and `progress` added. `confirm`
is only a suggestion; `matched` with `saved:true` acknowledges a confirmed and persisted row.

Python drains each action batch inside one SQLite transaction and verifies saved state/event status before
responding. A failed batch rolls back the database and in-memory presence/undo/learning state and retains
the actions for retry. An exception or failed verification is not acknowledged as saved. There is no distributed
transaction across devices, so check the desktop after a process crash or unresolved save failure.

Android retains one outstanding request and at most one queued action behind a status check. A missing
acknowledgment triggers reconnection after 10 seconds. A fresh authenticated channel retransmits the same
request ID and body. The Mac caches the last 2,048 request fingerprints/responses across transport reconnects;
the same ID/body returns the original response, while a changed body with that ID is rejected. These records
are in memory, scoped to the scanner session, and are not an unlimited/durable exactly-once guarantee.

Status heartbeats run every five seconds while foregrounded and no action is pending. The helper expires
an idle subscriber after 30 seconds. Reconnect attempts are bounded; the user can tap Reconnect to try again.
Backgrounding closes the phone transport, preserving an in-memory pending operation. App process restarts,
scanner restarts, and switching audits require checking pending state on the Mac and pairing again.

## Verification

`tests/test_bluetooth_protocol.py` covers cross-language cryptographic vectors, wrong keys, tampering,
replays, invalid requests, save failures, reconnect idempotence, and audit revocation. The main-window
integration test checks actual SQLite state, a single confirmation event after reconnect, and undo.
Android unit tests exercise the same crypto vectors and offline generated Code 128, QR, Data Matrix,
PDF417, rotation, inversion, empty frames, and multi-label decoding. The Mac helper also needs compilation
and a radio advertising check. The physical checklist in BLUETOOTH_USAGE.md remains mandatory before relying
on the S24/Mac connection for an audit.
