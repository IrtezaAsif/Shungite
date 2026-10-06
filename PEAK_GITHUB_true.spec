# -*- mode: python ; coding: utf-8 -*-
# Build with: pyinstaller PEAK_GITHUB_true.spec --noconfirm
# Output: dist3/Shungite/Shungite.exe (onedir, no console)

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

# deno (optional JS runtime for yt-dlp challenges): bundle when present
# locally, skip on machines without it. detect_deno() falls back to PATH at
# runtime, so a missing deno is fine.
_deno = os.path.join(os.path.expanduser("~"), ".deno", "bin", "deno.exe")
if not os.path.isfile(_deno):
    for _cand in (r"C:\Program Files\deno\deno.exe", "deno\deno.exe"):
        if os.path.isfile(_cand):
            _deno = _cand
            break
_extra_datas = [(_deno, "deno")] if os.path.isfile(_deno) else []

a = Analysis(
    ["PEAK_GITHUB_true.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("ffmpeg/ffmpeg.exe", "ffmpeg"),
        ("ffmpeg/ffprobe.exe", "ffmpeg"),
        ("spotify_api.py", "."),
        ("browser_cookies.py", "."),
        ("login_window.py", "."),
        ("matchers.py", "."),
    ] + _extra_datas,
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
    name="Shungite",
    version="version_info.txt",
    icon=r"installer/shungite.ico",
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
    name="Shungite",
)
