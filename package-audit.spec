# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for BuildingLink Package Audit.

Build a self-contained macOS .app bundle:

    uv run pyinstaller package-audit.spec

Output lands in dist/Package Audit.app
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821 — injected by PyInstaller

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # PySide6 platform plugin required for macOS .app bundles.
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtPrintSupport",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Unused heavy packages — keep the bundle lean.
        "numpy",
        "pandas",
        "matplotlib",
        "scipy",
        "IPython",
        "tkinter",
        "xmlrpc",
        "unittest",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Package Audit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,        # No terminal window on macOS.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,     # Native arch of the build machine.
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Package Audit",
)

app = BUNDLE(  # noqa: F821
    coll,
    name="Package Audit.app",
    icon=None,
    bundle_identifier="com.packageaudit.app",
    info_plist={
        "CFBundleDisplayName": "Package Audit",
        "CFBundleShortVersionString": "0.4.0",
        "CFBundleVersion": "0.4.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    },
)
