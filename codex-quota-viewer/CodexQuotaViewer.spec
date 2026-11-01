# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台、带图标与版本信息。"""

import os

block_cipher = None

ROOT = os.path.abspath(SPECPATH)
ICON = os.path.join(ROOT, "icon.ico")
VERSION_FILE = os.path.join(ROOT, "packaging", "version_info.txt")

a = Analysis(
    [os.path.join(ROOT, "run.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[(ICON, ".")],
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "codexquota",
        "codexquota.appenv",
        "codexquota.theme",
        "codexquota.providers",
        "codexquota.events",
        "codexquota.community_api",
        "codexquota.analytics",
        "codexquota.collector",
        "codexquota.ui",
        "codexquota.glass",
        "codexquota.token_usage",
        "codexquota.icons",
        "codexquota.input_layer",
        "codexquota.pricing",
        "codexquota.lifecycle",
        "codexquota.instance",
        "codexquota.placement",
        "psutil",
        "codexquota.selftest",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 精简掉用不到的重型标准库
    excludes=[
        "tkinter",
        "PyQt6",
        "PyQt5",
        "PySide2",
        "numpy",
        "pandas",
        "matplotlib",
        "PIL",
        "scipy",
        "pytest",
        "setuptools",
        "pip",
        "unittest",
        "pydoc_data",
        "lib2to3",
        "test",
        "distutils",
        "http.server",
        "xmlrpc",
        "sqlite3",
        "multiprocessing",
        "asyncio",
        "curses",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Codex额度查看器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON if os.path.exists(ICON) else None,
    version=VERSION_FILE if os.path.exists(VERSION_FILE) else None,
)

# 常驻联动器独立打包，避免 Qt runtime hook 加载界面库。
follower_analysis = Analysis(
    [os.path.join(ROOT, "follow_codex.py")],
    pathex=[ROOT], binaries=[], datas=[],
    hiddenimports=["codexquota.lifecycle", "codexquota.instance", "psutil"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["PySide6", "shiboken6", "PyQt6", "PyQt5", "tkinter", "numpy", "pandas",
              "matplotlib", "PIL", "scipy", "pytest", "setuptools", "pip", "unittest",
              "codexquota.ui", "codexquota.glass", "codexquota.input_layer", "codexquota.icons"],
    noarchive=False,
)
follower_pyz = PYZ(follower_analysis.pure)
follower_exe = EXE(
    follower_pyz, follower_analysis.scripts, follower_analysis.binaries, follower_analysis.datas, [],
    name="Codex额度联动", debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
    icon=ICON,
    version=os.path.join(ROOT, "packaging", "follower_version_info.txt"),
)
