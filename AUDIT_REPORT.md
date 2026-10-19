# Package Audit — audit and repair report

Reviewed September 26–27, 2026, against the working tree supplied in this session.

The project imports BuildingLink package logs into a local desktop audit, collects physical-package
confirmations from Android over Bluetooth or an optional browser scanner, resolves exceptions, and exports
reports. Its separate training workflow collects explicitly selected label images, encrypts and annotates
them locally, and trains signed offline models. The audit covered those workflows, their shared persistence
and matching rules, Android lifecycle behavior, the Swift Bluetooth bridge, and packaging.

The existing uncommitted work was preserved. The pre-audit source snapshot is at
`/tmp/package-audit-review/source-before.tar.gz`. Tests used temporary databases, generated labels, and an
isolated Android debug package. Production audit records and collected label images were not used as test data.

## Removal and cleanup follow-up

At the user's request, the Cloudflare remote scanner was removed entirely: toolbar control, tunnel manager,
remote server configuration/retries, remote-only browser script, smoke-test command, setup instructions,
and dedicated tests. Bluetooth and the same-network browser scanner remain supported.

Reference checks also identified and removed unused database tracking-history/count helpers, redundant
last-four wrappers, old audit-metric aliases, an unread annotation-state field, an unused packaging import,
and duplicate ignore entries. Database migrations and legacy row-identity handling remain in use.
The Python 3.10 test path now declares and uses `tomli`, since `tomllib` is available only on newer Python.
Continuous integration now includes scripts in its Bandit scan.

The cleanup snapshot and logs are in `/tmp/package-audit-cleanup/`. The Python wheel and Mac bundle's
module inventory were checked to ensure the deleted tunnel module is absent. The Mac bundle was rebuilt
after removal, and the checks below were rerun against the cleaned tree.

## Findings fixed

Priority describes the potential impact of the original behavior, not an outstanding defect.

