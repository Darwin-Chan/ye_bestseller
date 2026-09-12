# -*- mode: python ; coding: utf-8 -*-
# 启动壳的打包配置：exe 里不放项目代码（IS-52 / ADR-0007）。
# 它只负责推导项目根、找本机 python、拉起 <项目根>\gui.py；界面与采集都来自源码目录，
# 所以这里既不带 webview / pythonnet，也不带 docs 与 bestseller_monitor。
datas = []
binaries = []
hiddenimports = []


a = Analysis(
    ['gui_launcher.py'],
    pathex=[],
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
    name='bestseller_gui',
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
