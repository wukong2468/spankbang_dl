# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（onedir 模式）。

打包思路
--------
* **要收**：Python 运行时 + `playwright`（含 101 MB 的 Node 驱动）+ pathvalidate
  + mutagen + 本项目代码。
* **不要收**：Playwright 自带的浏览器（chromium/firefox/webkit，1.1 GB）。
  本项目用 `channel="chrome"` 驱动**系统装的 Google Chrome**，所以一个字节都不用带。
* **运行时要求**：目标机器必须装了 Google Chrome。

产物：`dist/spankbang-dl/`，里面是 `spankbang-dl.exe` 加一个 `_internal/`。

用 onedir 而不是 onefile 的原因
------------------------------
onefile 每次启动都要把 ~120 MB 解压到 `%TEMP%\\_MEIxxxx`（慢 3~8 秒），
而且 Playwright 要拉起 Node 驱动的子进程，路径每次都在变，容易出问题。
"""

from PyInstaller.utils.hooks import collect_all

# playwright 的 driver 目录（node.exe + driver 的 js）是纯数据文件，
# PyInstaller 不会自动识别，必须显式收集。
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")

datas = list(pw_datas)
binaries = list(pw_binaries)
hiddenimports = list(pw_hidden) + [
    "playwright.async_api",
    "pathvalidate",
    "mutagen",
    "mutagen.mp4",
]

a = Analysis(
    ["packaging/entry.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 用不到，剔掉能小一点
        "tkinter",
        "unittest",
        "pydoc_data",
        "test",
        "distutils",
        "lib2to3",
        # 解析用的浏览器是系统 Chrome，不需要 playwright 的其它语言绑定
        "playwright.sync_api",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="spankbang-dl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX 压缩会让杀软误报，不加
    console=True,           # 控制台程序：要显示中文进度
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="spankbang-dl",
)