| Priority | Finding and repaired behavior | Main implementation |
| --- | --- | --- |
| High | A Bluetooth reconnect could remain stuck because CoreBluetooth did not report the disconnect the Python session required. Fresh handshakes now replace keys and clear authentication even when the subscription survives; request deduplication survives reconnects. Android also retries the device already discovered for that pairing. | `app/bluetooth_protocol.py`, `app/bluetooth_scanner.py`, `BleClient.java` |
| High | Large training-upload messages failed on the connected phone. At MTU 517 the sender used 514-byte writes, exceeding the 512-byte GATT attribute limit. Writes now obey both limits. Multi-chunk transfer passed over the actual phone/Mac radio link. | `BleClient.java` |
| High | A scanner save failure could leave part of a batch committed, memory claiming presence, or drained work lost. Nested savepoints and an outer batch transaction now roll back failed batches, restore memory/undo/learning state, and retain actions for retry. Failure during preparatory database reads also retains the queue. Manual presence changes now save before changing memory. | `app/database.py`, `app/main_window.py` |
| High | An overlong barcode could be split into a valid-looking prefix and match the wrong audit tracking number. Token boundaries now prevent truncated exact matches. | `app/scanner_matching.py` |
| High | BuildingLink IDs following tightly spaced markers such as `AMZ -#1234567890` could be treated as tracking numbers. Carrier, last-four, and scanner parsing now share the same package-field split. | `app/models.py`, `app/scanner_matching.py` |
| High | Learned OCR aliases could equate different numeric apartments; weak incidental readings could override explicit conflicting units. Distinct numeric identities cannot be aliases, low-confidence readings cannot verify a unit, and conflicting strong readings require review. Learning excludes unrelated numbers from confirmed scans. | `app/unit_matching.py`, `app/scanner_learning.py` |
| High | Overlapping tracking aliases across three rows could produce different desktop, phone, and report counts. All three now use connected groups with the earliest row canonical; scanning an alias affects the complete duplicate group and undo restores its prior presence state. | `app/scanner_matching.py`, `app/audit_workflow.py`, `app/main_window.py` |
| High | Non-finite release metrics, including NaN, could pass model approval comparisons. Approval now requires finite numeric metrics, valid ranges, and integer sample counts. | `app/model_release.py` |
| High | Held-out images could be evaluated again by freezing another dataset version; identical images could also cross splits under different capture groups. Frozen versions reject cross-split image duplicates, and final evaluations atomically reserve image hashes before opening test pixels, including reservations from legacy evaluation records. | `app/label_training.py`, `app/model_training.py` |
| High | Losing an existing dataset key could silently create a replacement key, leaving existing encrypted records unreadable. The store now refuses replacement when prior data exists and creates new keys exclusively with restricted permissions. | `app/label_training.py` |
| Medium | Android fed grayscale camera luminance to a model trained on RGB images. Inference now converts camera frames to RGB, applies crop/rotation, and uses the matching normalization. Native model-session options are closed. | `LabelModel.java` |
| Medium | Android tracking/location choice dialogs combined a message with a list, hiding the choices. The dialogs now display their selectable lists. Both lists were checked on the connected phone. | `MainActivity.java` |
| Medium | Returning from the background with a confirmation pending could leave the camera stopped. Camera startup now includes pending results, and training confirmation requires an active camera. The background/resume path passed on the phone. | `MainActivity.java` |
| Medium | Stale OCR readings could carry into later packages, and more than twelve readings could cause the receiver to reject an otherwise usable scan. Empty-frame transitions clear stale candidates; payloads retain at most twelve strongest valid aligned readings. | `MainActivity.java`, `ScanPayload.java` |
| Medium | A failed action submission could leave the training-confirmation workflow waiting indefinitely. Submission failure is now surfaced. Automatic training JPEG preparation also honors EXIF rotation. | `MainActivity.java` |
| Medium | Cached scanner decisions ignored changes in barcode format, unit confidence, and desktop presence state. Cache identity/invalidation now includes them; pruning an older scan cannot remove a newer mapping. Expired scan actions are rejected without depending on a heartbeat. | `app/scanner_matching.py`, `app/scanner_server.py` |
| Medium | Rejected confirmations and undone scans could still accept certain later actions. The state transitions now reject those stale actions and expose appropriate undo availability. | `app/scanner_server.py` |
| Medium | Routine phone events rebuilt editable exception tables and could interrupt a desktop edit. Those tables are now repopulated only when their stored rows change. Clearing an audit also refreshes visible rows. | `app/main_window.py` |
| Medium | Alerts arriving during export preparation or the save dialog could bypass review. Export paths drain pending actions and recheck open alerts at those boundaries. Interrupted area checklists now retain partial completion. | `app/main_window.py` |
| Medium | A package block crossing a PDF page boundary could lose its continuation. Parsing now assembles blocks across pages while retaining their starting page and preserving ordinary existing row IDs. | `app/parser.py` |
| Medium | Training metrics reported the final epoch rather than the selected checkpoint, and ONNX parity used only a blank tensor. Metrics now evaluate the selected model; parity includes real validation tensors and rejects non-finite differences. Invalid epoch/batch settings are rejected. | `app/model_training.py` |
| Medium | Annotation geometry accepted non-finite values; review types and API JSON shapes were insufficiently validated. The API now returns controlled client errors for malformed objects, sends private/no-store headers, and keeps the reviewed carrier summary consistent. | `app/label_training.py`, `app/scanner_server.py` |
| Medium | Interrupting the training API could deadlock by invoking server shutdown from its serving thread. Cleanup now closes the server directly. | `scripts/run_training_api.py` |

