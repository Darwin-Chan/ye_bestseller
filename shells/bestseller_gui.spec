# -*- mode: python ; coding: utf-8 -*-
# 采集壳的打包配置（IS-52 / ADR-0007）：exe 里不放项目代码。
# 入口是薄入口 gui_launcher.py（正文在 launcher_core.py，票 14 抽芯）；它只负责推导项目根、
# 找本机 python、拉起 <项目根>\gui.py。界面与采集都来自源码目录，
# 所以这里既不带 webview / pythonnet，也不带 docs 与 bestseller_monitor。
datas = []
binaries = []
hiddenimports = []


a = Analysis(
    [str(SPECPATH) + '/gui_launcher.py'],
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
