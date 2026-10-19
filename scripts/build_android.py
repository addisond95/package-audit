"""Build a locally signed release APK; keep the update key private and reusable."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess  # nosec B404
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_PERMISSIONS = {
    "android.permission.BLUETOOTH_CONNECT",
    "android.permission.BLUETOOTH_SCAN",
    "android.permission.CAMERA",
}
FORBIDDEN_PERMISSIONS = {
    "android.permission.ACCESS_NETWORK_STATE",
    "android.permission.INTERNET",
    "android.permission.READ_EXTERNAL_STORAGE",
    "android.permission.WRITE_EXTERNAL_STORAGE",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.RECORD_AUDIO",
}


def resolve_java_home(environment: dict[str, str]) -> Path:
    """Find JDK 17 without requiring shell profile configuration."""
    candidates = [
        environment.get("JAVA_HOME", ""),
        "/Applications/Android Studio.app/Contents/jbr/Contents/Home",
        "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home",
        "/usr/local/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home",
    ]
    for candidate in candidates:
        path = Path(candidate) if candidate else None
        if path is not None and (path / "bin" / "keytool").is_file():
            return path
    raise SystemExit("Install JDK 17 or set JAVA_HOME to its directory before building.")


def resolve_android_home(environment: dict[str, str]) -> Path:
    """Find the Android SDK used by the reproducible local build."""
    candidates = [
        environment.get("ANDROID_HOME", ""),
        environment.get("ANDROID_SDK_ROOT", ""),
        str(Path.home() / "Library" / "Android" / "sdk"),
        "/opt/homebrew/share/android-commandlinetools",
    ]
    for candidate in candidates:
        path = Path(candidate) if candidate else None
        if path is not None and (path / "build-tools").is_dir():
            return path
    raise SystemExit("Install Android SDK Build Tools or set ANDROID_HOME before building.")


def validate_permissions(permissions: set[str]) -> None:
    """Validate the final merged permission set, independent of build tooling."""
    forbidden = permissions & FORBIDDEN_PERMISSIONS
    missing = REQUIRED_PERMISSIONS - permissions
    if forbidden or missing:
        raise SystemExit(f"Unsafe APK permissions: forbidden={sorted(forbidden)}, missing={sorted(missing)}")


def verify_permissions(apk: Path, environment: dict[str, str]) -> None:
    """Fail release creation if merged dependency manifests widen data access."""
    android_home = environment.get("ANDROID_HOME") or environment.get("ANDROID_SDK_ROOT")
    if not android_home:
        raise SystemExit("Set ANDROID_HOME so the final APK permissions can be verified.")
    tools = sorted((Path(android_home) / "build-tools").glob("*/aapt"), reverse=True)
    if not tools:
        raise SystemExit("Android Build Tools must include aapt for APK permission verification.")
    # The SDK aapt path and argv are constructed locally; no shell is used.
    completed = subprocess.run(  # nosec B603
        [str(tools[0]), "dump", "permissions", str(apk)],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    permissions = {
        line.split("name='", 1)[1].split("'", 1)[0]
        for line in completed.stdout.splitlines()
        if "uses-permission: name='" in line
    }
    validate_permissions(permissions)


def build() -> None:
    environment = dict(os.environ)
    java_home = resolve_java_home(environment)
    android_home = resolve_android_home(environment)
    keytool = java_home / "bin" / "keytool"
    signing = ROOT / ".signing"
    signing.mkdir(mode=0o700, exist_ok=True)
    signing.chmod(0o700)
    password_file = signing / "password"
    keystore = signing / "android-release.jks"
    if not password_file.exists():
        if keystore.exists():
            raise SystemExit(
                "Signing password is missing. Restore it; do not replace the existing signing key."
            )
        descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(secrets.token_hex(32))
    environment["JAVA_HOME"] = str(java_home)
    environment["ANDROID_HOME"] = str(android_home)
    environment["PACKAGE_AUDIT_KEY_PASSWORD"] = password_file.read_text().strip()
    environment["PACKAGE_AUDIT_KEYSTORE"] = str(keystore)
    if not keystore.exists():
        # The resolved JDK keytool and all arguments are fixed; no shell is used.
        subprocess.run(  # nosec B603
            [
                str(keytool),
                "-genkeypair",
                "-keystore",
                str(keystore),
                "-alias",
                "package-audit",
                "-keyalg",
                "RSA",
                "-keysize",
                "3072",
                "-validity",
                "10000",
                "-dname",
                "CN=Package Audit Local Scanner",
                "-storepass:env",
                "PACKAGE_AUDIT_KEY_PASSWORD",
                "-keypass:env",
                "PACKAGE_AUDIT_KEY_PASSWORD",
                "-noprompt",
            ],
            env=environment,
            check=True,
        )
        keystore.chmod(0o600)
    # Run the repository Gradle wrapper with fixed release tasks and no shell.
    subprocess.run(  # nosec B603
        [
            str(ROOT / "android/gradlew"),
            "--no-daemon",
            ":app:testReleaseUnitTest",
            ":app:lintRelease",
            ":app:assembleRelease",
        ],
        cwd=ROOT / "android",
        env=environment,
        check=True,
    )
    built_apk = ROOT / "android/app/build/outputs/apk/release/app-release.apk"
    verify_permissions(built_apk, environment)
    destination = ROOT / "dist/PackageAuditScanner-0.17.0.apk"
    destination.parent.mkdir(exist_ok=True)
    shutil.copy2(built_apk, destination)
    print(f"APK ready: {destination}")
    print("Keep .signing/ private and back it up securely; future updates need this key.")


if __name__ == "__main__":
    build()
