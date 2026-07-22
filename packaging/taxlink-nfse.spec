# -*- mode: python ; coding: utf-8 -*-

import os

project_root = os.path.dirname(SPECPATH)
entrypoint = os.path.join(project_root, "src", "taxlink_nfse", "__main__.py")
source_root = os.path.join(project_root, "src")

a = Analysis(
    [entrypoint],
    pathex=[source_root],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="taxlink-nfse",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

service_exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="taxlink-nfse-service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
