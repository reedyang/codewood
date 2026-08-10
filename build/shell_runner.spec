# -*- mode: python ; coding: utf-8 -*-
"""Standalone build of the sandbox runner (Windows only).

The sandbox spawns this executable with CreateProcessWithLogonW under a
sandbox user; it derives the restricted token and starts the real command.
Packaged separately so the main ``codewood.exe`` never needs a Python
interpreter at runtime. Output: ``dist\\codewood\\shell-runner\\shell-runner.exe``
(onedir: the bootloader needs no temp extraction, so the sandbox user can
read the bundle from the install directory).

Only the standard library is used (ctypes/struct/argparse/os), so the
bundle stays tiny.
"""


a = Analysis(
    ['..\\cli\\core\\sandbox\\windows_runner.py'],
    pathex=['.venv-windows\\Lib\\site-packages'],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
    [],
    exclude_binaries=True,
    name='shell-runner',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='shell-runner',
)
