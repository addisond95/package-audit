#!/usr/bin/env python3
"""Run a tiny synthetic end-to-end label-model pipeline smoke test."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from app.label_training import TrainingDataset
from app.model_release import verify_release
from app.model_training import train_model


def image_bytes(index: int) -> bytes:
    image = Image.new("RGB", (420, 280), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 380, 240), outline="black", width=4)
    for offset in range(20):
        x = 90 + offset * 8
        draw.line((x, 130, x, 200), fill="black" if (offset + index) % 2 else "gray", width=3)
    draw.text((60, 65), "UPS" if index % 2 == 0 else "USPS", fill="black")
    draw.text((60, 90), f"Synthetic example {index}", fill="black")
    output = io.BytesIO()
    image.save(output, "JPEG")
    return output.getvalue()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="package-audit-ml-smoke-") as temporary:
        root = Path(temporary)
        dataset = TrainingDataset(root / "dataset")
        try:
            for index, split in enumerate(("train", "train", "validation", "validation", "test", "test")):
                sample = dataset.add_sample(
                    image_bytes(index),
                    {"consent": True, "outcome": "correct", "carrier": "UPS" if index % 2 == 0 else "USPS"},
                )
                dataset.review_sample(
                    sample["sample_id"],
                    status="approved",
                    annotations={
                        "carrier": "UPS" if index % 2 == 0 else "USPS",
                        "condition": "good" if index % 2 == 0 else "blur",
                        "orientation": 0,
                        "group_id": f"synthetic-{index}",
                        "regions": [{"kind": "barcode", "box": [0.2, 0.4, 0.6, 0.3]}],
                    },
                    reviewer="ml-smoke",
                    dataset_split=split,
                )
            version = dataset.create_version("synthetic smoke test")
            release = train_model(
                dataset,
                version["version_id"],
                root / "releases",
                root / "signing.pem",
                epochs=1,
                batch_size=2,
                pretrained=False,
                final_evaluation=True,
            )
            verified = verify_release(
                Path(release["release_dir"]),
                (root / "signing.pem").with_suffix(".public.pem"),
            )
            if verified["release_id"] != release["release_id"] or verified["approved"] is not False:
                raise RuntimeError("Synthetic release verification returned an invalid result.")
            print("ml-smoke-ok")
        finally:
            dataset.close()


if __name__ == "__main__":
    main()
