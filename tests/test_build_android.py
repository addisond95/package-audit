"""Release safety checks for the native Android scanner build."""

import pytest

from scripts.build_android import REQUIRED_PERMISSIONS, validate_permissions


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
