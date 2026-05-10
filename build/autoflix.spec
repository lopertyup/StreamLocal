# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for AutoFlix Desktop.

Build:
    pyinstaller build/autoflix.spec --clean --noconfirm

Output: dist/AutoFlix.exe (single file, no console, maximized pywebview).
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

block_cipher = None

package_metadata = []
for dist_name in [
    "autoflix-cli",
    "beautifulsoup4",
    "blinker",
    "click",
    "curl_cffi",
    "flask",
    "html5lib",
    "itsdangerous",
    "jinja2",
    "jsbeautifier",
    "m3u8",
    "pillow",
    "platformdirs",
    "pycryptodome",
    "pygments",
    "pystray",
    "pywebview",
    "readchar",
    "rich",
    "markupsafe",
    "werkzeug",
]:
    try:
        package_metadata += copy_metadata(dist_name)
    except Exception:
        pass

hiddenimports = [
    "autoflix_cli",
    "autoflix_cli.desktop",
    "autoflix_cli.proxy",
    "autoflix_cli.app.server",
    "autoflix_cli.app.models",
    "autoflix_cli.app.store",
    "autoflix_cli.app.encoding",
    "autoflix_cli.app.providers",
    "autoflix_cli.app.scans",
    "autoflix_cli.app.playback",
    "autoflix_cli.app.downloads",
    "autoflix_cli.app.tracker_service",
    "autoflix_cli.app.diagnostics",
    "autoflix_cli.handlers.anime_sama",
    "autoflix_cli.handlers.coflix",
    "autoflix_cli.handlers.french_stream",
    "autoflix_cli.handlers.wiflix",
    "autoflix_cli.handlers.goldenanime",
    "autoflix_cli.handlers.goldenms",
    "autoflix_cli.scraping.player",
    "autoflix_cli.scraping.manga",
    "autoflix_cli.scraping.lelscans",
    "autoflix_cli.scraping.subtitles",
    "autoflix_cli.scraping.deobfuscate",
    "webview",
    "curl_cffi",
    "curl_cffi.requests",
    "m3u8",
    "pystray",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "jsbeautifier",
    "platformdirs",
    "Crypto",
    "Crypto.Cipher",
    "Crypto.Cipher.AES",
    "html5lib",
]
hiddenimports += collect_submodules("flask")
hiddenimports += collect_submodules("werkzeug")
hiddenimports += collect_submodules("jinja2")
hiddenimports += collect_submodules("webview")
hiddenimports += collect_submodules("curl_cffi")
hiddenimports += collect_submodules("bs4")
hiddenimports += collect_submodules("Crypto")
hiddenimports += collect_submodules("pystray")
hiddenimports += collect_submodules("PIL")

a = Analysis(
    [str(ROOT / "build" / "launcher.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[
        (str(SRC / "autoflix_cli" / "app" / "static"), "autoflix_cli/app/static"),
        (str(ROOT / "data"), "data"),
        (str(ROOT / "build" / "autoflix.ico"), "build"),
    ] + package_metadata,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "matplotlib",
        "numpy",
        "pandas",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

ICON_PATH = ROOT / "build" / "autoflix.ico"

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AutoFlix",
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
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,
)
