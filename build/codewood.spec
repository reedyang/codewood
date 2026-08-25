# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('../skills', 'skills'), ('../additional-subagents', 'additional-subagents'), ('../cli', 'cli'), ('../desktop/frontend/dist', 'frontend'), ('../desktop/host', 'host'), ('../models', 'models'), ('../build/app_icon.ico', 'codewood_assets'), ('../bin/rg.exe', 'bin'), ('../bin/rg-version.txt', 'bin')]
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
# setuptools>=70 loads jaraco through pkg_resources.extern; PyInstaller's
# pyi_rth_pkgres hook needs it at process start.
tmp_ret = collect_all('jaraco')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('setuptools')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
hiddenimports += ['pkg_resources', 'jaraco', 'jaraco.text', 'jaraco.functools', 'jaraco.context', 'jaraco.collections']


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
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='codewood',
)
