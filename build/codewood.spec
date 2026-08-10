# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('../skills', 'skills'), ('../additional-subagents', 'additional-subagents'), ('../cli', 'cli'), ('../desktop/frontend/dist', 'frontend'), ('../desktop/host', 'host'), ('../models', 'models'), ('../build/app_icon.ico', 'codewood_assets')]
binaries = []
hiddenimports = ['clr', 'winpty', 'winpty.ptyprocess', 'winpty.enums', 'tiktoken_ext', 'tiktoken_ext.openai_public']
# The Windows sandbox backend is imported inside a try/except in
# cli.core.sandbox.get_sandbox_backend, which PyInstaller treats as an
# optional import and skips. Bundle it explicitly so frozen builds report
# "supported" on Windows instead of silently falling back to the stub.
hiddenimports += ['cli.core.sandbox.windows']
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pythonnet')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('clr_loader')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('winpty')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tiktoken')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['..\\cli\\main.py'],
    pathex=['.venv-windows\\Lib\\site-packages'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Second entry point: the sandbox command runner. It is spawned by
# CreateProcessWithLogonW under a sandbox user and needs no Python
# interpreter at runtime. Bundled into the SAME one-dir folder so both
# executables share one _internal/ runtime (no duplicated python313.dll /
# base_library.zip). Only the standard library is used (ctypes/struct/
# argparse/os), so this Analysis stays tiny.
a2 = Analysis(
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
pyz2 = PYZ(a2.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='codewood',
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
    icon=['app_icon.ico'],
    manifest='codewood.exe.manifest',
)
exe2 = EXE(
    pyz2,
    a2.scripts,
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
    exe2,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='codewood',
)
