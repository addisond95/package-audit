"""Conventional supervised training for the offline shipping-label vision model."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import random
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter

from app.label_training import REGION_KINDS, TrainingDataset
from app.model_release import MODEL_CARD_FILENAME, MODEL_FILENAME, release_gate, sign_release

INPUT_SIZE = 224


def reserve_test_evaluation(dataset: TrainingDataset, manifest: dict[str, Any]) -> None:
    """Consume held-out images before opening them, including across dataset versions."""
    version_id = manifest["version_id"]
    with dataset.conn:
        dataset.conn.execute(
            "CREATE TABLE IF NOT EXISTS evaluated_test_images "
            "(image_sha256 TEXT PRIMARY KEY, dataset_version TEXT NOT NULL)"
        )
        # Honor evaluations created before the per-image guard was introduced.
        for record in (dataset.root / "test_evaluations").glob("*.json"):
            previous = json.loads(record.read_text(encoding="utf-8"))
            previous_manifest = dataset.get_version(previous["dataset_version"])
            dataset.conn.executemany(
                "INSERT OR IGNORE INTO evaluated_test_images VALUES (?, ?)",
                [
                    (item["image_sha256"], previous_manifest["version_id"])
                    for item in previous_manifest["samples"]
                    if item["split"] == "test"
                ],
            )
    hashes = {item["image_sha256"] for item in manifest["samples"] if item["split"] == "test"}
    try:
        with dataset.conn:
            dataset.conn.executemany(
                "INSERT INTO evaluated_test_images VALUES (?, ?)",
                [(digest, version_id) for digest in sorted(hashes)],
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError(
            "These held-out test images have already been evaluated. "
            "Collect a fresh held-out test set before another final evaluation."
        ) from exc


def box_iou(left: list[float], right: list[float]) -> float:
    lx1, ly1, lw, lh = left
    rx1, ry1, rw, rh = right
    lx2, ly2, rx2, ry2 = lx1 + lw, ly1 + lh, rx1 + rw, ry1 + rh
    intersection = max(0.0, min(lx2, rx2) - max(lx1, rx1)) * max(0.0, min(ly2, ry2) - max(ly1, ry1))
    union = lw * lh + rw * rh - intersection
    return intersection / union if union > 0 else 0.0


def rotate_box(box: list[float], orientation: int) -> list[float]:
    """Rotate a normalized x/y/width/height box clockwise with its image."""
    x, y, width, height = box
    if orientation == 90:
        return [1.0 - y - height, x, height, width]
    if orientation == 180:
        return [1.0 - x - width, 1.0 - y - height, width, height]
    if orientation == 270:
        return [y, 1.0 - x - width, height, width]
    return list(box)


def _ml_imports():
    # ORT 1DS telemetry is enabled by default in official macOS wheels. This
    # variable must exist before the native library is imported; the later API
    # call alone can occur after an initialization event and device-ID write.
    os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
    try:
        import numpy as np
        import onnxruntime as ort
        import torch
        import torch.nn as nn
        from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small
        from torchvision.transforms import functional as tvf
    except ImportError as exc:
        raise RuntimeError("ML dependencies are not installed. Run: ./scripts/setup_ml.sh") from exc
    ort.disable_telemetry_events()
    return np, ort, torch, nn, MobileNet_V3_Small_Weights, mobilenet_v3_small, tvf


def _model_class(nn, mobilenet_v3_small):
    class LabelVisionModel(nn.Module):
        def __init__(self, carrier_count: int, condition_count: int, weights=None):
            super().__init__()
            base = mobilenet_v3_small(weights=weights)
            self.features = base.features
            self.pool = nn.AdaptiveAvgPool2d(1)
            self.shared = nn.Sequential(nn.Flatten(), nn.Dropout(0.2), nn.Linear(576, 256), nn.Hardswish())
            self.carrier = nn.Linear(256, carrier_count)
            self.condition = nn.Linear(256, condition_count)
            self.regions = nn.Linear(256, len(REGION_KINDS) * 5)

        def forward(self, image):
            shared = self.shared(self.pool(self.features(image)))
            return self.carrier(shared), self.condition(shared), self.regions(shared)

    return LabelVisionModel


def _prepare_image(image: Image.Image, orientation: int, *, augment: bool, tvf, torch):
    image = image.convert("RGB")
    if orientation:
        image = image.rotate(-orientation, expand=True)
    if augment:
        # Non-cryptographic PRNG is intentional for reproducible augmentation.
        if random.random() < 0.5:  # nosec B311
            image = ImageEnhance.Contrast(image).enhance(
                random.uniform(0.75, 1.25)  # nosec B311
            )
        if random.random() < 0.35:  # nosec B311
            image = ImageEnhance.Brightness(image).enhance(
                random.uniform(0.8, 1.2)  # nosec B311
            )
        if random.random() < 0.2:  # nosec B311
            image = image.filter(
                ImageFilter.GaussianBlur(random.uniform(0.1, 1.0))  # nosec B311
            )
    image = tvf.resize(image, [INPUT_SIZE, INPUT_SIZE], antialias=True)
    tensor = tvf.to_tensor(image)
    return tvf.normalize(tensor, mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]).to(
        dtype=torch.float32
    )


def _targets(annotation: dict[str, Any], carrier_labels: list[str], condition_labels: list[str], torch):
    regions = {
        item["kind"]: rotate_box(item["box"], annotation["orientation"]) for item in annotation["regions"]
    }
    target = []
    for kind in REGION_KINDS:
        if kind in regions:
            target.extend([1.0, *regions[kind]])
        else:
            target.extend([0.0, 0.0, 0.0, 0.0, 0.0])
    return (
        torch.tensor(carrier_labels.index(annotation["carrier"]), dtype=torch.long),
        torch.tensor(condition_labels.index(annotation["condition"]), dtype=torch.long),
        torch.tensor(target, dtype=torch.float32),
    )


def _load_split(dataset: TrainingDataset, manifest: dict[str, Any], split: str, labels, *, augment: bool):
    _np, _ort, torch, _nn, _weights, _mobile, tvf = _ml_imports()
    carrier_labels, condition_labels = labels
    values = []
    for member in manifest["samples"]:
        if member["split"] != split:
            continue
        image_bytes = dataset.image_bytes(member["sample_id"])
        if hashlib.sha256(image_bytes).hexdigest() != member["image_sha256"]:
            raise ValueError(f"Training image {member['sample_id']} failed its integrity check.")
        image = Image.open(__import__("io").BytesIO(image_bytes))
        image.load()
        annotation = member["annotations"]
        values.append(
            (
                _prepare_image(image, annotation["orientation"], augment=augment, tvf=tvf, torch=torch),
                *_targets(annotation, carrier_labels, condition_labels, torch),
            )
        )
    return values


def _batches(values, batch_size: int, torch, *, shuffle: bool):
    indices = list(range(len(values)))
    if shuffle:
        random.shuffle(indices)
    for start in range(0, len(indices), batch_size):
        selected = [values[index] for index in indices[start : start + batch_size]]
        yield tuple(torch.stack(items) for items in zip(*selected, strict=True))


def _loss(outputs, carrier, condition, regions, nn, torch):
    carrier_logits, condition_logits, raw_regions = outputs
    raw_regions = raw_regions.reshape(-1, len(REGION_KINDS), 5)
    truth = regions.reshape(-1, len(REGION_KINDS), 5)
    presence = nn.functional.binary_cross_entropy_with_logits(raw_regions[:, :, 0], truth[:, :, 0])
    mask = truth[:, :, 0] > 0.5
    boxes = torch.sigmoid(raw_regions[:, :, 1:])
    box_loss = nn.functional.smooth_l1_loss(boxes[mask], truth[:, :, 1:][mask]) if mask.any() else 0.0
    return (
        nn.functional.cross_entropy(carrier_logits, carrier)
        + nn.functional.cross_entropy(condition_logits, condition)
        + presence
        + box_loss * 2.0
    )


def _evaluate(model, values, labels, torch) -> dict[str, float]:
    carrier_labels, condition_labels = labels
    carrier_correct = condition_correct = total = 0
    carrier_actual = [0] * len(carrier_labels)
    carrier_predicted = [0] * len(carrier_labels)
    carrier_true_positive = [0] * len(carrier_labels)
    condition_actual = [0] * len(condition_labels)
    condition_predicted = [0] * len(condition_labels)
    condition_true_positive = [0] * len(condition_labels)
    region_true_positive = region_false_positive = region_false_negative = 0
    ious: list[float] = []
    model.eval()
    with torch.no_grad():
        for images, carriers, conditions, regions in _batches(values, 16, torch, shuffle=False):
            carrier_logits, condition_logits, raw_regions = model(images)
            carrier_predictions = carrier_logits.argmax(1)
            condition_predictions = condition_logits.argmax(1)
            carrier_correct += int((carrier_predictions == carriers).sum())
            condition_correct += int((condition_predictions == conditions).sum())
            total += len(images)
            predicted = raw_regions.reshape(-1, len(REGION_KINDS), 5)
            truth = regions.reshape(-1, len(REGION_KINDS), 5)
            for row in range(len(images)):
                carrier_truth = int(carriers[row])
                carrier_prediction = int(carrier_predictions[row])
                condition_truth = int(conditions[row])
                condition_prediction = int(condition_predictions[row])
                carrier_actual[carrier_truth] += 1
                carrier_predicted[carrier_prediction] += 1
                condition_actual[condition_truth] += 1
                condition_predicted[condition_prediction] += 1
                if carrier_truth == carrier_prediction:
                    carrier_true_positive[carrier_truth] += 1
                if condition_truth == condition_prediction:
                    condition_true_positive[condition_truth] += 1
                for region in range(len(REGION_KINDS)):
                    present = float(truth[row, region, 0]) > 0.5
                    predicted_present = float(torch.sigmoid(predicted[row, region, 0])) >= 0.5
                    if present and predicted_present:
                        region_true_positive += 1
                    elif predicted_present:
                        region_false_positive += 1
                    elif present:
                        region_false_negative += 1
                    if present:
                        box = torch.sigmoid(predicted[row, region, 1:]).tolist()
                        ious.append(box_iou(box, truth[row, region, 1:].tolist()))

    def macro_recall(true_positive: list[int], actual: list[int]) -> float:
        scores = [correct / count for correct, count in zip(true_positive, actual, strict=True) if count]
        return sum(scores) / len(scores) if scores else 0.0

    def macro_f1(true_positive: list[int], actual: list[int], predicted_count: list[int]) -> float:
        scores = [
            2 * correct / (truth_count + prediction_count) if truth_count + prediction_count else 0.0
            for correct, truth_count, prediction_count in zip(
                true_positive, actual, predicted_count, strict=True
            )
        ]
        return sum(scores) / len(scores) if scores else 0.0

    region_denominator = 2 * region_true_positive + region_false_positive + region_false_negative
    return {
        "carrier_accuracy": carrier_correct / total if total else 0.0,
        "carrier_macro_recall": macro_recall(carrier_true_positive, carrier_actual),
        "carrier_macro_f1": macro_f1(carrier_true_positive, carrier_actual, carrier_predicted),
        "condition_accuracy": condition_correct / total if total else 0.0,
        "condition_macro_recall": macro_recall(condition_true_positive, condition_actual),
        "condition_macro_f1": macro_f1(condition_true_positive, condition_actual, condition_predicted),
        "region_presence_f1": (2 * region_true_positive / region_denominator if region_denominator else 0.0),
        "region_mean_iou": sum(ious) / len(ious) if ious else 0.0,
        "samples": total,
        "carrier_classes": len(carrier_labels),
        "condition_classes": len(condition_labels),
    }


def train_model(
    dataset: TrainingDataset,
    dataset_version: str,
    output_root: Path,
    signing_key: Path,
    *,
    epochs: int = 12,
    batch_size: int = 8,
    seed: int = 20260923,
    pretrained: bool = True,
    final_evaluation: bool = False,
) -> dict[str, Any]:
    """Train, evaluate, ONNX-verify, gate, document, and sign one model candidate."""
    if type(epochs) is not int or epochs < 1 or type(batch_size) is not int or batch_size < 1:
        raise ValueError("Epochs and batch size must be positive integers.")
    np, ort, torch, nn, Weights, mobilenet, _tvf = _ml_imports()
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)
    manifest = dataset.get_version(dataset_version)
    evaluation_dir = dataset.root / "test_evaluations"
    evaluation_record = evaluation_dir / f"{manifest['dataset_sha256']}.json"
    if final_evaluation and evaluation_record.exists():
        raise ValueError(
            "This dataset version's held-out test split has already been evaluated. "
            "Freeze a new dataset version for another final release decision."
        )
    carrier_labels = sorted({item["annotations"]["carrier"] for item in manifest["samples"]})
    condition_labels = sorted({item["annotations"]["condition"] for item in manifest["samples"]})
    if len(carrier_labels) < 2:
        raise ValueError("Training requires examples from at least two carrier or label-family classes.")
    if len(condition_labels) < 2:
        raise ValueError("Training requires examples from at least two label-condition classes.")
    for split in ("train", "validation", "test"):
        split_members = [item for item in manifest["samples"] if item["split"] == split]
        split_carriers = {item["annotations"]["carrier"] for item in split_members}
        split_conditions = {item["annotations"]["condition"] for item in split_members}
        if set(carrier_labels) - split_carriers or set(condition_labels) - split_conditions:
            raise ValueError(f"Every carrier and condition class must appear in the {split} split.")
    labels = (carrier_labels, condition_labels)
    train_members = [item for item in manifest["samples"] if item["split"] == "train"]
    validation = _load_split(dataset, manifest, "validation", labels, augment=False)
    if not train_members or not validation:
        raise ValueError("Training requires non-empty train, validation, and test splits.")

    Model = _model_class(nn, mobilenet)
    weights = Weights.DEFAULT if pretrained else None
    model = Model(len(carrier_labels), len(condition_labels), weights=weights)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.0003, weight_decay=0.01)
    best_state = None
    best_score = -math.inf
    history: list[dict[str, Any]] = []
    for epoch in range(1, epochs + 1):
        # Rebuild augmented tensors every epoch so small real-world datasets do
        # not see the same randomized image transformation for the whole run.
        train = _load_split(dataset, manifest, "train", labels, augment=True)
        model.train()
        losses = []
        for images, carriers, conditions, regions in _batches(train, batch_size, torch, shuffle=True):
            optimizer.zero_grad(set_to_none=True)
            loss = _loss(model(images), carriers, conditions, regions, nn, torch)
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach()))
        validation_metrics = _evaluate(model, validation, labels, torch)
        score = (
            validation_metrics["carrier_macro_f1"]
            + validation_metrics["condition_macro_f1"]
            + validation_metrics["region_presence_f1"]
            + validation_metrics["region_mean_iou"]
        )
        history.append({"epoch": epoch, "loss": sum(losses) / len(losses), **validation_metrics})
        if score > best_score:
            best_score = score
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
    if best_state is None:
        raise RuntimeError("Training produced no checkpoint.")
    model.load_state_dict(best_state)
    selected_validation = _evaluate(model, validation, labels, torch)
    test = []
    if final_evaluation:
        reserve_test_evaluation(dataset, manifest)
        test = _load_split(dataset, manifest, "test", labels, augment=False)
    test_metrics = _evaluate(model, test, labels, torch) if final_evaluation else {}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release_id = f"label-model-{stamp}-{manifest['content_sha256'][:8]}"
    release_dir = output_root / release_id
    release_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    model_path = release_dir / MODEL_FILENAME
    example = torch.zeros(1, 3, INPUT_SIZE, INPUT_SIZE)
    model.eval()
    torch.onnx.export(
        model,
        (example,),
        model_path,
        input_names=["image"],
        output_names=["carrier_logits", "condition_logits", "regions"],
        dynamic_axes={"image": {0: "batch"}},
        opset_version=18,
        dynamo=False,
    )
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    parity_errors = []
    for parity_image in [example, *(value[0].unsqueeze(0) for value in validation[:3])]:
        with torch.no_grad():
            torch_outputs = [value.detach().numpy() for value in model(parity_image)]
        onnx_outputs = session.run(None, {"image": parity_image.numpy()})
        parity_errors.extend(
            float(np.max(np.abs(left - right)))
            for left, right in zip(torch_outputs, onnx_outputs, strict=True)
        )
    onnx_error = max(parity_errors) if all(math.isfinite(value) for value in parity_errors) else math.nan
    metrics = {
        "final_evaluation": final_evaluation,
        **{f"validation_{name}": value for name, value in selected_validation.items()},
        "onnx_max_abs_error": onnx_error,
        "history": history,
    }
    if final_evaluation:
        metrics.update(
            {
                "test_carrier_accuracy": test_metrics["carrier_accuracy"],
                "test_carrier_macro_recall": test_metrics["carrier_macro_recall"],
                "test_carrier_macro_f1": test_metrics["carrier_macro_f1"],
                "test_condition_accuracy": test_metrics["condition_accuracy"],
                "test_condition_macro_recall": test_metrics["condition_macro_recall"],
                "test_condition_macro_f1": test_metrics["condition_macro_f1"],
                "test_region_presence_f1": test_metrics["region_presence_f1"],
                "test_region_mean_iou": test_metrics["region_mean_iou"],
            }
        )
    approved, failures = release_gate(metrics, manifest["split_counts"])
    release_manifest = {
        "schema_version": 1,
        "release_id": release_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset_version": dataset_version,
        "dataset_content_sha256": manifest["content_sha256"],
        "dataset_sha256": manifest["dataset_sha256"],
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "input": {"name": "image", "shape": [1, 3, INPUT_SIZE, INPUT_SIZE], "color": "RGB"},
        "normalization": {
            "mean": [0.485, 0.456, 0.406],
            "std": [0.229, 0.224, 0.225],
        },
        "carrier_labels": carrier_labels,
        "condition_labels": condition_labels,
        "training": {
            "epochs": epochs,
            "batch_size": batch_size,
            "seed": seed,
            "pretrained_imagenet": pretrained,
            "torch": importlib.metadata.version("torch"),
            "torchvision": importlib.metadata.version("torchvision"),
            "onnx": importlib.metadata.version("onnx"),
            "onnxruntime": importlib.metadata.version("onnxruntime"),
        },
        "region_kinds": list(REGION_KINDS),
        "trained_region_kinds": sorted(
            {
                region["kind"]
                for item in manifest["samples"]
                if item["split"] == "train"
                for region in item["annotations"]["regions"]
            }
        ),
        "metrics": metrics,
        "split_counts": manifest["split_counts"],
        "approved": approved,
        "gate_failures": failures,
        "safety": "Advisory only; exact tracking matching remains mandatory.",
    }
    sign_release(release_dir, release_manifest, signing_key)
    test_summary = (
        f"- Test carrier accuracy: {test_metrics['carrier_accuracy']:.3f}\n"
        f"- Test carrier macro recall: {test_metrics['carrier_macro_recall']:.3f}\n"
        f"- Test carrier macro F1: {test_metrics['carrier_macro_f1']:.3f}\n"
        f"- Test condition accuracy: {test_metrics['condition_accuracy']:.3f}\n"
        f"- Test condition macro recall: {test_metrics['condition_macro_recall']:.3f}\n"
        f"- Test condition macro F1: {test_metrics['condition_macro_f1']:.3f}\n"
        f"- Test region-presence F1: {test_metrics['region_presence_f1']:.3f}\n"
        f"- Test region mean IoU: {test_metrics['region_mean_iou']:.3f}\n"
        if final_evaluation
        else "- Held-out test split: not opened (candidate run)\n"
    )
    card = (
        f"# {release_id}\n\n"
        f"- Dataset: `{dataset_version}`\n"
        f"- Approved: **{'yes' if approved else 'no'}**\n"
        f"{test_summary}"
        f"- ONNX parity max error: {onnx_error:.8f}\n"
        f"- Gate failures: {', '.join(failures) if failures else 'none'}\n\n"
        "The model is advisory. It cannot confirm package presence without an exact tracking match.\n"
    )
    (release_dir / MODEL_CARD_FILENAME).write_text(card, encoding="utf-8")
    (release_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    checkpoint = release_dir / "training_checkpoint.pt"
    torch.save({"state_dict": best_state, "manifest": release_manifest}, checkpoint)
    shutil.copy2(dataset.versions / f"{dataset_version}.json", release_dir / "dataset_manifest.json")
    if final_evaluation:
        evaluation_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = evaluation_record.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "dataset_version": dataset_version,
                    "release_id": release_id,
                    "evaluated_at": release_manifest["created_at"],
                    "metrics": {key: value for key, value in metrics.items() if key.startswith("test_")},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(evaluation_record)
    return {"release_dir": str(release_dir), **release_manifest}
