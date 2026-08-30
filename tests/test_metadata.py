"""Release metadata consistency checks."""

from importlib.metadata import version

from app import __version__


def test_runtime_version_matches_installed_package_metadata():
    assert __version__ == version("package-audit")
