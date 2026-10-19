"""Release safety checks for the native Android scanner build."""

import pytest

from scripts.build_android import (
    REQUIRED_PERMISSIONS,
    resolve_android_home,
    resolve_java_home,
    validate_permissions,
)


def test_android_release_permission_policy_accepts_offline_scanner():
    validate_permissions(REQUIRED_PERMISSIONS | {"android.permission.VIBRATE"})


@pytest.mark.parametrize(
    "permission",
    [
        "android.permission.INTERNET",
        "android.permission.ACCESS_NETWORK_STATE",
        "android.permission.ACCESS_FINE_LOCATION",
        "android.permission.READ_EXTERNAL_STORAGE",
        "android.permission.RECORD_AUDIO",
    ],
)
def test_android_release_permission_policy_rejects_data_access(permission):
    with pytest.raises(SystemExit, match="Unsafe APK permissions"):
        validate_permissions(REQUIRED_PERMISSIONS | {permission})


def test_android_release_permission_policy_requires_camera_and_bluetooth():
    with pytest.raises(SystemExit, match="missing"):
        validate_permissions(REQUIRED_PERMISSIONS - {"android.permission.CAMERA"})


def test_build_tool_resolution_honors_valid_environment_paths(tmp_path):
    java = tmp_path / "jdk"
    sdk = tmp_path / "sdk"
    (java / "bin").mkdir(parents=True)
    (java / "bin" / "keytool").touch()
    (sdk / "build-tools").mkdir(parents=True)

    environment = {"JAVA_HOME": str(java), "ANDROID_HOME": str(sdk)}
    assert resolve_java_home(environment) == java
    assert resolve_android_home(environment) == sdk
