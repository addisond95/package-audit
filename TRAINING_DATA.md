# Label training-data workflow

Package Audit v0.17 provides a complete conventional supervised-learning pipeline. It is not an agentic system
and it is not reinforcement learning: people explicitly collect and annotate examples, a fixed training program
optimizes a convolutional network, fixed metrics gate releases, and Android runs the frozen model offline.

## Carrier-format learning

Distinctive documented patterns are recognized conservatively. UPS documents its 18-character `1Z` format and
USPS publishes domestic and international examples. Ambiguous all-numeric values are not assigned to a carrier
from length alone. After three consistent confirmed audit matches, the local profile may classify the same
barcode format and structural mask for that installation. The profile stores shapes such as
`CODE_128:12:999999999999`, never the tracking value itself. Carrier classification is descriptive metadata and
cannot mark a package present; exact full tracking matching remains mandatory.

Sources:

- [UPS tracking-number formats](https://www.ups.com/us/en/support/tracking-support)
- [USPS tracking-number examples](https://tools.usps.com/go/TrackAction)

## Capture an explicit phone example

For a package that reaches the confirmation screen, tap **Confirm + add label to training**. The package is
confirmed first, then one compressed label image is transferred through the authenticated Bluetooth session and
added directly to the Mac's encrypted Pending dataset. Ordinary **Confirm unit** remains image-free. If transfer
fails, the audit confirmation stays saved and the phone clearly reports that the training example was not added.

For a label that cannot reach confirmation:

1. Open Package Audit Scanner and frame the relevant full label.
2. Tap **Save standalone training example**.
3. Choose why the example is useful: correct, missed barcode, wrong tracking,
   wrong carrier, unit-read error, unusual address/name layout, damaged label, or other.
4. Choose where Android saves the `.pasample` file.
5. Move the file to the Mac by USB, Quick Share, or another approved transfer method.
6. In the Mac application, open **Training Data** and select **Import Phone Examples**.
7. After successful import, delete the original `.pasample` file from shared storage unless retention is
   explicitly required.
8. Select one imported row and choose **Review / Annotate Selected** (or double-click it).
9. Set the carrier/label family, primary condition, correct orientation, and capture group. Draw the barcode or
   printed-tracking region plus useful unit, destination-address, and last-name regions. Never type resident
   names, addresses, or tracking values into annotation fields.
10. Assign the entire capture group to train, validation, or test and approve it. Use the same group for examples
    from the same audit/session so near-duplicates cannot leak across splits.
11. When all three splits contain approved examples, choose **Freeze Dataset Version**.

A `.pasample` export is a transport bundle and is not encrypted. It may contain a complete shipping label,
including a resident name, address, unit, and tracking number. Import it promptly. The Mac re-encodes the image
to remove embedded image metadata, encrypts the label and private annotations with AES-256-GCM, and stores them
under `~/.package_audit/training_data` with restrictive local permissions. Back up `dataset.key` securely: losing
it makes the collected images unrecoverable; disclosure exposes the dataset.

Routine package scanning does not save frames and never enters this dataset automatically.

## Train and evaluate a candidate

Install the isolated Python 3.12 ML environment once (internet is required only for this setup):

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit
./scripts/setup_ml.sh
```

Train the newest frozen dataset version:

```bash
.venv-ml/bin/python scripts/train_label_model.py --epochs 12
```

The trainer uses a MobileNetV3-small convolutional backbone and separate output heads for carrier family, label
condition, and normalized regions (`barcode`, `tracking_text`, `unit`, `address`, `last_name`). It selects the
checkpoint using validation data, exports ONNX, checks PyTorch/ONNX numerical parity, writes a model card, and
signs the manifest with a local Ed25519 key. Candidate runs do not open the test split.

When validation results justify a final release decision, run exactly once for that dataset version:

```bash
.venv-ml/bin/python scripts/train_label_model.py --epochs 12 --final-evaluation
```

This reserves the held-out test image hashes before opening their pixels, then evaluates the selected model.
Those images cannot be reused in another final evaluation, even through a renamed or newly frozen dataset
version. A failed final run also consumes the reservation; use fresh held-out examples for a subsequent release
decision. Frozen datasets reject identical image content assigned to different splits, even across capture groups.

A release is approved only with at least 30 training, 10 validation, and 10 test examples; carrier accuracy of
at least 85% with macro recall/F1 of 80%; condition accuracy of at least 70% with macro recall/F1 of 60%; region-presence
F1 of at least 75%; region mean IoU of at least 60%; and ONNX parity error no more than 0.001. These are initial
safety floors, not proof of product readiness. Real deployment should use a much larger and more diverse dataset
and compare against the fixed scanner baseline.

Run the synthetic pipeline check at any time:

```bash
.venv-ml/bin/python scripts/ml_smoke.py
```

## Deploy or roll back an approved model

Each candidate is stored under `~/.package_audit/training_data/model_releases/RELEASE_ID`. To stage an approved
release, build the APK, and install it on the connected phone:

```bash
uv run python scripts/deploy_label_model.py RELEASE_ID --install
```

Rollback uses the identical command with an older approved `RELEASE_ID`. Deployment verifies the manifest
signature and model hash and refuses a release that failed its gate. Android repeats those checks before loading
the model. The model is bundled into the signed APK and needs no Internet permission or runtime download.

The deployed model is advisory. A high-confidence carrier prediction may fill otherwise unknown carrier metadata.
The package is still marked present only after deterministic exact tracking matching and the existing unit/review
workflow.

## Local API

Start the development/review API on loopback only:

```bash
cd /Users/aldorevenwaters/workspaces/personal_projects/package-audit
uv run python scripts/run_training_api.py
```

The command prints a temporary bearer token. Every endpoint requires `Authorization: Bearer TOKEN`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/v1/import` | Import a `.pasample` multipart field named `bundle` |
| `POST` | `/v1/samples` | Import `image` plus JSON `metadata` multipart fields |
| `GET` | `/v1/samples` | List non-sensitive sample summaries; optional `?status=pending` |
| `GET` | `/v1/samples/{id}` | Read decrypted private metadata for review |
| `GET` | `/v1/samples/{id}/image` | Read the decrypted review image with no-store headers |
| `PATCH` | `/v1/samples/{id}` | Approve/reject with a reviewer ID; approval also requires annotations and a train/validation/test split |
| `DELETE` | `/v1/samples/{id}` | Permanently delete one sample and its encrypted image |
| `GET` | `/v1/dataset-versions` | List immutable dataset manifests |
| `POST` | `/v1/dataset-versions` | Freeze the approved, split-safe dataset |
| `GET` | `/v1/dataset-versions/{id}` | Read one version manifest |

The supplied runner binds only to `127.0.0.1`; it is not a production or cloud deployment. Do not expose it to
a network. A future hosted service still needs TLS, identity/roles, tenant isolation, rate limits, immutable
audit history, retention enforcement, backups, and an approved resident-privacy policy.

## What this release does not claim

- It does not claim that a model trained from a handful of samples is useful or safe.
- It does not upload images automatically or connect the phone directly to a database.
- It does not treat operator confirmation as unquestionable model truth.
- It does not infer missing barcode digits or resident information from a damaged label.

The code can now annotate, version, train, evaluate, sign, deploy, and roll back models. What remains is the real
data and field evidence: collect a representative sample, preserve an untouched test set, meet the release gate,
then prove the candidate improves the fixed baseline without increasing false package confirmations.
