#!/usr/bin/env python3
"""Train, evaluate, export, gate, and sign an offline label model."""

from __future__ import annotations

import argparse

from app.constants import APP_DIR
from app.label_training import TrainingDataset
from app.model_training import train_model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-version", help="Frozen dataset version; defaults to the newest")
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260923)
    parser.add_argument("--no-pretrained", action="store_true")
    parser.add_argument(
        "--final-evaluation",
        action="store_true",
        help="Open the held-out test split once and make a release-gate decision",
    )
    args = parser.parse_args()
    if not 1 <= args.epochs <= 500:
        parser.error("epochs must be between 1 and 500")
    if not 1 <= args.batch_size <= 128:
        parser.error("batch size must be between 1 and 128")

    dataset = TrainingDataset(APP_DIR / "training_data")
    try:
        versions = dataset.list_versions()
        version_id = args.dataset_version or (versions[0]["version_id"] if versions else "")
        if not version_id:
            parser.error("no frozen dataset version exists; create one in the Mac Training Data tab")
        result = train_model(
            dataset,
            version_id,
            dataset.root / "model_releases",
            dataset.root / "model_signing_key.pem",
            epochs=args.epochs,
            batch_size=args.batch_size,
            seed=args.seed,
            pretrained=not args.no_pretrained,
            final_evaluation=args.final_evaluation,
        )
    finally:
        dataset.close()
    print(f"Release: {result['release_id']}")
    print(f"Directory: {result['release_dir']}")
    print(f"Approved: {result['approved']}")
    if result["gate_failures"]:
        print("Release gate failures:")
        for failure in result["gate_failures"]:
            print(f"- {failure}")


if __name__ == "__main__":
    main()
