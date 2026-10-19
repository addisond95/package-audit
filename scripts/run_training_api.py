#!/usr/bin/env python3
"""Run the opt-in label dataset API on Mac loopback only."""

from __future__ import annotations

import argparse
import secrets

from werkzeug.serving import make_server

from app.constants import APP_DIR
from app.label_training import TrainingDataset, create_training_api


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")

    token = secrets.token_urlsafe(32)
    dataset = TrainingDataset(APP_DIR / "training_data")
    server = make_server("127.0.0.1", args.port, create_training_api(dataset, token))
    print(f"Training API: http://127.0.0.1:{args.port}")
    print(f"Temporary bearer token: {token}")
    print("Loopback only; press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        dataset.close()


if __name__ == "__main__":
    main()