Android's platform references explain the [dialog list/message restriction](https://developer.android.com/develop/ui/views/components/dialogs)
and [MTU behavior](https://developer.android.com/reference/android/bluetooth/BluetoothGatt#requestMtu(int));
the [GATT server reference](https://developer.android.com/reference/android/bluetooth/BluetoothGattServer)
documents the 512-byte attribute limit. The corresponding failures and repairs were verified on this device.

## Verification

- **308 Python tests passed on Python 3.10 and 3.14.** The initial repair pass reached 320 tests; the cleanup
  removed twelve tests for deleted functionality. Added regressions cover data integrity, matching,
  lifecycle, parsing, exports, training, and the Bluetooth protocol.
- **12 Android JVM tests passed**, and both debug and signed-release lint/build tasks completed.
- **Connected Samsung Galaxy S24 Ultra, Android 16:** instrumentation verified visible tracking/location
  choices, RGB input channels, camera background/resume, encrypted lookup, interrupted confirmation recovery,
  replaying the same confirmation without duplicating its event, undo, and a multi-chunk training JPEG upload.
  The Mac side checked actual temporary SQLite presence/event state and encrypted pending-sample storage.
- **Final packaged Mac app:** UI, scanner, and export smoke tests passed. The export test checks PDF annotation,
  report output, and spreadsheet formula neutralization. The scanner smoke covers generated image decoding
  and the local browser workflow.
- **ML pipeline:** the disposable synthetic training/ONNX smoke test passed. This checks pipeline execution;
  it does not measure real-label accuracy or approve a production model.
- **Static/dependency checks:** Ruff lint/format and Bandit passed. `pip-audit` found no known vulnerabilities
  in the installed Python dependencies it could audit; the local project itself is not a PyPI package and
  was excluded by that tool. This is not an Android dependency vulnerability scan or a formal security proof.
- The Swift helper compiled, the Android APK permission check passed, and Python wheel/source builds passed.

The repeated PyMuPDF/SWIG deprecation warnings and Android SDK-tool metadata/deprecated-API warnings did not
fail tests or builds. They remain dependency/tooling cleanup items.

## Builds and repeatable device test

The repaired Mac bundle is `dist/audited/Package Audit.app`; the previous `dist/Package Audit.app` was not
replaced. Use the audited bundle or the updated source to get these desktop fixes.
The signed phone installer is `dist/PackageAuditScanner-0.17.0.apk`. The version remains 0.17.0, so the version
label alone does not distinguish this audit build from the previous build.
The signed release was installed successfully on the connected phone with `adb install -r`, preserving app
data. Pair it again with the audited Mac app after launching the updated apps.

The prior phone APK was saved at `/tmp/package-audit-review/phone-before.apk` for rollback. Build and test
logs for the first audit are in `/tmp/package-audit-review/`, with the final cleanup pass in
`/tmp/package-audit-cleanup/`; temporary-system storage is not a permanent backup location.

To repeat the disposable device test, build/install `:app:installDebug` and `:app:installDebugAndroidTest`
with the Android Gradle wrapper, grant Camera/Nearby devices permissions to the debug app, build the Swift
helper, unlock the connected phone, then run:

```sh
.venv/bin/python -m scripts.bluetooth_device_smoke
```

Debug builds now use `com.packageaudit.scanner.debug`, allowing tests beside the release app. The test
runner only invokes its debug instrumentation component and creates a temporary synthetic Mac audit.

## Remaining limits

- Real package-room optical performance, glare, damaged labels, long-distance radio range, battery behavior,
  and hours-long sessions still need field testing. Camera lifecycle and generated barcode decoding passed;
  this audit did not physically aim the camera at a representative set of real shipping labels.
- Other phones, Windows desktop execution, and OS-version combinations were not exercised.
- Existing saved incorrect confirmations, manual entries, historical OCR aliases, or previously trained
  models were not automatically rewritten. The new matching rules prevent conflicting numeric aliases from
  taking effect, but past audit decisions still require operator review where needed.
- The Bluetooth protocol remains a custom pre-shared-key design without forward secrecy, as documented in
  `BLUETOOTH_PROTOCOL.md`. Its tests do not constitute independent cryptographic certification.
- Dataset/field quality remains the limiting factor for training accuracy. No production model was trained,
  approved, or deployed as part of this audit.
