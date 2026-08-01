# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for BuildingLink Package Audit — Windows build.

Produces a one-directory bundle under dist/Package Audit/.
Run via GitHub Actions on windows-latest; do NOT run this on macOS
(use package-audit.spec instead).
"""

from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821 — injected by PyInstaller

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtPrintSupport",
        "zxingcpp",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
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
    console=False,   # No console window.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
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
