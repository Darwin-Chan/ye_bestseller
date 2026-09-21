# -*- mode: python ; coding: utf-8 -*-
# 交换台壳的打包配置（票 14 / ADR-0007）：产物 `dist\inventory_exchange.exe`，exe 里不放项目代码。
# 入口是薄入口 exchange_launcher.py（正文在 launcher_core.py）；它只负责推导项目根、
# 找本机 python、拉起 <项目根>\exchange.py --window。交换台与采集都来自源码目录，
# 所以这里既不带 webview / pythonnet，也不带 docs 与 bestseller_monitor。
datas = []
binaries = []
hiddenimports = []


a = Analysis(
    [str(SPECPATH) + '/exchange_launcher.py'],
    pathex=[SPECPATH],
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
    a.binaries,
    a.datas,
    [],
    name='inventory_exchange',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
