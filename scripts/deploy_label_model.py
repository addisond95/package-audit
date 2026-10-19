#!/usr/bin/env python3
"""Verify and stage a label-model release for the offline Android scanner."""

from __future__ import annotations

import argparse
import subprocess  # nosec B404
from pathlib import Path

from app.constants import APP_DIR
from app.model_release import install_android_assets

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_id", help="Release directory name, including an older release for rollback")
    parser.add_argument("--allow-unapproved", action="store_true", help="Development testing only")
    parser.add_argument("--build", action="store_true", help="Build a signed APK after staging")
    parser.add_argument("--install", action="store_true", help="Build and install to the connected phone")
    args = parser.parse_args()
    if Path(args.release_id).name != args.release_id or not args.release_id.startswith("label-model-"):
        parser.error("release_id must be one label-model-* directory name")
    training_root = APP_DIR / "training_data"
    release_dir = training_root / "model_releases" / args.release_id
    if not release_dir.is_dir():
        parser.error(f"release not found: {release_dir}")
    manifest = install_android_assets(
        release_dir,
        ROOT / "android" / "app" / "src" / "main" / "assets",
        allow_unapproved=args.allow_unapproved,
        trusted_public_key=training_root / "model_signing_key.public.pem",
    )
    print(f"Staged verified model {manifest['release_id']} for Android.")
    if args.build or args.install:
        command = [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "scripts" / "build_android.py")]
        # The local build-script path and interpreter are fixed; no shell is used.
        subprocess.run(command, cwd=ROOT, check=True)  # nosec B603
    if args.install:
        apk = ROOT / "dist" / "PackageAuditScanner-0.17.0.apk"
        # The local adb path and install argv are fixed; no shell is used.
        subprocess.run(  # nosec B603
            [
                "/opt/homebrew/share/android-commandlinetools/platform-tools/adb",
                "install",
                "-r",
                str(apk),
            ],
            cwd=ROOT,
            check=True,
        )


if __name__ == "__main__":
    main()
