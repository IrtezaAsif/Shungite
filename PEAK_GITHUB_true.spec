# -*- mode: python ; coding: utf-8 -*-
# Build with: pyinstaller PEAK_GITHUB_true.spec --noconfirm
# Output: dist/PEAK_GITHUB_true/PEAK_GITHUB_true.exe (onedir)

import os
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Fail loudly if the ffmpeg binaries are missing
_required = ["ffmpeg.exe", "ffprobe.exe"]
_missing = [b for b in _required if not os.path.isfile(os.path.join("ffmpeg", b))]
if _missing:
    raise SystemExit("Missing from ffmpeg\\ folder: " + ", ".join(_missing))

hidden = (
    collect_submodules("yt_dlp")
    + ["mutagen", "browser_cookies", "spotify_api", "login_window", "matchers", "rapidfuzz"]
    + collect_submodules("webview")
    + ["webview.platforms.edgechromium", "webview.platforms.winforms",
       "webview.platforms.windowsforms", "clr", "clr_loader", "pythonnet"]
)

a = Analysis(
    ["PEAK_GITHUB_true.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("ffmpeg/ffmpeg.exe", "ffmpeg"),
        ("ffmpeg/ffprobe.exe", "ffmpeg"),
        (r"C:\Users\Itsa\.deno\bin\deno.exe", "deno"),
        ("spotify_api.py", "."),
        ("browser_cookies.py", "."),
        ("login_window.py", "."),
        ("matchers.py", "."),
    ],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["yt_dlp_plugins", "yt_dlp_plugins.extractor",
              "yt_dlp_plugins.extractor.getpot_bgutil",
              "yt_dlp_plugins.extractor.getpot_bgutil_http",
              "yt_dlp_plugins.extractor.getpot_bgutil_script",
              "bgutil_ytdlp_pot_provider"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PEAK_GITHUB_true",
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

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PEAK_GITHUB_true",
)
