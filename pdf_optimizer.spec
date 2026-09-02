# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all


# CustomTkinter and TkinterDnD2 have platform-aware PyInstaller hooks.  Let
# those hooks select only the data/native files needed by the current OS.
pike_datas, pike_binaries, pike_hiddenimports = collect_all("pikepdf")

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=pike_binaries,
    datas=pike_datas,
    hiddenimports=pike_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PDFOptimizer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
