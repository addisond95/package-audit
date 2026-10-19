"""Release metadata consistency checks."""

import re
from importlib.metadata import version
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from app import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_runtime_version_matches_installed_package_metadata():
    assert __version__ == version("package-audit")


def test_release_versions_and_android_artifact_name_stay_in_sync():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    gradle = (ROOT / "android/app/build.gradle").read_text(encoding="utf-8")
    spec = (ROOT / "package-audit.spec").read_text(encoding="utf-8")
    build_script = (ROOT / "scripts/build_android.py").read_text(encoding="utf-8")

    assert project["project"]["version"] == __version__
    assert re.search(rf"versionName ['\"]{re.escape(__version__)}['\"]", gradle)
    assert spec.count(f'"{__version__}"') >= 2
    assert f"PackageAuditScanner-{__version__}.apk" in build_script
