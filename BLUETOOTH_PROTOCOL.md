# Offline scanner protocol v1

## Components and data boundaries

Android CameraX supplies in-memory luminance frames to bundled ZXing. Stable decoded barcode sets go to
the Mac; images do not. The Java BLE central connects to a Swift CoreBluetooth peripheral. The helper
only frames bytes and sends newline-delimited JSON over private QProcess stdin/stdout. Python authenticates,
decrypts, calls the existing tracking matcher, and persists actions on the Qt thread before acknowledging saves.
No web server or Cloudflare tunnel starts in Bluetooth mode.

The app requests Camera, Nearby devices (BLE scan/connect), and vibration permissions. It has no INTERNET,
storage, location, microphone, analytics, or remotely downloaded barcode-model dependency. Camera frames,
pairing credentials, and pending messages are held in RAM. Android backup is disabled and data extraction
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
record limit. There are no image transfers. Android serializes characteristic writes; Swift handles notification
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

This is a small custom PSK protocol using standard cryptographic primitives, not a claim of a formal security
audit. It has no forward secrecy: later PSK compromise can expose recorded traffic for that scanner session.
Radio identifiers, timing, traffic length, and presence remain observable. Radio interference or an unwanted
subscriber can deny service. OS/device compromise and someone photographing the pairing QR are outside the
protection provided by the encrypted link. Stop/restart the scanner if the QR may have leaked.

## Requests, saves, and retries

Encrypted requests include `id` (a UUID, maximum 64 characters) and `op`. Operations are `status`, `scan`
(aligned `barcodes`/`formats`, at most eight), `confirm` (`scan_id`, `item_id`), `reject`, and `undo` (`scan_id`).
Responses contain the same id, `ok`, and either `result` or `error`. Audit results reuse the existing scanner
schema, including logged unit and tracking-only candidates, with `saved` and `progress` added. `confirm`
is only a suggestion; `matched` with `saved:true` acknowledges a confirmed and persisted row.

Python drains the action queue synchronously and verifies saved SQLite state/event status before responding.
An exception or failed verification is not acknowledged as saved; a disk failure can leave a partial action
which must be checked on the desktop. There is no distributed transaction across devices.

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
