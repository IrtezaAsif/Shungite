"""
YOUTUBE DOWNLOAD PEAK — PREMIUM EDITION
========================================
Made By You boy Itsa

A standalone YouTube Music + Spotify downloader with:
  - Auto browser-cookie extraction (YouTube + Spotify) — pick your browser in Settings
  - 256k Opus guaranteed output
  - Personal Spotify recommendations (Made For You, Daily Mixes, top mixes)
  - Bulk CSV batch downloads (resumable)
  - Single URL / Playlist / Search / Video downloads
  - Bundled ffmpeg + auto-detect deno

GitHub-safe: no credentials or user paths hardcoded. All settings stored
in ~/.peak_config.json on first run.
"""

import yt_dlp
import shutil
import pandas as pd

# --- DPI awareness: must run BEFORE Tk and before WebView2 initializes. ---
# Without this, WebView2 (used by the embedded login window) forces the process
# to a higher DPI level and Windows bitmap-scales the Tk window down to ~25%.
import ctypes as _ct
try:
    _ct.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE_V2
except Exception:
    try:
        _ct.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import threading
import os
import re
import sys
import json
import time
import pathlib
import subprocess
import platform
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import spotify_api as sp
try:
    import spotify_oauth as spo
except ImportError:
    spo = None
from download_queue import DownloadQueue
try:
    import titanium_enrich as ten
except ImportError:
    ten = None
try:
    import innertube as itb
except ImportError:
    itb = None
try:
    import web_remote
except ImportError:
    web_remote = None

try:
    from yt_dlp.utils import DownloadCancelled
except ImportError:
    class DownloadCancelled(Exception):
        pass

try:
    import mutagen  # noqa
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

# ── Paths (all relative to the exe, nothing hardcoded) ────────────────────────
# no console flash for child processes (windowed exe)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)

FFMPEG_EXE = resource_path("ffmpeg/ffmpeg.exe")
FFPROBE_EXE = resource_path("ffmpeg/ffprobe.exe")

# Auto-detect deno: bundled -> PATH -> common locations -> None
def detect_deno():
    for c in [
        resource_path("deno/deno.exe"),
        resource_path("deno/deno"),
        os.path.join(os.path.expanduser("~"), ".deno", "bin", "deno.exe"),
        os.path.join(os.path.expanduser("~"), ".deno", "bin", "deno"),
        "/usr/local/bin/deno",
        "deno",  # in PATH
    ]:
        if os.path.exists(c):
            return c
    return None

# ── no console flashes: every child process (yt-dlp's ffmpeg, whisperX,
# deno) inherits CREATE_NO_WINDOW unless it asks for something else. This
# is what keeps the GUI window the ONLY thing on screen.
import subprocess as _sp
_orig_popen = _sp.Popen
def _quiet_popen(*args, **kw):
    if not kw.get("creationflags"):
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return _orig_popen(*args, **kw)
_sp.Popen = _quiet_popen
_orig_run = _sp.run
def _quiet_run(*args, **kw):
    if not kw.get("creationflags"):
        kw["creationflags"] = 0x08000000
    return _orig_run(*args, **kw)
_sp.run = _quiet_run

DEN_PATH = detect_deno()
if DEN_PATH:
    os.environ["JS_RUNTIMES"] = f"deno:{DEN_PATH}"

# ── Data directory: %LOCALAPPDATA%\..\LocalLow\Shungite ─────────────
def _data_dir():
    """All SHUNGITE user data lives here (LocalLow\\Shungite). Created on
    first run; the old ~/.peak_* files are migrated transparently."""
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    low = os.path.normpath(os.path.join(os.path.dirname(base), "LocalLow"))
    d = os.path.join(low, "Shungite")
    os.makedirs(d, exist_ok=True)
    return d

DATA_DIR = _data_dir()

def _data_file(name):
    """Path to a data file under LocalLow\\Shungite. Migrates a legacy
    ~/.peak_<name> file into the new home on first touch."""
    new = os.path.join(DATA_DIR, name)
    legacy = os.path.join(os.path.expanduser("~"), name)
    try:
        if not os.path.exists(new) and os.path.exists(legacy) \
                and os.path.isfile(legacy):
            shutil.copy2(legacy, new)
    except Exception:
        pass
    return new

LIBRARY_ROOT = _data_file("Music Library")
os.makedirs(LIBRARY_ROOT, exist_ok=True)
def _disk_ok(path, min_gb=2.0):
    """True if the drive holding `path` has more than min_gb free."""
    try:
        drive = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
        return shutil.disk_usage(drive).free > min_gb * 1024 ** 3
    except Exception:
        return True


DEFAULT_OUT = LIBRARY_ROOT

CONFIG_PATH = _data_file(".peak_config.json")
HISTORY_PATH = _data_file(".peak_history.json")

BROWSERS = ["firefox", "chrome", "chromium", "edge", "brave", "opera", "safari", "vivaldi"]

DEFAULT_CONFIG = {
    "output_dir": DEFAULT_OUT,
    "library_root": LIBRARY_ROOT,
    "audio_quality": "opus256",
    "max_workers": 4,
    "speed_limit_kbps": 0,
    "normalize_audio": False,
    "browser": "firefox",         # which browser to pull cookies from
    "deno_path": DEN_PATH or "",
    "spotify_enabled": True,
}

# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    return cfg

def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# Module-level config accessor. `download_one` is a module-level function (no
# `self`), so it can't reach App.cfg directly. App.__init__ binds _APP_CFG to its
# loaded config; standalone callers fall back to load_config().
_APP_CFG = None
_APP_INSTANCE = None


def _cfg_get(key, default=None):
    cfg = _APP_CFG
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
    return cfg.get(key, default) if isinstance(cfg, dict) else default

# ── History ────────────────────────────────────────────────────────────────────

def load_history():
    try:
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def append_history(entry):
    try:
        hist = load_history()
        hist.insert(0, entry)
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(hist[:500], f, indent=2)
    except Exception:
        pass

# ── yt-dlp opts (browser-cookie based) ─────────────────────────────────────────

def build_opts(download_type, save_path, outtmpl, audio_quality="opus256",
               speed_limit_kbps=0, normalize_audio=False, progress_hook=None,
               browser=None, download_archive=None):
    """All downloads use browser cookies (auto-extracted on every call).
    No cookie files to manage."""
    opts = {
        "ffmpeg_location": FFMPEG_EXE,
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "continuedl": True,
        "windowsfilenames": True,
        "writethumbnail": True,
    }

    # Browser cookies: system browser via yt-dlp, OR a user-supplied cookies.txt
    _premium_slot = audio_quality in ("opusprem", "opusnative", "flac")
    if browser and browser != "none" and not _premium_slot:
        opts["cookiesfrombrowser"] = (browser,)
    cf = _cfg_get("yt_cookies_file", "")
    if cf and os.path.isfile(cf):
        opts["cookiefile"] = cf

    if DEN_PATH and os.path.exists(DEN_PATH):
        opts["js_runtimes"] = {"deno": {"path": DEN_PATH}}

    opts["sleep_interval_requests"] = 1

    if speed_limit_kbps and speed_limit_kbps > 0:
        opts["ratelimit"] = int(speed_limit_kbps) * 1024

    if download_archive:
        opts["download_archive"] = download_archive

    if download_type == "audio":
        opts["format"] = "bestaudio/best"
        if audio_quality == "opus256":
            # Two-stage: wav forces decode, opus@256 forces the encode
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "wav"},
                {"key": "FFmpegExtractAudio", "preferredcodec": "opus", "preferredquality": "256"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]
        elif audio_quality == "best":
            # 'best' is not a valid FFmpegExtractAudio codec — it made
            # every best-quality download fail. Opus without a quality
            # value = pure stream copy for opus sources (no re-encode).
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "opus"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]
        elif audio_quality == "m4a":
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]
        elif audio_quality == "flac":
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "flac"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]
        else:
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]
    else:
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail"},
        ]

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts


def _enhance_audio(src_path, target_bitrate=256, out_codec="opus"):
    """Loss-preserving quality lift: soxr very-high resample to 48k
    then re-encode at target bitrate with opus."""
    import subprocess, os
    base, ext = os.path.splitext(src_path)
    out_path = base + "._enh." + out_codec
    enc = "libopus" if out_codec == "opus" else out_codec
    cmd = [
        FFMPEG_EXE, "-y", "-i", src_path,
        "-af", "aresample=48000:resampler=soxr:precision=28:cheby=1"
               ":dither_method=triangular",
        "-c:a", enc, "-b:a", f"{target_bitrate}k", "-vbr", "on",
        "-compression_level", "10", "-application", "audio", out_path,
    ]
    CREATE = 0x08000000 if os.name == "nt" else 0
    subprocess.run(cmd, creationflags=CREATE, check=True,
                   capture_output=True, timeout=1200)
    if os.path.exists(out_path):
        os.replace(out_path, src_path)
        return src_path
    raise RuntimeError("enhance produced no output")


def _vid_from_url(url):
    """Extract an 11-char YouTube video id from any watch/embed/be url."""
    import re as _re
    if not url:
        return ""
    m = _re.search(r"(?:v=|youtu\.be/|/embed/|/shorts/)([\w-]{11})", url)
    return m.group(1) if m else url

def download_one(title, url, save_path, dtype, quality, speed_limit,
                 normalize=False, status_cb=None, cancel_event=None,
                 browser=None, download_archive=None, cookiefile=None,
                 meta_artist="", yt_url="", subs=False):
    """Download one track — SINGLE yt-dlp call, no separate info extraction.
    The title from search is used for status display, yt-dlp handles the filename."""
    _sm = _source_mode()
    if _sm == "ytmusic":
        url = url.replace("www.youtube.com", "music.youtube.com")
    else:
        # 'youtube' and 'both' resolve to the canonical youtube host
        url = url.replace("music.youtube.com", "www.youtube.com")

    def report(msg):
        if status_cb:
            try:
                status_cb(msg)
            except Exception:
                pass
        if hasattr(_APP_INSTANCE, "_gui_queue"):
            _APP_INSTANCE._gui_queue().put(("status", msg))

    def hook(d):
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Cancelled")
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "").strip()
            spd = d.get("_speed_str", "").strip()
            report(f"⬇ {pct} @ {spd}")
        elif d.get("status") == "finished":
            report("⚙ encoding…")

    # outtmpl: use the cleaned (Artist - Title) name when the caller gave us
    # one so downloads land with the right filename even if yt-dlp's own
    # metadata title is messy (mixes, romaji, cover vids).
    if title and isinstance(title, str):
        outtmpl = os.path.join(save_path, title + ".%(ext)s")
    else:
        outtmpl = os.path.join(save_path, "%(title)s.%(ext)s")
    opts = {
        "ffmpeg_location": FFMPEG_EXE,
        "outtmpl": outtmpl,
        "quiet": True, "no_warnings": True, "noprogress": True,
        "noplaylist": True, "retries": 4, "socket_timeout": 30,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10485760,
        "continuedl": True, "windowsfilenames": True,
        "writethumbnail": True,
        "embedmetadata": True,
        "format": "bestaudio/best",
        "progress_hooks": [hook],
        # YouTube JS-challenge solver component: yt-dlp must fetch its
        # remote solver (from GitHub) when the local cache lacks it —
        # without it every download in a fresh install dies with
        # "The page needs to be reloaded" (exe-only failure; source
        # installs had it cached).
        "remote_components": ["ejs:github"],
    }
    # ── cookies: the #1 reason downloads fail is bot-checks on anonymous
    # requests. Wire the cookiefile through; fall back to the harvested
    # login cookies so a logged-in session is always used when available.
    _cf = cookiefile
    if not (_cf and os.path.isfile(_cf) and os.path.getsize(_cf) > 100):
        _harvest = _data_file(".peak_yt_cookies.txt")
        if os.path.isfile(_harvest) and os.path.getsize(_harvest) > 500:
            _cf = _harvest
        else:
            _cf = None
    if _cf and os.path.isfile(_cf):
        opts["cookiefile"] = _cf
    # Seal-style custom format override (e.g. "best[height<=720]", "bestaudio[ext=m4a]").
    cf = (_cfg_get("custom_format", "") or "").strip()
    if cf:
        opts["format"] = cf
    if _cfg_get("restrict_filenames"):
        opts["restrictfilenames"] = True
    # subtitles / proxy / aria2c (Seal-style)
    # `subs` (per-call) OR config both enable it; keep original-language
    # subtitles ('und' = the video's own language) plus en variants.
    if subs or _cfg_get("download_subtitles"):
        opts["writesubtitles"] = True
        _sl = [_cfg_get("subtitle_lang")] if _cfg_get("subtitle_lang") else ["en.*", "en", "und"]
        opts["subtitleslangs"] = _sl
        opts["writeautomaticsub"] = True
    prx = (_cfg_get("proxy", "") or "").strip()
    if prx:
        opts["proxy"] = prx
    if _cfg_get("use_aria2c", False):
        import shutil as _sh
        if _sh.which("aria2c"):
            opts["downloader"] = "aria2c"
    _pits = (_cfg_get("playlist_items", "") or "").strip()
    if _pits:
        opts["playlist_items"] = _pits

    # custom folder template: %artist%/%album%/%track% - %title%.%(ext)s
    try:
        tmpl = _cfg_get("folder_template", "") or ""
        if tmpl and "%" in tmpl:
            opts["outtmpl"] = os.path.join(
                save_path,
                tmpl.replace("%(title)s", "%(title)s")
                     .replace("%(artist)s", "%(artist)s")
                     .replace("%(album)s", "%(album)s")
                     .replace("%(track)s", "%(track_number,playlist_index)s")
                     .replace("%(ext)s", "%(ext)s"))
        else:
            opts["outtmpl"] = outtmpl
    except Exception:
        opts["outtmpl"] = outtmpl
    if _cfg_get("sponsorblock"):
        # yt-dlp marks SB categories as chapters natively (no extra API hit)
        opts["sponsorblock_mark"] = ["sponsor", "intro", "outro",
                                     "selfpromo"]

    # Cookie policy (2026-10): YouTube bot-checks anonymous mass downloads
    # hard ("Sign in to confirm you're not a bot" / format refusals). Use the
    # HARVESTED cookie jar (stable file, not the live browser session) for
    # every audio download; it carries a logged-in session without lifting
    # the user's live Firefox. Harvest file wins over browser cookies.
    _harvest = _data_file(".peak_yt_cookies.txt")
    if os.path.isfile(_harvest) and os.path.getsize(_harvest) > 500:
        opts["cookiefile"] = _harvest
    elif browser and browser != "none":
        _pq = quality in ("opusprem", "opusnative", "flac") or bool(
            _cfg_get("yt_music_browser_cookies", False))
        if _pq:
            opts["cookiesfrombrowser"] = (browser,)

    if DEN_PATH and os.path.exists(DEN_PATH):
        opts["js_runtimes"] = {"deno": {"path": DEN_PATH}}
    if speed_limit and speed_limit > 0:
        opts["ratelimit"] = int(speed_limit) * 1024
    if download_archive:
        opts["download_archive"] = download_archive

    # ── format & quality selection ──────────────────────────────────────
    _wp = bool(_cfg_get("yt_premium", False))
    # Premium cookie source: Firefox first (the live session that actually has
    # 774/141) — but only when the user explicitly picked a "premium" quality.
    _premium_quality = quality in ("opusprem",) or (quality == "opusnative" and _wp)
    if _premium_quality and not cookiefile:
        try:
            opts["cookiesfrombrowser"] = ("firefox",)
            opts.pop("cookiefile", None)
        except Exception:
            pass

    # Premium-quality request: 774/141 high formats require web_music + missing_pot.
    # Only engage the premium client for an explicit "premium" quality slot —
    # otherwise a disabled account just produces "Video unavailable" errors.
    if quality in ("opusprem",) or (quality == "opusnative" and _wp):
        try:
            _cur = opts.get("extractor_args", {}).get("youtube", {})
            _cur = dict(_cur)
            _cur["player_client"] = ["web_music"]
            _cur.setdefault("formats", []).append("missing_pot")
            opts.setdefault("extractor_args", {})["youtube"] = _cur
        except Exception:
            pass

    if not cf:
        if dtype == "video":
            # VIDEO: merge best video + best audio, mp4 container.
            _vh = int(_cfg_get("max_video_height", 0) or 0)
            if _vh > 0:
                opts["format"] = (f"bestvideo[height<={_vh}][ext=mp4]+bestaudio[ext=m4a]/"
                                  f"bestvideo[height<={_vh}]+bestaudio/best")
            else:
                opts["format"] = ("bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                                  "bestvideo+bestaudio/best")
            opts["merge_output_format"] = "mp4"

        elif quality == "opusprem":
            # Premium 256k: hidden high formats (774=256k Opus, 141=256k AAC).
            # Output .opus via FFmpegExtractAudio with NO preferredquality ->
            # pure Ogg container swap for opus sources (audio stream copied).
            opts["format"] = "774/141/251/140/bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "opus"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]

        elif quality == "opusnative":
            # Real best Opus: prefer the native Opus stream (untouched). If the
            # best available audio is NOT Opus, convert it to Opus at the
            # SOURCE's own quality (never up-re-encode to a higher bitrate,
            # which adds artifacts / "weird high pitch"). Opus->Opus is a
            # lossless same-codec pass-through (no re-encode).
            if _wp:
                # only when premium slots chain have a working web_music session
                opts["format"] = ("774[acodec=opus]/251[acodec=opus]/"
                                  "bestaudio[acodec=opus]/bestaudio/best")
            else:
                opts["format"] = "bestaudio[acodec=opus]/bestaudio/best"
            opts["writethumbnail"] = False
            # ExtractAudio with preferredcodec=opus: if source is already opus
            # yt-dlp remuxes it (no re-encode); if m4a/aac, converts to opus at
            # a transparent VBR that does NOT inflate the source bitrate.
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "opus",
                 "nopostoverwrites": False},
                {"key": "FFmpegMetadata", "add_metadata": True},
            ]

        elif quality == "opus256":
            # Highest Opus (re-encode only if a target bitrate is set).
            opts["format"] = "bestaudio[acodec=opus]/bestaudio/best"
            _brmax = int(_cfg_get("audio_bitrate_max", 0) or 0)
            _pp_opus = {"key": "FFmpegExtractAudio", "preferredcodec": "opus"}
            if _brmax > 0:
                _pp_opus["preferredquality"] = str(_brmax)
            opts["postprocessors"] = [
                _pp_opus,
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]

        elif quality == "best":
            # NOTE: FFmpegExtractAudio has no 'copy' codec — 'copy' made
            # every 'best' download fail with postprocessor error. The
            # format already selects opus; 'opus' + no quality = stream
            # copy into .opus (no re-encode).
            opts["format"] = "bestaudio[acodec=opus]/bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "opus"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]

        elif quality == "m4a":
            # Best M4A: AAC stream copy when the source is AAC (most YT
            # music videos serve m4a/AAC audio) — re-encode otherwise.
            opts["format"] = ("bestaudio[acodec=aac]/bestaudio/best")
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]

        elif quality == "flac":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "flac"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]

        else:  # mp3
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                 "preferredquality": "320"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]



        current_opts = dict(opts)
        if subs:
            current_opts["writesubtitles"] = True
            current_opts["subtitleslangs"] = ["en", "en-US", "en-GB"]
            current_opts["writeautomaticsub"] = False

        for attempt in range(4):
            try:
                # spacing between attempts so YouTube doesn't rate-lump bulk jobs
                if attempt > 0:
                    time.sleep(1 + attempt)
                with yt_dlp.YoutubeDL(current_opts) as ydl:
                    ydl.download([url])
                report(f"✓ {title[:30]}")
                append_history({"date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                               "title": title, "url": url, "type": dtype,
                               "quality": quality, "path": save_path})
                try:
                    import glob as _g
                    _p = os.path.join(save_path,
                                      title.replace("/", "_")[:180] + ".*")
                    _files = _g.glob(_p)
                    _sz = os.path.getsize(_files[0]) if _files else 0
                except Exception:
                    _sz = 0
                try:
                    import history_db as _hdb0
                    _hdb0.record(title, "", dtype, url, "ok", _sz)
                except Exception:
                    pass
                # TITANIUM: lyrics + cover art in background (never blocks)
                try:
                    import titanium_enrich as _ten_n
                    _apath = (_files[0] if _files else
                              os.path.join(save_path, title.replace("/", "_")[:180]))
                    if _cfg_get("enhance_audio", False):
                        import mutagen as _mgx
                        try:
                            _inf = _mgx.File(_apath)
                            if _inf and _inf.info and getattr(_inf.info, "bitrate", 0) < 240000:
                                _enhance_audio(_apath, 256, "opus")
                        except Exception:
                            pass
                    if _cfg_get("normalize_loudness"):
                        try:
                            _ten_n.normalize_loudness(_apath)
                        except Exception:
                            pass
                    if _cfg_get("replaygain", False):
                        try:
                            _ten_n.compute_replaygain(_apath)
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    if _cfg_get("subsonic_push") and _APP_INSTANCE is not None:
                        _APP_INSTANCE._subsonic_scan()
                except Exception:
                    pass
                if ten and ENRICH_ENABLED[0]:
                    _ma = meta_artist
                    _yu = yt_url or url
                    def _enrich():
                        try:
                            import glob as _g2
                            _real = None
                            for ext in (".opus", ".mp3", ".m4a", ".flac"):
                                _c = _g2.glob(os.path.join(
                                    save_path,
                                    title.replace("/", "_")[:180] + ext))
                                if _c:
                                    _real = _c[0]
                                    break
                            if not _real and title:
                                _tot = title.split(" - ")[0].strip()[:40]
                                for cand in _g2.glob(os.path.join(save_path, "*")):
                                    if os.path.splitext(cand)[1].lower() not in (".opus", ".mp3", ".m4a", ".flac"):
                                        continue
                                    if _tot and _tot.lower() in os.path.basename(cand).lower():
                                        _real = cand
                                        break
                            if _real:
                                base = os.path.splitext(_real)[0]
                                # the file we just downloaded is authoritative:
                                # we know its true duration. Use it for
                                # LRClib's duration-matched lyrics lookup.
                                try:
                                    import mutagen as _mg_dur
                                    _audio = _mg_dur.File(_real)
                                    _dur_s = (int(round(_audio.info.length))
                                              if _audio and _audio.info
                                              and getattr(_audio.info, "length", None)
                                              else None)
                                    if _dur_s:
                                        try:
                                            setattr(_enrich, "_last_dur", _dur_s)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                # if a .vtt landed next to the audio, convert it
                                # into a .lrc (properly timed original-language
                                # lyrics) BEFORE falling back to lrclib/genius
                                _vtt = base + ".en-US.vtt"
                                try:
                                    from titanium_enrich import vtt_to_lrc
                                    if not os.path.exists(base + ".lrc"):
                                        for lang_pref in (".en-US.vtt", ".en.vtt",
                                                         ".und.vtt", ".vtt"):
                                            _cand = base + lang_pref
                                            if os.path.exists(_cand):
                                                if vtt_to_lrc(_cand, base + ".lrc"):
                                                    break
                                except Exception:
                                    pass
                                need = (not os.path.exists(base + ".lrc")
                                        or not os.path.exists(base + ".jpg"))
                                if need or EMBED_ART[0]:
                                    ten.enrich_file(
                                        _real, title, _ma,
                                        do_embed=EMBED_ART[0],
                                        yt_url=_yu,
                                        duration_s=getattr(
                                            _enrich, "_last_dur", None),
                                        fetch_sponsorblock=bool(
                                            _cfg_get("sponsorblock", True)))
                        except Exception:
                            pass
                    threading.Thread(target=_enrich, daemon=True).start()
                # post-process: write bitrate into the audio file's tags so
                # Windows Explorer / players show it in Properties.
                try:
                    import glob as _gg, os as _os
                    cands = []
                    for ext in (".opus", ".mp3", ".m4a", ".flac"):
                        cands.extend(_gg.glob(_os.path.join(save_path,
                                         title.replace("/", "_")[:180] + ext)))
                    _written = cands[0] if cands else None
                    if _written:
                        try:
                            from mutagen import File as _mfile
                            _af = _mfile(_written, easy=False)
                            # mutagen exposes bitrate on .info for most types
                            _br = getattr(getattr(_af, "info", None), "bitrate", 0)
                            if _br:
                                # make sure the standard "bitrate" field is filled
                                for key in ("bitrate", "BITRATE", "DESCRIPTION"):
                                    try:
                                        if key.lower() == "description":
                                            continue
                                        if hasattr(_af, "__setitem__"):
                                            _af[key] = str(_br)
                                            _af.save()
                                            break
                                    except Exception:
                                        continue
                        except Exception:
                            pass
                except Exception:
                    pass
                return ("ok", "")
            except DownloadCancelled:
                return ("cancelled", "")
            except Exception as e:
                msg = str(e)
                if "rate-limited" in msg.lower():
                    report("⏳ rate-limited — cooling 2 min…")
                    time.sleep(120)
                    continue
                if "page needs to be reloaded" in msg.lower():
                    # YouTube bot/check — flip to the TV client: it
                    # serves full audio formats WITHOUT a PO token.
                    # (web_embedded was wrong for music: embedded players
                    # get NO audio formats -> 'format not available'.)
                    if attempt == 0 or attempt == 1:
                        try:
                            _cur = current_opts.get("extractor_args", {}).get(
                                "youtube", {})
                            _cur = dict(_cur)
                            _cur["player_client"] = ["tv"]
                            current_opts.setdefault("extractor_args", {})[ "youtube"] = _cur
                            report("↻ bot-check — switching to TV client…")
                        except Exception:
                            pass
                    # pace hard so ongoing bulk jobs stay under the radar
                    time.sleep(3 + attempt * 4)
                    if attempt < 3:
                        continue
                    report(f"✗ blocked")
                    _dbg("DL FAIL blocked url=%s err=%s" % (str(url)[:60], str(msg)[:120]))
                    return ("failed", msg[:150])
                if "403" in msg and attempt < 3:
                    time.sleep(1)
                    continue
                if "Requested format is not available" in msg.lower():
                    # Premium IDs (774/141) missing OR the web_embedded
                    # fallback client returned NO formats at all. Demote to
                    # the universal chain, drop the embedded-client override,
                    # and retry while attempts remain.
                    cur_f = current_opts.get("format", "") or ""
                    if "774" in cur_f or "141" in cur_f:
                        current_opts["format"] = "bestaudio/best"
                        report("↻ premium format missing — falling back to bestaudio/best")
                    elif "bestaudio" in cur_f:
                        current_opts["format"] = "bestaudio/best"
                        report("↻ format unavailable — demoting to bestaudio/best")
                    try:
                        _e = current_opts.get("extractor_args", {}).get(
                            "youtube", {})
                        if _e.get("player_client"):
                            _e = dict(_e)
                            _e.pop("player_client", None)
                            current_opts.setdefault("extractor_args", {})["youtube"] = _e
                    except Exception:
                        pass
                    if attempt < 3:
                        time.sleep(1 + attempt)
                        continue

                _needs_cookies = ("sign in" in msg.lower()
                                  or "requested format is not available" in msg.lower()
                                  or "not available" in msg.lower())
                if _needs_cookies and not current_opts.get("cookiefile"):
                    # one-shot cookie fallback — the webview (embedded-login) cookies
                    # save anonymous runs that hit YouTube's "Sign in" wall.
                    _cff = _data_file(".peak_yt_cookies.txt")
                    if os.path.isfile(_cff):
                        current_opts["cookiefile"] = _cff
                        current_opts.pop("cookiesfrombrowser", None)
                        if attempt < 3:
                            report("↻ anon failed — retrying with saved cookies…")
                            time.sleep(2 + attempt)
                            continue

                if "unavailable" in msg.lower():
                    # Don't trust "unavailable" on the first hit — it hides
                    # region-lock walls, age gates, PO-token checks, bot walls.
                    # Try saved cookies + TV client before believing it.
                    _cff = _data_file(".peak_yt_cookies.txt")
                    _did_fallback = False
                    if not current_opts.get("cookiefile") and os.path.isfile(_cff):
                        current_opts["cookiefile"] = _cff
                        current_opts.pop("cookiesfrombrowser", None)
                        _did_fallback = True
                        report("↻ unavailable — trying saved cookies…")
                    try:
                        _e = current_opts.get("extractor_args", {}).get("youtube", {}) or {}
                        if _e.get("player_client") != ["tv"]:
                            _e = dict(_e); _e["player_client"] = ["tv"]
                            current_opts.setdefault("extractor_args", {})[ "youtube"] = _e
                            _did_fallback = True
                    except Exception:
                        pass
                    if _did_fallback and attempt < 3:
                        time.sleep(2 + attempt * 2)
                        continue
                    report(f"✗ video gone")
                    _dbg("DL FAIL gone url=%s err=%s" % (str(url)[:60],
                                                          str(msg)[:120]))
                    return ("failed", msg[:150])
                if attempt >= 3:
                    _dbg("DL FAIL generic url=%s err=%s" % (
                        str(url)[:60], str(msg)[:120]))
                    report(f"✗ {msg[:40]}")
                    return ("failed", msg[:150])
                time.sleep(1)
    try:
        _dbg("DL FAIL retries url=%s err=%s" % (str(url)[:60],
                                                str(msg)[:120] if 'msg' in dir() else "?"))
    except Exception:
        pass
    return ("failed", "retries exhausted")


def _source_mode():
    """YT Music vs YouTube vs both. Values: 'both', 'ytmusic', 'youtube'."""
    return (_cfg_get("source_mode", "") or "both").strip().lower()


def _watch_url(vid, mode=None):
    """Build a watch URL using the chosen source host for the given video id."""
    mode = mode or _source_mode()
    if mode == "youtube":
        return "https://www.youtube.com/watch?v=" + vid
    if mode == "ytmusic":
        return "https://music.youtube.com/watch?v=" + vid
    return "https://www.youtube.com/watch?v=" + vid   # 'both' -> canonical


def _dbg(m):
    """One-line search/download trace for diagnosing frozen-exe issues."""
    try:
        import datetime as _dtt
        with open(os.path.join(DATA_DIR, "search_debug.log"), "a",
                  encoding="utf-8") as _f:
            _f.write("%s %s\n" % (_dtt.datetime.now().strftime("%H:%M:%S"), m))
    except Exception:
        pass


def search_youtube(query, n=8, browser=None, source=None,
                   cap_on=True, cap_min=40, cap_max=720):
    """Search YT Music (InnerTube) and/or YouTube depending on `source`.
    source: None=global cfg | 'ytmusic' | 'youtube' | 'both'."""
    sm = (source or _source_mode()).lower()
    # InnerTube fast path (YT Music API): no subprocess, real artist names.
    try:
        if itb and sm in ("ytmusic", "both"):
            it = []
            for r in itb.search_music(query, n):
                secs = r.get("seconds")
                dur_str = (f"{secs // 60}:{secs % 60:02d}"
                           if secs else "")
                it.append({"title": r["title"],
                           "url": _watch_url(r["videoId"]),
                           "channel": r.get("artist") or "",
                           "duration": dur_str,
                           "seconds": secs,
                           "id": r["videoId"]})
            if len(it) >= min(n, 3):
                # one extra flat ytsearch when InnerTube lost durations — gives
                # the strict gate what it needs to kill mixes/covers.
                if any(r.get("seconds") is None for r in it):
                    try:
                        _o = {"quiet": True, "extract_flat": True,
                              "skip_download": True, "no_warnings": True}
                        if browser and browser != "none":
                            _o["cookiesfrombrowser"] = (browser,)
                        if DEN_PATH:
                            _o["js_runtimes"] = {"deno": {"path": DEN_PATH}}
                        with yt_dlp.YoutubeDL(_o) as _y:
                            _fl = _y.extract_info(f"ytsearch{n}:{query}",
                                                  download=False)
                        _byid = {e["id"]: e for e in
                                 (_fl or {}).get("entries", []) or [] if e}
                        for _r in it:
                            if _r.get("seconds") is None:
                                _e = _byid.get(_r.get("id"))
                                if _e and isinstance(_e.get("duration"), int):
                                    _r["seconds"] = _e["duration"]
                                    _r["duration"] = f"{_e['duration']//60}:{_e['duration']%60:02d}"
                                    if _e.get("view_count"):
                                        _r["views"] = _e["view_count"]
                    except Exception:
                        pass
                _dbg("innertube q=%r n=%d" % (query[:50], len(it)))
                return it
        _dbg("innertube EMPTY q=%r" % query[:50])
    except Exception as _ei:
        _dbg("innertube EXC q=%r err=%s" % (query[:50], str(_ei)[:90]))
    if sm == "ytmusic":
        return []
    opts = {"quiet": True, "extract_flat": True, "skip_download": True,
            "no_warnings": True}
    _harvest = _data_file(".peak_yt_cookies.txt")
    if os.path.isfile(_harvest) and os.path.getsize(_harvest) > 500:
        opts["cookiefile"] = _harvest
    elif browser and browser != "none":
        opts["cookiesfrombrowser"] = (browser,)
    try:
        _dbg("search q=%r cookiefile=%s mode=%s" % (
            query[:60], bool(opts.get("cookiefile")), sm))
    except Exception:
        pass
    if DEN_PATH:
        opts["js_runtimes"] = {"deno": {"path": DEN_PATH}}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    except Exception as _e:
        _dbg("ytsearch FAILED q=%r err=%s" % (query[:50], str(_e)[:90]))
        raise
    entries = (info or {}).get("entries") or []
    _dbg("ytsearch q=%r entries=%d" % (query[:50], len(entries)))
    if not entries:
        # ytsearch bot-walled: last resort = YT MUSIC search endpoint
        # (different backend from plain ytsearch).
        try:
            with yt_dlp.YoutubeDL(dict(opts)) as _y2:
                _i2 = _y2.extract_info(f"ytmusicsearch{n}:{query}",
                                       download=False)
            entries = (_i2 or {}).get("entries") or []
            _dbg("ytmusicsearch q=%r entries=%d" % (query[:50],
                                                    len(entries)))
        except Exception as _e2:
            _dbg("ytmusicsearch FAILED q=%r err=%s" % (
                query[:50], str(_e2)[:90]))
        if entries:
            info = {"entries": entries}
    results = []
    for e in (info or {}).get("entries", []) or []:
        if not e:
            continue
        dur = e.get("duration")
        dur_str = f"{dur // 60}:{dur % 60:02d}" if isinstance(dur, int) else ""
        results.append({"id": e.get("id"), "title": e.get("title") or "?",
                        "channel": e.get("uploader") or "", "duration": dur_str,
                        "seconds": dur if isinstance(dur, int) else None,
                        "views": e.get("view_count") or 0,
                        "url": _watch_url(e.get("id") or "")})
    return results


def _parse_secs(s):
    """'3:58' -> 238. Accepts 'h:mm:ss' too. None if unparseable."""
    if not s:
        return None
    try:
        parts = [int(p) for p in str(s).split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except Exception:
        return None
    return None


_BAD_WORDS = ("karaoke", "cover", "nightcore", "8d", "8-d", "sped up",
              "slowed", "reverb", "live", "remix", "mashup", "tribute",
              "instrumental", "acoustic", "parody", "fan made", "amv",
              "reaction", "dance practice", "teaser", "trailer", "snippet")


def _norm_txt(s):
    import re as _r, unicodedata as _u
    s = _u.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not _u.combining(c))
    return _r.sub(r"[^a-z0-9 ]+", " ", s)


def pick_best(results, target_seconds=None, artist="", title="",
              browser=None, cookiefile=None, enrich_top=0):
    """Score ytsearch results and return the best match.

    score = views_score + duration_score + similarity - junk penalties
      - views_score: log10(view_count+1) * 2 (popularity — user wants the
        most-viewed upload of the song, not a random one)
      - duration_score: up to +10 when target_seconds known, decays with
        |dur - target|; result is REJECTED if off by >20% (>8s floor) —
        that's how covers/8D/song-vs-edit mismatches get filtered
      - similarity: difflib ratio of normalized '{artist} {title}' vs the
        result's '{channel} {title}', * 6
      - junk penalty: -8 per bad word (cover/nightcore/live...) unless the
        query title itself contains that word
    Views are enriched via a cheap yt-dlp metadata fetch on only the top
    `enrich_top` candidates (flat ytsearch entries lack view_count).
    """
    import difflib
    if not results:
        return None

    def norm_dur(r):
        if r.get("seconds"):
            return r["seconds"]
        return _parse_secs(r.get("duration"))

    q_t = _norm_txt(f"{artist} {title}" if artist else title)
    q_lower = q_t

    # enrich views on a few candidates with yt-dlp (metadata only, fast)
    cand = list(results)[: max(enrich_top, 0)]
    rest = list(results)[max(enrich_top, 0):]
    if cand:
        opts = {"quiet": True, "skip_download": True, "no_warnings": True,
                "extract_flat": False}
        if browser and browser != "none":
            opts["cookiesfrombrowser"] = (browser,)
        if cookiefile:
            opts["cookiefile"] = cookiefile
        if DEN_PATH:
            opts["js_runtimes"] = {"deno": {"path": DEN_PATH}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                for r in cand:
                    if r.get("views"):
                        continue
                    try:
                        info = ydl.extract_info(
                            "https://www.youtube.com/watch?v=" + (r.get("id") or ""),
                            download=False)
                        if info:
                            r["views"] = info.get("view_count") or 0
                            if not r.get("seconds") and info.get("duration"):
                                r["seconds"] = int(info["duration"])
                    except Exception:
                        r.setdefault("views", 0)
        except Exception:
            pass
    results = cand + rest

    import math
    best, best_score = None, float("-inf")
    for r in results:
        dur = norm_dur(r)
        # hard caps: songs are ~1-12 min. anything longer is a mix/comp/
        # full-album upload — never the track the user asked for.
        if dur and (dur > 720 or dur < 40):
            continue
        if target_seconds and 30 <= target_seconds <= 900 and dur:
            diff = abs(dur - target_seconds)
            tol = max(8, 0.20 * target_seconds)
            if diff > tol and diff > 12:
                continue                      # wrong song version — skip
            dur_score = 10.0 * max(0.0, 1.0 - diff / max(target_seconds, 1))
        else:
            dur_score = 5.0                   # unknown/ignored target: neutral
        views_score = math.log10((r.get("views") or 0) + 1) * 2.0
        hay = _norm_txt(f'{r.get("channel", "")} {r.get("title", "")}')
        sim = difflib.SequenceMatcher(None, q_t, hay).ratio() * 6.0 if q_t else 0.0
        pen = 0.0
        t_low = _norm_txt(r.get("title", ""))
        for w in _BAD_WORDS:
            if w in t_low and w not in q_lower:
                pen += 8.0
        score = views_score + dur_score + sim - pen
        if score > best_score:
            best, best_score = r, score
    return best or (results[0] if results else None)


# ── GUI ────────────────────────────────────────────────────────────────────────

BG, BG2, BG3 = "#0a0a0b", "#121214", "#17181a"   # matt black
FG, FG2 = "#f5f5f4", "#9ca3af"                 # white on black
RED, GREEN, GOLD = "#ef4444", "#22c55e", "#e5e5e5"  # gold slot = white accent
STEEL = "#3f3f46"     # dark border zinc
SILVER = "#fafafa"    # pure white highlight
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
FONT_SM = ("Segoe UI", 8)
WATERMARK = "Made By You boy Itsa"
ENRICH_ENABLED = [True]     # fetch lyrics + cover after downloads
EMBED_ART = [True]          # embed art into audio tags



def _root_default():
    try:
        return tk._default_root or tk.Tk()
    except Exception:
        return None

def style(s):
    s.theme_use("clam")
    s.configure("TNotebook", background=BG, borderwidth=0)
    s.configure("TNotebook.Tab", background=BG2, foreground=FG2,
                padding=[18, 9], font=FONT_B, borderwidth=0)
    s.map("TNotebook.Tab",
          background=[("selected", "#f5f5f4")],
          foreground=[("selected", "#0a0a0b"), ("!selected", FG2)])
    # Treeview + scrollbars in matt black too
    s.configure("Treeview", background=BG2, foreground=FG,
                fieldbackground=BG2, borderwidth=0, rowheight=24)
    s.configure("Treeview.Heading", background=BG3, foreground=FG2,
                relief="flat")
    s.map("Treeview", background=[("selected", BG3)])
    s.configure("TProgressbar", troughcolor=BG3, bordercolor=BG2)
    s.configure("TCombobox", fieldbackground=BG3, background=BG3, foreground=FG,
                arrowcolor=FG, font=FONT)
    s.configure("TCombobox", selectbackground=BG3, selectforeground=FG)
    # dropdown popup listbox: dark bg, white text, readable selection
    # Bind options to the ACTUAL root window so the combobox popup (a child
    # Toplevel of root) inherits them. The old _root_default() dance returned a
    # stale/None root so these never reached the real popup -> dark-on-dark text.
    _rt = getattr(sys.modules.get(__name__), "ROOT_TK", None)
    if _rt is None:
        _rt = getattr(s, "master", None) or _root_default()
    if _rt is not None:
        _rt.option_add("*TCombobox*Listbox.background", BG3)
        _rt.option_add("*TCombobox*Listbox.foreground", FG)
        _rt.option_add("*TCombobox*Listbox.selectBackground", STEEL)
        _rt.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        _rt.option_add("*TCombobox*Listbox.font", FONT)
    s.configure("TProgressbar", troughcolor=BG2, background=RED, thickness=6)
    s.configure("Treeview", background=BG3, fieldbackground=BG3, foreground=FG,
                font=FONT, rowheight=26, borderwidth=0)
    s.configure("Treeview.Heading", background=BG2, foreground=FG2, font=FONT_B)
    s.map("Treeview", background=[("selected", "#333")])


_sched_last = {"date": None}



def _norm_q(s):
    import re as _re
    return _re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def _probe_meta(vid):
    """Flat-fetch a video's real (channel, duration, title). Cached, quiet."""
    try:
        if not vid:
            return ("", 0, "")
        vid = vid.strip().rsplit("=", 1)[-1].strip()
        cache = getattr(_probe_meta, "_c", None)
        if cache is None:
            cache = _probe_meta._c = {}
        if vid in cache:
            return cache[vid]
        import yt_dlp
        _o = {"quiet": True, "extract_flat": True, "skip_download": True,
              "no_warnings": True}
        try:
            _ck = _data_file(".peak_yt_cookies.txt")
            if os.path.isfile(_ck) and os.path.getsize(_ck) > 500:
                _o["cookiefile"] = _ck
        except Exception:
            pass
        with yt_dlp.YoutubeDL(_o) as _y:
            _i = _y.extract_info(
                "https://www.youtube.com/watch?v=%s" % vid, download=False)
        _ch, _du, _ti = "", 0, ""
        if _i:
            _ch = (_i.get("channel") or _i.get("uploader") or "").lower()
            _ti = (_i.get("title") or "")
            try:
                _du = int(_i.get("duration") or 0)
            except Exception:
                _du = 0
        cache[vid] = (_ch, _du, _ti)
        return (_ch, _du, _ti)
    except Exception:
        return ("", 0, "")

def _loose_match(results, track, artist):
    """Conservative last-resort matcher: artist must appear in title/channel,
    title similarity >= 0.32, no junk tokens. Returns most-viewed survivor."""
    import re as _re
    import matchers as _mt
    _n = _mt._norm
    tq, aq = _n(track), _n(artist)
    a_tok = set(t for t in aq.split() if len(t) > 2)
    if not a_tok:
        return None
    junk = ("1 hour", "1hour", "mix", "full album", "reaction", "reacts",
            "nightcore", "speed up", "sped up", "8d", "karaoke",
            "instrumental", "cover by", "live at", "tutorial")
    best, best_v = None, -1
    for r in results or []:
        ti = _n(r.get("title", ""))
        ch = _n(r.get("channel", ""))
        blob = ti + " " + ch
        _raw = (r.get("title") or "")
        _ratio_raw = _mt._ratio(tq, _norm_q(_raw))
        _tch = (r.get("channel") or "").lower()
        _topic_bridge = (_ratio_raw >= 0.55
                         and ("topic" in _tch
                              or "official" in _raw.lower()))
        if not any(t in blob for t in a_tok) and not _topic_bridge:
            continue
        if any(j in blob for j in junk):
            continue
        sim = _mt._ratio(tq, ti)
        if sim < 0.32 and _topic_bridge:
            # normalized title lost the match to punctuation, but the raw
            # title is a strong hit on an official/Topic channel
            sim = _ratio_raw
        if sim < 0.32:
            continue
        v = r.get("views") or 0
        if v > best_v:
            best_v, best = v, r
    return best


def _match_track(track, artist, browser, prefs=None, n=8, dur_s=None):
    """One pipeline for every tab: search -> strict -> loose -> rescue query."""
    _src = (prefs or {}).get("source", None)
    _con = bool((prefs or {}).get("cap_on", True))
    _cmin = (prefs or {}).get("cap_min", 40)
    _cmax = (prefs or {}).get("cap_max", 720)
    # Spotify CSV gives the REAL duration — tighten the window around it
    # (+/-25%) to kill live/short covers when we know the target length.
    if dur_s:
        _cmin = max(15, int(dur_s * 0.75))
        _cmax = int(dur_s * 1.25) + 15
        _con = True
    q = (track + " " + artist).strip()
    try:
        results = search_youtube(q, n, browser, source=_src,
                                 cap_on=_con, cap_min=_cmin, cap_max=_cmax)
    except Exception:
        return None, []
    matched = None
    if results:
        try:
            import matchers as _mt
            matched = _mt.strict_match(results, track, artist,
                                       caps_on=_con, cap_min_s=_cmin,
                                       cap_max_s=_cmax)
        except Exception as _merr:
            _dbg("MATCHER CRASH track=%r err=%s" % (str(track)[:30],
                                                   str(_merr)[:100]))
            try:
                matched = pick_best(results, artist=artist, title=track)
            except Exception:
                matched = None
        if not matched:
            try:
                matched = _loose_match(results, track, artist)
            except Exception:
                matched = None
    # RESCUE 1: if the survivor is unofficial (reupload/lyric channel)
    # or duration-less, try the targeted 'official audio' query once.
    def _is_official(c):
        if not isinstance(c, dict):
            return False
        ch = (c.get("channel") or "").strip().lower()
        return ch in ("song", "video", "songs") or "topic" in ch or \
               (artist and artist.lower() in ch)
    if matched and (not _is_official(matched)
                    or matched.get("seconds") is None):
        try:
            r3 = search_youtube(q + " official audio", n, browser,
                                source=_src, cap_on=_con,
                                cap_min=_cmin, cap_max=_cmax)
            if r3:
                try:
                    import matchers as _mt3
                    m3 = _mt3.strict_match(r3, track, artist,
                                            caps_on=_con, cap_min_s=_cmin,
                                            cap_max_s=_cmax)
                except Exception:
                    m3 = None
                if m3 and _is_official(m3) and \
                        (m3.get("seconds") or matched.get("seconds") is None):
                    matched = m3
        except Exception:
            pass
    if not matched:
        try:
            _simple_t = re.sub(r"\s*\([^)]*\)\s*", " ", track)
            _simple_t = re.sub(r"\s*-\s*[^-]*\s*$", "", _simple_t)
            _simple_t = re.sub(r"\s+", " ", _simple_t).strip()
            if _simple_t and _norm_q(_simple_t) != _norm_q(track):
                r2 = search_youtube(artist + " " + _simple_t, n, browser,
                                    source=_src, cap_on=_con,
                                    cap_min=_cmin, cap_max=_cmax)
                if r2:
                    try:
                        import matchers as _mt2
                        matched = _mt2.strict_match(r2, track, artist,
                                                    caps_on=_con,
                                                    cap_min_s=_cmin,
                                                    cap_max_s=_cmax)
                    except Exception:
                        matched = None
                    if not matched:
                        try:
                            matched = _loose_match(r2, track, artist)
                        except Exception:
                            matched = None
        except Exception:
            pass
    return matched, results




def _post_download_fixups(music, want_fn, track, artist, res_title="",
                          genre=""):
    """Synchronous post-download guarantee, shared by EVERY tab (was batch-CSV
    only): Spotify title/artist/genre tags, timed .lrc from YouTube subtitles,
    LRClib fallback keyed by real duration, lyrics embedded into the file
    tags, leftover sidecars cleaned up. Runs in the worker thread."""
    actual = want_fn + ".opus"
    _f = os.path.join(music, actual)
    if not genre:
        try:
            from titanium_enrich import itunes_genre
            genre = itunes_genre(track, artist)
        except Exception:
            genre = ""
    try:
        if os.path.exists(_f):
            import mutagen as _mgw
            _af = _mgw.File(_f, easy=True)
            if _af is not None:
                _af["title"] = track
                _af["artist"] = artist
                if genre:
                    _af["genre"] = genre
                _af.save()
    except Exception:
        pass
    _base = os.path.join(music, want_fn)
    try:
        if not os.path.exists(_base + ".lrc"):
            import glob as _gl
            from titanium_enrich import vtt_to_lrc
            _vtts = sorted(_gl.glob(_gl.escape(_base) + "*.vtt"),
                           key=lambda x: len(x))
            for _cand in _vtts:
                if vtt_to_lrc(_cand, _base + ".lrc"):
                    break
    except Exception:
        pass
    try:
        if not os.path.exists(_base + ".lrc"):
            import mutagen as _mgl
            _af2 = _mgl.File(_f)
            _ds = (int(round(_af2.info.length))
                   if _af2 and _af2.info
                   and getattr(_af2.info, "length", None)
                   else None)
            if _ds:
                from titanium_enrich import fetch_lyrics
                fetch_lyrics(artist, track, _f, duration_s=_ds)
    except Exception:
        pass
    try:
        if os.path.exists(_base + ".lrc"):
            import mutagen as _mge
            _lrc_txt = open(_base + ".lrc", encoding="utf-8",
                            errors="replace").read()
            _afe = _mge.File(_f, easy=True)
            if _afe is not None:
                _afe["lyrics"] = _lrc_txt
                _afe.save()
    except Exception:
        pass
    try:
        import glob as _gc
        _stem = _gc.escape(os.path.join(music, want_fn))
        _junks = (_gc.glob(_stem + "*.vtt")
                  + _gc.glob(_stem + "*.lrc")
                  + _gc.glob(_stem + "*.info.json")
                  + _gc.glob(_stem + "*.segments.json")
                  + _gc.glob(_stem + "*.webp")
                  + _gc.glob(_stem + "*.jpg")
                  + _gc.glob(_stem + "*.temp.*"))
        # thumbnails yt-dlp leaves under slightly different stems
        # (e.g. 'want_fn <videoid>.jpg' / 'want_fn.jpg' variants)
        _wf_low = want_fn.lower()
        for f2 in os.listdir(music):
            if f2.lower().endswith((".jpg", ".webp", ".png")):
                if f2.lower().startswith(_wf_low[:60]):
                    _junks.append(os.path.join(music, f2))
        for _junk in _junks:
            try:
                os.remove(_junk)
            except Exception:
                pass
    except Exception:
        pass
    try:
        old_guess = re.sub(r'[<>:"/\\|?*]', "_",
                           (res_title or "")).strip(" .")[:200] + ".opus"
        _o = os.path.join(music, old_guess)
        _n = os.path.join(music, actual)
        if old_guess != actual and os.path.exists(_o) \
                and not os.path.exists(_n):
            os.replace(_o, _n)
    except Exception:
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        global _APP_CFG
        _APP_CFG = self.cfg
        self._gq = None
        self._gui_queue()  # start the pump immediately
        global _APP_INSTANCE
        _APP_INSTANCE = self
        try:
            import titanium_enrich as _te_init
            _te_init.set_embed_only(bool(self.cfg.get("embed_only", True)))
            _te_init.set_cover_source("itunes", bool(self.cfg.get("cover_itunes", True)))
            _te_init.set_cover_source("deezer", bool(self.cfg.get("cover_deezer", True)))
            _te_init.set_cover_source("lastfm", bool(self.cfg.get("cover_lastfm", True)))
        except Exception:
            pass
        root.title("SHUNGITE — YouTube Music + Spotify Downloader")
        root.geometry("1020x820")
        root.configure(bg=BG)
        root.resizable(True, True)
        self._cancel = {}
        self.dq = DownloadQueue(workers=int(self.cfg.get("max_workers", 3)))
        self._lib_lock = threading.Lock()
        self._last_failed = []   # retry descriptors from last run
        style(ttk.Style())
        self._header()
        self._notebook()
        self._footer()
        # ── First-run WIZARD: guides new users (folder → login → done) ──
        try:
            self._maybe_run_wizard()
        except Exception:
            pass

    def _maybe_run_wizard(self):
        """One-time setup wizard on first launch. Steps:
        1. Welcome  2. Pick Music library folder  3. Optional YouTube login
        4. Finish. Writes config so it never shows again."""
        if self.cfg.get("wizard_done"):
            return
        win = tk.Toplevel(self.root)
        win.title("SHUNGITE — Setup Wizard")
        win.geometry("560x400")
        win.configure(bg=BG)
        win.grab_set()
        step = {"n": 0}
        chosen = {"lib": LIBRARY_ROOT}

        def go(n):
            step["n"] = n
            for w in win.winfo_children():
                w.destroy()
            if n == 0:
                tk.Label(win, text="◆ SHUNGITE", font=("Segoe UI", 22, "bold"),
                         bg=BG, fg=FG).pack(pady=(40, 6))
                tk.Label(win, text="YouTube Music + Spotify Downloader",
                         bg=BG, fg="#9aa4b2").pack()
                tk.Label(win, text="\nLet's set you up in 3 quick steps.",
                         bg=BG, fg=FG).pack(pady=16)
                tk.Label(win, text="Downloads land as Artist - Title.opus with\n"
                                   "embedded cover art, synced lyrics, genre and\n"
                                   "the YouTube link.",
                         bg=BG, fg="#9aa4b2", justify="center").pack()
                tk.Button(win, text="Start Setup  →", bg=GREEN, fg="#051405",
                          font=("Segoe UI", 11, "bold"), relief="flat",
                          command=lambda: go(1)).pack(pady=28, ipadx=14, ipady=4)
            elif n == 1:
                tk.Label(win, text="Step 1 of 3 — Your Music Library",
                         font=("Segoe UI", 13, "bold"),
                         bg=BG, fg=GOLD).pack(pady=(26, 8))
                tk.Label(win, text="Pick where Music/ and Playlists/ should live.",
                         bg=BG, fg="#9aa4b2").pack()
                v = tk.StringVar(value=chosen["lib"])
                e = tk.Entry(win, textvariable=v, bg=BG3, fg=FG,
                             font=("Segoe UI", 10), relief="flat", width=58)
                e.pack(pady=14, ipady=4)
                def browse():
                    d = filedialog.askdirectory(title="Choose library folder")
                    if d:
                        v.set(d)
                tk.Button(win, text="Browse…", bg=BG3, fg=FG, relief="flat",
                          command=browse).pack()
                def nxt():
                    chosen["lib"] = v.get().strip()
                    go(2)
                tk.Button(win, text="Next  →", bg=GREEN, fg="#051405",
                          font=("Segoe UI", 11, "bold"), relief="flat",
                          command=nxt).pack(pady=22, ipadx=14, ipady=4)
            elif n == 2:
                tk.Label(win, text="Step 2 of 3 — YouTube Login (recommended)",
                         font=("Segoe UI", 13, "bold"),
                         bg=BG, fg=GOLD).pack(pady=(26, 8))
                tk.Label(win, text="Logging into YouTube inside SHUNGITE avoids\n"
                                   "bot-checks and unlocks your Premium quality.\n"
                                   "You can skip this and do it later in Settings.",
                         bg=BG, fg="#9aa4b2", justify="center").pack()
                def do_login():
                    win.destroy()
                    self.cfg["wizard_done"] = True
                    self.cfg["library_root"] = chosen["lib"]
                    self.cfg["output_dir"] = chosen["lib"]
                    save_config(self.cfg)
                    try:
                        self._yt_login()
                    except Exception:
                        pass
                def skip():
                    go(3)
                row = tk.Frame(win, bg=BG); row.pack(pady=20)
                tk.Button(row, text="Log in to YouTube", bg=GREEN, fg="#051405",
                          font=("Segoe UI", 11, "bold"), relief="flat",
                          command=do_login).pack(side="left", padx=6, ipadx=8, ipady=4)
                tk.Button(row, text="Skip for now", bg=BG3, fg=FG,
                          relief="flat", command=skip).pack(side="left", padx=6,
                                                             ipadx=8, ipady=4)
            else:
                self.cfg["wizard_done"] = True
                self.cfg["library_root"] = chosen["lib"]
                self.cfg["output_dir"] = chosen["lib"]
                save_config(self.cfg)
                # refresh path fields on every tab
                try:
                    self._sdir.delete(0, tk.END); self._sdir.insert(0, chosen["lib"])
                    self._bdir.delete(0, tk.END); self._bdir.insert(0, chosen["lib"])
                    self._bout.delete(0, tk.END); self._bout.insert(0, chosen["lib"])
                except Exception:
                    pass
                tk.Label(win, text="✓ You're all set!",
                         font=("Segoe UI", 16, "bold"),
                         bg=BG, fg=GREEN).pack(pady=(50, 8))
                tk.Label(win, text="Library: " + chosen["lib"][:44] + "\n\n"
                                   "Paste a link in Single, or open Batch CSV\n"
                                   "and point it at exported Spotify CSVs.",
                         bg=BG, fg="#9aa4b2", justify="center").pack()
                tk.Button(win, text="Done", bg=GREEN, fg="#051405",
                          font=("Segoe UI", 11, "bold"), relief="flat",
                          command=win.destroy).pack(pady=24, ipadx=16, ipady=4)
        go(0)

    def _ui(self, fn):
        try:
            self.root.after(0, fn)
        except tk.TclError:
            pass

    def _apply_manual_sp_cookie(self):
        """If the user picked a Spotify cookies.txt in Settings, extract its
        sp_dc into the api-layer cache so all Spotify calls use it."""
        p = (self.cfg.get("sp_cookies_file") or "").strip()
        if not p or not os.path.exists(p):
            return False
        try:
            dc = None
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("#") or "\t" not in line:
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 7 and parts[5] == "sp_dc":
                        dc = parts[6]
                        break
            if dc:
                with open(sp.SP_DC_PATH, "w", encoding="utf-8") as f:
                    f.write(dc)
                return True
        except Exception:
            pass
        return False

    def _yt_cookiefile(self):
        """Manual cookies.txt from Settings; falls back to the harvested
        login cookies so batch downloads reuse the logged-in session.
        Self-heals: a directory or missing file in the config is cleared
        so it can never poison downloads again."""
        p = (self.cfg.get("yt_cookies_file") or "").strip()
        if p and os.path.isfile(p) and os.path.getsize(p) > 100:
            return p
        if p:
            try:
                self.cfg["yt_cookies_file"] = ""
                save_config(self.cfg)
            except Exception:
                pass
        h = _data_file(".peak_yt_cookies.txt")
        if os.path.isfile(h) and os.path.getsize(h) > 500:
            return h
        return None

    def browser(self):
        b = self.cfg.get("browser", "auto")
        if b == "auto" or not b:
            return None          # callers treat None as "no forced browser";
                                 # cookie extraction scans every browser and
                                 # yt-dlp uses its default client set
        return b

    # ── Header + Footer (watermark everywhere) ──────────────────────────

    def _header(self):
        hdr = tk.Frame(self.root, bg=BG, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text="◆ SHUNGITE", font=("Segoe UI", 20, "bold"),
                 bg=BG, fg=SILVER).pack(side="left", padx=16)
        tk.Label(hdr, text="YouTube Music · Spotify · 256k Opus",
                 font=("Segoe UI", 9), bg=BG, fg=FG2).pack(side="left", padx=8)
        tk.Label(hdr, text=f"⚙ {WATERMARK}", font=("Segoe UI", 9, "italic"),
                 bg=BG, fg=GOLD).pack(side="right", padx=16)

    def _footer(self):
        ftr = tk.Frame(self.root, bg=BG2, pady=4)
        ftr.pack(fill="x", side="bottom")
        tk.Label(ftr, text=f"♪ {WATERMARK} ♪", font=("Segoe UI", 8, "italic"),
                 bg=BG2, fg="#555").pack()

    def _notebook(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self._tab_single(nb)
        self._tab_playlist(nb)
        self._tab_search(nb)
        self._tab_batch(nb)
        self._tab_spotify(nb)
        self._tab_ytm(nb)
        self._tab_stats(nb)
        self._tab_enhance(nb)
        self._tab_lyricsync(nb)
        self._tab_settings(nb)
        # deep-link: Shungite.exe --tab <index|name> opens on that tab
        try:
            _names = ["single", "playlist", "search", "batch", "spotify",
                      "ytm", "stats", "enhance", "lyric", "settings"]
            for _a in sys.argv[1:]:
                if _a.lower().startswith("--tab="):
                    _v = _a.split("=", 1)[1].strip().lower()
                    _i = (_names.index(_v) if _v in _names
                          else int(_v) if _v.isdigit() else 0)
                    nb.select(_i)
                    _dbg("deep-link tab -> %d (argv=%r)" % (_i, sys.argv[1:]))
                    break
        except Exception as _te:
            try:
                _dbg("deep-link FAILED: %s" % str(_te)[:90])
            except Exception:
                pass

        self._start_scheduler()
        self._check_update_async()
        # frozen-env self-test: one real download at startup proves the
        # whole chain (cookies/deno/formats) and logs the truth.
        try:
            import tempfile as _tf
            import threading as _tth
            _tstd = os.path.join(_tf.gettempdir(), "shungite_selftest")
            os.makedirs(_tstd, exist_ok=True)
            def _selftest():
                try:
                    import subprocess as _ssp
                    import yt_dlp as _yd
                    _dbg("SELFTEST yt-dlp=%s deno=%s jsruntimes=%s tmp=%s"
                         % (getattr(_yd.version, "__version__", "?"),
                            DEN_PATH, os.environ.get("JS_RUNTIMES"),
                            _tf.gettempdir()))
                    try:
                        _pv = _ssp.run([DEN_PATH, "--version"],
                                       capture_output=True, timeout=30)
                        _dbg("SELFTEST deno rc=%s out=%s"
                             % (_pv.returncode,
                                (_pv.stdout or b"").decode("utf-8",
                                                          "replace")[:40]))
                    except Exception as _pe:
                        _dbg("SELFTEST deno SPAWN FAILED: %s"
                             % str(_pe)[:100])
                    # verbose-traced download: every yt-dlp decision line
                    # lands in search_debug.log (client selection, PO
                    # token attempts, deno/JS runtime use, real error)
                    class _L:
                        def debug(self, m):
                            _dbg("YTDBG %s" % str(m)[:200])
                        def warning(self, m):
                            _dbg("YTWARN %s" % str(m)[:200])
                        def error(self, m):
                            _dbg("YTERR %s" % str(m)[:200])
                    _orig_ydl = _yd.YoutubeDL

                    class _YDLWrap(_orig_ydl):
                        def __init__(self, *a, **kw):
                            if a and isinstance(a[0], dict):
                                a = (dict(a[0]),) + a[1:]
                                a[0].setdefault("verbose", True)
                                a[0].setdefault("logger", _L())
                            super().__init__(*a, **kw)
                    _yd.YoutubeDL = _YDLWrap
                    try:
                        _st, _sr = download_one(
                            "shungite_selftest",
                            "https://www.youtube.com/watch?v=Vhh_GeBPOhs",
                            _tstd, "audio", "opus256", 0,
                            browser="firefox", cancel_event=None,
                            cookiefile=None, subs=False)
                    finally:
                        _yd.YoutubeDL = _orig_ydl
                    _dbg("SELFTEST %s :: %s" % (_st, str(_sr)[:100]))
                except Exception as _se:
                    _dbg("SELFTEST EXC %s" % str(_se)[:120])
            _tth.Thread(target=_selftest, daemon=True).start()
        except Exception as _ste:
            try:
                _dbg("SELFTEST START FAILED: %s" % str(_ste)[:100])
            except Exception:
                pass

        # phone remote: local web UI on :8765 (SECURE: loopback by
        # default; LAN only if the user opted in; PIN-paired).
        self._remote_url = ""
        self._remote_pin = ""
        try:
            if web_remote and self.cfg.get("web_remote", True):
                web_remote._State.app = self
                _expose = bool(self.cfg.get("web_remote_lan", False))
                _srv, _addr = web_remote.start_server(8765, expose=_expose)
                self._remote_url = _addr
                self._remote_pin = web_remote.new_pin() if _expose else ""
        except Exception:
            pass

        if self.cfg.get("watch_folder"):
            try:
                self._start_watcher()
            except Exception:
                pass

    # ── Shared widgets ───────────────────────────────────────────────────

    def _frame(self, parent):
        return tk.Frame(parent, bg=BG2, padx=14, pady=10)

    def _btn(self, parent, text, cmd, bg=RED, fg="#fff"):
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                         font=FONT_B, relief="flat", padx=18, pady=8,
                         cursor="hand2", activebackground="#c0392b")

    def _cancel_btn(self, parent, key):
        def c():
            ev = self._cancel.get(key)
            if ev:
                ev.set()
            # hard-kill any in-flight ffmpeg PROCESS TREES so partials stop
            try:
                import subprocess as _sp, psutil as _ps
                for proc in _ps.process_iter(["name"]):
                    try:
                        if proc.info["name"] and "ffmpeg" in proc.info["name"].lower():
                            _sp.call(["taskkill", "/F", "/PID", str(proc.pid), "/T"],
                                     stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                                     creationflags=0x08000000)
                    except Exception:
                        pass
            except Exception:
                pass
            self._ui(lambda: self._loginstat.config(text="⏹ cancelled") 
                     if hasattr(self, "_loginstat") else None)
            self._ui(lambda: self._bstat.config(text="⏹ cancelled")
                     if hasattr(self, "_bstat") else None)
        return tk.Button(parent, text="Cancel", command=c, bg=BG3, fg="#e63946",
                         font=FONT, relief="flat", padx=14, pady=8, cursor="hand2")

    def _quality_row(self, parent, var):
        f = tk.Frame(parent, bg=BG2)
        f.pack(fill="x", pady=4)
        tk.Label(f, text="Quality:", bg=BG2, fg=FG2, font=FONT).pack(side="left", padx=8)
        ttk.Combobox(f, values=[
            "Best Opus", "Best M4A"],
            textvariable=var, state="readonly", width=28, font=FONT).pack(side="left")

    def _folder_row(self, parent, label="Save to:"):
        f = tk.Frame(parent, bg=BG2)
        f.pack(fill="x", pady=4)
        tk.Label(f, text=label, bg=BG2, fg=FG2, font=FONT).pack(side="left", padx=8)
        e = tk.Entry(f, bg=BG3, fg=FG, font=FONT, relief="flat")
        e.insert(0, self.cfg.get("library_root", LIBRARY_ROOT))
        e.pack(side="left", fill="x", expand=True, ipady=5, padx=4)
        def browse():
            d = filedialog.askdirectory()
            if d:
                e.delete(0, tk.END)
                e.insert(0, d)
        tk.Button(f, text="Browse", command=browse, bg=BG3, fg=FG,
                  font=FONT, relief="flat", padx=10, cursor="hand2").pack(side="left")
        return e

    def _gui_queue(self):
        """Every worker posts (kind, *args) here; main thread flushes it.
        Starts once widgets are available (lazy)."""
        # idempotent: always returns the queue; starts the Tk pump when widgets exist.
        if not hasattr(self, "_gq") or self._gq is None:
            import queue as _q
            self._gq = _q.Queue()
            self._gq_started = False
        if not self._gq_started and hasattr(self, "_blog") and hasattr(self, "root"):
            self._gq_started = True
            def _pump():
                try:
                    while True:
                        _msg = self._gq.get_nowait()
                        # Messages arrive as (kind, text) or (kind, args).
                        # Indexing a bare string gives only its first
                        # character (bracket display bug) - normalize.
                        kind = _msg[0]
                        a = _msg[1]
                        if isinstance(a, tuple):
                            a = a[0] if a else ""
                        try:
                            if kind == "blog":
                                self._blog.insert(tk.END, str(a) + "\n")
                                self._blog.see(tk.END)
                            elif kind == "bstat":
                                self._bstat.config(text=str(a))
                            elif kind == "status" and hasattr(self, "_status"):
                                self._status.config(text=str(a))
                        except Exception:
                            pass
                        self._gq.task_done()
                except Exception:
                    pass
                if hasattr(self, "root"):
                    self.root.after(50, _pump)
            try:
                self.root.after(50, _pump)
            except Exception:
                pass
        return self._gq

    def _log(self, parent, h=8):
        """Activity log with 500-line cap + save button, auto-displayed."""
        container = tk.Frame(parent, bg=BG3)
        container.pack(fill="both", expand=True, padx=8, pady=4)
        txt = tk.Text(container, height=h, bg=BG3, fg=FG2, font=("Consolas", 9),
                      relief="flat", wrap="word",
                      insertbackground=FG,
                      selectbackground="#264f78", selectforeground="#ffffff",
                      inactiveselectbackground="#264f78")
        txt.pack(fill="both", expand=True)
        btnbar = tk.Frame(container, bg=BG3)
        btnbar.pack(fill="x", pady=(2, 0))
        def _save_log():
            try:
                from tkinter.filedialog import asksaveasfilename
                fn = asksaveasfilename(defaultextension=".txt",
                                       filetypes=[("Log", "*.txt;*.log")])
                if fn:
                    with open(fn, "w", encoding="utf-8") as f:
                        f.write(txt.get("1.0", "end-1c"))
            except Exception:
                pass
        def _trim_log():
            try:
                line_count = int(txt.index('end-1c').split('.')[0])
                if line_count > 500:
                    txt.delete("1.0", f"{line_count - 500}.0")
            except Exception:
                pass
        self._btn(btnbar, "💾 Save log…", _save_log, bg=BG3, fg=FG).pack(
            side="right", padx=4)
        self._btn(btnbar, "🧹 Trim 500", _trim_log, bg=BG3, fg=FG2).pack(
            side="right", padx=4)
        # auto-trim whenever new text lands (cheap)
        def _auto_trim(*_):
            _trim_log()
        txt.bind("<<Modified>>", _auto_trim)
        return txt

    def _type_row(self, parent, var):
        f = tk.Frame(parent, bg=BG2)
        f.pack(fill="x", pady=4)
        tk.Label(f, text="Format:", bg=BG2, fg=FG2, font=FONT).pack(side="left", padx=8)
        ttk.Combobox(f, values=["audio", "video (MP4)"], textvariable=var,
                     state="readonly", width=16, font=FONT).pack(side="left")

    @staticmethod
    def qkey(label):
        if label.startswith("Best Opus"):
            return "opus256"
        if label.startswith("Best M4A"):
            return "m4a"
        if label.startswith("Best MP3"):
            return "mp3"
        if label.startswith("Opus native") or "no re-encode" in label.lower():
            return "opusnative"
        if label.startswith("Opus 256k premium"):
            return "opusprem"
        if label.startswith("Opus"):
            return "opus256"
        if label.startswith("FLAC"):
            return "flac"
        if label.startswith("MP3"):
            return "mp3"
        return "opus256"
    def _watermark_label(self, parent):
        return tk.Label(parent, text=WATERMARK, bg=BG2, fg="#444",
                        font=("Segoe UI", 7, "italic"))

    # ── Tab: Single ──────────────────────────────────────────────────────
    def _tab_single(self, nb):
        o = tk.Frame(nb, bg=BG)
        nb.add(o, text="  Single  ")
        _prefs_card = tk.Frame(o, bg=BG2)
        _prefs_card.pack(fill="x", padx=8, pady=2)
        self._search_prefs_row(_prefs_card, "src_single")
        card = self._frame(o)
        card.pack(fill="x", padx=8, pady=8)
        tk.Label(card, text="Paste a YouTube / YT Music URL:", bg=BG2, fg=FG2,
                 font=FONT).pack(anchor="w")
        self._surl = tk.Entry(card, bg=BG3, fg=FG, font=FONT, relief="flat")
        self._surl.pack(fill="x", ipady=7, pady=(0, 8))
        self._sfmt = tk.StringVar(value=self.cfg.get("fmt_single", "audio"))
        self._sq = tk.StringVar(value="Best Opus")
        self._sdir = self._folder_row(card)
        self._type_row(card, self._sfmt)
        self._quality_row(card, self._sq)
        ar = tk.Frame(card, bg=BG2); ar.pack(fill="x")
        self._autodl = tk.BooleanVar(value=bool(self.cfg.get(
            "auto_dl_on_paste", False)))
        tk.Checkbutton(ar,
                        text="Auto-download the moment you paste a URL",
                        variable=self._autodl, bg=BG2, fg=FG2, font=FONT_SM,
                        selectcolor=BG3, activebackground=BG2).pack(
            anchor="w", padx=8)
        def _auto_dl(_e=None):
            try:
                now = self._surl.get().strip()
                if self._autodl.get() and now and now != getattr(
                        self, "_last_auto_url", ""):
                    self._last_auto_url = now
                    self._do_single()
            except Exception:
                pass
        self._surl.bind("<KeyRelease>", _auto_dl)
        self._surl.bind("<<Paste>>", lambda _e: self.root.after(60, _auto_dl))
        br = tk.Frame(card, bg=BG2)
        br.pack(fill="x", pady=(8, 0))
        self._btn(br, "Download", self._do_single).pack(side="left")
        self._retry1_btn = self._btn(br, "♻ Retry failed",
                                     self._sp_retry_failed, bg=BG3, fg=FG)
        self._retry1_btn.pack(side="left", padx=6)
        self._cancel_btn(br, "single").pack(side="left", padx=6)
        self._btn(br, "📂 Library", self._open_library,
                  bg=BG3, fg=FG).pack(side="left", padx=4)
        self._sstat = tk.Label(br, text="", bg=BG2, fg=GREEN, font=FONT)
        self._sstat.pack(side="left", padx=12)
        self._watermark_label(card).pack(anchor="e")
        self._slog = self._log(o)

    def _do_single(self):
        url = self._surl.get().strip()
        if not url:
            return
        # skip if this exact video id is already in the index (avoid re-download)
        try:
            idx = os.path.join(self._music_dir(), "_index.txt")
            if os.path.exists(idx) and url:
                import re as _re
                mm = _re.search(r"(?:v=|youtu\.be/)([\w-]{11})", url)
                vid = mm.group(1) if mm else url
                for line in open(idx, encoding="utf-8", errors="replace"):
                    if vid and vid in line:
                        self._sstat.config(
                            text="◉ Already in library — skipped")
                        self._notify_done("SHUNGITE", "Already downloaded")
                        return
        except Exception:
            pass
        ev = threading.Event()
        self._cancel["single"] = ev
        self._sstat.config(text="Downloading…")

        def run():
            s, m = download_one(url, url, self._sdir.get(),
                               "audio" if "audio" in self._sfmt.get() else "video",
                               self.qkey(self._sq.get()),
                               self.cfg.get("speed_limit_kbps", 0),
                               browser=self.browser(), cancel_event=ev,
                               cookiefile=self._yt_cookiefile(),
                               subs=self.cfg.get("fetch_subtitles", False))
            def _summarize():
                if s != "ok":
                    self._sstat.config(text=f"✗ {m[:60]}")
                    return
                try:
                    import glob as _g, subprocess as _sp
                    folder = self._sdir.get()
                    files = sorted(_g.glob(os.path.join(folder, "*.opus")) +
                                   _g.glob(os.path.join(folder, "*.mp3")) +
                                   _g.glob(os.path.join(folder, "*.mp4")) +
                                   _g.glob(os.path.join(folder, "*.webm")) +
                                   _g.glob(os.path.join(folder, "*.m4a")))
                    if not files:
                        self._sstat.config(text="✓ Done")
                        return
                    fp = files[-1]
                    sz = os.path.getsize(fp)
                    ok = "ok"
                    try:
                        r = _sp.run(["ffprobe", "-v", "error", "-show_format",
                                     "-of", "json", fp],
                                    capture_output=True, timeout=30)
                        ok = "ok" if b"format" in r.stdout else "?"
                    except Exception:
                        ok = "?"
                    self._sstat.config(text=f"✓ Done · {sz//1024} KB · probe={ok}")
                except Exception:
                    self._sstat.config(text="✓ Done")
            self._ui(_summarize)
        threading.Thread(target=run, daemon=True).start()

    def _open_library(self):
        """Open the library folder in the OS file explorer."""
        try:
            os.startfile(self._music_dir())
        except Exception:
            try:
                import webbrowser
                webbrowser.open("file://" + self._music_dir())
            except Exception:
                pass

    # ── Tab: Playlist ────────────────────────────────────────────────────
    def _tab_playlist(self, nb):
        o = tk.Frame(nb, bg=BG)
        nb.add(o, text="  Playlist  ")
        _prefs_card = tk.Frame(o, bg=BG2)
        _prefs_card.pack(fill="x", padx=8, pady=2)
        self._search_prefs_row(_prefs_card, "src_playlist")
        card = self._frame(o)
        card.pack(fill="x", padx=8, pady=8)
        tk.Label(card, text="Playlist / channel URL:", bg=BG2, fg=FG2,
                 font=FONT).pack(anchor="w")
        self._purl = tk.Entry(card, bg=BG3, fg=FG, font=FONT, relief="flat")
        self._purl.pack(fill="x", ipady=7, pady=(0, 8))
        self._pfmt = tk.StringVar(value=self.cfg.get("fmt_playlist", "audio"))
        self._pq = tk.StringVar(value="Best Opus")
        self._pdir = self._folder_row(card)
        self._type_row(card, self._pfmt)
        self._quality_row(card, self._pq)
        wf = tk.Frame(card, bg=BG2)
        wf.pack(fill="x", pady=4)
        tk.Label(wf, text="Parallel:", bg=BG2, fg=FG2, font=FONT).pack(side="left", padx=8)
        self._pw = tk.Spinbox(wf, from_=1, to=10, width=4, bg=BG3, fg=FG,
                              font=FONT, relief="flat")
        self._pw.delete(0, tk.END)
        self._pw.insert(0, str(self.cfg.get("max_workers", 4)))
        self._pw.pack(side="left")
        br = tk.Frame(card, bg=BG2)
        br.pack(fill="x", pady=(8, 0))
        self._btn(br, "Start", self._do_playlist).pack(side="left")
        self._retry2_btn = self._btn(br, "♻ Retry failed",
                                     self._sp_retry_failed, bg=BG3, fg=FG)
        self._retry2_btn.pack(side="left", padx=6)
        self._cancel_btn(br, "pl").pack(side="left", padx=6)
        self._pstat = tk.Label(br, text="", bg=BG2, fg=GREEN, font=FONT)
        self._pstat.pack(side="left", padx=12)
        self._watermark_label(card).pack(anchor="e")

        cols = ("#", "title", "status")
        self._ptree = ttk.Treeview(o, columns=cols, show="headings", height=10)
        for c, w in zip(cols, (40, 550, 180)):
            self._ptree.heading(c, text=c)
            self._ptree.column(c, width=w)
        self._ptree.pack(fill="both", expand=True, padx=8, pady=4)

    def _do_playlist(self):
        url = self._purl.get().strip()
        if not url:
            return
        ev = threading.Event()
        self._cancel["pl"] = ev
        self._pstat.config(text="Fetching…")

        def run():
            opts = {"quiet": True, "extract_flat": True, "skip_download": True}
            if self.browser() != "none":
                opts["cookiesfrombrowser"] = (self.browser(),)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = info.get("entries") or [info]

            def show():
                for i, e in enumerate(entries):
                    if e:
                        self._ptree.insert("", "end", iid=f"r{i}",
                                           values=(i + 1, e.get("title", "?"), "⏳"))
            self._ui(show)

            dtype = "audio" if "audio" in self._pfmt.get() else "video"
            qual = self.qkey(self._pq.get())
            save = self._pdir.get()
            browser = self.browser()

            def st(i, text):
                def do():
                    iid = f"r{i}"
                    if self._ptree.exists(iid):
                        v = list(self._ptree.item(iid, "values"))
                        v[2] = text
                        self._ptree.item(iid, values=v)
                self._ui(do)

            def work(i, e):
                download_one(e.get("title", "?"),
                             f"https://www.youtube.com/watch?v={e.get('id')}",
                             save, dtype, qual, self.cfg.get("speed_limit_kbps", 0),
                             browser=browser,
                             cookiefile=self._yt_cookiefile(), status_cb=lambda s, i=i: st(i, s),
                             cancel_event=ev,
                             subs=self.cfg.get("fetch_subtitles", False))

            with ThreadPoolExecutor(max_workers=int(self._pw.get() or 4)) as pool:
                futs = [pool.submit(work, i, e) for i, e in enumerate(entries) if e]
                for f in futs:
                    f.result()
            self._ui(lambda: self._pstat.config(text="✓ Complete"))
        threading.Thread(target=run, daemon=True).start()

    # ── Tab: Search ──────────────────────────────────────────────────────
    def _tab_search(self, nb):
        o = tk.Frame(nb, bg=BG)
        nb.add(o, text="  Search  ")
        _prefs_card = tk.Frame(o, bg=BG2)
        _prefs_card.pack(fill="x", padx=8, pady=2)
        self._search_prefs_row(_prefs_card, "src_search")
        card = self._frame(o)
        card.pack(fill="x", padx=8, pady=8)
        row = tk.Frame(card, bg=BG2)
        row.pack(fill="x")
        self._search = tk.Entry(row, bg=BG3, fg=FG, font=FONT, relief="flat")
        self._search.pack(side="left", fill="x", expand=True, ipady=7)
        self._search.bind("<Return>", lambda e: self._do_search())
        self._btn(row, "Search", self._do_search).pack(side="left", padx=(8, 0))
        self._srchlib = tk.BooleanVar(value=False)
        tk.Checkbutton(row, text="search local library", variable=self._srchlib,
                       bg=BG2, fg=FG2, font=FONT_SM, selectcolor=BG3,
                       activebackground=BG2).pack(side="left", padx=8)
        self._sq2 = tk.StringVar(value="Best Opus")
        self._sfmt2 = tk.StringVar(value=self.cfg.get("fmt_search", "audio"))
        self._sdir2 = self._folder_row(card)
        fmt_f = tk.Frame(card, bg=BG2)
        fmt_f.pack(fill="x", pady=4)
        tk.Label(fmt_f, text="Format:", bg=BG2, fg=FG2, font=FONT).pack(side="left", padx=8)
        ttk.Combobox(fmt_f, values=["audio", "video"], textvariable=self._sfmt2,
                     state="readonly", width=28, font=FONT).pack(side="left")
        self._quality_row(card, self._sq2)
        self._watermark_label(card).pack(anchor="e")

        cols = ("title", "channel", "dur")
        self._stree = ttk.Treeview(o, columns=cols, show="headings", height=10,
                                   selectmode="extended")
        for c, w in zip(cols, (480, 200, 60)):
            self._stree.heading(c, text=c)
            self._stree.column(c, width=w)
        self._stree.pack(fill="both", expand=True, padx=8, pady=4)
        self._stree.bind("<Double-1>", self._dl_search)
        self._stree.bind("<Control-c>", self._search_copy_url)
        if not hasattr(self, "_stree_menu"):
            self._stree_menu = tk.Menu(self._stree, tearoff=0)
            self._stree_menu.add_command(label="Download selected",
                                          command=self._dl_search)
            self._stree_menu.add_command(label="Copy video URL",
                                          command=self._search_copy_url)
        self._stree.bind("<Button-3>", lambda e: self._stree_menu.post(
            e.x_root, e.y_root))

        br = tk.Frame(o, bg=BG)
        br.pack(fill="x", padx=8, pady=4)
        self._btn(br, "Download Selected", self._dl_search).pack(side="left")
        self._retry3_btn = self._btn(br, "♻ Retry failed",
                                     self._sp_retry_failed, bg=BG3, fg=FG)
        self._retry3_btn.pack(side="left", padx=6)
        self._cancel_btn(br, "search").pack(side="left", padx=6)
        self._sstat2 = tk.Label(br, text="", bg=BG, fg=GREEN, font=FONT)
        self._sstat2.pack(side="left", padx=12)
        self._sres = []

    def _search_copy_url(self, _e=None):
        try:
            sel = self._stree.selection()
            if not sel:
                return
            idx = self._stree.index(sel[0])
            hit = self._sres[idx] if 0 <= idx < len(self._sres) else None
            if hit:
                url = hit.get("url") or ""
                self.root.clipboard_clear()
                self.root.clipboard_append(url)
                self._sstat2.config(text="URL copied")
        except Exception:
            pass

    def _search_library(self, query):
        """Search the local library (embedded tags + index) for `query`.
        Returns list of {title, artist, file}."""
        import glob as _g, mutagen as _mg
        music = self._music_dir()
        q = (query or "").strip().lower()
        if not q:
            return []
        results = []
        # fast path: grep the index file first
        idx = os.path.join(music, "_index.txt")
        if os.path.exists(idx):
            for line in open(idx, encoding="utf-8", errors="replace"):
                if q in line.lower():
                    parts = line.strip().split("|", 3)
                    if len(parts) >= 3:
                        results.append({"title": parts[1], "artist": parts[2],
                                        "file": parts[0]})
        if results:
            return results[:200]
        # fallback: scan embedded tags
        files = []
        for ext in (".opus", ".mp3", ".m4a", ".flac"):
            files += _g.glob(os.path.join(music, "**", "*" + ext),
                             recursive=True)
        for p in files:
            try:
                a = _mg.File(p, easy=True)
                t = " ".join(a.get("title", []) or [])
                ar = " ".join(a.get("artist", []) or [])
                al = " ".join(a.get("album", []) or [])
            except Exception:
                t = os.path.basename(p); ar = al = ""
            blob = f"{t} {ar} {al}".lower()
            if q in blob:
                results.append({"title": t or os.path.basename(p),
                                "artist": ar, "file": os.path.basename(p)})
                if len(results) >= 200:
                    break
        return results

    def _do_search(self):
        q = self._search.get().strip()
        if not q:
            return
        if getattr(self, "_srchlib", None) and self._srchlib.get():
            hits = self._search_library(q)
            for r in self._stree.get_children():
                self._stree.delete(r)
            for h in hits:
                self._stree.insert("", "end", values=(
                    h["title"], h.get("artist", ""), ""))
            self._sstat2.config(text=f"● {len(hits)} local match(es)")
            return
        self._sstat2.config(text="Searching…")
        for r in self._stree.get_children():
            self._stree.delete(r)

        # Channel / playlist / ALBUM URL support.
        _is_album = "OLAK5uy_" in q
        if any(k in q.lower() for k in ("youtube.com/@", "youtube.com/channel/",
                                        "youtube.com/c/", "youtu.be/",
                                        "&list=", "?list=",
                                        "youtube.com/watch")):
            def run_channel():
                try:
                    import yt_dlp as _yd
                    opts = {"quiet": True, "extract_flat": True,
                            "skip_download": True, "no_warnings": True,
                            "playlistend": 100}
                    with _yd.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(q, download=False)
                    entries = (info or {}).get("entries") or []
                    res = []
                    for e in entries:
                        if not e:
                            continue
                        dur = e.get("duration") or 0
                        res.append({"title": e.get("title", "?"),
                                    "url": ("https://www.youtube.com/watch?v="
                                            + (e.get("id") or "")),
                                    "channel": (e.get("uploader")
                                                or e.get("channel") or ""),
                                    "duration": (f"{dur//60}:{dur%60:02d}"
                                                 if dur else "")})
                    self._sres = res

                    def show_c(n=len(res)):
                        for i2, r in enumerate(res):
                            self._stree.insert("", "end", iid=str(i2),
                                               values=(r["title"],
                                                       r["channel"],
                                                       r["duration"]))
                        self._sstat2.config(
                            text=f"{len(res)} items from "
                                 f"{'album' if _is_album else 'channel/playlist'}")
                    self._ui(show_c)
                except Exception as e:
                    self._ui(lambda s=str(e)[:60]: self._sstat2.config(
                        text=f"Failed: {s}", fg=RED))
            threading.Thread(target=run_channel, daemon=True).start()
            return

        def run():
            try:
                res = search_youtube(q, 10, self.browser())
            except Exception as e:
                _msg = str(e)[:80]
                self._ui(lambda m=_msg: self._sstat2.config(
                    text=f"Search failed: {m}"))
                return
            self._sres = res

            def show():
                for i, r in enumerate(res):
                    self._stree.insert("", "end", iid=str(i),
                                       values=(r["title"], r["channel"], r["duration"]))
                self._sstat2.config(text=f"{len(res)} results")
            self._ui(show)
        threading.Thread(target=run, daemon=True).start()

    def _dl_search(self):
        sel = self._stree.selection()
        if not sel:
            return
        ev = threading.Event()
        self._cancel["search"] = ev
        items = [self._sres[int(i)] for i in sel]
        qual = self.qkey(self._sq2.get())
        save = self._sdir2.get()

        def run():
            fmt = self._sfmt2.get()
            for it in items:
                download_one(it["title"], it["url"], save, fmt, qual,
                             self.cfg.get("speed_limit_kbps", 0),
                             browser=self.browser(), cancel_event=ev,
                             cookiefile=self._yt_cookiefile(),
                             status_cb=lambda s, ti=it["title"][:30]: None,
                             subs=self.cfg.get("fetch_subtitles", False))
            self._ui(lambda: self._sstat2.config(text="✓ Complete"))
        threading.Thread(target=run, daemon=True).start()

    # ── Tab: Batch CSV ───────────────────────────────────────────────────

    def _search_prefs_row(self, parent, key_prefix):
        """Per-tab 'source' combobox + duration-cap controls.
        Saves cfg keys '<key_prefix>_source', '<key_prefix>_cap_on',
        '<key_prefix>_cap_min', '<key_prefix>_cap_max'."""
        row = tk.Frame(parent, bg=BG2)
        row.pack(fill="x", pady=2)
        # source selector
        tk.Label(row, text="Search in:", bg=BG2, fg=FG2,
                 font=FONT).pack(side="left", padx=(0, 6))
        srcvar = tk.StringVar(value=self.cfg.get(key_prefix + "_source", "both"))
        setattr(self, key_prefix + "_src_var", srcvar)
        cb = ttk.Combobox(
            row, textvariable=srcvar, state="readonly", width=18,
            values=["both (YouTube+YTM)", "ytmusic only", "youtube only"])
        cb.pack(side="left")
        cur = srcvar.get()
        cb.current({"both": 0, "ytmusic": 1, "youtube": 2}.get(cur, 0))
        def _src_cb(*_e):
            i = cb.current()
            srcvar.set(["both", "ytmusic", "youtube"][i])
            self.cfg[key_prefix + "_source"] = srcvar.get()
            save_config(self.cfg)
        cb.bind("<<ComboboxSelected>>", _src_cb)

        # caps: on/off + min/max sec
        capon = tk.BooleanVar(value=bool(self.cfg.get(key_prefix + "_cap_on",
                                                      True)))
        setattr(self, key_prefix + "_cap_on_var", capon)
        tk.Checkbutton(row, text="Length window", variable=capon,
                       bg=BG2, fg=FG2, selectcolor=BG3,
                       activebackground=BG2).pack(side="left", padx=(12, 4))
        def _cap_on_cb(*_e):
            self.cfg[key_prefix + "_cap_on"] = capon.get()
            save_config(self.cfg)
        capon.trace_add("write", _cap_on_cb)

        tk.Label(row, text="min s:", bg=BG2, fg=FG2,
                 font=FONT).pack(side="left")
        minv = tk.Spinbox(row, from_=10, to=600, width=4, bg=BG3, fg=FG,
                          font=FONT, relief="flat")
        minv.delete(0, tk.END)
        minv.insert(0, str(int(self.cfg.get(key_prefix + "_cap_min", 40))))
        minv.pack(side="left")
        setattr(self, key_prefix + "_cap_min_spin", minv)

        tk.Label(row, text="max s:", bg=BG2, fg=FG2,
                 font=FONT).pack(side="left", padx=(8, 0))
        maxv = tk.Spinbox(row, from_=60, to=3600, width=5, bg=BG3, fg=FG,
                          font=FONT, relief="flat")
        maxv.delete(0, tk.END)
        maxv.insert(0, str(int(self.cfg.get(key_prefix + "_cap_max", 720))))
        maxv.pack(side="left")
        setattr(self, key_prefix + "_cap_max_spin", maxv)

        def _save_caps(*_e):
            try:
                self.cfg[key_prefix + "_cap_min"] = int(minv.get())
                self.cfg[key_prefix + "_cap_max"] = int(maxv.get())
                save_config(self.cfg)
            except Exception:
                pass
        minv.bind("<FocusOut>", _save_caps)
        maxv.bind("<FocusOut>", _save_caps)
        minv.bind("<Return>", _save_caps)
        maxv.bind("<Return>", _save_caps)
        return row

    def _get_search_prefs(self, key_prefix):
        """Read back (source, caps_on, cap_min, cap_max) for a tab."""
        def _i(k, d):
            try:
                return int(getattr(self, key_prefix + k).get())
            except Exception:
                return d
        src = getattr(self, key_prefix + "_src_var", None)
        source = src.get() if src is not None else "both"
        capon_v = getattr(self, key_prefix + "_cap_on_var", None)
        capon = bool(capon_v.get()) if capon_v is not None else True
        return (source, capon,
                _i("_cap_min_spin", 40),
                _i("_cap_max_spin", 720))

    def _tab_batch(self, nb):
        o = tk.Frame(nb, bg=BG)
        nb.add(o, text="  Batch CSV  ")
        _prefs_card = tk.Frame(o, bg=BG2)
        _prefs_card.pack(fill="x", padx=8, pady=2)
        self._search_prefs_row(_prefs_card, "src_batch")
        card = self._frame(o)
        card.pack(fill="x", padx=8, pady=8)
        tk.Label(card, text="Pick a folder of playlist CSVs (Exportify format) — bulk download, resumable",
                 bg=BG2, fg=GOLD, font=FONT_B).pack(anchor="w", pady=(0, 6))
        self._bdir = self._folder_row(card, label="CSV folder:")
        self._bout = self._folder_row(card, label="Library root:")
        tk.Label(card, text="(auto-starts 2s after you pick a folder; stop anytime)",
                 bg=BG2, fg="#777", font=FONT_SM).pack(anchor="w", padx=8)

        def _b_auto(*_args):
            try:
                import threading as _th, time as _t
                def try_start():
                    _t.sleep(2)
                    d = self._bdir.get().strip()
                    if (self.cfg.get("batch_auto_start", True)
                            and os.path.isdir(d)
                            and any(f.endswith(".csv")
                                    for f in os.listdir(d))):
                        self._do_batch()
                _th.Thread(target=try_start, daemon=True).start()
            except Exception:
                pass
        self._bdir.bind("<FocusOut>", _b_auto)
        tk.Label(card, text="⚠ Music/ and Playlists/ are created side-by-side — keep them together",
                 bg=BG2, fg="#666", font=("Segoe UI", 7)).pack(anchor="w", padx=8)
        self._bq = tk.StringVar(value="Best Opus")
        self._bfmt = tk.StringVar(
            value=self.cfg.get("fmt_batch", "audio"))
        fmtrow_b = tk.Frame(card, bg=BG2)
        fmtrow_b.pack(fill="x", pady=4)
        tk.Label(fmtrow_b, text="Format:", bg=BG2, fg=FG2,
                 font=FONT).pack(side="left", padx=8)
        ttk.Combobox(fmtrow_b, values=["audio", "video"],
                     textvariable=self._bfmt, state="readonly",
                     width=12, font=FONT).pack(side="left")
        self._quality_row(card, self._bq)

        wf = tk.Frame(card, bg=BG2)
        wf.pack(fill="x", pady=4)
        tk.Label(wf, text="Workers:", bg=BG2, fg=FG2, font=FONT).pack(side="left", padx=8)
        self._bw = tk.Spinbox(wf, from_=1, to=15, width=4, bg=BG3, fg=FG,
                              font=FONT, relief="flat")
        self._bw.delete(0, tk.END)
        self._bw.insert(0, "6")
        self._bw.pack(side="left")

        br = tk.Frame(card, bg=BG2)
        br.pack(fill="x", pady=(8, 0))
        self._btn(br, "Start Batch (Full)", self._do_batch, bg=GREEN).pack(side="left")
        self._btn(br, "🔄 Sync Missing Only", self._do_batch_sync, bg=GOLD).pack(side="left", padx=6)
        self._btn(br, "📝 Generate M3U Only", self._do_batch_m3u, bg=BG3, fg=FG).pack(side="left", padx=6)
        self._btn(br, "📦 Import Spotify data.zip", self._import_spotify_zip,
                bg="#1DB954", fg="#000").pack(side="left", padx=6)
        self._retryb_btn = self._btn(br, "♻ Retry failed", self._sp_retry_failed,
                                        bg=BG3, fg=FG)
        self._retryb_btn.pack(side="left", padx=6)
        self._cancel_btn(br, "batch").pack(side="left", padx=6)
        self._bstat = tk.Label(br, text="", bg=BG2, fg=GREEN, font=FONT)
        self._bstat.pack(side="left", padx=12)
        self._watchvar = tk.BooleanVar(value=bool(self.cfg.get("watch_folder", False)))
        tk.Checkbutton(br, text="👁 Watch folder (auto-ingest new CSVs)",
                       variable=self._watchvar, bg=BG2, fg=FG2, font=FONT_SM,
                       selectcolor=BG3, activebackground=BG2,
                       command=self._toggle_watch).pack(side="left", padx=(12, 0))
        self._watermark_label(card).pack(anchor="e")
        self._blog = self._log(o, 10)

    def _import_spotify_zip(self):
        """Import a Spotify account-data export zip; extract unique tracks;
        look up real durations via iTunes in a background thread; write
        Spotify_history_all.csv; kick off the sync. UI never blocks."""
        import tkinter.filedialog as _fd
        import zipfile as _zf
        import json as _j
        import collections as _col
        import threading as _th

        dlg = _fd.askopenfilename(
            title="Select Spotify data export zip",
            filetypes=[("Spotify data zip", "*.zip"), ("All files", "*.*")])
        if not dlg:
            return

        counts = _col.Counter()
        try:
            with _zf.ZipFile(dlg) as z:
                names = [n for n in z.namelist()
                         if n.endswith(".json")
                         and "Streaming_History_Audio" in n]
                if not names:
                    messagebox.showerror("No history found",
                        "No Streaming_History_Audio*.json inside that zip.")
                    return
                for n in names:
                    for rec in _j.loads(z.read(n)):
                        name = rec.get("master_metadata_track_name")
                        art = rec.get("master_metadata_album_artist_name") or ""
                        if not name:
                            continue
                        k = (name.strip(), art.strip())
                        # presence only; durations resolved separately
                        counts.setdefault(k, 0)
        except Exception as e:
            messagebox.showerror("Import failed", str(e)[:200])
            return

        f = self._bdir.get().strip()
        if not f or not os.path.isdir(f):
            f = _fd.askdirectory(title="Pick folder to save the CSV into")
            if not f:
                return
            self._bdir.delete(0, tk.END)
            self._bdir.insert(0, f)

        out_csv = os.path.join(f, "Spotify_history_all.csv")
        track_list = sorted(counts)

        def _log(msg):
            try:
                self._ui(lambda m=msg: (self._blog.insert(tk.END, m + "\n"),
                                        self._blog.see(tk.END)))
            except Exception:
                pass

        def _write_csv(dur_map):
            with open(out_csv, "w", encoding="utf-8-sig", newline="") as fh:
                fh.write("Track Name,Artist Name(s),Duration (ms)\n")
                for name, art in track_list:
                    safe = name.replace('"', "'")
                    dms = dur_map.get((name, art), "")
                    if dms:
                        fh.write(f'"{safe}","{art}",{int(dms)}\n')
                    else:
                        fh.write(f'"{safe}","{art}",\n')

        def _finish(dur_map):
            try:
                _write_csv(dur_map)
            except Exception as e:
                _msg = str(e)[:200]
                self._ui(lambda m=_msg: messagebox.showerror("Write failed",
                                                             m))
                return
            _log(f"  ✅ wrote {len(track_list)} tracks -> "
                 f"{os.path.basename(out_csv)}")
            self._ui(lambda: self._bstat.config(
                text=f"imported {len(track_list)} tracks — syncing missing…"))
            self._ui(lambda: self.root.after(1000, self._do_batch_sync))

        def _work():
            _log(f"  … parsing history done: {len(track_list)} unique tracks")
            # Duration column left blank — iTunes lookup was removed (3% hit
            # rate, stalled the import for 30+ min). Matching uses the per-tab
            # Length-window caps + popularity/fuzzy ranking instead.
            dur_map = {}
            _log(f"  ✓ {len(track_list)} tracks parsed (durations left blank)")
            _finish(dur_map)

        _th.Thread(target=_work, daemon=True).start()


    def _batch_worker(self, row, qual, browser, ev, music, index, file_norms,
                      stats, lock, fail_fh, save_index_every=25, prefs=None):
        pl_name, track, artist, durms, _pg = (list(row) + [None, None])[:5]
        genre = _pg or ""
        q = (track + " " + artist).strip()
        _src = (prefs or {}).get("source", None)
        _con = bool((prefs or {}).get("cap_on", True))
        _cmin = int((prefs or {}).get("cap_min", 40))
        _cmax = int((prefs or {}).get("cap_max", 720))
        try:
            with lock:
                stats["started"] = stats.get("started", 0) + 1
                n0 = stats["started"]
            # Spotify-tab-style per-track line, universal across all tabs
            self._gui_queue().put(("blog",
                "[%d] %s %s — %s" % (n0, "\U0001F50D", track[:34], artist[:20])))
        except Exception:
            pass
        try:
            _dur_s = None
            try:
                if durms and str(durms).strip().isdigit():
                    _dur_s = int(durms) / 1000.0
            except Exception:
                _dur_s = None
            matched, results = _match_track(track, artist, browser, prefs,
                                            dur_s=_dur_s)
        except Exception as e:
            with lock:
                stats["fail"] = stats.get("fail", 0) + 1
                if fail_fh:
                    try:
                        fail_fh.write(
                            "%s - %s  (search error: %s)\n"
                            % (artist, track, str(e)[:80]))
                        fail_fh.flush()
                    except Exception:
                        pass
            return
        if not matched:
            with lock:
                stats["fail"] += 1
                if fail_fh:
                    try:
                        seen = [(r.get("title","")[:60], r.get("seconds"))
                                for r in (results or [])[:6]]
                        fail_fh.write(
                            "%s - %s  (no match; hits=%d sample=%s)\n"
                            % (artist, track, len(results or []), seen))
                        fail_fh.flush()
                    except Exception:
                        pass
            return
        want_fn = re.sub(r'[<>:"/\\|?*]', "_",
                         (artist + " - " + track) if artist else track).strip(" .")[:200]
        tried = ([matched] if isinstance(matched, dict) else []) + [
            r for r in (results or [])
            if isinstance(r, dict)
            and matched is not None and r.get("id") != matched.get("id")]
        _retried_with_cookies = False
        for res in tried:
            if ev.is_set():
                return
            try:
                # subs=True: YouTube's own timed subtitles (original language)
                # get saved as .vtt and converted to synced .lrc below — this
                # is the "properly timed lyrics" path for every track.
                st, rr = download_one(want_fn, res.get("url", ""), music,
                                      "audio", qual, 0,
                                      browser=browser, cancel_event=ev,
                                      cookiefile=self._yt_cookiefile(),
                                      meta_artist=artist,
                                      yt_url=res.get("url", ""),
                                      subs=True)
            except Exception as e:
                try:
                    fail_fh.write(
                        "%s - %s  (dl crash: %s)\n"
                        % (artist, track, str(e)[:100]))
                    fail_fh.flush()
                except Exception:
                    pass
                continue
            if st != "ok" and not _retried_with_cookies:
                _retried_with_cookies = True
                try:
                    st, rr = download_one(want_fn, res.get("url", ""),
                                          music, "audio", qual, 0,
                                          browser=(browser or self.browser()
                                                   or "firefox"),
                                          cancel_event=ev,
                                          cookiefile=None,
                                          meta_artist=artist,
                                          yt_url=res.get("url", ""),
                                          subs=self.cfg.get("fetch_subtitles", False))
                except Exception:
                    pass
            if st == "ok":
                try:
                    self._gui_queue().put(("blog",
                        "  ✓ %s — %s" % (artist, track)))
                except Exception:
                    pass
                actual = want_fn + ".opus"
                _post_download_fixups(music, want_fn, track, artist,
                                      res.get("title", ""), genre=genre)
                try:
                    old_guess = re.sub(r'[<>:"/\\|?*]', "_",
                                       res.get("title", "")).strip(" .")[:200] + ".opus"
                    _o = os.path.join(music, old_guess)
                    _n = os.path.join(music, actual)
                    if old_guess != actual and os.path.exists(_o) \
                            and not os.path.exists(_n):
                        os.replace(_o, _n)
                except Exception:
                    pass
                with lock:
                    index[actual] = {"track": track, "artist": artist,
                                     "uri": _vid_from_url(res.get("url", ""))}
                    file_norms[self._normalize_match(actual[:-5])] = actual
                    stats["ok"] += 1
                    if stats["ok"] % save_index_every == 0:
                        try:
                            idx_f = os.path.join(music, "_index.txt")
                            tmp = idx_f + ".tmp"
                            with open(tmp, "w", encoding="utf-8") as _fh:
                                _fh.write("# filename|track|artist|uri\n")
                                for _k, _v in index.items():
                                    _fh.write("%s|%s|%s|%s\n" % (
                                        _k, _v.get("track", ""),
                                        _v.get("artist", ""),
                                        _v.get("uri", "")))
                            os.replace(tmp, idx_f)
                        except Exception:
                            pass
                return
            elif st == "cancelled":
                return
        with lock:
            stats["fail"] += 1
            if fail_fh:
                try:
                    fail_fh.write(
                        "%s - %s  (all %d candidates failed)\n"
                        % (artist, track, len(tried)))
                    fail_fh.flush()
                except Exception:
                    pass

    def _run_batch_rows(self, rows, qual, browser, ev, log):
        try:
            workers = max(1, min(15, int(self._bw.get())))
        except Exception:
            workers = 6
        prefs_src, prefs_con, prefs_cmin, prefs_cmax = self._get_search_prefs("src_batch")
        prefs = {"source": prefs_src, "cap_on": prefs_con,
                 "cap_min": prefs_cmin, "cap_max": prefs_cmax}
        try:
            root = (self._bout.get().strip() or self._bdir.get().strip())
        except Exception:
            root = self._bdir.get().strip()
        music = os.path.join(root, "Music")
        os.makedirs(music, exist_ok=True)
        idx_file = os.path.join(music, "_index.txt")
        index = {}
        if os.path.exists(idx_file):
            try:
                with open(idx_file, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split("|", 3)
                        if len(parts) >= 2:
                            index[parts[0]] = {
                                "track": parts[1] if len(parts) > 1 else "",
                                "artist": parts[2] if len(parts) > 2 else "",
                                "uri": parts[3] if len(parts) > 3 else ""}
            except Exception:
                index = {}
        index = {k: v for k, v in index.items()
                 if os.path.isfile(os.path.join(music, k))}
        file_norms = self._build_file_norms(music)
        stats = {"ok": 0, "fail": 0, "skip": 0, "started": 0}
        lock = threading.Lock()
        csv_dir = self._bdir.get().strip()
        fail_path = os.path.join(csv_dir, "failed_downloads.txt")
        fail_fh = open(fail_path, "a", encoding="utf-8")
        todo = []
        seen = set()
        for row in rows:
            row = tuple(row) + (None,) * (5 - len(row)) if len(row) < 5 else tuple(row)
            _p, track, artist, _d, _pg = row[:5]
            if ev.is_set():
                break
            if self._check_have(track, artist, index, file_norms):
                stats["skip"] += 1
                continue
            k = self._normalize_match(("%s %s" % (track, artist)))[:60]
            if k in seen:
                stats["skip"] += 1
                continue
            seen.add(k)
            todo.append(row)
        total = len(todo)
        self._gui_queue().put(("blog",
            "⬇ %d to download · %d already have/dupes · workers=%d"
            % (total, stats["skip"], workers)))
        self._gui_queue().put(("bstat",
            "Running… %d/%d" % (0, total)))
        done_ct = {"n": 0}
        def _wrap(r):
            try:
                self._batch_worker(r, qual, browser, ev, music, index,
                                   file_norms, stats, lock, fail_fh,
                                   prefs=prefs)
            except Exception:
                with lock:
                    stats["fail"] = stats.get("fail", 0) + 1
            finally:
                # ALWAYS release the submission slot so the feeder can
                # continue — this was the >24-track freeze bug
                try:
                    sem.release()
                except Exception:
                    pass
                with lock:
                    done_ct["n"] += 1
                    n = done_ct["n"]
                self._gui_queue().put(("bstat",
                    "[%d/%d] ✓%d ↷%d ✗%d"
                    % (n, total, stats["ok"], stats["skip"], stats["fail"])))
        import threading as _th
        from concurrent.futures import ThreadPoolExecutor
        # bounded submission so the queue smells like a pipe, not a sword
        sem = _th.BoundedSemaphore(workers * 4)
        ex = ThreadPoolExecutor(max_workers=workers)
        futs = []
        try:
            for r in todo:
                if ev.is_set():
                    break
                sem.acquire()
                futs.append(ex.submit(_wrap, r))
            # check for cancel every 300ms
            while True:
                alive = [f for f in futs if not f.done()]
                if not alive:
                    break
                if ev.is_set():
                    for f in alive:
                        f.cancel()
                    try:
                        import subprocess as _sp
                        _sp.call(["taskkill", "/F", "/IM", "ffmpeg.exe"],
                                 stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                                 creationflags=0x08000000)
                    except Exception:
                        pass
                    break
                ev.wait(0.3)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        try:
            fail_fh.flush()
            fail_fh.close()
        except Exception:
            pass
        self._gui_queue().put(("bstat",
            "✓ %d new · ↷ %d had · ✗ %d failed"
            % (stats["ok"], stats["skip"], stats["fail"])))
        try:
            with lock:
                idx_file = os.path.join(music, "_index.txt")
                tmp = idx_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write("# filename|track|artist|uri\n")
                    for _k, _v in index.items():
                        fh.write("%s|%s|%s|%s\n" % (
                            _k, _v.get("track", ""), _v.get("artist", ""),
                            _v.get("uri", "")))
                os.replace(tmp, idx_file)
        except Exception:
            pass
        return stats

    def _read_batch_csvs(self, csv_dir, log):
        """Parse every Exportify-style CSV in csv_dir into
        {pl_name: [(track, artist, durms), ...]}."""
        playlists = {}
        for fname in sorted(os.listdir(csv_dir)):
            if not fname.endswith(".csv"):
                continue
            m = (re.match(r"^(.+)_part_(\d+)\.csv$", fname)
                 or re.match(r"^(.+)\.csv$", fname))
            pl_name = m.group(1) if m else fname[:-4]
            try:
                df = pd.read_csv(os.path.join(csv_dir, fname),
                                 encoding="utf-8-sig")
            except Exception as e:
                log("✗ %s: %s" % (fname, e))
                continue
            n = 0
            for _, row in df.iterrows():
                if pd.isnull(row.get("Track Name")):
                    continue
                track = str(row["Track Name"]).strip()
                artist = str(row.get("Artist Name(s)", "") or "").strip()
                durms = str(row.get("Duration (ms)", "") or "").strip()
                if track and track != "nan":
                    playlists.setdefault(pl_name, []).append(
                        (track, artist, durms))
                    n += 1
            log("[%s] %d tracks" % (pl_name, n))
        return playlists

    def _do_batch(self):
        """Start Batch (Full): parallel download of all unique tracks into
        the shared Music/ library."""
        csv_dir = self._bdir.get().strip()
        if not os.path.isdir(csv_dir):
            messagebox.showerror("Not found", csv_dir)
            return
        ev = threading.Event()
        self._cancel["batch"] = ev
        qual = self.qkey(self._bq.get())
        browser = self.browser()
        log_w = self._blog

        def log(msg):
            self._ui(lambda m=msg: (log_w.insert(tk.END, m + "\n"),
                                    log_w.see(tk.END)))

        def run():
            playlists = self._read_batch_csvs(csv_dir, log)
            rows = [(p, t, a, d)
                    for p, ts in playlists.items() for t, a, d in ts]
            log("TOTAL: %d rows across %d playlist(s)"
                % (len(rows), len(playlists)))
            st = self._run_batch_rows(rows, qual, browser, ev, log)
            idx = self._load_lib_index()
            for pl_name, ts in playlists.items():
                found, miss, m3u = self._write_m3u(pl_name, ts, idx)
                log("  %s.m3u — %d found, %d missing" % (pl_name[:35], found, miss))
            log("✅ BATCH COMPLETE — ✓ %d new · ↷ %d had · ✗ %d failed "
                "(see failed_downloads.txt)" % (st["ok"], st["skip"], st["fail"]))
            self._notify_done("SHUNGITE", "Batch download complete")
            self._ui(lambda: self._bstat.config(
                text="✓ %d new · ↷ %d · ✗ %d" % (st["ok"], st["skip"],
                                                 st["fail"])))
        threading.Thread(target=run, daemon=True).start()


    def _tab_spotify(self, nb):
        o = tk.Frame(nb, bg=BG)
        nb.add(o, text="  Spotify  ")
        _prefs_card = tk.Frame(o, bg=BG2)
        _prefs_card.pack(fill="x", padx=8, pady=2)
        self._search_prefs_row(_prefs_card, "src_spotify")

        banner = tk.Frame(o, bg="#0d2818", padx=14, pady=8)
        banner.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(banner, text="♪ SPOTIFY", font=("Segoe UI", 14, "bold"),
                 bg="#0d2818", fg="#1DB954").pack(side="left")
        tk.Label(banner, text="  personal recommendations · Made For You · browse",
                 font=("Segoe UI", 9), bg="#0d2818", fg="#9fd8b4").pack(side="left", padx=6)
        self._spstat = tk.Label(banner, text="● loading…", bg="#0d2818",
                                fg="#1DB954", font=FONT_B)
        self._spstat.pack(side="right")
        tk.Label(banner, text=WATERMARK, bg="#0d2818", fg="#2d5a3f",
                 font=("Segoe UI", 7, "italic")).pack(side="right", padx=10)

        br = tk.Frame(o, bg=BG2, padx=12, pady=6)
        br.pack(fill="x", padx=8, pady=3)
        self._btn(br, "↻ Refresh", self._sp_load, bg=BG3, fg=FG).pack(side="left")
        tk.Label(br, text="auto-detects your Spotify login from browser cookies",
                 bg=BG2, fg="#6b6b6b", font=FONT_SM).pack(side="left", padx=10)
        self._spq = tk.StringVar(value="Best Opus")
        self._spfmt = tk.StringVar(value=self.cfg.get("fmt_spotify", "audio"))
        fmtrow = tk.Frame(br, bg=BG2)
        fmtrow.pack(side="left", padx=(10, 0))
        tk.Label(fmtrow, text="Format:", bg=BG2, fg=FG2, font=FONT).pack(side="left", padx=(0, 4))
        ttk.Combobox(fmtrow, values=["audio", "video"], textvariable=self._spfmt,
                     state="readonly", width=12, font=FONT).pack(side="left")
        self._quality_row(br, self._spq)
        self._spout = self._folder_row(o, label="Library root (Music/ + Playlists/):")
        tk.Label(o, text="⚠ Music/ and Playlists/ are created side-by-side inside this folder — keep them together for .m3u to work",
                 bg=BG, fg="#666", font=("Segoe UI", 7)).pack(anchor="w", padx=8)

        # filter
        sf = tk.Frame(o, bg=BG, padx=8)
        sf.pack(fill="x", pady=(2, 0))
        self._spfilter = tk.Entry(sf, bg=BG3, fg=FG, font=FONT, relief="flat")
        self._spfilter.insert(0, "🔍 filter…")
        self._spfilter.bind("<FocusIn>", lambda e: self._spfilter.delete(0, tk.END)
                           if self._spfilter.get().startswith("🔍") else None)
        self._spfilter.pack(fill="x", ipady=4)

        split = tk.Frame(o, bg=BG)
        split.pack(fill="both", expand=True, padx=8, pady=4)

        left = tk.Frame(split, bg=BG3, padx=1, pady=1)
        left.pack(side="left", fill="both", expand=True)
        self._sptree = ttk.Treeview(left, columns=("name", "by", "n"),
                                    show="tree headings", height=14,
                                    selectmode="extended")
        self._sptree.heading("#0", text="Section")
        self._sptree.heading("name", text="Playlist")
        self._sptree.heading("by", text="By")
        self._sptree.heading("n", text="Tracks")
        self._sptree.column("#0", width=170)
        self._sptree.column("name", width=220)
        self._sptree.column("by", width=100)
        self._sptree.column("n", width=45)
        sb = ttk.Scrollbar(left, command=self._sptree.yview)
        self._sptree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._sptree.pack(fill="both", expand=True)
        self._sptree.bind("<<TreeviewSelect>>", self._sp_select)

        right = tk.Frame(split, bg=BG)
        right.pack(side="right", fill="both", expand=True, padx=(4, 0))
        self._spname = tk.Label(right, text="Select a playlist", bg=BG2, fg=FG,
                                 font=("Segoe UI", 11, "bold"), anchor="w")
        self._spname.pack(fill="x", padx=8, pady=(4, 0))
        self._spsub = tk.Label(right, text="", bg=BG2, fg=FG2, font=FONT_SM, anchor="w")
        self._spsub.pack(fill="x", padx=8, pady=(0, 4))
        self._sptracks = ttk.Treeview(right, columns=("t", "a", "l"),
                                       show="headings", height=12)
        self._sptracks.heading("t", text="Title")
        self._sptracks.heading("a", text="Artists")
        self._sptracks.heading("l", text="⏱")
        self._sptracks.column("t", width=260)
        self._sptracks.column("a", width=180)
        self._sptracks.column("l", width=45)
        tsb = ttk.Scrollbar(right, command=self._sptracks.yview)
        self._sptracks.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self._sptracks.pack(fill="both", expand=True)

        urlrow = tk.Frame(o, bg=BG)
        urlrow.pack(fill="x", padx=8)
        tk.Label(urlrow, text="Or paste playlist URL(s):", bg=BG, fg=FG2,
                 font=FONT_SM).pack(side="left")
        self._spurl = tk.Entry(urlrow, bg=BG3, fg=FG, font=FONT_SM,
                               relief="flat")
        self._spurl.pack(side="left", fill="x", expand=True, ipady=4, padx=6)

        self._spmerge = tk.BooleanVar(value=False)
        tk.Checkbutton(o, text="Merge mode — combine selected playlists "
                               "into one cross-deduplicated download",
                       variable=self._spmerge, bg=BG, fg=FG2, font=FONT_SM,
                       selectcolor=BG3, activebackground=BG).pack(
            anchor="w", padx=8)

        ab = tk.Frame(o, bg=BG)
        ab.pack(fill="x", padx=8, pady=(2, 2))
        self._btn(ab, "⬇ DOWNLOAD FULL PLAYLIST", self._sp_dl, bg="#1DB954").pack(side="left")
        self._btn(ab, "→ YT Music (transfer)", self._sp_transfer_ytm, bg=BG3).pack(side="left", padx=(6, 0))
        self._btn(ab, "🔄 SYNC MISSING + M3U", self._sp_sync, bg=SILVER, fg="#0a0a0b").pack(side="left", padx=6)
        self._btn(ab, "📝 M3U ONLY", self._sp_m3u_only, bg=STEEL, fg="#ffffff").pack(side="left", padx=6)
        self._btn(ab, "☑ SELECT ALL", self._sp_select_all, bg=BG3, fg=FG).pack(side="left", padx=6)
        self._retry_sp_btn = self._btn(ab, "♻ RETRY FAILED", self._sp_retry_failed,
                                       bg=BG3, fg=FG)
        self._retry_sp_btn.pack(side="left", padx=6)
        self._cancel_btn(ab, "sp").pack(side="left", padx=6)

        pb_frame = tk.Frame(o, bg=BG)
        pb_frame.pack(fill="x", padx=8)
        self._sppb = ttk.Progressbar(pb_frame, mode="determinate", maximum=100)
        self._sppb.pack(fill="x")
        self._spblabel = tk.Label(pb_frame, text="", bg=BG, fg=FG2, font=FONT_SM)
        self._spblabel.pack(anchor="w")
        tk.Label(ab, text=WATERMARK, bg=BG, fg="#333",
                 font=("Segoe UI", 7, "italic")).pack(side="right", padx=8)

        # Live activity log — shows exactly what's happening step by step
        log_frame = tk.Frame(o, bg=BG3, padx=1, pady=1)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(2, 4))
        self._splog = tk.Text(log_frame, height=5, bg=BG3, fg="#888",
                               font=("Consolas", 8), relief="flat", wrap="word")
        splog_sb = ttk.Scrollbar(log_frame, command=self._splog.yview)
        self._splog.configure(yscrollcommand=splog_sb.set)
        splog_sb.pack(side="right", fill="y")
        self._splog.pack(fill="both", expand=True)
        self._splog.insert(tk.END, "  Activity log — downloads show here in real-time\n")

        self._spmap = {}
        self.root.after(500, self._sp_load)

    _enrich_sema = None

    def _enrich_async(self, audio_path, title, artist):
        """Fetch lyrics + cover for a freshly downloaded file (background)."""
        if not ten or not self.cfg.get("enrich_enabled", True):
            return
        try:
            import threading as _th
            if App._enrich_sema is None:
                App._enrich_sema = _th.Semaphore(2)

            def run():
                with App._enrich_sema:
                    try:
                        res = ten.enrich_file(audio_path, title, artist,
                                              do_embed=self.cfg.get(
                                                  "embed_art", True))
                        if any(res.values()):
                            got = ", ".join(k for k, v in res.items() if v)
                            def ui():
                                try:
                                    self._splog.insert(tk.END,
                                        f"  ✨ {title[:28]}: {got}\n")
                                    self._splog.see(tk.END)
                                except Exception:
                                    pass
                            self._ui(ui)
                    except Exception:
                        pass
            _th.Thread(target=run, daemon=True).start()
        except Exception:
            pass

    def _build_file_norms(self, folder):
        """Normalized names of ALL audio files in folder (any common ext).
        This is what makes new-version matching at least as good as old:
        the old version also matched mp3/m4a/flac it had downloaded before."""
        norms = {}
        try:
            EXTS = (".opus", ".mp3", ".m4a", ".flac", ".wav", ".ogg")
            for f in os.listdir(folder):
                low = f.lower()
                if any(low.endswith(e) for e in EXTS):
                    base = f[:f.rfind(".")]
                    key = self._normalize_match(base)
                    # drop stray format tokens left by normalize
                    toks = [t for t in key.split()
                            if t not in ("mp3", "m4a", "flac", "opus",
                                         "wav", "ogg")]
                    key = " ".join(toks)
                    norms.setdefault(key, f)
        except Exception:
            pass
        return norms

    _BAD_TITLE_WORDS = ("8d audio", "8d", "sped up", "slowed", "nightcore",
                        "live", "cover", "remix", "karaoke", "instrumental",
                        "lyric video", "reaction")

    def _have_track(self, track, artist, index, file_norms):
        """True if we already have this track. Checks:
        1. exact norm key of 'track artist' / 'track'
        2. token-set match: all query tokens present in candidate (and the
           title's first 2 tokens present) - order-independent
        3. strong substring containment either way"""
        n1 = self._normalize_match(f"{track} {artist}")
        n2 = self._normalize_match(track)
        if n2 and (n2 in file_norms):
            return True
        if n1 and n1 in file_norms:
            return True

        def toks(s):
            return set(t for t in s.split() if len(t) > 1)

        q1 = toks(n1)
        q2 = toks(n2)
        if not (q1 or q2):
            return False
        for k in list(file_norms.keys()) + list((index or {}).keys()):
            kt = toks(k if isinstance(k, str) else "")
            if not kt:
                continue
            # every meaningful query token appears in this candidate?
            probe = q2 or q1
            if probe and probe.issubset(kt):
                # require artist/title disambiguation: at least half of q1 too
                if not q1 or len(q1 & kt) >= max(1, len(q1) // 2):
                    return True
            # strong containment fallback
            if n2 and len(n2) >= 20 and (n2 in k or k[:len(n2)] == n2):
                return True
        return False

    def _get_sp_or_urls(self):
        """Selected playlists; if none, parse pasted playlist URLs from the
        URL box (Spotify/YTM both accepted). Returns list of dicts."""
        pls = self._get_selected_sp()
        if pls:
            return pls
        raw = ""
        try:
            raw = self._spurl.get().strip()
        except Exception:
            pass
        if not raw:
            return []
        out = []
        import re as _re2
        for line in raw.splitlines():
            line = line.strip()
            m = _re2.search(r"playlist[/:]([A-Za-z0-9_-]+)", line)
            a = _re2.search(r"album[/:]([A-Za-z0-9_-]+)", line)
            ar = _re2.search(r"artist[/:]([A-Za-z0-9_-]+)", line)
            if m:
                pid = m.group(1)
                out.append({"name": "Pasted " + pid[:8], "id": pid,
                            "owner": "pasted", "tracks": 0,
                            "kind": "playlist"})
            elif a:
                aid = a.group(1)
                out.append({"name": "Album " + aid[:8], "id": aid,
                            "owner": "pasted", "tracks": 0,
                            "kind": "album"})
            elif ar:
                arid = ar.group(1)
                out.append({"name": "Artist " + arid[:8], "id": arid,
                            "owner": "pasted", "tracks": 0,
                            "kind": "artist"})
        return out

    def _resolve_album_or_artist(self, pl_id, kind):
        """Resolve pasted album/artist ids into a track list via spotify_api.
        Returns (tracks, err)."""
        try:
            if kind == "album":
                tracks = spo.album_tracks(pl_id)
            elif kind == "artist":
                tracks = spo.artist_top_tracks(pl_id)
            else:
                return None, f"unknown kind {kind}"
            norm = []
            for t in tracks:
                norm.append({"name": t.get("title") or t.get("name") or "?",
                             "artist": t.get("artist", ""),
                             "duration": t.get("seconds", 0)})
            return norm, ""
        except Exception as e:
            return None, f"album/artist fetch error: {str(e)[:50]}"

    def _sp_api_tracks(self, pl_id):
        """Tracklist via official OAuth when connected; falls back to the
        cookie-based client.

        Returns (tracks, err):
          (list, "")      -> success (possibly empty list = genuinely empty)
          (None, "msg")   -> FETCH FAILED (callers must NOT treat as "all
                             downloaded"; they skip with a warning instead).
        """
        # Liked Songs pseudo-playlist
        if pl_id == "__LIKED__":
            cached = getattr(self, "_liked_cache", None)
            if cached:
                return cached, ""
            try:
                if spo:
                    tok = spo.get_access_token(None)
                    if tok:
                        liked = spo.get_liked(tok)
                        if liked:
                            self._liked_cache = liked
                            return liked, ""
                        return None, "could not fetch liked songs (not logged in?)"
            except Exception as e:
                return None, f"liked fetch error: {str(e)[:50]}"
            return None, "no Spotify connection for liked songs"

        errs = []
        oauth_failed = False
        # 1) OAuth (official API - most reliable track data)
        try:
            if spo:
                tok = spo.get_access_token(None)
                if tok:
                    d = spo.get_playlist_tracks(tok, pl_id, limit=500)
                    if d:                       # non-empty list = success
                        return d, ""
                    if d is None:
                        oauth_failed = True     # API error for THIS playlist
                    # d == [] -> could be real empty; fall through to verify
                else:
                    oauth_failed = False        # simply not logged in
            else:
                oauth_failed = False
        except Exception as e:
            errs.append(f"oauth: {str(e)[:40]}")
            oauth_failed = True

        # 2) Cookie-based client fallback
        try:
            tracks = sp.playlist_tracks(pl_id, limit=500)
            if tracks:
                return tracks, ""
            errs.append("cookie client returned 0")
            if oauth_failed:
                # both layers failed -> do NOT claim "empty/already done"
                return None, "; ".join(errs) or "fetch failed"
            return [], "; ".join(errs)   # verified genuinely-empty playlist
        except Exception as e:
            errs.append(f"cookies: {str(e)[:40]}")
            return None, "; ".join(errs)

    def _sp_api_playlists(self):
        """User playlists via OAuth if connected; else None (caller falls back)."""
        try:
            if spo:
                tok = spo.get_access_token(None)
                if tok:
                    pls = spo.get_my_playlists(tok, limit=50)
                    if pls:
                        return [{"name": p["name"], "id": p["id"],
                                 "owner": p.get("owner", ""),
                                 "tracks": p.get("tracks", 0)} for p in pls]
        except Exception:
            pass
        return None

    def _sp_load(self):
        self._apply_manual_sp_cookie()
        for r in self._sptree.get_children():
            self._sptree.delete(r)
        self._spmap.clear()
        self._spstat.config(text="● loading…", fg=GOLD)

        def run():
            sections = []

            # OAuth library first (your real playlists) when connected
            oauth_pls = self._sp_api_playlists()
            if oauth_pls:
                sections.append((("📚 Your Library (%d playlists)" % len(oauth_pls)),
                                 oauth_pls))
                # Liked Songs pseudo-playlist at the very top
                try:
                    tok = spo.get_access_token(None)
                    if tok:
                        liked = spo.get_liked(tok)
                        if liked:
                            self._liked_cache = liked
                            sections.append(("❤ Liked Songs (%d tracks)" % len(liked),
                                             [{"name": "Liked Songs",
                                               "id": "__LIKED__",
                                               "owner": "you",
                                               "tracks": len(liked)}]))
                except Exception:
                    pass
            try:
                for name, rows in sp.home_sections(20):
                    sections.append((f"✨ {name}", rows))
            except Exception as e:
                _msg = str(e)[:50]
                if not oauth_pls:
                    self._ui(lambda m=_msg: self._spstat.config(
                        text=f"● {m}", fg="#e63946"))
                    return

            if not oauth_pls and spo:
                sections.insert(0, ("🔑 Tip: Settings → Login to Spotify for "
                                    "your personal playlists", []))

            def fill():
                for name, rows in sections:
                    parent = self._sptree.insert("", "end", text=name, open=True)
                    for p in rows:
                        iid = self._sptree.insert(parent, "end", values=(
                            p.get("name", "?"), p.get("owner", ""),
                            p.get("tracks", "")))
                        self._spmap[iid] = p
                self._spstat.config(
                    text=f"● {len(self._spmap)} playlists loaded",
                    fg="#1DB954")
            self._ui(fill)
        threading.Thread(target=run, daemon=True).start()

    def _sp_select(self, _):
        sel = self._sptree.selection()
        if not sel or sel[0] not in self._spmap:
            return
        pl = self._spmap[sel[0]]
        self._spname.config(text=pl.get("name", ""))
        self._spsub.config(text=f"by {pl.get('owner', '?')} · {pl.get('tracks', '?')} tracks")
        for r in self._sptracks.get_children():
            self._sptracks.delete(r)

        def run():
            try:
                tracks = sp.playlist_tracks(pl["id"], limit=50)
            except Exception:
                return

            def fill():
                for t in tracks:
                    self._sptracks.insert("", "end", values=t)
            self._ui(fill)
        threading.Thread(target=run, daemon=True).start()

    def _get_selected_sp(self):
        """Playlist dicts for every selected leaf row (multi-select)."""
        out = []
        for sid in self._sptree.selection():
            p = self._spmap.get(sid)
            if p and p.get("id"):
                out.append(p)
        return out

    def _sp_dl(self):
        """Download EVERY selected playlist via the parallel queue.
        Playlists run concurrently (bounded); tracks within one stay sequential."""
        pls = self._get_sp_or_urls()
        if not pls:
            messagebox.showinfo("Pick one", "Select a playlist first or paste a playlist URL below it.")
            return
        npl = len(pls)
        ev = threading.Event()
        self._cancel["sp"] = ev
        music = self._music_dir()
        qual = self.qkey(self._spq.get())
        browser = self.browser()
        self.dq.reset_cancel()
        self._spstat.config(text=f"● 0/{npl} queued — starting…", fg=GOLD)

        prog = {"done": 0}

        def status(msg):
            def do():
                self._spstat.config(text=f"● {msg}")
                try:
                    # overall progress: fraction of finished playlists
                    frac = min(100.0, 100.0 * prog["done"] / max(npl, 1))
                    self._sppb.config(value=frac)
                    self._spblabel.config(text=msg[:90])
                except Exception:
                    pass
                try:
                    self._splog.insert(tk.END, f"  {msg}\n")
                    self._splog.see(tk.END)
                except Exception:
                    pass
            self._ui(do)

        def run():
            # MERGE MODE: one combined cross-deduplicated download
            if getattr(self, "_spmerge", None) and self._spmerge.get() \
                    and npl > 1:
                status(f"merge mode — fetching {npl} tracklists…")
                merged = {}
                pl_of = {}
                for pi, pl in enumerate(pls):
                    tracks, ferr = self._sp_api_tracks(pl["id"])
                    if ferr or not tracks:
                        status(f"⚠ {pl.get('name', '?')[:24]}: fetch "
                               f"failed — skipped")
                        continue
                    for t in tracks:
                        key = self._normalize_match(
                            (t.get("artist") or "") + " " +
                            (t.get("name") or t.get("title") or ""))
                        if key not in merged:
                            merged[key] = t
                            pl_of[key] = pl.get("name", "?")
                items = list(merged.values())
                status(f"merged: {len(items)} unique of "
                       f"{sum(1 for _ in merged)} "
                       f"(cross-deduplicated)")
                ev2 = threading.Event()
                okc = fai = skp = 0
                for ti, t in enumerate(items):
                    if ev.is_set():
                        break
                    title = t.get("name") or t.get("title") or "?"
                    artist = t.get("artist") or ""
                    st, msg = self._download_track_best_effort(
                        title, artist, music, qual, browser, ev)
                    if st == "ok":
                        okc += 1
                    elif st == "skip":
                        skp += 1
                    else:
                        fai += 1
                    if ti % 5 == 0:
                        status(f"merge {ti+1}/{len(items)} — "
                               f"✓{okc} ↷{skp} ✗{fai}")
                status(f"MERGE DONE — ✓{okc} ↷{skp} ✗{fai} from "
                      f"{len(items)} unique tracks")
                prog["done"] = npl
                return

            index = self._load_lib_index()
            g_ok = g_fail = g_skip = 0

            def job(pl, pi):
                nonlocal index
                pl_name = pl.get("name", "playlist")
                status(f"[{pi+1}/{npl}] {pl_name[:28]} — fetching tracklist…")
                kind = pl.get("kind")
                if kind in ("album", "artist"):
                    tracks, ferr = self._resolve_album_or_artist(pl["id"], kind)
                else:
                    tracks, ferr = self._sp_api_tracks(pl["id"])
                try:
                    pl["_tracks"] = tracks or []
                except Exception:
                    pass
                if tracks is None:
                    status(f"⚠ [{pi+1}/{npl}] {pl_name[:24]}: FETCH FAILED "
                           f"({ferr}) — skipped, NOT counted as done")
                    return
                total = len(tracks)
                if not total:
                    status(f"⚠ [{pi+1}/{npl}] {pl_name[:24]}: playlist is empty")
                    return
                ok = 0
                file_norms = self._build_file_norms(music)
                for i, (name, artists, _) in enumerate(tracks):
                    if ev.is_set():
                        break
                    if self._have_track(name, artists, index, file_norms):
                        status(f"[{pi+1}/{npl}] [i] already have: {name[:30]}")
                        continue
                    q = f"{name} {artists}"
                    status(f"[{pi+1}/{npl}] [{i+1}/{total}] 🔍 {name[:30]}")
                    try:
                        prefs = {"source": None, "cap_on": True,
                                 "cap_min": 40, "cap_max": 720}
                        try:
                            _ps = self._get_search_prefs("src_spotify")
                            prefs = {"source": _ps[0], "cap_on": _ps[1],
                                     "cap_min": _ps[2], "cap_max": _ps[3]}
                        except Exception:
                            pass
                        matched, results = _match_track(name, artists,
                                                        browser, prefs)
                        results = [matched] if matched else []
                        got = False
                        for res in results or []:
                            s, _err = download_one(
                                res["title"], res["url"], music,
                                self._spfmt.get(), qual, 0, browser=browser,
                                cancel_event=ev,
                                cookiefile=self._yt_cookiefile(),
                                subs=self.cfg.get("fetch_subtitles", False))
                            if s == "ok":
                                want_fn = (artists + " - " + name)
                                want_fn = re.sub(r'[<>:"/\\|?*]', "_",
                                                 want_fn).strip(" .")[:200]
                                genre = (tracks[i][3]
                                         if len(tracks[i]) > 3 else "")
                                _post_download_fixups(
                                    music, want_fn, name, artists,
                                    res.get("title", ""), genre=genre)
                                actual_fn = want_fn + ".opus"
                                with self._lib_lock:
                                    index[actual_fn] = {"track": name,
                                                        "artist": artists,
                                                        "uri": _vid_from_url(res.get("url", ""))}
                                ok += 1
                                got = True
                                break
                            elif "unavailable" in str(_err).lower() or "403" in str(_err):
                                continue
                            else:
                                break
                        if not got:
                            raise RuntimeError("no usable result")
                    except Exception as e:
                        self._record_failure("search", name, q)
                        status(f"[{pi+1}/{npl}] ✗ {name[:28]} — {str(e)[:40]}")
                    time.sleep(0.3)
                # per-playlist m3u after its downloads
                try:
                    found, _m, m3u = self._write_m3u(pl_name, tracks, index)
                    status(f"✓ [{pi+1}/{npl}] {pl_name[:24]}: {ok}/{total} new → "
                           f"{os.path.basename(m3u)}")
                except Exception as e:
                    status(f"✗ [{pi+1}/{npl}] {pl_name[:24]}: .m3u failed ({str(e)[:40]})")

            # submit each playlist as a queue job
            done_ct = {"n": 0}
            errs = {"n": 0}
            lock = threading.Lock()

            def on_done(jid, err):
                with lock:
                    done_ct["n"] += 1
                    if err:
                        errs["n"] += 1
                prog["done"] = done_ct["n"]
                status(f"… {done_ct['n']}/{npl} playlists finished")
                if done_ct["n"] >= len(pls):
                    try:
                        self._save_lib_index(index)
                    except Exception:
                        pass
                    final = ("⏹ cancelled — " if ev.is_set() else "✓ ALL DONE — ")
                    status(f"{final}queue complete across {npl} playlists "
                           f"({errs['n']} playlist errors)")
                    self._maybe_shutdown()
                    # duplicates report: same track in multiple playlists
                    try:
                        seen = {}
                        for p in pls:
                            for t in (p.get("_tracks") or []):
                                key = self._normalize_match(f"{t[0]} {t[1]}")[:50]
                                if key:
                                    seen.setdefault(key, set()).add(p.get("name", "?"))
                        dups = {k: v for k, v in seen.items() if len(v) > 1}
                        if dups:
                            status(f"ℹ {len(dups)} track(s) appear in multiple playlists:")
                            for k, v in list(dups.items())[:10]:
                                status(f"    · {k[:40]}  ←  {'; '.join(sorted(v))[:70]}")
                    except Exception:
                        pass

            for pi, pl in enumerate(pls):
                if ev.is_set():
                    break
                self.dq.submit(job, pl, pi,
                               job_id="spdl-" + str(pl.get("id", pi)),
                               label=pl.get("name", "?"),
                               on_done=on_done)

        threading.Thread(target=run, daemon=True).start()

    def _sp_transfer_ytm(self):
        """Transfer Spotify playlists -> REAL YouTube Music playlists.

        Creates a matching playlist on music.youtube.com (via InnerTube) and
        adds the best-search-matched video for each track. No downloads, no
        .m3u files — the result lives in your YT Music library like
        tunemymusic.com."""
        pls = self._get_sp_or_urls()
        if not pls:
            messagebox.showinfo("Pick one",
                                "Select a Spotify playlist (or paste its URL) first.")
            return

        # Auth check: need YT Music authed cookies (SAPISID + __Secure-1PSID)
        try:
            import browser_cookies as _bc
            _ck = _bc.yt_cookies_dict_from_file()
        except Exception:
            _ck = {}
        if not (_ck.get("SAPISID") and _ck.get("__Secure-1PSID")):
            self._spstat.config(
                text="\u26a0 Log into YouTube Music first (Login \u2192 YouTube)",
                fg="#e63946")
            messagebox.showinfo("Sign in required",
                                "Log into YouTube Music inside the app first, "
                                "then transfer again.")
            return

        ev = threading.Event()
        self._cancel["sp"] = ev
        browser = self.browser()
        self._spstat.config(text="\u25cf transferring Spotify \u2192 YouTube Music\u2026",
                            fg=GOLD)

        def status(msg):
            self._ui(lambda m=msg: (self._spstat.config(text="\u25cf " + m),
                                    self._splog.insert(tk.END, "  " + m + "\n"),
                                    self._splog.see(tk.END)))

        def run():
            import innertube as _itb
            for pl in pls:
                if ev.is_set():
                    break
                name = pl.get("name", "playlist")
                status(f"creating '{name[:30]}' on YouTube Music\u2026")
                pid = _itb.create_playlist(name, _ck)
                if not pid:
                    status(f"\u2717 {name[:24]}: could not create YT Music playlist")
                    continue
                tracks, ferr = self._sp_api_tracks(pl["id"])
                tracks, ferr = self._sp_api_tracks(pl["id"])
                if not tracks:
                    status(f"\u26a0 {name[:24]}: no tracks ({ferr})")
                    continue
                ok = 0
                for k, trow in enumerate(tracks):
                    tname, tartist = trow[0], trow[1]
                    tsec = None
                    if len(trow) > 2 and trow[2]:
                        _m = re.match(r"(\d+):(\d{2})", str(trow[2]))
                        if _m:
                            tsec = int(_m.group(1)) * 60 + int(_m.group(2))
                    if ev.is_set():
                        break
                    q = f"{tname} {tartist}".strip()
                    status(f"[{k+1}/{len(tracks)}] \U0001f50d {tname[:24]}")
                    # search several candidates, keep the most-viewed
                    # duration-matched one (not just hits[0])
                    try:
                        hits = _itb.search_music(q, 6)
                    except Exception:
                        hits = []
                    if not hits:
                        try:
                            hits = search_youtube(q, 6, browser)
                        except Exception:
                            hits = []
                    if hits:
                        hits = [{"id": h.get("videoId") or h.get("id"),
                                 "title": h.get("title", ""),
                                 "channel": h.get("artist") or h.get("channel") or "",
                                 "seconds": h.get("seconds"),
                                 "views": h.get("views") or 0,
                                 "url": h.get("url") or ""}
                                for h in hits if h.get("videoId") or h.get("id")]
                    best = pick_best(hits, target_seconds=tsec,
                                     artist=tartist, title=tname,
                                     browser=browser,
                                     cookiefile=self._yt_cookiefile(),
                                     enrich_top=0) if hits else None
                    vid = best.get("id") if best else None
                    if vid and _itb.add_to_playlist(pid, vid, _ck):
                        ok += 1
                status(f"\u2713 '{name[:24]}' -> YT Music playlist ({ok}/{len(tracks)} added)")

        threading.Thread(target=run, daemon=True).start()


    def _do_batch_sync(self):
        """Sync Missing Only: parallel, Library-aware, spotDL-grade matching.
        Thin wrapper over the same engine Start Batch uses."""
        csv_dir = self._bdir.get().strip()
        if not os.path.isdir(csv_dir):
            messagebox.showerror("Not found", csv_dir)
            return
        ev = threading.Event()
        self._cancel["batch"] = ev
        qual = self.qkey(self._bq.get())
        browser = self.browser()
        log_w = self._blog
        self._bstat.config(text="Scanning…")

        def log(msg):
            self._ui(lambda m=msg: (log_w.insert(tk.END, m + "\n"),
                                    log_w.see(tk.END)))

        def run():
            playlists = self._read_batch_csvs(csv_dir, log)
            rows = [(p, t, a, d)
                    for p, ts in playlists.items() for t, a, d in ts]
            log("TOTAL: %d rows across %d playlist(s)"
                % (len(rows), len(playlists)))
            st = self._run_batch_rows(rows, qual, browser, ev, log)
            idx = self._load_lib_index()
            log("📝 Generating .m3u files…")
            for pl_name, ts in playlists.items():
                found, miss, m3u = self._write_m3u(pl_name, ts, idx)
                log("  %s.m3u — %d found, %d missing" % (pl_name[:35], found, miss))
            log("✅ SYNC COMPLETE — ✓ %d new · ↷ %d had · ✗ %d failed"
                % (st["ok"], st["skip"], st["fail"]))
            self._notify_done("SHUNGITE", "Batch sync complete")
            self._ui(lambda: self._bstat.config(
                text="✓ %d new · ↷ %d · ✗ %d" % (st["ok"], st["skip"],
                                                 st["fail"])))
        threading.Thread(target=run, daemon=True).start()


    def _do_batch_m3u(self):
        """Just generate .m3u files from CSVs (no downloads)."""
        csv_dir = self._bdir.get().strip()
        if not os.path.isdir(csv_dir):
            messagebox.showerror("Not found", csv_dir)
            return
        self._bstat.config(text="Generating .m3u…")

        def run():
            music = self._music_dir()
            index = self._load_lib_index()
            playlists = {}
            for fname in sorted(os.listdir(csv_dir)):
                if not fname.endswith(".csv"):
                    continue
                m = re.match(r'^(.+)_part_(\d+)\.csv$', fname) or re.match(r'^(.+)\.csv$', fname)
                pl_name = m.group(1) if m else fname[:-4]
                try:
                    df = pd.read_csv(os.path.join(csv_dir, fname), encoding="utf-8-sig")
                except Exception:
                    continue
                for _, row in df.iterrows():
                    if pd.isnull(row.get("Track Name")):
                        continue
                    track = str(row["Track Name"]).strip()
                    artist = str(row.get("Artist Name(s)", "")).strip()
                    if track and track != "nan":
                        playlists.setdefault(pl_name, []).append((track, artist, ""))

            for pl_name, tracks in playlists.items():
                found, miss, m3u = self._write_m3u(pl_name, tracks, index)
                self._ui(lambda p=pl_name, f=found, m=miss: self._blog.insert(
                    tk.END, f"  {p[:35]}.m3u — {f} found, {m} missing\n"))

            self._ui(lambda: self._bstat.config(text="✓ .m3u files generated"))
        threading.Thread(target=run, daemon=True).start()

    # ── Music library / M3U sync (shared with Spotify tab) ──────────────

    def _music_dir(self):
        """Flat Music folder — all songs in one place.
        Defaults to ~/Desktop/Downloaded_Media2.0/Music
        Customizable via the 'Save to' field (must be sibling of Playlists/)."""
        # Check if user set a custom path in the GUI
        custom = self._spout.get().strip() if hasattr(self, '_spout') else ""
        if custom and custom != self.cfg.get("output_dir", ""):
            # User changed it — use as the library root
            root = custom
        else:
            root = self.cfg.get("library_root", LIBRARY_ROOT)
        music = os.path.join(root, "Music")
        os.makedirs(music, exist_ok=True)
        return music

    def _playlist_dir(self):
        """Playlists folder — .m3u files. Must be sibling of Music/."""
        custom = self._spout.get().strip() if hasattr(self, '_spout') else ""
        if custom and custom != self.cfg.get("output_dir", ""):
            root = custom
        else:
            root = self.cfg.get("library_root", LIBRARY_ROOT)
        pl = os.path.join(root, "Playlists")
        os.makedirs(pl, exist_ok=True)
        return pl

    def _load_lib_index(self):
        """Load or scan the _index.txt for the flat Music folder."""
        music = self._music_dir()
        idx_file = os.path.join(music, "_index.txt")
        index = {}
        if os.path.exists(idx_file):
            try:
                with open(idx_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split("|", 3)
                        if len(parts) >= 2:
                            index[parts[0]] = {
                                "track": parts[1] if len(parts) > 1 else "",
                                "artist": parts[2] if len(parts) > 2 else "",
                                "uri": parts[3] if len(parts) > 3 else ""}
            except Exception:
                pass
        # Scan for unindexed files
        for f in os.listdir(music):
            if f.endswith(".opus") and f not in index:
                base = f[:-5]
                if " - " in base:
                    artist, track = base.split(" - ", 1)
                else:
                    artist, track = "", base
                index[f] = {"track": track, "artist": artist, "uri": ""}
        return index

    def _save_lib_index(self, index):
        music = self._music_dir()
        with open(os.path.join(music, "_index.txt"), "w", encoding="utf-8") as f:
            f.write(f"# PEAK Music Library — {len(index)} songs\n")
            f.write(f"# Generated: {time.strftime('%Y-%m-%d %H:%M')}\n\n")
            for fn in sorted(index):
                info = index[fn]
                f.write(f"{fn}|{info['track']}|{info['artist']}|{info.get('uri','')}\n")

    def _normalize_match(self, s):
        """Normalize for fuzzy matching."""
        s = s.lower().strip()
        s = re.sub(r'\(.*?\)', '', s)
        s = re.sub(r'\[.*?\]', '', s)
        s = re.sub(r'[^\w\s]', ' ', s)
        return re.sub(r'\s+', ' ', s).strip()

    def _check_have(self, track, artist, index, file_norms):
        """Check if a track is already downloaded (exact + fuzzy)."""
        key = f"{track}|{artist}"
        for fn, info in index.items():
            if f"{info['track']}|{info['artist']}" == key:
                if os.path.exists(os.path.join(self._music_dir(), fn)):
                    return fn
        norm = self._normalize_match(f"{track} {artist}")
        if norm in file_norms:
            return file_norms[norm]
        track_norm = self._normalize_match(track)
        if track_norm and len(track_norm) > 5:
            for fnorm, fn in file_norms.items():
                if track_norm in fnorm:
                    return fn
        return None

    def _write_m3u(self, pl_name, tracks, index):
        """Generate .m3u with RELATIVE paths (portable, works on mobile)
        + Harmonoid Playlists.JSON with ABSOLUTE file:/// URIs (desktop).

        .m3u uses relative paths so the whole folder is portable:
          Music/     ← songs
          Playlists/ ← .m3u files
        Copy both to your phone and they work as-is.

        Harmonoid gets absolute file:/// URIs because it doesn't resolve
        relative paths reliably."""
        # tolerate broken entries: None names/artists crash .lower() later
        clean = []
        for t in tracks:
            try:
                trk, art = t[0], t[1]
                dur = t[2] if len(t) > 2 else ""
            except Exception:
                continue
            if not trk or not str(trk).strip():
                continue
            clean.append((str(trk), str(art) if art else "", dur))
        tracks = clean
        music = os.path.normpath(self._music_dir())
        pl_dir = os.path.normpath(self._playlist_dir())
        safe = re.sub(r'[<>:"/\\|?*]', "_", pl_name)[:80]
        m3u_path = os.path.join(pl_dir, f"{safe}.m3u")

        # Build file lookup
        file_norms = {}
        for f in os.listdir(music):
            if f.endswith(".opus"):
                file_norms[self._normalize_match(f[:-5])] = f

        found = missing = 0
        harmonoid_tracks = []

        with open(m3u_path, "w", encoding="utf-8", errors="replace", newline="\n") as f:
            f.write("#EXTM3U\n")
            f.write(f"#PLAYLIST:{pl_name}\n")
            f.write("#EXTATTRIBUTES:name=" + pl_name.replace("|", " ") +
                    "|type=0\n\n")
            import mutagen as _mg

            def _dur_of(absf):
                """Seconds as int via mutagen; 0 if unreadable."""
                try:
                    audio = _mg.File(absf)
                    if audio and audio.info and audio.info.length:
                        return int(round(audio.info.length))
                except Exception:
                    pass
                return 0

            for track, artist, _dur in tracks:
                fn = self._check_have(track, artist, index, file_norms)
                if fn:
                    abs_file = os.path.normpath(os.path.join(music, fn))

                    # RELATIVE path for .m3u (portable)
                    rel_path = os.path.relpath(abs_file, pl_dir).replace("\\", "/")
                    dsec = _dur_of(abs_file) if not _dur else \
                        (int(_dur) if str(_dur).isdigit() else 0)
                    f.write(f"#EXTINF:{dsec},{artist} - {track}\n")
                    f.write(f"{rel_path}\n")

                    # ABSOLUTE URI for Harmonoid JSON (desktop only)
                    abs_fwd = abs_file.replace("\\", "/")
                    uri = pathlib.Path(abs_file).as_uri()

                    harmonoid_tracks.append({
                        "uri": uri,
                        "trackName": track,
                        "albumName": "",
                        "trackNumber": len(harmonoid_tracks) + 1,
                        "discNumber": 1,
                        "albumLength": len(tracks),
                        "albumArtistName": artist.split(";")[0] if artist else artist,
                        "trackArtistNames": [a.strip() for a in artist.split(";")] if artist else [],
                        "timeAdded": int(time.time()),
                        "duration": 0,
                        "bitrate": 256,
                    })
                    found += 1
                else:
                    missing += 1

        self._write_harmonoid(pl_name, harmonoid_tracks)
        return found, missing, m3u_path

    def _write_harmonoid(self, pl_name, tracks):
        """Write playlist directly to Harmonoid's Playlists.JSON."""
        import pathlib
        harmonoid_path = os.path.join(os.path.expanduser("~"), ".Harmonoid", "Playlists.json")

        data = {"playlists": []}
        max_id = 0
        if os.path.exists(harmonoid_path):
            try:
                with open(harmonoid_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for p in data.get("playlists", []):
                    pid = p.get("id", 0)
                    if isinstance(pid, int) and pid > max_id:
                        max_id = pid
            except Exception:
                pass

        new_playlist = {
            "name": pl_name,
            "id": max_id + 1,
            "tracks": tracks,
        }

        # Remove existing playlist with same name (re-sync replaces it)
        data["playlists"] = [
            p for p in data.get("playlists", [])
            if p.get("name") != pl_name
        ]
        data["playlists"].append(new_playlist)

        os.makedirs(os.path.dirname(harmonoid_path), exist_ok=True)
        with open(harmonoid_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    def _sp_sync(self):
        """Smart-sync EVERY selected playlist via the parallel queue."""
        pls = self._get_sp_or_urls()
        if not pls:
            messagebox.showinfo("Pick one", "Select a playlist first or paste a playlist URL below it.")
            return
        npl = len(pls)
        ev = threading.Event()
        self._cancel["sp"] = ev
        qual = self.qkey(self._spq.get())
        browser = self.browser()
        self.dq.reset_cancel()

        def status(msg):
            def do():
                self._spstat.config(text=f"● {msg}")
                try:
                    self._splog.insert(tk.END, f"  {msg}\n")
                    self._splog.see(tk.END)
                except Exception:
                    pass
            self._ui(do)

        def run():
            g_ok = g_fail = 0

            def job(pl, pi):
                nonlocal g_ok, g_fail
                pl_name = pl.get("name", "playlist")
                status(f"[{pi+1}/{npl}] {pl_name[:28]} — getting tracks…")
                tracks, ferr = self._sp_api_tracks(pl["id"])
                if tracks is None:
                    status(f"⚠ [{pi+1}/{npl}] {pl_name[:24]}: FETCH FAILED "
                           f"({ferr}) — skipped, NOT counted as done")
                    return
                status(f"[{pi+1}/{npl}] {len(tracks)} tracks — checking library…")

                index = self._load_lib_index()
                music = self._music_dir()
                file_norms = self._build_file_norms(music)

                missing = []
                have = 0
                for t_row in tracks:
                    track, artist = t_row[0], t_row[1]
                    tsec = 0
                    if len(t_row) > 2 and t_row[2]:
                        m2 = re.match(r"(\d+):(\d{2})", str(t_row[2]))
                        if m2:
                            tsec = int(m2.group(1)) * 60 + int(m2.group(2))
                    if self._have_track(track, artist, index, file_norms):
                        have += 1
                    else:
                        missing.append((track, artist, tsec))

                if not missing:
                    status(f"[{pi+1}/{npl}] ✓ all {have} already downloaded — .m3u…")
                    found, _, m3u_path = self._write_m3u(pl_name, tracks, index)
                    status(f"✓ [{pi+1}/{npl}] {found} tracks → "
                           f"{os.path.basename(m3u_path)}")
                    return

                dl_ok = dl_fail = 0
                total = len(missing)
                for i, miss_row in enumerate(missing):
                    track, artist = miss_row[0], miss_row[1]
                    tsec = miss_row[2] if len(miss_row) > 2 else 0
                    if ev.is_set():
                        break
                    q = f"{track} {artist}"
                    status(f"[{pi+1}/{npl}] [{i+1}/{total}] \U0001f50d {track[:30]} — {artist[:15]}")
                    try:
                        results = search_youtube(q, 6, browser)
                        if not results:
                            dl_fail += 1
                            status(f"[{pi+1}/{npl}] ✗ not found: {track[:25]}")
                            continue
                        # pick the most-viewed duration-matched result instead
                        # of blindly trying result[0] first
                        best = pick_best(results, target_seconds=tsec or None,
                                         artist=artist, title=track,
                                         browser=browser,
                                         cookiefile=self._yt_cookiefile())
                        ordered = ([best] + [r for r in results if r is not best]
                                   if best else results)
                        downloaded = False
                        for r_idx, res in enumerate(ordered):
                            if ev.is_set():
                                break
                            yt_title = res["title"]
                            # save as "Artist - Title" so library tags/m3u match
                            # Spotify metadata, not the YouTube video title
                            want_fn = re.sub(r'[<>:\"/\\|?*]', "_",
                                             f"{artist} - {track}"
                                             if artist else track).strip(" .")[:200]
                            state, result = download_one(
                                want_fn, res["url"], music,
                                self._spfmt.get(), qual, 0,
                                browser=browser, cancel_event=ev,
                                cookiefile=self._yt_cookiefile(),
                                meta_artist=artist,
                                yt_url=res.get("url", ""),
                                status_cb=lambda s, i=i, t=total, tr=track:
                                    status(f"[{pi+1}/{t}] {s} {tr[:25]}"),
                                subs=self.cfg.get("fetch_subtitles", False))
                            if state == "ok":
                                dl_ok += 1
                                actual_fn = want_fn + ".opus"
                                # if the file came out under the youtube title
                                # (custom outtmpl etc), rename it to Artist-Title
                                yt_guess = re.sub(r'[<>:\"/\\|?*]', "_",
                                                  yt_title).strip(" .")[:200] + ".opus"
                                try:
                                    _old = os.path.join(music, yt_guess)
                                    _new = os.path.join(music, actual_fn)
                                    if (yt_guess != actual_fn
                                            and os.path.exists(_old)
                                            and not os.path.exists(_new)):
                                        os.replace(_old, _new)
                                except Exception:
                                    pass
                                with self._lib_lock:
                                    index[actual_fn] = {"track": track,
                                                        "artist": artist,
                                                        "uri": _vid_from_url(res.get("url", ""))}
                                downloaded = True
                                break
                            elif state == "cancelled":
                                break
                            elif ("unavailable" in str(result).lower()
                                  or "403" in str(result)):
                                continue
                            else:
                                break
                        if not downloaded and not ev.is_set():
                            dl_fail += 1
                            self._record_failure("search", track, q)
                            if results:
                                status(f"[{pi+1}/{npl}] ✗ all "
                                       f"{len(results)} results failed: {track[:22]}")
                    except Exception as e:
                        dl_fail += 1
                        self._record_failure("search", track, f"{track} {artist}")
                        status(f"[{pi+1}/{npl}] ✗ {str(e)[:30]}")
                    time.sleep(0.3)

                self._save_lib_index(index)
                found, _, m3u_path = self._write_m3u(pl_name, tracks, index)
                status(f"✓ [{pi+1}/{npl}] {pl_name[:24]}: {found} matched · "
                       f"{dl_ok} new · {dl_fail} failed → {os.path.basename(m3u_path)}")

            done_ct = {"n": 0}

            def on_done(jid, err):
                with app_lock:
                    done_ct["n"] += 1
                if hasattr(self, "_sppb"):
                    self._sppb.config(value=min(100.0, 100.0 * done_ct["n"] / max(npl, 1)))
                status(f"… {done_ct['n']}/{npl} playlists synced")
                if done_ct["n"] >= len(pls):
                    final = "⏹ cancelled" if ev.is_set() else "✓ ALL DONE"
                    status(f"{final} across {len(pls)} playlists")
                    self._maybe_shutdown()

            app_lock = threading.Lock()
            for pi, pl in enumerate(pls):
                if ev.is_set():
                    break
                self.dq.submit(job, pl, pi,
                               job_id="spsync-" + str(pl.get("id", pi)),
                               label=pl.get("name", "?"),
                               on_done=on_done)

        threading.Thread(target=run, daemon=True).start()
    def _sp_m3u_only(self):
        """Generate .m3u for EVERY selected playlist (no downloads)."""
        pls = self._get_sp_or_urls()
        if not pls:
            messagebox.showinfo("Pick one", "Select a playlist first or paste a playlist URL below it.")
            return
        npl = len(pls)
        ev = threading.Event()
        self._cancel["sp"] = ev

        def run():
            for pi, pl in enumerate(pls):
                if ev.is_set():
                    break
                pl_name = pl.get("name", "playlist")
                self._ui(lambda p=pi, n=pl_name: self._spstat.config(
                    text=f"● [{p+1}/{npl}] {n[:28]}…", fg=GOLD))
                tracks, ferr = self._sp_api_tracks(pl["id"])
                if tracks is None:
                    self._ui(lambda s=ferr[:50]: self._spstat.config(
                        text=f"● ⚠ fetch failed ({s}) — skipped",
                        fg="#e63946"))
                    continue
                if not tracks:
                    self._ui(lambda p=pi: self._spstat.config(
                        text=f"● [{p+1}/{npl}] playlist is empty", fg=GOLD))
                    continue
                index = self._load_lib_index()
                found, missing, m3u_path = self._write_m3u(pl_name, tracks, index)
                self._ui(lambda f=found, m=os.path.basename(m3u_path), p=pi, t=npl:
                         self._spstat.config(
                             text=f"● [{p+1}/{t}] ✓ {f} tracks → {m}",
                             fg="#1DB954"))
        threading.Thread(target=run, daemon=True).start()

    # ── Tab: Settings ────────────────────────────────────────────────────


    # ── Tab: YT Music ──────────────────────────────────────────────────────
    def _tab_ytm(self, nb):
        o = tk.Frame(nb, bg=BG)
        nb.add(o, text="  YT Music  ")
        _prefs_card = tk.Frame(o, bg=BG2)
        _prefs_card.pack(fill="x", padx=8, pady=2)
        self._search_prefs_row(_prefs_card, "src_ytm")

        banner = tk.Frame(o, bg="#271300", padx=14, pady=8)
        banner.pack(fill="x")
        tk.Label(banner, text="♪ YT MUSIC",
                 font=("Segoe UI", 12, "bold"), bg="#271300",
                 fg="#ff6d00").pack(side="left")
        tk.Label(banner, text="your playlists · recommendations · auto-login from browser cookies",
                 font=("Segoe UI", 9), bg="#271300", fg="#ffd9b0").pack(side="left", padx=6)
        self._ytmstat = tk.Label(banner, text="● loading…", bg="#271300",
                                 fg="#ff6d00", font=FONT_B)
        self._ytmstat.pack(side="right")

        br = tk.Frame(o, bg=BG2, padx=12, pady=6)
        br.pack(fill="x", padx=8, pady=3)
        self._btn(br, "\u21bb Refresh", self._ytm_load, bg=BG3, fg=FG).pack(side="left")
        tk.Label(br, text="auto-detects your YouTube login from browser cookies",
                 bg=BG2, fg="#6b6b6b", font=FONT_SM).pack(side="left", padx=10)
        self._ytmq = tk.StringVar(value="Best Opus")
        self._ytmfmt = tk.StringVar(value=self.cfg.get("fmt_ytm", "audio"))
        fmtrow = tk.Frame(br, bg=BG2)
        fmtrow.pack(side="left", padx=(10, 0))
        tk.Label(fmtrow, text="Format:", bg=BG2, fg=FG2, font=FONT).pack(side="left", padx=(0, 4))
        ttk.Combobox(fmtrow, values=["audio", "video"], textvariable=self._ytmfmt,
                     state="readonly", width=12, font=FONT).pack(side="left")
        self._quality_row(br, self._ytmq)

        self._ytmout = self._folder_row(o, label="Save to:")
        sf = tk.Frame(o, bg=BG, padx=8)
        sf.pack(fill="x", pady=(2, 0))
        self._ytmfilter = tk.Entry(sf, bg=BG3, fg=FG, font=FONT, relief="flat")
        self._ytmfilter.insert(0, "\U0001f50d filter\u2026")
        self._ytmfilter.bind("<FocusIn>", lambda e: self._ytmfilter.delete(0, tk.END)
                             if self._ytmfilter.get().startswith("\U0001f50d") else None)
        self._ytmfilter.pack(fill="x", ipady=4)

        split = tk.Frame(o, bg=BG)
        split.pack(fill="both", expand=True, padx=8, pady=4)

        left = tk.Frame(split, bg=BG3, padx=1, pady=1)
        left.pack(side="left", fill="both", expand=True)
        cols = ("name", "by", "n")
        self._ytmtree = ttk.Treeview(left, columns=cols,
                                     show="tree headings", height=14,
                                     selectmode="extended")
        self._ytmtree.heading("#0", text="Section")
        self._ytmtree.heading("name", text="Playlist / Mix")
        self._ytmtree.heading("by", text="By")
        self._ytmtree.heading("n", text="Items")
        self._ytmtree.column("#0", width=170)
        self._ytmtree.column("name", width=220)
        self._ytmtree.column("by", width=100)
        self._ytmtree.column("n", width=45)
        sb = ttk.Scrollbar(left, command=self._ytmtree.yview)
        self._ytmtree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._ytmtree.pack(fill="both", expand=True)
        self._ytmtree.bind("<<TreeviewSelect>>", self._ytm_select)

        right = tk.Frame(split, bg=BG)
        right.pack(side="right", fill="both", expand=True, padx=(4, 0))
        self._ytmname = tk.Label(right, text="Select a playlist", bg=BG2, fg=FG,
                                 font=("Segoe UI", 11, "bold"), anchor="w")
        self._ytmname.pack(fill="x", padx=8, pady=(4, 0))
        self._ytmsub = tk.Label(right, text="", bg=BG2, fg=FG2, font=FONT_SM, anchor="w")
        self._ytmsub.pack(fill="x", padx=8, pady=(0, 4))

        tframe = tk.Frame(right, bg=BG3, padx=1, pady=1)
        tframe.pack(fill="both", expand=True)
        self._ytmtracks = ttk.Treeview(tframe, columns=("t", "a", "d"),
                                       show="headings", height=9)
        self._ytmtracks.heading("t", text="Title")
        self._ytmtracks.heading("a", text="Artists")
        self._ytmtracks.heading("d", text="Dur")
        self._ytmtracks.column("t", width=240)
        self._ytmtracks.column("a", width=150)
        self._ytmtracks.column("d", width=50)
        tsb = ttk.Scrollbar(tframe, command=self._ytmtracks.yview)
        self._ytmtracks.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self._ytmtracks.pack(fill="both", expand=True)

        ab = tk.Frame(o, bg=BG)
        ab.pack(fill="x", padx=8, pady=(2, 4))
        self._btn(ab, "\u2b07 DOWNLOAD SELECTED", self._ytm_dl,
                  bg="#ff6d00", fg="#000").pack(side="left")
        self._btn(ab, "\U0001f4dd M3U ONLY", self._ytm_m3u_only,
                  bg=STEEL, fg="#ffffff").pack(side="left", padx=8)
        self._btn(ab, "☑ SELECT ALL", self._ytm_select_all,
                  bg=BG3, fg=FG).pack(side="left", padx=8)
        self._btn(ab, "\u2192 Spotify (transfer)", self._ytm_transfer_sp,
                  bg=BG3, fg="#1DB954").pack(side="left", padx=8)
        self._retry_yt_btn = self._btn(ab, "♻ RETRY FAILED", self._ytm_retry_failed,
                                       bg=BG3, fg=FG)
        self._retry_yt_btn.pack(side="left", padx=8)
        self._cancel_btn(ab, "ytm").pack(side="left", padx=4)

        ypb_frame = tk.Frame(o, bg=BG)
        ypb_frame.pack(fill="x", padx=8)
        self._ytmpb = ttk.Progressbar(ypb_frame, mode="determinate", maximum=100)
        self._ytmpb.pack(fill="x")
        self._ytmpblabel = tk.Label(ypb_frame, text="", bg=BG, fg=FG2, font=FONT_SM)
        self._ytmpblabel.pack(anchor="w")

        log_frame = tk.Frame(o, bg=BG)
        log_frame.pack(fill="x", padx=8, pady=(0, 6))
        self._ytmlog = tk.Text(log_frame, height=5, bg=BG3, fg="#888",
                               insertbackground=FG, relief="flat", font=FONT_SM)
        ylog_sb = ttk.Scrollbar(log_frame, command=self._ytmlog.yview)
        self._ytmlog.configure(yscrollcommand=ylog_sb.set)
        ylog_sb.pack(side="right", fill="y")
        self._ytmlog.pack(fill="both", expand=True)
        self._ytmlog.insert(tk.END, "  Activity log \u2014 downloads show here in real-time\n")

        self._ytmmap = {}
        self._ytm_cancel = threading.Event()
        self._cancel["ytm"] = self._ytm_cancel
        self.root.after(600, self._ytm_load)

    def _ytm_log(self, msg):
        def do():
            self._ytmlog.insert(tk.END, msg + "\n")
            self._ytmlog.see(tk.END)
        try:
            self._ui(do)
        except Exception:
            pass

    def _ytm_load(self):
        for r in self._ytmtree.get_children():
            self._ytmtree.delete(r)
        self._ytmmap.clear()
        self._ytmstat.config(text="\u25cf loading\u2026", fg=GOLD)

        import xml.etree.ElementTree as ET
        import urllib.request as _ur

        def run():
            sections = []
            try:
                bc_ok = False
                hdr = ""
                try:
                    import browser_cookies as _bc
                    # prefer the embedded-webview cookie file (what the user
                    # actually logged into via the app) over system-browser cookies
                    _ck = _bc.yt_cookies_dict_from_file()
                    if _ck:
                        hdr = "; ".join("%s=%s" % (k, v) for k, v in _ck.items() if v)
                        bc_ok = True
                    else:
                        hdr = _bc.youtube_cookie_header() or ""
                        bc_ok = bool(hdr)
                except Exception:
                    pass
                # Fetch library + guide in PARALLEL — the old sequential chain
                # (~10-20s) now completes in ~2-4s; whichever answers first wins.
                import concurrent.futures as _cf

                lib = []
                guide_names = []

                def _fetch_lib():
                    out = []
                    if bc_ok:
                        try:
                            import innertube as _itb2
                            for plp in _itb2.get_library_playlists(_ck):
                                out.append({"name": plp.get("title", "?"),
                                            "owner": "YT Music",
                                            "id": plp.get("playlistId", ""),
                                            "type": "ytm"})
                        except Exception:
                            pass
                    return out

                def _fetch_guide():
                    out_n = []
                    try:
                        import urllib.request as _ur2
                        url = ("https://music.youtube.com/youtubei/v1/guide"
                               "?prettyPrint=false")
                        body = json.dumps({"context": {"client": {
                            "clientName": "WEB_REMIX",
                            "clientVersion": "1.20240101.01.00"}}}).encode()
                        req2 = _ur2.Request(url, data=body, headers={
                            "Content-Type": "application/json",
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                            "Cookie": hdr or ""})
                        with _ur2.urlopen(req2, timeout=8) as r:
                            guide = json.load(r)
                        def walk(o):
                            if isinstance(o, dict):
                                if "guideEntryRenderer" in o:
                                    g = o["guideEntryRenderer"]
                                    t = "".join(x.get("text", "") for x in
                                                g.get("formattedTitle", {}).get("content", [])
                                                ) if isinstance(g.get("formattedTitle"), dict) else ""
                                    if not t:
                                        t = "".join(x.get("text", "") for x in
                                                    g.get("title", {}).get("runs", []))
                                    pid = g.get("navigationEndpoint", {}).get(
                                        "watchPlaylistEndpoint", {}).get("playlistId")
                                    if t and pid:
                                        out_n.append((t, pid))
                                for v in o.values():
                                    walk(v)
                            elif isinstance(o, list):
                                for v in o:
                                    walk(v)
                        walk(guide)
                    except Exception:
                        pass
                    return out_n

                with _cf.ThreadPoolExecutor(max_workers=2) as ex:
                    f_lib, f_guide = ex.submit(_fetch_lib), ex.submit(_fetch_guide)
                    lib = f_lib.result()
                    guide_names = f_guide.result()

                # premium library first, then guide extras, then mixes fallback.
                seen_pids = {pl["id"] for pl in lib}
                for t, pid in guide_names:
                    if pid not in seen_pids:
                        lib.append({"name": t, "owner": "YT Music",
                                    "id": pid, "type": "ytm"})
                if lib:
                    sections.append(("📚 Your library", lib))
                elif bc_ok:
                    sections.append(("📚 Your library (empty — like or add playlists)", []))
                else:
                    sections.append(("⚠ Sign into YouTube to see your library", []))
                for sec_name, pl_id in [("Trending", "PLrAXtmErZgOdP_8GztsuKi9nrraNbKKp4"),
                                         ("Top ~ New Releases", "PL4fGSI1pDJn6puJdseH2Rt9sMvt9E2M4i")]:
                    sections.append((f"\u2728 {sec_name}", [
                        {"name": sec_name + " playlist", "owner": "YouTube Music",
                         "id": pl_id, "type": "ytm"}]))
            except Exception as e:
                pass
            finally:
                def fill():
                    for name, rows in sections:
                        parent = self._ytmtree.insert("", "end", text=name, open=True)
                        for p in rows:
                            iid = self._ytmtree.insert(parent, "end", values=(
                                p.get("name", "?"), p.get("owner", ""),
                                p.get("tracks", "")))
                            self._ytmmap[iid] = p
                    self._ytmstat.config(
                        text=f"\u25cf {len(self._ytmmap)} playlists loaded",
                        fg="#ff6d00" if self._ytmmap else "#e63946")
                self._ui(fill)

        threading.Thread(target=run, daemon=True).start()

    def _ytm_select(self, _evt):
        sel = self._ytmtree.selection()
        if not sel:
            return
        info = self._ytmmap.get(sel[0])
        if not info or not info.get("id"):
            return
        for row in self._ytmtracks.get_children():
            self._ytmtracks.delete(row)
        self._ytmname.config(text=info.get("name", "Playlist"))
        self._ytmsub.config(text="loading\u2026")

        def run():
            try:
                opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                        "skip_download": True, "playlist_items": "1-100"}
                b = self.browser()
                if b and b != "none":
                    opts["cookiesfrombrowser"] = (b,)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    inf = ydl.extract_info(
                        f"https://music.youtube.com/playlist?list={info['id']}",
                        download=False)
                rows = []
                for e in (inf or {}).get("entries") or []:
                    if not e:
                        continue
                    dur = e.get("duration")
                    ds = f"{dur // 60}:{dur % 60:02d}" if isinstance(dur, int) else ""
                    rows.append((e.get("title") or "?",
                                 e.get("uploader") or "", ds))
                def fill():
                    for t, a, d in rows[:200]:
                        self._ytmtracks.insert("", "end", values=(t, a, d))
                    self._ytmsub.config(text=f"{len(rows)} tracks")
                self._ui(fill)
            except Exception as e:
                self._ui(lambda: self._ytmsub.config(text=f"\u26a0 {str(e)[:60]}"))

        threading.Thread(target=run, daemon=True).start()

    def _get_selected_ytm(self):
        return [self._ytmmap[i] for i in self._ytmtree.selection()
                if i in self._ytmmap and self._ytmmap[i].get("id")]

    def _ytm_dl_one_playlist(self, pl, save_root, ev, tag):
        """Download one YT Music playlist by searching each track on YouTube."""
        premium_q = self.qkey(self._ytmq.get())
        folder = re.sub(r'[<>:"/\\|?*]', "_", pl.get("name", "playlist"))[:80]
        save_dir = os.path.join(save_root, folder)
        os.makedirs(save_dir, exist_ok=True)
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                "skip_download": True}
        b = self.browser()
        if b and b != "none":
            opts["cookiesfrombrowser"] = (b,)
        with yt_dlp.YoutubeDL(opts) as ydl:
            inf = ydl.extract_info(
                f"https://music.youtube.com/playlist?list={pl['id']}", download=False)
        entries = [e for e in (inf or {}).get("entries") or [] if e]
        ok = fail = 0
        total = len(entries)
        for i, e in enumerate(entries):
            if ev.is_set():
                break
            title = e.get("title") or "?"
            vid = e.get("id")
            url = f"https://www.youtube.com/watch?v={vid}"
            self._ui(lambda i=i, t=total, ti=title: (
                self._ytmstat.config(text=f"\u25cf [{i+1}/{t}] {ti[:30]}")))
            s, m = download_one(title, url, save_dir, self._ytmfmt.get(), premium_q,
                                self.cfg.get("speed_limit_kbps", 0),
                                browser=b, cancel_event=ev,
                                cookiefile=self._yt_cookiefile(),
                                status_cb=lambda st: None,
                                subs=self.cfg.get("fetch_subtitles", False))
            if s == "ok":
                ok += 1
                self._ytm_log(f"  \u2713 [{i+1}/{total}] {title[:45]}")
            else:
                fail += 1
                self._ytm_log(f"  \u2717 [{i+1}/{total}] {title[:45]} \u2014 {str(m)[:40]}")
        return ok, fail

    def _ytm_transfer_sp(self):
        """Transfer YT Music playlists -> REAL Spotify playlists.

        Fetches track names/artists from the selected YT Music playlist,
        searches Spotify for each track URI, and creates a matching Spotify
        playlist (via OAuth). No downloads, no .m3u."""
        pls = self._get_selected_ytm()
        if not pls:
            messagebox.showinfo("Pick one", "Select a YT Music playlist first.")
            return
        import spotify_oauth as _so
        tok = _so.get_access_token(None)
        if not tok:
            self._ytmstat.config(text="\u26a0 Log into Spotify first (Spotify tab)",
                                 fg="#e63946")
            messagebox.showinfo("Sign in required",
                                "Log into Spotify first, then transfer again.")
            return
        me = _so.get_me(tok)
        uid = me.get("id") if me else None
        if not uid:
            self._ytmstat.config(text="\u26a0 could not read Spotify user id", fg="#e63946")
            return

        ev = threading.Event()
        self._cancel["ytm"] = ev
        b = self.browser()

        def status(msg):
            self._ui(lambda m=msg: self._ytmstat.config(text="\u25cf " + m))

        def run():
            for pl in pls:
                if ev.is_set():
                    break
                name = pl.get("name", "playlist")
                status(f"creating '{name[:28]}' on Spotify\u2026")
                pid = _so.create_playlist(tok, uid, name)
                if not pid:
                    self._ui(lambda n=name: self._ytm_log(
                        f"  \u2717 {n[:40]} \u2014 Spotify create failed"))
                    continue
                # fetch tracks
                opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                        "skip_download": True}
                if b and b != "none":
                    opts["cookiesfrombrowser"] = (b,)
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        inf = ydl.extract_info(
                            f"https://music.youtube.com/playlist?list={pl['id']}",
                            download=False)
                    entries = [e for e in (inf or {}).get("entries") or [] if e]
                except Exception:
                    entries = []
                uris = []
                total = len(entries)
                for i, e in enumerate(entries):
                    if ev.is_set():
                        break
                    title = e.get("title") or "?"
                    artist = e.get("uploader") or e.get("channel") or ""
                    q = f"{title} {artist}".strip()
                    status(f"[{i+1}/{total}] \U0001f50d {title[:22]}")
                    uri = _so.search_track(tok, q)
                    if uri:
                        uris.append(uri)
                if uris:
                    _so.add_items(tok, pid, uris)
                self._ui(lambda n=name, u=len(uris): self._ytm_log(
                    f"  \u2713 {n[:40]} \u2014 {u}/{total} added to Spotify"))
            status("\u2713 transfer complete \u2014 open Spotify")

        threading.Thread(target=run, daemon=True).start()

    def _ytm_dl(self):
        pls = self._get_selected_ytm()
        if not pls:
            messagebox.showinfo("Pick", "Select one or more playlists first.")
            return
        save_root = self._ytmout.get().strip() or r"D:\PEAK_downloads"
        ev = threading.Event()
        self._ytm_cancel = ev
        self._cancel["ytm"] = ev

        def run():
            gok = gfail = 0
            try:
                for pi, pl in enumerate(pls):
                    if ev.is_set():
                        break
                    pname = pl.get("name", "?")
                    try:
                        self._ytm_log(f"=== {pname} ({pi+1}/{len(pls)}) ===")
                        ok, fail = self._ytm_dl_one_playlist(pl, save_root, ev, "")
                        gok += ok
                        gfail += fail
                    except Exception as e:
                        gfail += 1
                        self._ytm_log(f"\u2717 [{pi+1}/{len(pls)}] {pname[:28]} "
                                      f"failed ({str(e)[:40]}) \u2014 continuing")
                msg = f"\u2713 done \u2014 {gok} downloaded, {gfail} failed"
                self._ytm_log(("\u23f9 " if ev.is_set() else "") + msg)
                self._ui(lambda: self._ytmstat.config(
                    text=msg[:60], fg="#ff6d00"))
            except Exception as e:
                self._ytm_log(f"\u2717 error: {str(e)[:80]}")

        threading.Thread(target=run, daemon=True).start()

    def _ytm_m3u_only(self):
        pls = self._get_selected_ytm()
        if not pls:
            messagebox.showinfo("Pick", "Select one or more playlists first.")
            return
        save_root = self._ytmout.get().strip() or r"D:\PEAK_downloads"

        def run():
            for pi, pl in enumerate(pls):
                folder = re.sub(r'[<>:"/\\|?*]', "_", pl.get("name", "playlist"))[:80]
                save_dir = os.path.join(save_root, folder)
                os.makedirs(save_dir, exist_ok=True)
                opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
                b = self.browser()
                if b and b != "none":
                    opts["cookiesfrombrowser"] = (b,)
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        inf = ydl.extract_info(
                            f"https://music.youtube.com/playlist?list={pl['id']}",
                            download=False)
                except Exception as e:
                    self._ytm_log(f"\u2717 {pl.get('name','?')}: {str(e)[:60]}")
                    continue
                lines = ["#EXTM3U"]
                found = missing = 0
                for e in (inf or {}).get("entries") or []:
                    if not e:
                        continue
                    title = e.get("title") or "?"
                    match = None
                    if os.path.isdir(save_dir):
                        probe = re.sub(r"[^\w]", "", title.lower())[:40]
                        for f in os.listdir(save_dir):
                            base_, ext_ = os.path.splitext(f)
                            if ext_.lower() not in (".opus", ".m4a", ".mp3"):
                                continue
                            nb_ = re.sub(r"[^\w]", "", base_.lower())
                            if probe and (probe in nb_ or nb_[:40] in probe):
                                match = f
                                break
                    if match:
                        lines.append("#EXTINF:-1," + title)
                        lines.append(match)
                        found += 1
                    else:
                        missing += 1
                out = os.path.join(save_dir, folder + ".m3u")
                with open(out, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines))
                self._ytm_log(f"\U0001f4dd {folder}.m3u \u2014 {found} found ({missing} missing)")
            self._ui(lambda: self._ytmstat.config(text="\u2713 .m3u files written", fg="#ff6d00"))

        threading.Thread(target=run, daemon=True).start()

    def _sp_select_all(self):
        """Select every leaf row (real playlists) in the Spotify tree."""
        leaves = []
        def walk(parent):
            for iid in self._sptree.get_children(parent):
                if self._sptree.get_children(iid):
                    walk(iid)
                else:
                    leaves.append(iid)
        walk("")
        if not leaves:                       # flat tree fallback
            leaves = list(self._sptree.get_children(""))
        self._sptree.selection_set(leaves)
        if leaves:
            self._sptree.focus(leaves[0])
        n = len(self._get_selected_sp())
        self._spstat.config(text=f"☑ {n} playlists selected", fg=GOLD)

    def _ytm_select_all(self):
        """Select every leaf row (real playlists/mixes) in the YT Music tree."""
        leaves = []
        def walk(parent):
            for iid in self._ytmtree.get_children(parent):
                if self._ytmtree.get_children(iid):
                    walk(iid)
                else:
                    leaves.append(iid)
        walk("")
        if not leaves:
            leaves = list(self._ytmtree.get_children(""))
        self._ytmtree.selection_set(leaves)
        if leaves:
            self._ytmtree.focus(leaves[0])
        n = len(self._get_selected_ytm())
        self._ytmstat.config(text=f"☑ {n} playlists selected", fg="#ff6d00")



    def _tab_enhance(self, nb):
        """Enhance tab is deprecated — every download now runs
        titanium_enrich (cover + lyrics + tags) automatically."""
        o = tk.Frame(nb, bg=BG)
        nb.add(o, text="  Enhance  ")
        card = self._frame(o); card.pack(fill="both", expand=True, padx=8, pady=8)
        tk.Label(card, text="✨ Enhancement happens automatically on every download",
                 bg=BG2, fg=FG, font=FONT_B).pack(anchor="w", padx=10, pady=10)
        tk.Label(card, text=("Covers from iTunes/YouTube thumbnails, lyrics via LRClib "
                             "or YouTube description, embedded right after each download. "
                             "This tab's standalone buttons are stubbed — use Sync/Start "
                             "Batch inside Batch CSV / Playlist tabs and embedding happens inline."),
                 bg=BG2, fg=FG2, font=FONT, wraplength=720, justify="left").pack(anchor="w", padx=10, pady=(0, 10))

    def _enh_counts(self, event=None):
        """Show how many tracks already have cover/lyrics.
        Looks in the entered folder and its Music/ subfolder (the app's
        layout), so counts are never 0 when files exist."""
        try:
            import titanium_enrich as _te
            roots = []
            d = self._enhdir.get().strip()
            if d:
                roots.append(d)
                msub = os.path.join(d, "Music")
                if os.path.isdir(msub):
                    roots.append(msub)
                # also try configured library root's Music/
                lib = self.cfg.get("library_root", "")
                if lib:
                    lmsub = os.path.join(lib, "Music")
                    if lmsub not in roots and os.path.isdir(lmsub):
                        roots.insert(0, lmsub)
                    if lib not in roots:
                        roots.insert(0, lib)
            total = cov = lrc = 0
            seen = set()
            for root_dir in roots:
                if not os.path.isdir(root_dir):
                    continue
                real = os.path.realpath(root_dir)
                if real in seen:
                    continue
                seen.add(real)
                for f in os.listdir(root_dir):
                    low = f.lower()
                    if any(low.endswith(e) for e in _te.AUDIO_EXTS):
                        total += 1
                        base = os.path.join(root_dir, f[:f.rfind(".")])
                        if os.path.exists(base + ".jpg"):
                            cov += 1
                        if os.path.exists(base + ".lrc"):
                            lrc += 1
            msg = (f"{total} tracks · {cov} covers · {lrc} lyrics"
                   if total else "no audio found — check the path above")
            try:
                import glob as _g2
                seg = 0
                for root in roots:
                    for ext in ("*.opus", "*.mp3", "*.m4a", "*.flac"):
                        for p in _g2.glob(os.path.join(root, "**", ext),
                                          recursive=True):
                            seg += 1 if os.path.exists(
                                os.path.splitext(p)[0]
                                + ".segments.json") else 0
                if hasattr(self, "_enh_dash"):
                    self._enh_dash.config(
                        text=f"⏭ SponsorBlock sidecars: {seg}")
            except Exception:
                pass
            self._enhstat.config(text=msg, fg="#c0c8d8")
        except Exception:
            pass


    def _export_namida_playlists(self):
        """Write Playlists/export.json with all .m3u playlists found in the
        library (Namida can import M3U; this JSON is a convenience index)."""
        import json as _json
        import glob as _g
        root = self._enhdir.get().strip()
        pls_dir = os.path.join(root, "Playlists")
        out = []
        for m3u in sorted(_g.glob(os.path.join(pls_dir, "*.m3u"))):
            name = os.path.splitext(os.path.basename(m3u))[0]
            tracks = []
            try:
                for line in open(m3u, encoding="utf-8", errors="replace"):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    tracks.append(line)
            except Exception:
                pass
            out.append({"name": name, "trackCount": len(tracks),
                        "file": os.path.basename(m3u)})
        dest = os.path.join(pls_dir, "export.json")
        try:
            os.makedirs(pls_dir, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                _json.dump({"playlists": out}, f, indent=1,
                           ensure_ascii=False)
            self._ui(lambda n=len(out): self._enhstat.config(
                text=f"exported {n} playlists -> export.json",
                fg="#22c55e"))
        except Exception as e:
            self._ui(lambda s=str(e)[:50]: self._enhstat.config(
                text=f"export failed: {s}", fg=RED))

    def _fetch_artist_images(self):
        """Scan library index, save an artist image per distinct artist."""
        idx = os.path.join(self._enhdir.get().strip(), "Music", "_index.txt")
        if not os.path.exists(idx):
            messagebox.showinfo("No index",
                                f"No _index.txt at:\n{idx}")
            return
        artists = set()
        with open(idx, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|", 3)
                if len(parts) >= 3 and parts[2].strip():
                    artists.add(parts[2].strip())
        ev = threading.Event()
        self._cancel["enhance"] = ev

        def status(msg):
            self._ui(lambda m=msg: self._enhlog.insert(tk.END,
                     f"  {m}\n") or self._enhlog.see(tk.END))

        def run():
            import titanium_enrich as _te
            key = ""
            try:
                key = _te.lastfm_key()
            except Exception:
                pass

            lib = self._enhdir.get().strip()
            key = ""
            try:
                import titanium_enrich as _te2
                key = _te2.lastfm_key()
            except Exception:
                pass
            done = 0
            for i, a in enumerate(sorted(artists)):
                if ev.is_set():
                    break
                ok = _te.save_artist_image(a, lib, lastfm_key=key)
                done += 1 if ok else 0
                if i % 10 == 0:
                    status(f"🖼 [{i+1}/{len(artists)}] {a[:30]}")
            status(f"✓ artist images: {done}/{len(artists)} fetched")

        threading.Thread(target=run, daemon=True).start()

    def _auto_playlists(self):
        """Generate auto playlists: By Genre, By Decade, Recently Added."""
        import smart_playlists as _spm
        import mutagen as _mgm
        lib = self._enhdir.get().strip()
        if not os.path.isdir(os.path.join(lib, "Music")):
            messagebox.showinfo("Auto playlists", "Music folder not found.")
            return
        ev = threading.Event()
        self._cancel["enhance"] = ev

        def status(m):
            self._ui(lambda s=m: self._enhstat.config(text="● " + s))

        def run():
            try:
                res = _spm.generate_auto_collection(lib, _mgm)
                lines = [f"  [ {n:>4} ]  {name}" for name, n in res]
                summary = "\n".join(lines) or "  (no playlists generated)"
                self._ui(lambda: self._enhstat.config(
                    text=f"● {len(res)} auto playlists written"))
                self._notify_done("SHUNGITE",
                                  f"{len(res)} auto playlists generated")
            except Exception as e:
                _msg = str(e)[:60]
                self._ui(lambda m=_msg: self._enhstat.config(text="⚠ " + m))

        threading.Thread(target=run, daemon=True).start()

    def _backfill_missing(self):
        """Enrich files lacking album/lyrics/cover, reading title+artist from
        their embedded tags (covers files missing from the index too)."""
        import titanium_enrich as _te
        lib = self._enhdir.get().strip()
        if not os.path.isdir(os.path.join(lib, "Music")):
            messagebox.showinfo("Backfill", "Music folder not found.")
            return
        ev = threading.Event()
        self._cancel["enhance"] = ev
        embed = bool(getattr(self, "_enh_embed", None)
                     and self._enh_embed.get()) or True

        def status(m):
            self._ui(lambda s=m: self._enhstat.config(text="● " + s))

        def prog(done, total, fn, res):
            self._ui(lambda d=done, t=total: self._enhpb.config(
                value=100.0 * d / max(t, 1)) if hasattr(self, "_enhpb") else None)

        def run():
            try:
                status("backfill scanning…")
                d, e = _te.backfill_missing(lib, progress_cb=prog,
                                            should_stop=ev, do_embed=embed,
                                            target="all")
                self._ui(lambda: self._enhstat.config(
                    text=f"● backfill done — {e} enriched of {d} scanned"))
                self._notify_done("SHUNGITE", f"Backfill complete ({e})")
            except Exception as ex:
                _msg = str(ex)[:60]
                self._ui(lambda m=_msg: self._enhstat.config(text="⚠ " + m))

        threading.Thread(target=run, daemon=True).start()

    def _album_completion(self):
        """List partial albums and offer to fetch the full tracklist via
        MusicBrainz and download missing tracks."""
        import mutagen as _mg, glob as _g
        import musicbrainz as _mb
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        files = []
        for ext in (".opus", ".mp3", ".m4a", ".flac"):
            files += _g.glob(os.path.join(music, "**", "*" + ext),
                             recursive=True)
        albums = {}
        for p in files:
            try:
                a = _mg.File(p, easy=True)
                alb = " ".join(a.get("album", []) or [])
                if not alb:
                    continue
                title = " ".join(a.get("title", []) or []) or os.path.basename(p)
                artist = " ".join(a.get("artist", []) or [])
                albums.setdefault(alb, []).append((title, artist, p))
            except Exception:
                pass
        ordered = sorted(albums.items(), key=lambda kv: len(kv[1]))
        partial = [(a, ts) for a, ts in ordered if 1 <= len(ts) < 8]
        if not partial:
            messagebox.showinfo("Album completion",
                                "No obviously-partial albums found.")
            return
        win = tk.Toplevel(self.master)
        win.title("Partial albums")
        win.configure(bg=BG2)
        lb = tk.Listbox(win, bg=BG3, fg=FG, font=FONT, width=70, height=20)
        lb.pack(fill="both", expand=True, padx=8, pady=8)
        self._partial_albums = partial
        for a, ts in partial:
            lb.insert(tk.END, f"[{len(ts)} tracks] {a[:60]}  —  {ts[0][1][:20]}")
        st = tk.Label(win, text="", bg=BG2, fg=FG, font=FONT_SM)
        st.pack(pady=4)

        def use():
            sel = lb.curselection()
            if not sel:
                return
            alb, ts = partial[sel[0]]
            rep_title, rep_artist, _ = ts[0]
            st.config(text="fetching full tracklist…")
            win.update_idletasks()
            r = _mb.lookup_tracklist(rep_title, rep_artist) or {}
            full = r.get("tracks", [])
            if not full:
                st.config(text="could not resolve a full tracklist")
                return
            have = {t[0].lower() for t in ts}
            missing = [(tno, ttitle) for tno, ttitle, _ms in full
                       if ttitle.lower() not in have]
            if not missing:
                st.config(text="album appears complete already")
                return
            qual = self._fmt_ytm.get() if hasattr(self, "_fmt_ytm") else "opus256"
            browser = self._browser.get().split(" ")[0] if hasattr(self, "_browser") and self._browser.get() else "auto"

            def run():
                n = ok = 0
                for tno, ttitle in missing:
                    n += 1
                    q = f"{ttitle} {rep_artist}".strip()
                    res = search_youtube(q, 2, browser)
                    for rr in res or []:
                        s, _e = download_one(rr["title"], rr["url"], music,
                                             "audio", qual, 0, browser=browser)
                        if s == "ok":
                            ok += 1
                            break
                        if "unavailable" in str(_e).lower():
                            continue
                    self._ui(lambda o=ok, t=n: st.config(
                        text=f"filled {o}/{t}"))
                self._ui(lambda o=ok, t=n: st.config(
                    text=f"done — {o}/{t} missing tracks downloaded"))
                self._notify_done("SHUNGITE",
                                  f"Album completion: {ok}/{n} filled")
            threading.Thread(target=run, daemon=True).start()
            win.destroy()

        tk.Button(win, text="Fill missing tracks", command=use,
                  bg=GREEN, fg="#000", font=FONT).pack(pady=6)

    def _backfill_covers(self):
        """Targeted, faster cover-art backfill (parallel) for files lacking
        embedded cover art. Skips files that already have artwork."""
        import titanium_enrich as _te, glob as _g, mutagen as _mg
        from concurrent.futures import ThreadPoolExecutor as _TPE
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        files = []
        for ext in (".opus", ".mp3", ".m4a", ".flac"):
            files += _g.glob(os.path.join(music, "**", "*" + ext),
                             recursive=True)
        # narrow to files lacking cover (tag or sidecar)
        need = []
        for p in files:
            base = os.path.splitext(p)[0]
            if os.path.exists(base + ".jpg"):
                continue
            # an embedded picture counts as cover — check cheaply
            try:
                a = _mg.File(p, easy=True)
                # easy=True doesn't expose picture; fall back to full for opus
            except Exception:
                pass
            need.append(p)
        if not need:
            messagebox.showinfo("Covers", "No files are missing cover art. 👍")
            return
        self._ui(lambda: self._enhstat.config(
            text=f"🖼 backfilling covers for {len(need)} files…"))
        ev = threading.Event()
        self._cancel["enhance"] = ev
        done = 0

        def work(p):
            try:
                a = _mg.File(p, easy=True)
                title = " ".join(a.get("title", []) or []) or os.path.basename(p)
                artist = " ".join(a.get("artist", []) or [])
            except Exception:
                title = os.path.basename(p); artist = ""
            return _te.save_cover(title, artist, p)

        def run():
            n = ok = 0
            with _TPE(max_workers=8) as ex:
                for got in ex.map(work, need):
                    if ev.is_set():
                        break
                    n += 1
                    if got:
                        ok += 1
                    if n % 25 == 0:
                        self._ui(lambda d=n, o=ok: self._enhstat.config(
                            text=f"🖼 covers {o}/{d}"))
            self._ui(lambda: self._enhstat.config(
                text=f"🖼 cover backfill done — {ok} of {n} fetched"))
            self._notify_done("SHUNGITE", f"Cover backfill: {ok}/{n}")

        threading.Thread(target=run, daemon=True).start()

    def _backfill_lyrics(self):
        """Targeted parallel lyrics backfill for files lacking embedded
        lyrics and no .lrc sidecar."""
        import titanium_enrich as _te, glob as _g, mutagen as _mg
        from concurrent.futures import ThreadPoolExecutor as _TPE
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        files = []
        for ext in (".opus", ".mp3", ".m4a", ".flac"):
            files += _g.glob(os.path.join(music, "**", "*" + ext),
                             recursive=True)
        need = []
        for p in files:
            base = os.path.splitext(p)[0]
            if os.path.exists(base + ".lrc"):
                continue
            need.append(p)
        if not need:
            messagebox.showinfo("Lyrics", "No files are missing lyrics. 👍")
            return
        self._ui(lambda: self._enhstat.config(
            text=f"🎤 backfilling lyrics for {len(need)} files…"))
        ev = threading.Event()
        self._cancel["enhance"] = ev

        def work(p):
            try:
                a = _mg.File(p, easy=True)
                title = " ".join(a.get("title", []) or []) or os.path.basename(p)
                artist = " ".join(a.get("artist", []) or [])
            except Exception:
                title = os.path.basename(p); artist = ""
            # fetch lyrics into tags (embed-only) — no .lrc sidecar left
            return _te.fetch_lyrics_any(artist, title, p)

        def run():
            n = ok = 0
            with _TPE(max_workers=6) as ex:
                for got in ex.map(work, need):
                    if ev.is_set():
                        break
                    n += 1
                    if got:
                        ok += 1
                    if n % 25 == 0:
                        self._ui(lambda d=n, o=ok: self._enhstat.config(
                            text=f"🎤 lyrics {o}/{d}"))
            self._ui(lambda: self._enhstat.config(
                text=f"🎤 lyrics backfill done — {ok} of {n} found"))
            self._notify_done("SHUNGITE", f"Lyrics backfill: {ok}/{n}")

        threading.Thread(target=run, daemon=True).start()

    def _bpm_playlist(self):
        """Analyze BPM for the library, tag files, then write an energy-ramped
        playlist (slow→fast) under Playlists/. Runs in a background thread."""
        import acoustic_dedup as _ad, titanium_enrich as _te
        import glob as _g, mutagen as _mg
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        files = []
        for ext in (".opus", ".mp3", ".m4a", ".flac"):
            files += _g.glob(os.path.join(music, "**", "*" + ext),
                             recursive=True)
        if not files:
            messagebox.showinfo("BPM", "No audio files found.")
            return
        ev = threading.Event()
        self._cancel["enhance"] = ev
        self._ui(lambda: self._enhstat.config(
            text=f"🎚 analyzing BPM for {len(files)} files…"))

        def run():
            rows = []
            done = 0
            for p in files:
                if ev.is_set():
                    break
                bpm = _ad.detect_bpm(p)
                if bpm:
                    _te.tag_bpm(p, bpm)
                    rows.append((bpm, os.path.relpath(p, lib)))
                done += 1
                if done % 20 == 0:
                    self._ui(lambda d=done: self._enhstat.config(
                        text=f"🎚 BPM {d}/{len(files)}"))
            rows.sort()  # slow -> fast
            pl_dir = os.path.join(lib, "Playlists")
            os.makedirs(pl_dir, exist_ok=True)
            out = os.path.join(pl_dir, "BPM Energy.m3u")
            with open(out, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n#PLAYLIST:BPM Energy (slow→fast)\n")
                for bpm, rel in rows:
                    f.write(f"#EXTINF:-1,{bpm} BPM\n")
                    f.write(rel.replace(os.sep, "/") + "\n")
            self._ui(lambda: self._enhstat.config(
                text=f"🎚 BPM playlist written ({len(rows)} tracks)"))
            self._notify_done("SHUNGITE",
                              f"BPM playlist: {len(rows)} tracks")

        threading.Thread(target=run, daemon=True).start()

    def _normalize_batch(self):
        """Batch EBU R128 loudness normalization (-16 LUFS) across the
        library, skipping files already tagged as normalized."""
        import titanium_enrich as _te, glob as _g, mutagen as _mg
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        files = []
        for ext in (".opus", ".mp3", ".m4a", ".flac"):
            files += _g.glob(os.path.join(music, "**", "*" + ext),
                             recursive=True)
        todo = []
        for p in files:
            try:
                a = _mg.File(p, easy=False)
                t = dict(getattr(a, "tags", {}) or {})
                if "REPLAYGAIN_TRACK_GAIN" in t or t.get("loudnorm"):
                    continue
            except Exception:
                pass
            todo.append(p)
        if not todo:
            messagebox.showinfo("Normalize", "Nothing to normalize "
                                "(all files already tagged).")
            return
        ev = threading.Event()
        self._cancel["enhance"] = ev
        self._ui(lambda: self._enhstat.config(
            text=f"🔊 normalizing {len(todo)} files…"))

        def run():
            done = ok = 0
            for p in todo:
                if ev.is_set():
                    break
                try:
                    if _te.normalize_loudness(p):
                        ok += 1
                except Exception:
                    pass
                done += 1
                if done % 10 == 0:
                    self._ui(lambda d=done, o=ok: self._enhstat.config(
                        text=f"🔊 normalized {o}/{d} of {len(todo)}"))
            self._ui(lambda: self._enhstat.config(
                text=f"🔊 loudness batch done — {ok} files"))
            self._notify_done("SHUNGITE", f"Normalized {ok} files")

        threading.Thread(target=run, daemon=True).start()

    def _export_full_m3u(self):
        """Write a single M3U playlist of the entire library (relative
        ../Music/ paths, portable for phone sync)."""
        import glob as _g, mutagen as _mg, time as _t
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        files = []
        for ext in (".opus", ".mp3", ".m4a", ".flac"):
            files += _g.glob(os.path.join(music, "**", "*" + ext),
                             recursive=True)
        files.sort()
        if not files:
            messagebox.showinfo("M3U", "No audio files found.")
            return
        pl_dir = os.path.join(lib, "Playlists")
        os.makedirs(pl_dir, exist_ok=True)
        out = os.path.join(pl_dir, "All Library.m3u")
        with open(out, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n#PLAYLIST:All Library\n")
            for p in files:
                rel = os.path.relpath(p, lib).replace(os.sep, "/")
                title = os.path.splitext(os.path.basename(p))[0]
                try:
                    a = _mg.File(p, easy=True)
                    t = " ".join(a.get("title", []) or [])
                    ar = " ".join(a.get("artist", []) or [])
                    if t:
                        title = f"{ar} - {t}" if ar else t
                except Exception:
                    pass
                f.write(f"#EXTINF:-1,{title}\n")
                f.write(rel + "\n")
        self._ui(lambda: self._enhstat.config(
            text=f"📜 All Library.m3u written ({len(files)} tracks)"))
        self._notify_done("SHUNGITE", f"Full M3U: {len(files)} tracks")
        try:
            os.startfile(out)
        except Exception:
            pass

    def _embed_sidecars(self):
        """Embed existing .lrc/.jpg sidecars into the audio tags, then delete
        the sidecars (backfills files enriched before embed-only mode)."""
        import titanium_enrich as _te
        lib = self._enhdir.get().strip()
        ev = threading.Event()
        self._cancel["enhance"] = ev
        self._ui(lambda: self._enhstat.config(
            text="📦 embedding existing sidecars…"))

        def prog(done, total, fn, res):
            self._ui(lambda d=done, t=total: self._enhstat.config(
                text=f"📦 {d}/{t}"))

        def run():
            try:
                p, lyr, cover = _te.embed_existing_sidecars(
                    lib, progress_cb=prog, should_stop=ev, delete_after=True)
                self._ui(lambda: self._enhstat.config(
                    text=f"📦 embedded {lyr} lyrics + {cover} covers "
                         f"({p} scanned)"))
                self._notify_done("SHUNGITE",
                                  f"Embedded {lyr} lyrics, {cover} covers")
            except Exception as ex:
                _msg = str(ex)[:60]
                self._ui(lambda m=_msg: self._enhstat.config(
                    text="⚠ " + m))

        threading.Thread(target=run, daemon=True).start()

    def _verify_downloads(self):
        """Check every audio file is valid/playable via ffmpeg decode, and
        report corrupt/empty files with a written report."""
        import titanium_enrich as _te, glob as _g
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        files = []
        for ext in (".opus", ".mp3", ".m4a", ".flac", ".wav", ".ogg"):
            files += _g.glob(os.path.join(music, "**", "*" + ext),
                             recursive=True)
        if not files:
            messagebox.showinfo("Verify", "No audio files found.")
            return
        ev = threading.Event()
        self._cancel["enhance"] = ev
        self._ui(lambda: self._enhstat.config(
            text=f"✅ verifying {len(files)} downloads…"))

        def run():
            bad = []
            good = 0
            done = 0
            for p in files:
                if ev.is_set():
                    break
                r = _te.verify_audio(p)
                if r.get("valid"):
                    good += 1
                else:
                    bad.append((os.path.basename(p), r.get("reason", "?"),
                                r.get("size", 0)))
                done += 1
                if done % 100 == 0:
                    self._ui(lambda d=done: self._enhstat.config(
                        text=f"✅ verified {d}/{len(files)}"))
            rpt = os.path.join(lib, "verify_downloads.txt")
            with open(rpt, "w", encoding="utf-8") as f:
                f.write(f"SHUNGITE download verification\n"
                        f"total: {len(files)} · valid: {good} · "
                        f"corrupt/empty: {len(bad)}\n\n")
                for name, reason, size in bad:
                    f.write(f"{name}\t{size} B\t{reason[:120]}\n")
            self._ui(lambda: self._enhstat.config(
                text=f"✅ {good} valid / {len(bad)} bad → verify_downloads.txt"))
            self._notify_done("SHUNGITE",
                              f"Verified: {good} OK, {len(bad)} bad")
            try:
                os.startfile(rpt)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _batch_reencode(self):
        """Re-encode the whole library to a chosen codec/bitrate (safe:
        temp file + atomic replace, tags preserved via -map_metadata)."""
        import glob as _g, subprocess as _sp
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        if not os.path.isdir(music):
            messagebox.showinfo("Re-encode", "Music folder not found.")
            return
        win = tk.Toplevel(self.master)
        win.title("Batch re-encode")
        win.configure(bg=BG2)
        tk.Label(win, text="Target codec:", bg=BG2, fg=FG, font=FONT
                 ).pack(anchor="w", padx=10, pady=4)
        codec = ttk.Combobox(win, values=["opus", "mp3", "m4a", "flac"],
                             state="readonly", width=20, font=FONT)
        codec.set("opus")
        codec.pack(padx=10)
        tk.Label(win, text="Bitrate (kbps, ignored for flac):", bg=BG2,
                 fg=FG, font=FONT).pack(anchor="w", padx=10, pady=(8, 4))
        br = tk.Spinbox(win, from_=64, to=320, width=8, bg=BG3, fg=FG,
                        font=FONT, relief="flat")
        br.delete(0, tk.END); br.insert(0, "256")
        br.pack(padx=10)
        st = tk.Label(win, text="", bg=BG2, fg=FG, font=FONT_SM)
        st.pack(pady=6)

        def run():
            c = codec.get()
            rate = br.get()
            files = []
            for ext in ("*.opus", "*.mp3", "*.m4a", "*.flac"):
                files += _g.glob(os.path.join(music, "**", ext),
                                 recursive=True)
            # skip files already in target codec
            if c == "opus":
                files = [f for f in files if not f.lower().endswith(".opus")]
            elif c == "mp3":
                files = [f for f in files if not f.lower().endswith(".mp3")]
            elif c == "m4a":
                files = [f for f in files if not f.lower().endswith(".m4a")]
            elif c == "flac":
                files = [f for f in files if not f.lower().endswith(".flac")]
            if not files:
                self._ui(lambda: st.config(
                    text="nothing to convert (already " + c + ")"))
                win.destroy()
                return
            import titanium_enrich as _te
            ff = _te._ffmpeg_path()
            if not ff:
                self._ui(lambda: st.config(text="ffmpeg not found"))
                return
            enc = {"opus": "libopus", "mp3": "libmp3lame",
                   "m4a": "aac", "flac": "flac"}[c]
            done = err = 0
            total = len(files)
            CREATE = 0x08000000 if os.name == "nt" else 0
            import mutagen as _mg
            ext_out = "." + c if c != "flac" else ".flac"
            for n, f in enumerate(files):
                base = os.path.splitext(f)[0]
                src_tags = {}
                try:
                    a = _mg.File(f)
                    tg = dict(getattr(a, "tags", None) or {})
                    for k in ("title", "artist", "album", "genre", "date",
                              "tracknumber"):
                        if tg.get(k):
                            src_tags[k] = tg.get(k)
                except Exception:
                    pass
                tmp = base + "._rc." + (c if c != "flac" else "flac")
                cmd = [ff, "-y", "-i", f, "-c:a", enc]
                if c != "flac":
                    cmd += ["-b:a", f"{rate}k"]
                cmd += [tmp]
                try:
                    _sp.run(cmd, creationflags=CREATE, check=True,
                            capture_output=True, timeout=1800)
                    # re-apply tags (ffmpeg won't map opus comments -> ID3)
                    def _s(v):
                        if isinstance(v, (list, tuple)):
                            return v[0] if v else None
                        return v or None
                    if src_tags:
                        try:
                            ti = _s(src_tags.get("title"))
                            ar = _s(src_tags.get("artist"))
                            al = _s(src_tags.get("album"))
                            gn = _s(src_tags.get("genre"))
                            b = _mg.File(tmp)
                            if getattr(b, "tags", None) is None and \
                                    os.path.splitext(tmp)[1].lower() == ".mp3":
                                try:
                                    b.add_tags()
                                except Exception:
                                    pass
                            if ext_out == ".mp3":
                                from mutagen.id3 import TIT2, TPE1, TALB, TCON
                                if ti: b.tags.setall("TIT2", [TIT2(encoding=3, text=ti)])
                                if ar: b.tags.setall("TPE1", [TPE1(encoding=3, text=ar)])
                                if al: b.tags.setall("TALB", [TALB(encoding=3, text=al)])
                                if gn: b.tags.setall("TCON", [TCON(encoding=3, text=gn)])
                            else:
                                if ti: b["title"] = ti
                                if ar: b["artist"] = ar
                                if al: b["album"] = al
                                if gn: b["genre"] = gn
                            b.save()
                        except Exception:
                            pass
                    os.replace(tmp, f)
                    done += 1
                except Exception:
                    err += 1
                if n % 20 == 0:
                    self._ui(lambda d=n, t=total: st.config(
                        text=f"{d}/{t} … ok={done} err={err}"))
            self._ui(lambda: st.config(
                text=f"DONE — {done} converted, {err} errors"))
            self._notify_done("SHUNGITE",
                              f"Re-encode {c}@{rate}k complete ({done})")
            win.after(1500, win.destroy)

        tk.Button(win, text="Start", command=lambda: threading.Thread(
            target=run, daemon=True).start(),
            bg=GREEN, fg="#000", font=FONT).pack(pady=8)

    def _clean_orphans(self):
        """Find and (after confirm) delete .lrc/.jpg/.png/.segments.json
        sidecars with no matching audio file. Safe: confirm dialog first."""
        import glob as _g
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        audio_exts = {".opus", ".mp3", ".m4a", ".flac", ".wav", ".ogg"}
        orphans = []
        for sfx in ("*.lrc", "*.jpg", "*.png", "*.segments.json"):
            for s in _g.glob(os.path.join(music, "**", sfx), recursive=True):
                base = os.path.splitext(s)[0]
                if sfx == "*.segments.json":
                    base = s[:-len(".segments.json")]
                has_audio = any(os.path.exists(base + e) for e in audio_exts)
                if not has_audio:
                    orphans.append(s)
        if not orphans:
            messagebox.showinfo("Clean orphans", "No orphan sidecars found. 👍")
            return
        ok = messagebox.askyesno(
            "Clean orphans",
            f"{len(orphans)} orphan sidecar file(s) found "
            f"(no matching audio).\n\nDelete them?")
        if not ok:
            return
        # safety: snapshot index first
        try:
            import time as _t, shutil as _sh
            idx = os.path.join(music, "_index.txt")
            if os.path.exists(idx):
                _sh.copy2(idx, os.path.join(
                    music, "_index.txt.bak-" + _t.strftime("%Y%m%d-%H%M%S")))
        except Exception:
            pass
        removed = 0
        for s in orphans:
            try:
                os.remove(s)
                removed += 1
            except OSError:
                pass
        self._ui(lambda: self._enhstat.config(
            text=f"🧹 removed {removed} orphan sidecars"))
        self._notify_done("SHUNGITE", f"Cleaned {removed} orphan sidecars")

    def _library_integrity(self):
        """Verify _index.txt against actual files. Reports dangling entries
        (index says exists, file gone) and untracked files (on disk, not in
        index). Offers to prune dangling entries."""
        import glob as _g
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        idx = os.path.join(music, "_index.txt")
        if not os.path.exists(idx):
            messagebox.showinfo("Integrity", "No _index.txt found.")
            return
        # entries in index
        index_files = set()
        for line in open(idx, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 3)
            if parts:
                index_files.add(parts[0])
        # actual files
        disk = set()
        for ext in (".opus", ".mp3", ".m4a", ".flac"):
            for p in _g.glob(os.path.join(music, "**", "*" + ext),
                             recursive=True):
                disk.add(os.path.basename(p))
        dangling = sorted(index_files - disk)
        untracked = sorted(disk - index_files)
        rpt = os.path.join(lib, "integrity_report.txt")
        with open(rpt, "w", encoding="utf-8") as f:
            f.write(f"SHUNGITE integrity\n"
                    f"index entries: {len(index_files)}\n"
                    f"files on disk: {len(disk)}\n"
                    f"dangling (in index, file gone): {len(dangling)}\n"
                    f"untracked (on disk, not in index): {len(untracked)}\n\n")
            for d in dangling[:500]:
                f.write("[dangling] " + d + "\n")
            for u in untracked[:500]:
                f.write("[untracked] " + u + "\n")
        msg = (f"index {len(index_files)} · disk {len(disk)}\n"
               f"dangling {len(dangling)} · untracked {len(untracked)}\n\n"
               f"report → integrity_report.txt")
        if dangling:
            ok = messagebox.askyesno("Integrity", msg +
                                     "\n\nPrune dangling entries from index?")
            if ok:
                keep = [ln for ln in open(idx, encoding="utf-8", errors="replace")
                        if ln.strip() and not ln.strip().startswith("#")
                        and ln.split("|", 1)[0] in disk]
                with open(idx, "w", encoding="utf-8") as f:
                    f.writelines(keep)
                self._ui(lambda: self._enhstat.config(
                    text=f"pruned {len(dangling)} dangling entries"))
        else:
            messagebox.showinfo("Integrity", msg)
        try:
            os.startfile(rpt)
        except Exception:
            pass

    def _bulk_rename(self):
        """Rename existing files using a template built from embedded tags
        (title/artist/album/tracknumber). Shows a preview dialog first."""
        import glob as _g, mutagen as _mg, re as _re
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        tmpl = self.cfg.get("folder_template", "") or             "%(artist)s - %(title)s"
        files = []
        for ext in (".opus", ".mp3", ".m4a", ".flac"):
            files += _g.glob(os.path.join(music, "**", "*" + ext),
                             recursive=True)

        def _render(t, a, al, tn, name):
            s = tmpl
            s = s.replace("%(title)s", t or name).replace("%(artist)s", a or "")
            s = s.replace("%(album)s", al or "").replace("%(track)s", str(tn or ""))
            s = s.replace("%(ext)s", "").replace("%(title)s", t or name)
            s = _re.sub(r'[<>:"/\\|?*]', "_", s).strip(" .")
            return s

        plan = []
        for p in files:
            try:
                m = _mg.File(p, easy=True)
                t = " ".join(m.get("title", []) or [])
                a = " ".join(m.get("artist", []) or [])
                al = " ".join(m.get("album", []) or [])
                tn = (" ".join(m.get("tracknumber", []) or [])
                      or " ".join(m.get("track", []) or []))
            except Exception:
                t = a = al = tn = ""
            newbase = _render(t, a, al, tn, os.path.splitext(os.path.basename(p))[0])
            ext = os.path.splitext(p)[1]
            newname = newbase + ext
            if newname != os.path.basename(p):
                plan.append((p, os.path.join(os.path.dirname(p), newname)))
        if not plan:
            messagebox.showinfo("Bulk rename", "No files need renaming "
                                "(names already match the template).")
            return
        show = "\n".join(f"{os.path.basename(a)[:30]}  →  {os.path.basename(b)[:40]}"
                          for a, b in plan[:20])
        if not messagebox.askyesno(
                "Bulk rename",
                f"{len(plan)} file(s) will be renamed.\n\n{show}\n"
                + ("\n… and more" if len(plan) > 20 else "") +
                "\n\nApply renames?"):
            return
        # safety: snapshot index first
        try:
            import time as _t, shutil as _sh
            idx = os.path.join(music, "_index.txt")
            if os.path.exists(idx):
                _sh.copy2(idx, os.path.join(
                    music, "_index.txt.bak-" + _t.strftime("%Y%m%d-%H%M%S")))
        except Exception:
            pass
        done = err = 0
        for srcp, dstp in plan:
            # also move sidecars if present
            try:
                os.rename(srcp, dstp)
                base0 = os.path.splitext(srcp)[0]
                base1 = os.path.splitext(dstp)[0]
                for sfx in (".jpg", ".lrc", ".segments.json"):
                    if os.path.exists(base0 + sfx):
                        os.rename(base0 + sfx, base1 + sfx)
                done += 1
            except OSError:
                err += 1
        self._ui(lambda: self._enhstat.config(
            text=f"✏️ renamed {done} files ({err} errors)"))
        self._notify_done("SHUNGITE", f"Bulk rename: {done} files")

    def _backup_library(self):
        """Back up library metadata (index + config + playlists, NOT audio)
        to a timestamped zip on the desktop."""
        import zipfile as _zf, time as _t
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        dest = os.path.join(os.path.expanduser("~"), "Desktop",
                            "shungite_backup_" + _t.strftime("%Y%m%d-%H%M%S") + ".zip")
        items = []
        idx = os.path.join(music, "_index.txt")
        if os.path.exists(idx):
            items.append((idx, "_index.txt"))
        playlists = os.path.join(lib, "Playlists")
        if os.path.isdir(playlists):
            import glob as _g
            for m3u in _g.glob(os.path.join(playlists, "*.m3u")):
                items.append((m3u, os.path.join("Playlists", os.path.basename(m3u))))
        # config
        cfgpath = None
        try:
            cfgpath = self.cfg.get("_cfg_path")
        except Exception:
            pass
        if cfgpath and os.path.exists(cfgpath):
            items.append((cfgpath, os.path.basename(cfgpath)))
        if not items:
            messagebox.showinfo("Backup", "Nothing to back up "
                                "(no index/playlists/config found).")
            return
        try:
            with _zf.ZipFile(dest, "w", _zf.ZIP_DEFLATED) as z:
                for srcp, arc in items:
                    z.write(srcp, arcname=arc)
            self._notify_done("SHUNGITE", "Library metadata backed up")
            os.startfile(dest)
        except Exception as e:
            messagebox.showerror("Backup", str(e))

    def _library_health(self):
        """Scan the library and report coverage gaps: missing album/genre
        tags, missing cover/lyrics sidecars, and orphan .lrc/.jpg files."""
        import glob as _g
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        files = []
        for ext in ("*.opus", "*.mp3", "*.m4a", "*.flac"):
            files += _g.glob(os.path.join(music, "**", ext),
                             recursive=True)
        if not files:
            messagebox.showinfo("Library health", "No audio files found.")
            return
        rpt = os.path.join(lib, "library_health.txt")
        import mutagen
        no_album = no_genre = no_cover = no_lrc = 0
        albums = {}
        orphans = 0
        with open(rpt, "w", encoding="utf-8") as out:
            out.write(f"SHUNGITE library health — {len(files)} files\n\n")
            for p in files:
                base = os.path.splitext(p)[0]
                try:
                    a = mutagen.File(p)
                    tags = dict(getattr(a, "tags", {}) or {})
                    album = tags.get("album") or tags.get("ALBUM")
                    if album:
                        albums.setdefault(str(album), []).append(p)
                    else:
                        no_album += 1
                    if not (tags.get("genre") or tags.get("GENRE")):
                        no_genre += 1
                except Exception:
                    no_album += 1
                if not (os.path.exists(base + ".jpg")
                        or os.path.exists(base + ".png")):
                    no_cover += 1
                if not os.path.exists(base + ".lrc"):
                    no_lrc += 1
            # orphan sidecars: .lrc/.jpg with no matching audio
            for sfx in ("*.lrc", "*.jpg"):
                for s in _g.glob(os.path.join(music, "**", sfx), recursive=True):
                    if not os.path.exists(os.path.splitext(s)[0] + ".opus") and \
                       not os.path.exists(os.path.splitext(s)[0] + ".mp3") and \
                       not os.path.exists(os.path.splitext(s)[0] + ".m4a") and \
                       not os.path.exists(os.path.splitext(s)[0] + ".flac"):
                        orphans += 1
            out.write(f"missing album tag: {no_album}\n")
            out.write(f"missing genre tag: {no_genre}\n")
            out.write(f"missing cover art: {no_cover}\n")
            out.write(f"missing lyrics:    {no_lrc}\n")
            out.write(f"orphan sidecars:   {orphans}\n\n")
            # partial albums (fewer than 3 tracks hints incomplete)
            out.write("Albums by track count:\n")
            for al in sorted(albums, key=lambda a: -len(albums[a])):
                out.write(f"  [{len(albums[al]):>3}] {al[:60]}\n")
        try:
            os.startfile(rpt)
        except Exception:
            pass
        self._ui(lambda: self._enhstat.config(
            text=f"Health report → library_health.txt "
                 f"(no album:{no_album} no genre:{no_genre} "
                 f"no cover:{no_cover} no lrc:{no_lrc} orphan:{orphans})"))

    def _open_dup_report(self):
        """Open acoustic_dupes.txt in the OS default editor."""
        try:
            lib = self._enhdir.get().strip()
            rpt = os.path.join(lib, "acoustic_dupes.txt")
            if not os.path.exists(rpt):
                messagebox.showinfo("No report",
                                    "Run an acoustic dup scan first.")
                return
            os.startfile(rpt)
        except Exception as e:
            messagebox.showerror("Open report", str(e))

    def _acoustic_dedup_scan(self):
        """Fingerprint every audio file; report groups that sound identical."""
        import glob as _g
        lib = self._enhdir.get().strip()
        files = []
        for ext in ("*.opus", "*.mp3", "*.m4a", "*.flac"):
            files += _g.glob(os.path.join(lib, "Music", "**", ext),
                             recursive=True)
        if not files:
            messagebox.showinfo("Nothing to scan",
                                f"No audio under {lib}\\Music")
            return

        ev = threading.Event()
        self._cancel["enhance"] = ev

        def status(m):
            self._ui(lambda s=m: (self._enhlog.insert(tk.END, f"  {s}\n"),
                                  self._enhlog.see(tk.END)))

        def run():
            try:
                import acoustic_dedup as _ad
            except ImportError:
                status("✗ acoustic_dedup module missing")
                return
            status(f"🎧 fingerprinting {len(files)} files…")
            groups = []
            try:
                def prog(n, total):
                    if n % 50 == 0:
                        status(f"  …{n}/{total}")
                groups = _ad.find_duplicates(files, progress=prog)
            except Exception as e:
                status(f"✗ scan failed: {e}")
                return
            rpt = os.path.join(lib, "acoustic_dupes.txt")
            csvp = os.path.join(lib, "acoustic_dupes.csv")
            import csv as _csv
            with open(rpt, "w", encoding="utf-8") as f:
                f.write(f"SHUNGITE acoustic duplicates — "
                        f"{len(groups)} groups\n\n")
                for g in groups:
                    for p in g:
                        f.write(p + "\n")
                    f.write("\n")
            with open(csvp, "w", newline="", encoding="utf-8-sig") as cf:
                w = _csv.writer(cf)
                w.writerow(["group", "file"])
                for gi, g in enumerate(groups, 1):
                    for p in g:
                        w.writerow([gi, p])
            status(f"✓ DONE — {len(groups)} duplicate groups → "
                   f"acoustic_dupes.txt")
            if groups:
                def ask_del(g=len(groups)):
                    if messagebox.askyesno(
                            "Duplicates found",
                            f"{g} duplicate-sounding groups found.\n\n"
                            f"Report saved to acoustic_dupes.txt\n\n"
                            f"Delete lower-quality copies now?\n"
                            f"(keeps the LARGEST file per group)"):
                        # safety: snapshot the index before any deletion
                        try:
                            import time as _t, shutil as _sh
                            _idx = os.path.join(lib, "_index.txt")
                            if os.path.exists(_idx):
                                _bak = os.path.join(lib, "_index.txt.bak-" +
                                                    _t.strftime("%Y%m%d-%H%M%S"))
                                _sh.copy2(_idx, _bak)
                        except Exception:
                            pass
                        import acoustic_dedup as _ad2
                        removed = 0
                        for grp in groups:
                            keep = max(grp, key=os.path.getsize)
                            for p in grp:
                                if p != keep:
                                    base2 = os.path.splitext(p)[0]
                                    for sfx in ("", ".jpg", ".lrc",
                                                ".segments.json",
                                                ".en.vtt"):
                                        try:
                                            os.remove(base2 + sfx)
                                        except OSError:
                                            pass
                                    removed += 1
                        status(f"🗑 deleted {removed} duplicate files "
                               f"(kept best per group)")
                self._ui(ask_del)

        threading.Thread(target=run, daemon=True).start()

    def _smart_playlist_dlg(self):
        """Build a rules-based playlist from embedded tags."""
        d = simpledialog.askstring(
            "Smart Playlist",
            "Rules, e.g.: genre=chill AND year=2010-2020\n"
            "fields: genre, artist, album, year\n"
            "(matches inside tags; case-insensitive)",
            parent=self.root)
        if not d or not d.strip():
            return
        match = {}
        name_parts = []
        try:
            for part in d.strip().split("AND"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    k = k.strip().lower()
                    v = v.strip()
                    if k == "year" and "-" in v:
                        a, b = v.split("-", 1)
                        match["year_from"] = int(a)
                        match["year_to"] = int(b)
                        name_parts.append(f"{v}")
                    else:
                        match[k] = v
                        name_parts.append(v[:20])
                elif ">" in part:
                    k, v = part.split(">", 1)
                    match["year_from"] = int(v.strip())
                    name_parts.append(">" + v.strip())
        except Exception as e:
            messagebox.showerror("Bad rules", str(e))
            return
        if not match:
            messagebox.showinfo("Nothing to do",
                                "No field=value rules found.")
            return
        name = "Smart: " + " ".join(name_parts) or "smart"

        ev = threading.Event()
        self._cancel["enhance"] = ev

        def status(m):
            self._ui(lambda s=m: (self._enhlog.insert(tk.END,
                     f"  {s}\n"), self._enhlog.see(tk.END)))

        def run():
            try:
                import smart_playlists as _spmod
                import mutagen
                m3u, n = _spmod.generate(
                    {"name": name, "match": match},
                    self._enhdir.get().strip(), mutagen)
                status(f"🧠 '{name}' → {n} tracks → "
                       f"{os.path.basename(m3u)}")
            except Exception as e:
                status(f"✗ smart playlist failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def _do_enhance(self):
        idx = os.path.join(self._enhdir.get().strip(), "Music", "_index.txt")
        if not os.path.exists(idx):
            messagebox.showinfo("No index",
                                f"No _index.txt found at:\n{idx}\n\n"
                                "It's created automatically when you download playlists.")
            return
        ev = threading.Event()
        self._cancel["enhance"] = ev
        embed = bool(self._enh_embed.get())
        self._enhpb.config(value=0)

        def status(msg):
            self._ui(lambda m=msg: self._enhlog.insert(tk.END, f"  {m}\n") or
                     self._enhlog.see(tk.END))

        def prog(done, total, fn, res):
            pct = 100.0 * done / max(total, 1)
            tag = "info"

            def do(p=pct, f2=fn[:44], t=tag, d=done, tt=total, res=res):
                try:
                    self._enhpb.config(value=p)
                    self._enhpblabel.config(text=f"{d} / {tt} tracks")
                    if res is None:
                        self._enhlog.insert(tk.END,
                            f"· [{d}/{tt}] {f2} (file not found)\n", "skip")
                    elif not res:
                        self._enhlog.insert(tk.END,
                            f"✓ [{d}/{tt}] {f2} already complete\n", "skip")
                    else:
                        got = ", ".join(k for k, v in res.items() if v) or "nothing"
                        self._enhlog.insert(tk.END,
                            f"✨ [{d}/{tt}] {f2}\n", "ok")
                        self._enhlog.insert(tk.END,
                            f"      ↳ {got}\n", "info")
                    self._enhlog.see(tk.END)
                except Exception:
                    pass
            self._ui(do)

        def run():
            status("starting enhancement run… (~1s per track, resumable)")
            done, enriched = ten.retrofit_index(idx, prog, ev,
                                                do_embed=embed,
                                                want_subs=bool(
                                                    self.cfg.get(
                                                        "fetch_subtitles",
                                                        False)),
                                                sb_segments=bool(
                                                    self.cfg.get(
                                                        "sponsorblock",
                                                    True)))
            status(f"{'⏹ cancelled' if ev.is_set() else '✓ DONE'} — "
                   f"{done} scanned, {enriched} enriched "
                   f"(run again anytime to resume)")
            try:
                self.cfg["last_enhance"] = {
                    "when": time.strftime("%Y-%m-%d %H:%M"),
                    "scanned": done,
                    "enriched": enriched,
                    "cancelled": ev.is_set()}
                hist = self.cfg.get("enhance_history", [])
                hist.append(dict(self.cfg["last_enhance"]))
                self.cfg["enhance_history"] = hist[-30:]
                self._save()
                if hasattr(self, "_enh_last"):
                    self._ui(lambda: self._enh_last.config(
                        text=f"last run: {self.cfg['last_enhance']['when']}"
                             f" — {self.cfg['last_enhance']['enriched']} "
                             f"of {self.cfg['last_enhance']['scanned']} "
                             f"enriched"))
            except Exception:
                pass
            # beets-style summary report written beside _index.txt
            try:
                rpt = os.path.join(os.path.dirname(idx),
                                   "enhance_report.txt")
                with open(rpt, "w", encoding="utf-8") as f:
                    f.write(f"SHUNGITE enhancement report\n")
                    f.write(f"Generated: {time.strftime('%Y-%m-%d %H:%M')}\n")
                    f.write(f"Scanned:  {done}\nEnriched: {enriched}\n\n")
                    for ln in self._enhlog.get("1.0", "end").splitlines():
                        if "↳" in ln or "not found" in ln:
                            f.write(ln.strip() + "\n")
                status(f"report saved → enhance_report.txt")
            except Exception:
                pass
            self._ui(lambda: self._enh_counts())
            try:
                self._ui(lambda: self._draw_enh_hist())
            except Exception:
                pass
            self._maybe_shutdown()
        threading.Thread(target=run, daemon=True).start()


    def _tab_stats(self, nb):
        o = tk.Frame(nb, bg=BG)
        nb.add(o, text="  📊 Stats  ")

        head = tk.Frame(o, bg=BG)
        head.pack(fill="x", padx=8, pady=(8, 0))
        self._btn(head, "\u21bb Refresh", self._stats_refresh,
                  bg=BG3, fg=FG).pack(side="left")
        self._btn(head, "💾 Export history", self._export_stats_csv,
                  bg=BG3, fg=FG).pack(side="left", padx=6)
        self._btn(head, "🧾 Export library", self._export_library_csv,
                  bg=BG3, fg=FG).pack(side="left", padx=6)
        self._btn(head, "📃 Save report", self._save_stats_report,
                  bg=BG3, fg=FG).pack(side="left", padx=6)
        tk.Label(head, text="all-time download statistics",
                 bg=BG, fg="#6b6b6b", font=FONT_SM).pack(side="left", padx=10)
        self._btn(head, "Retry failed jobs", self._dq_retry_failed,
                  bg=BG3, fg=FG).pack(side="right")
        self._btn(head, "Run next queued now", self._dq_run_next,
                  bg=BG3, fg=FG).pack(side="right")
        self._btn(head, "Clear finished queue jobs", self._dq_clear,
                  bg=BG3, fg=FG).pack(side="right")

        self._statcards = tk.Frame(o, bg=BG)
        self._statcards.pack(fill="x", padx=8, pady=6)

        body = tk.Frame(o, bg=BG)
        body.pack(fill="both", expand=True, padx=8)

        left = tk.Frame(body, bg=BG2, padx=12, pady=8)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="Last 14 days", bg=BG2, fg=GOLD,
                 font=FONT_B).pack(anchor="w")
        self._statsdays = tk.Text(left, height=14, bg=BG2, fg=FG,
                                  relief="flat", font=("Consolas", 10),
                                  state="disabled")
        self._statsdays.pack(fill="both", expand=True)

        right = tk.Frame(body, bg=BG2, padx=12, pady=8)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        tk.Label(right, text="Top artists", bg=BG2, fg=GOLD,
                 font=FONT_B).pack(anchor="w")
        self._statstop = tk.Text(right, height=14, bg=BG2, fg=FG,
                                 relief="flat", font=FONT_SM, state="disabled")
        self._statstop.pack(fill="both", expand=True)
        self.root.after(400, self._stats_refresh)

    def _dq_retry_failed(self):
        """Requeue every errored job in the worker pool."""
        try:
            n = 0
            with self.dq.lock:
                for k, v in self.dq.active.items():
                    if v["status"] == "error":
                        v["status"] = "queued"
                        # re-arm the original callable if stored
                        fn = getattr(v, "get", lambda *_: None)("fn")
                        if callable(fn):
                            import threading as _th
                            _th.Thread(target=self.dq.submit,
                                       args=(fn,),
                                       kwargs={"job_id": k,
                                               "label": v.get("label", "")},
                                       daemon=True).start()
                        n += 1
            messagebox.showinfo("Retried", f"{n} failed jobs requeued.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _dq_run_next(self):
        """Bump the oldest queued job to the head of the work queue.
        queue.Queue is FIFO: last put = first get. Drain, drop the target's
        old tuple, then re-put everything else and finally the target."""
        try:
            with self.dq.lock:
                oldest_id = None
                for k, v in self.dq.active.items():
                    if v["status"] == "queued":
                        oldest_id = k
                        break
            if not oldest_id:
                messagebox.showinfo("Nothing queued",
                                    "No queued job to bump.")
                return
            items = []
            while True:
                try:
                    items.append(self.dq.q.get_nowait())
                except Exception:
                    break
            target = None
            rest = []
            for it in items:
                if (target is None and isinstance(it, tuple) and it
                        and it[0] == oldest_id):
                    target = it
                else:
                    rest.append(it)
            if target is None:
                for it in items:      # restore untouched
                    self.dq.q.put(it)
                messagebox.showinfo(
                    "Not pending",
                    "That job is already running or gone.")
                return
            for it in rest:
                self.dq.q.put(it)
            self.dq.q.put(target)     # LAST put -> next worker pickup
            self._stats_refresh()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _dq_clear(self):
        """Remove finished/errored jobs from the live queue view."""
        try:
            with self.dq.lock:
                for k in [k for k, v in self.dq.active.items()
                          if v["status"] not in ("queued", "running")]:
                    self.dq.active.pop(k, None)
            self._stats_refresh()
        except Exception:
            pass

    def _save_stats_report(self):
        """Write one self-contained .txt report with cards + recent stats."""
        import history_db as _hdb, datetime as _dt, os as _o
        s = _hdb.stats()
        path = _o.path.join(_o.path.expanduser("~"), "Desktop",
                            "shungite_stats.txt")
        lines = ["SHUNGITE — stats report", "-" * 40, ""]
        lines.append(f"Total downloaded : {s['total']}")
        lines.append(f"Failed attempts  : {s['fails']}")
        lines.append(f"Data pulled      : {s['bytes']/1024/1024:.1f} MB")
        lines.append("")
        lines.append("Last 14 days:")
        for d, n in s.get("days", []):
            lines.append(f"  {d} : {n}")
        lines.append("")
        lines.append("Top artists:")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        try:
            os.startfile(path)
        except Exception:
            pass
        self._notify_done("SHUNGITE", f"Stats report → {os.path.basename(path)}")

    def _export_stats_csv(self):
        """Export download history to a CSV on the desktop."""
        import history_db as _hdb, csv as _csv, time as _t
        rows = _hdb.all_rows(100000)
        if not rows:
            messagebox.showinfo("Export", "No history to export.")
            return
        path = os.path.join(os.path.expanduser("~"), "Desktop",
                            "shungite_history_" + _t.strftime("%Y%m%d-%H%M%S") + ".csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(["ts", "title", "artist", "kind", "source",
                        "status", "bytes"])
            for r in rows:
                w.writerow([r["ts"], r["title"], r["artist"], r["kind"],
                            r["source"], r["status"], r["bytes"]])
        self._notify_done("SHUNGITE", f"History exported ({len(rows)} rows)")
        try:
            os.startfile(path)
        except Exception:
            pass

    def _export_library_csv(self):
        """Export the library index (file|track|artist) to CSV."""
        import csv as _csv, time as _t
        lib = self._enhdir.get().strip()
        music = os.path.join(lib, "Music")
        idx = os.path.join(music, "_index.txt")
        if not os.path.exists(idx):
            messagebox.showinfo("Export", "No _index.txt found.")
            return
        rows = []
        for line in open(idx, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 3)
            if len(parts) >= 3:
                rows.append((parts[0], parts[1], parts[2]))
        path = os.path.join(os.path.expanduser("~"), "Desktop",
                            "shungite_library_" + _t.strftime("%Y%m%d-%H%M%S") + ".csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(["file", "track", "artist"])
            w.writerows(rows)
        self._notify_done("SHUNGITE", f"Library exported ({len(rows)} tracks)")
        try:
            os.startfile(path)
        except Exception:
            pass

    def _stats_refresh(self):
        import history_db as _hdb
        s = _hdb.stats()

        def fmt_bytes(b):
            for unit in ("B", "KB", "MB", "GB"):
                if b < 1024:
                    return f"{b:.0f} {unit}"
                b /= 1024.0
            return f"{b:.1f} TB"

        # cards
        for w in self._statcards.winfo_children():
            w.destroy()
        try:
            import glob as _gl
            lib_n = sum(len(_gl.glob(os.path.join(
                self._music_dir(), "**", "*" + ex), recursive=True))
                for ex in (".opus", ".mp3", ".m4a", ".flac"))
        except Exception:
            lib_n = 0
        cards = [("Total downloaded", str(s["total"])),
                 ("Library tracks", str(lib_n)),
                 ("Failed attempts", str(s["fails"])),
                 ("Data pulled", fmt_bytes(s["bytes"]))]
        for i, (label, val) in enumerate(cards):
            c = tk.Frame(self._statcards, bg=BG3, padx=16, pady=10)
            c.grid(row=0, column=i, padx=(0 if i == 0 else 8, 0), sticky="ew")
            self._statcards.columnconfigure(i, weight=1)
            tk.Label(c, text=val, bg=BG3, fg="#1DB954",
                     font=("Segoe UI", 20, "bold")).pack()
            tk.Label(c, text=label, bg=BG3, fg=FG2, font=FONT_SM).pack()

        # 14-day chart (text bars)
        days = dict(s["days"])
        import datetime as _dt
        self._statsdays.config(state="normal")
        self._statsdays.delete("1.0", "end")
        maxn = max([v for _, v in s["days"]] or [1])
        today = _dt.date.today()
        for k in range(13, -1, -1):
            d = today - _dt.timedelta(days=k)
            ds = d.strftime("%Y-%m-%d")
            n = days.get(ds, 0)
            bar = "█" * int(40 * n / max(maxn, 1)) or "·"
            marker = " ◀" if k == 0 else ""
            self._statsdays.insert(tk.END,
                                   f"{ds}  {bar} {n}{marker}\n")
        # library growth (cumulative, last 30 days)
        try:
            import history_db as _hdb3
            okr = _hdb3.daily_ok(30)
            if okr:
                self._statsdays.config(state="normal")
                cum = 0
                self._statsdays.insert(tk.END,
                                       "\n--- library growth (30d) ---\n")
                for ds, n in okr:
                    cum += n
                    bars = min(int(round(cum / 50)) + 1, 30)
                    self._statsdays.insert(tk.END,
                                           f"{ds[5:]}  {'#' * bars} {cum}\n")
                self._statsdays.config(state="disabled")
        except Exception:
            pass

        # live queue status
        try:
            snap = self.dq.snapshot()

            lines = [f"  {v['status']:9s} {v['label'][:44]}"
                     for v in list(snap.values())[:12]]
            self._statsdays.insert(tk.END, "\n--- active queue ---\n")
            for ln in lines or ["  (idle)"]:
                self._statsdays.insert(tk.END, ln + "\n")
        except Exception:
            pass

        # 14-day mini bar chart (unicode blocks)
        try:
            import history_db as _hdb2
            days = _hdb2.daily_counts(14)
            if days:
                mx = max((ok + er) for _, ok, er in days) or 1
                self._statsdays.insert(tk.END, "\n--- last 14 days ---\n")

                for d, ok, er in days:
                    bars = int(round(ok / mx * 20))
                    self._statsdays.insert(
                        tk.END,
                        f"{d[5:]}  {'█' * bars}{'·' * (20 - bars)} {ok}"
                        + (f"  ({er} failed)" if er else "") + "\n")
        except Exception:
            pass

        # top artists
        self._statstop.config(state="normal")
        self._statstop.delete("1.0", "end")
        if s["top"]:
            for artist, n in s["top"]:
                self._statstop.insert(tk.END, f"  {artist[:40]:40s} {n}\n")
        else:
            self._statstop.insert(tk.END, "  nothing yet — go grab some music!")
        self._statstop.config(state="disabled")


    def _tab_lyricsync(self, nb):
        o = tk.Frame(nb, bg=BG)
        nb.add(o, text="  ▶ Lyric Syncer  ")

        banner = tk.Frame(o, bg="#101418", padx=14, pady=8)
        banner.pack(fill="x")
        tk.Label(banner, text="▶ LYRIC SYNCER",
                 font=("Segoe UI", 12, "bold"), bg="#101418", fg="#c0c8d8").pack(side="left")
        tk.Label(banner, text="AI-powered lyric timing (WhisperX) for .lrc files",
                 font=("Segoe UI", 9), bg="#101418", fg="#8e9aaf").pack(side="left", padx=10)

        card = self._frame(o)
        card.pack(fill="x", padx=8, pady=8)

        # ── Library root input (user asked for this) ────────────────────
        self._ls_root = self._folder_row(card, label="Library root:")
        self._ls_root.delete(0, "end")
        try:
            self._ls_root.insert(0, self._music_dir())
        except Exception:
            self._ls_root.insert(0, os.path.expanduser("~/Music"))

        # Spec probe
        probe_frame = tk.Frame(card, bg=BG2)
        probe_frame.pack(fill="x", pady=4)
        tk.Label(probe_frame, text="Hardware check:", bg=BG2, fg=FG2, font=FONT).pack(side="left", padx=(0,6))
        self._ls_probe_label = tk.Label(probe_frame, text="click 'Check my PC'", bg=BG2, fg=FG2, font=FONT_B)
        self._ls_probe_label.pack(side="left")
        tk.Button(card, text="Check my PC", command=self._ls_probe_specs,
                  bg=BG3, fg=FG, relief="flat", font=FONT_B, padx=12, pady=4).pack(anchor="w", pady=4)

        # Install button + state
        self._ls_install_btn = tk.Button(card, text="Install WhisperX (~2GB, needs 4GB free disk)",
                                         command=self._ls_install_whisperx,
                                         bg=BG3, fg=FG, relief="flat", font=FONT_B, padx=12, pady=6)
        self._ls_install_btn.pack(anchor="w", pady=4)
        self._ls_install_btn.config(state="disabled")

        # Model size picker
        row2 = tk.Frame(card, bg=BG2)
        row2.pack(fill="x", pady=4)
        tk.Label(row2, text="Model size:", bg=BG2, fg=FG2, font=FONT).pack(side="left", padx=(0,6))
        self._ls_model = tk.StringVar(value="small")
        for m in ("tiny","base","small","medium"):
            tk.Radiobutton(row2, text=m, value=m, variable=self._ls_model, bg=BG2, fg=FG,
                           selectcolor=BG2, font=FONT).pack(side="left", padx=4)

        # Sync button
        self._ls_sync_btn = tk.Button(card, text="Sync selected playlist",
                                      command=self._ls_sync_playlist,
                                      bg=BG3, fg=FG, relief="flat", font=FONT_B, padx=12, pady=6)
        self._ls_sync_btn.pack(anchor="w", pady=6)
        self._cancel_btn(card, "lyricsync").pack(anchor="w")

        # Status line
        self._ls_status = tk.Label(card, text="", bg=BG2, fg=FG2, font=FONT_B)
        self._ls_status.pack(anchor="w", pady=6)

    def _ls_probe_specs(self):
        import psutil
        vm = psutil.virtual_memory().total
        cpu_cores = psutil.cpu_count(logical=True)
        disk_free = psutil.disk_usage("D:\\").free
        lines = f"RAM: {vm/1e9:.1f} GB · CPU: {cpu_cores} cores · Free D: disk: {disk_free/1e9:.1f} GB"
        if vm >= 8*1e9 and disk_free >= 4*1e9:
            self._ls_probe_label.config(text=lines + " ✓ recommended: tiny/base/small")
        else:
            self._ls_probe_label.config(text=lines + " ⚠ low spec — use tiny model")
        self._ls_install_btn.config(state="normal")

    def _ls_install_whisperx(self):
        self._ls_install_btn.config(state="disabled", text="Installing… (this takes 2–5 mins)")
        import threading
        threading.Thread(target=self._ls_do_install, daemon=True).start()

    def _ls_do_install(self):
        import os
        py = r"C:\\Users\\Itsa\\whisperX\\.venv\\Scripts\\python.exe"
        if os.path.exists(py):
            self._ls_install_btn.config(text="whisperX found ✓ (your venv)", state="disabled")
            self._ls_status.config(text="whisperX ready — click Sync selected playlist")
        else:
            self._ls_install_btn.config(text="whisperX not found — run: cd C:\\Users\\Itsa\\whisperX & uv sync")
            self._ls_status.config(text=f"missing: {py}")

    def _ls_sync_playlist(self):
        model = self._ls_model.get()
        self._ls_status.config(text=f"Starting sync with model '{model}' (this may take a while)")
        import threading
        threading.Thread(target=self._ls_do_sync, args=(model,), daemon=True).start()

    def _ls_install_then_sync(self, model):
        pass  # unused — keep empty so callers from older code don't wake it

    def _ls_do_sync(self, model):
            """WhisperX per-file (its CLI takes NO directory), JSON output,
            converted to synced .lrc and embedded into the audio tags."""
            import os, subprocess, threading, json
            py = r"C:\Users\Itsa\whisperX\.venv\Scripts\python.exe"
            if not os.path.exists(py):
                self._ls_status.config(
                    text="whisperX venv missing — click 'Check my PC' / 'Install whisperX' first")
                return
            try:
                root = (self._ls_root.get().strip()
                        or self._music_dir())
            except Exception:
                try:
                    root = self._music_dir()
                except Exception:
                    root = os.path.expanduser("~/Music")

            ev = threading.Event()
            self._cancel["lyricsync"] = ev

            def _wx_json_to_lrc(jpath, lrc_path):
                """whisperX segment JSON -> [mm:ss.xx] line LRC."""
                try:
                    with open(jpath, encoding="utf-8") as fh:
                        d = json.load(fh)
                except Exception:
                    return False
                segs = d.get("segments") or []
                lines = []
                for s in segs:
                    txt = (s.get("text") or "").strip()
                    if not txt:
                        continue
                    t = float(s.get("start") or 0.0)
                    mm = int(t // 60)
                    ss = t - mm * 60
                    lines.append("[%02d:%05.2f] %s" % (mm, ss, txt))
                if not lines:
                    return False
                with open(lrc_path, "w", encoding="utf-8") as fh:
                    fh.write(chr(10).join(lines) + chr(10))
                return True

            def _bg():
                self._ls_status.config(text=f"scanning {root}…")
                exts = (".mp3", ".opus", ".flac", ".m4a", ".wav", ".ogg")
                files = []
                for bd, _, fs in os.walk(root):
                    for fn in fs:
                        if fn.lower().endswith(exts):
                            files.append(os.path.join(bd, fn))
                files = [f for f in files
                         if not os.path.exists(os.path.splitext(f)[0] + ".lrc")]
                if not files:
                    self._ls_status.config(text="✓ all already synced")
                    return
                total = len(files)
                done = 0
                for f in sorted(files):
                    if ev.is_set():
                        self._ls_status.config(text=f"⏹ cancelled ({done}/{total})")
                        return
                    base = os.path.splitext(f)[0]
                    jpath = base + ".json"
                    name = os.path.basename(f)[:40]
                    self._ls_status.config(
                        text=f"[{done+1}/{total}] {name}")
                    try:
                        try:
                            import torch as _torch
                            _gpu = _torch.cuda.is_available()
                        except Exception:
                            _gpu = False
                        p_ = subprocess.Popen(
                            [py, "-m", "whisperx", f,
                             "--model", model,
                             "--device", "cuda" if _gpu else "cpu",
                             "--compute_type", "float16" if _gpu else "int8",
                             "--output_format", "json",
                             "--output_dir", os.path.dirname(f) or "."],
                            creationflags=0x08000000,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
                        self._ls_proc = p_
                        for _line in p_.stdout:
                            if ev.is_set():
                                try:
                                    p_.kill()
                                except Exception:
                                    pass
                                self._ls_status.config(
                                    text=f"⏹ cancelled ({done}/{total})")
                                return
                        p_.wait()
                        if p_.returncode == 0 and os.path.exists(jpath):
                            if _wx_json_to_lrc(jpath, base + ".lrc"):
                                # embed into tags
                                try:
                                    import mutagen
                                    a = mutagen.File(f, easy=True)
                                    if a is not None:
                                        txt = open(base + ".lrc",
                                                   encoding="utf-8",
                                                   errors="replace").read()
                                        a["lyrics"] = txt
                                        a.save()
                                except Exception:
                                    pass
                                done += 1
                                self._ls_status.config(
                                    text=f"[{done}/{total}] ✓ {name}")
                            else:
                                self._ls_status.config(
                                    text=f"⚠ {name}: no speech segments")
                        else:
                            self._ls_status.config(
                                text=f"⚠ {name}: whisperX exit {p_.returncode}")
                    except FileNotFoundError:
                        self._ls_status.config(
                            text="whisperX venv missing — install first")
                        return
                    except Exception as e:
                        self._ls_status.config(
                            text=f"⚠ {name}: {str(e)[:60]}")
                    finally:
                        try:
                            if os.path.exists(jpath):
                                os.remove(jpath)
                        except Exception:
                            pass
                self._ls_status.config(
                    text=f"✓ synced {done}/{total} files")
                ev.clear()

            threading.Thread(target=_bg, daemon=True).start()
    def _tab_settings(self, nb):
        o = tk.Frame(nb, bg=BG)
        nb.add(o, text="  Settings  ")

        # scrollable container (settings page grew too tall for fullscreen)
        canv = tk.Canvas(o, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(o, orient="vertical", command=canv.yview)
        inner = tk.Frame(canv, bg=BG)
        inner.bind("<Configure>",
                   lambda e: canv.configure(scrollregion=canv.bbox("all")))
        canv.create_window((0, 0), window=inner, anchor="nw")
        canv.configure(yscrollcommand=vsb.set)
        canv.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._scroll_canvas = canv

        def _onwheel(e):
            canv.yview_scroll(-1 * int(e.delta / 120), "units")

        def _onwheel_linux(e):
            canv.yview_scroll(-1 if e.num == 4 else 1, "units")

        canv.bind_all("<MouseWheel>", _onwheel)
        canv.bind_all("<Button-4>", _onwheel_linux)
        canv.bind_all("<Button-5>", _onwheel_linux)

        card = self._frame(inner)
        card.pack(fill="x", padx=8, pady=8)

        # -- Auto-login status --
        ls = tk.Frame(card, bg=BG2)
        ls.pack(fill="x", pady=(0, 8))
        self._btn(ls, "Check logins", self._check_logins,
                  bg=BG3, fg=FG).pack(side="left")
        self._btn(ls, "Login to Spotify (recommended)", self._sp_oauth_login,
                  bg="#1DB954", fg="#000").pack(side="left", padx=(8, 0))
        self._btn(ls, "Logout Spotify", self._sp_oauth_logout,
                  bg=BG3, fg=FG).pack(side="left", padx=(8, 0))
        self._btn(ls, "Login to YouTube", self._yt_login,
                  bg="#FF0000", fg="#fff").pack(side="left", padx=(8, 0))
        self._btn(ls, "Logout YouTube", self._yt_logout,
                  bg=BG3, fg=FG).pack(side="left", padx=(4, 0))
        self._btn(ls, "Import cookies.txt", self._import_cookies,
                  bg=BG3, fg=FG).pack(side="left", padx=(8, 0))
        self._btn(ls, "Paste callback URL", self._sp_paste_callback,
                  bg=BG3, fg=FG).pack(side="left", padx=(8, 0))
        tk.Label(ls, text="  Login window:", bg=BG2, fg=FG2,
                 font=FONT).pack(side="left", padx=(12, 4))
        self._login_method = ttk.Combobox(ls, width=14, state="readonly",
                                          values=["embedded (in-app)",
                                                  "system browser"])
        self._login_method.set(
            "embedded (in-app)" if self.cfg.get("login_method") == "embedded"
            else "system browser")
        self._login_method.pack(side="left")
        self._login_method.bind("<<ComboboxSelected>>", lambda _e: (
            self.cfg.update({"login_method":
                             "system" if self._login_method.get().startswith("system")
                             else "embedded"}),
            save_config(self.cfg)))
        self._loginstat = tk.Label(ls, text="", bg=BG2, fg=GOLD, font=FONT)
        self._loginstat.pack(side="left", padx=12)

        # -- Spotify app credentials (one-time setup) --
        spf = tk.Frame(card, bg=BG2)
        spf.pack(fill="x", pady=(0, 8))
        tk.Label(spf, text="Spotify Client ID:", bg=BG2, fg=FG2,
                 font=FONT).pack(side="left", padx=8)
        self._spcid = tk.Entry(spf, bg=BG3, fg=FG, font=FONT_SM,
                               relief="flat", width=38)
        try:
            from spotify_oauth import CLIENT_ID_PATH
            cid = open(CLIENT_ID_PATH, encoding="utf-8").read().strip()
            self._spcid.insert(0, cid)
        except Exception:
            pass
        self._spcid.pack(side="left")
        self._btn(spf, "Save ID", self._save_sp_cid,
                  bg=BG3, fg=FG).pack(side="left", padx=6)
        self._btn(spf, "Manage IDs…", self._manage_ids,
                  bg=BG3, fg=FG).pack(side="left", padx=6)
        self._btn(spf, "Guide", self._show_id_guide,
                  bg=BG3, fg=FG).pack(side="left", padx=6)
        # -- Folder structure template --
        ftrow = tk.Frame(card, bg=BG2)
        ftrow.pack(fill="x", pady=4)
        tk.Label(ftrow, text="Folder structure (blank = flat):", bg=BG2,
                 fg=FG2, font=FONT_SM).pack(side="left", padx=8)
        self._ftentry = tk.Entry(ftrow, bg=BG3, fg=FG, font=FONT_SM,
                                 relief="flat", width=48)
        self._ftentry.insert(0, self.cfg.get("folder_template", ""))
        self._ftentry.pack(side="left")
        self._btn(ftrow, "Save", self._save_folder_template,
                  bg=BG3, fg=FG).pack(side="left", padx=6)
        tk.Label(card, text="Template vars: %(artist)s %(album)s "
                            "%(track)s %(title)s %(ext)s  e.g. "
                            "%(artist)s\\%(album)s\\%(track)s - %(title)s.%(ext)s",
                 bg=BG2, fg="#6b6b6b", font=FONT_SM).pack(anchor="w", padx=8)

        langrow = tk.Frame(card, bg=BG2)
        langrow.pack(fill="x", pady=4)
        tk.Label(langrow, text="Preferred lyric language (e.g. en, ko, "
                               "ja — blank = any):",
                 bg=BG2, fg=FG2, font=FONT_SM).pack(side="left", padx=8)
        self._langentry = tk.Entry(langrow, bg=BG3, fg=FG, font=FONT_SM,
                                   relief="flat", width=6)
        try:
            import titanium_enrich as _te3
            _lg = _te3.lyric_lang()
            if _lg:
                self._langentry.insert(0, _lg)
        except Exception:
            pass
        self._langentry.pack(side="left")
        self._btn(langrow, "Save", self._save_lyric_lang,
                  bg=BG3, fg=FG).pack(side="left", padx=6)

        subrow = tk.Frame(card, bg=BG2)
        subrow.pack(fill="x", pady=4)
        tk.Label(subrow, text="Subsonic/Navidrome server:",
                 bg=BG2, fg=FG2, font=FONT_SM).pack(side="left", padx=8)
        self._subsrv = tk.Entry(subrow, bg=BG3, fg=FG, font=FONT_SM,
                                relief="flat", width=30)
        self._subsrv.insert(0, self.cfg.get("subsonic_server", ""))
        self._subsrv.pack(side="left")
        tk.Label(subrow, text="user:", bg=BG2, fg=FG2,
                 font=FONT_SM).pack(side="left", padx=(10, 0))
        self._subuser = tk.Entry(subrow, bg=BG3, fg=FG, font=FONT_SM,
                                 relief="flat", width=14)
        self._subuser.insert(0, self.cfg.get("subsonic_user", ""))
        self._subuser.pack(side="left")
        tk.Label(subrow, text="pass:", bg=BG2, fg=FG2,
                 font=FONT_SM).pack(side="left", padx=(10, 0))
        self._subpass = tk.Entry(subrow, bg=BG3, fg="#c0c8d8",
                                 font=FONT_SM, relief="flat", width=14,
                                 show="•")
        self._subpass.insert(0, self.cfg.get("subsonic_pass", ""))
        self._subpass.pack(side="left")
        self._btn(subrow, "Test", self._subsonic_test,
                  bg=BG3, fg=FG).pack(side="left", padx=6)
        self._btn(subrow, "Browse albums", self._subsonic_browse,
                  bg=BG3, fg=FG).pack(side="left", padx=6)
        self._subpushvar = tk.BooleanVar(
            value=bool(self.cfg.get("subsonic_push", False)))
        tk.Checkbutton(subrow, text="auto-scan after downloads",
                       variable=self._subpushvar, bg=BG2, fg=FG2,
                       font=FONT_SM, selectcolor=BG3,
                       activebackground=BG2).pack(side="left")

        self._premvar = tk.BooleanVar(
            value=bool(self.cfg.get("yt_premium", False)))
        tk.Checkbutton(card,
                       text="YouTube Premium \u2014 unlock 256 kbps audio "
                            "(uses Firefox / embedded login cookies)",
                       variable=self._premvar, bg=BG2, fg=FG2, font=FONT_SM,
                       selectcolor=BG3, activebackground=BG2).pack(
            anchor="w", pady=2)
        self._enhvar = tk.BooleanVar(
            value=bool(self.cfg.get("enhance_audio", False)))
        tk.Checkbutton(card,
                       text="Enhance low-bitrate audio \u2014 loss-preserving "
                            "soxr upscale to 256 kbps (AI-grade DSP)",
                       variable=self._enhvar, bg=BG2, fg=FG2, font=FONT_SM,
                       selectcolor=BG3, activebackground=BG2).pack(
            anchor="w", pady=2)
        self._embonlyvar = tk.BooleanVar(
            value=bool(self.cfg.get("embed_only", True)))
        tk.Checkbutton(card,
                       text="Embed everything into the audio file "
                            "(no .jpg / .lrc sidecar files)",
                       variable=self._embonlyvar, bg=BG2, fg=FG2, font=FONT_SM,
                       selectcolor=BG3, activebackground=BG2).pack(
            anchor="w", pady=2)
        csrow = tk.Frame(card, bg=BG2); csrow.pack(fill="x", pady=2)
        tk.Label(csrow, text="Cover sources:", bg=BG2, fg=FG2,
                 font=FONT_SM).pack(side="left", padx=8)
        self._cs_itunes = tk.BooleanVar(
            value=bool(self.cfg.get("cover_itunes", True)))
        tk.Checkbutton(csrow, text="iTunes", variable=self._cs_itunes,
                       bg=BG2, fg=FG2, font=FONT_SM, selectcolor=BG3,
                       activebackground=BG2).pack(side="left")
        self._cs_deezer = tk.BooleanVar(
            value=bool(self.cfg.get("cover_deezer", True)))
        tk.Checkbutton(csrow, text="Deezer", variable=self._cs_deezer,
                       bg=BG2, fg=FG2, font=FONT_SM, selectcolor=BG3,
                       activebackground=BG2).pack(side="left")
        self._cs_lastfm = tk.BooleanVar(
            value=bool(self.cfg.get("cover_lastfm", True)))
        tk.Checkbutton(csrow, text="Last.fm", variable=self._cs_lastfm,
                       bg=BG2, fg=FG2, font=FONT_SM, selectcolor=BG3,
                       activebackground=BG2).pack(side="left")
        self._rgvar = tk.BooleanVar(
            value=bool(self.cfg.get("replaygain", False)))
        tk.Checkbutton(card,
                       text="Tag ReplayGain (track gain/peak) after download",
                       variable=self._rgvar, bg=BG2, fg=FG2, font=FONT_SM,
                       selectcolor=BG3, activebackground=BG2).pack(
            anchor="w", pady=2)
        self._normvar = tk.BooleanVar(
            value=bool(self.cfg.get("normalize_loudness", False)))
        tk.Checkbutton(card,
                       text="Normalize loudness to -16 LUFS after download "
                            "(EBU R128, recommended)",
                       variable=self._normvar, bg=BG2, fg=FG2, font=FONT_SM,
                       selectcolor=BG3, activebackground=BG2).pack(
            anchor="w", pady=4)

        lfmrow = tk.Frame(card, bg=BG2)
        lfmrow.pack(fill="x", pady=(0, 8))
        tk.Label(lfmrow, text="Last.fm API key (optional, better covers):",
                 bg=BG2, fg=FG2, font=FONT).pack(side="left", padx=8)
        self._lfmkey = tk.Entry(lfmrow, bg=BG3, fg=FG, font=FONT_SM,
                                relief="flat", width=32)
        try:
            import titanium_enrich as _te2
            k = _te2.lastfm_key()
            if k:
                self._lfmkey.insert(0, k)
        except Exception:
            pass
        self._lfmkey.pack(side="left")
        self._btn(lfmrow, "Save", self._save_lfm_key,
                  bg=BG3, fg=FG).pack(side="left", padx=6)
        tk.Label(card, text="One-time: developer.spotify.com → Dashboard → New app → "
                            "Redirect URI: http://127.0.0.1:47821/callback → copy Client ID here",
                 bg=BG2, fg="#6b6b6b", font=FONT_SM).pack(anchor="w", padx=8, pady=(0, 4))

        # -- Library folder --
        tk.Label(card, text="Library folder (Music/ + Playlists/ go here):",
                 bg=BG2, fg=FG2, font=FONT_B).pack(anchor="w")
        self._setdir = self._folder_row(card)
        tk.Label(card, text=f"Default: {LIBRARY_ROOT}",
                 bg=BG2, fg="#666", font=FONT_SM).pack(anchor="w", padx=8)
        tk.Label(card, text="⚠ Music/ and Playlists/ must stay side-by-side for .m3u files to work",
                 bg=BG2, fg="#e9c46a", font=FONT_SM).pack(anchor="w", padx=8, pady=(0, 8))

        # -- Browser for cookie extraction --
        bf = tk.Frame(card, bg=BG2)
        bf.pack(fill="x", pady=8)
        tk.Label(bf, text="Browser for cookies:", bg=BG2, fg=FG2,
                 font=FONT_B).pack(side="left", padx=8)
        self._browser = ttk.Combobox(bf, values=BROWSERS + ["auto (all browsers)", "none (no cookies)"],
                                      state="readonly", width=20, font=FONT)
        self._browser.set(self.cfg.get("browser", "auto (all browsers)"))
        self._browser.pack(side="left", padx=8)
        tk.Label(bf, text="(login to YouTube Music + Spotify in this browser)",
                 bg=BG2, fg="#6b6b6b", font=FONT_SM).pack(side="left", padx=6)

        # -- Custom cookies (optional manual override) --
        cf = tk.Frame(card, bg=BG2)
        cf.pack(fill="x", pady=8)
        tk.Label(cf, text="Custom cookies \u2014 point at your cookies.txt exports (optional):",
                 bg=BG2, fg=FG2, font=FONT_B).pack(anchor="w")
        row_dc = tk.Frame(cf, bg=BG2); row_dc.pack(fill="x", pady=2)
        tk.Label(row_dc, text="Spotify cookies.txt:", bg=BG2, fg=FG2,
                 font=FONT).pack(side="left", padx=8, anchor="n")
        self._spdc_row = self._folder_row(row_dc, label="")
        self._spdc_path = self.cfg.get("sp_cookies_file", "")
        if self._spdc_path:
            self._spdc_row.delete(0, tk.END)
            self._spdc_row.insert(0, self._spdc_path)
        row_dy = tk.Frame(cf, bg=BG2); row_dy.pack(fill="x", pady=2)
        tk.Label(row_dy, text="YouTube cookies.txt:", bg=BG2, fg=FG2,
                 font=FONT).pack(side="left", padx=8, anchor="n")
        self._ytck_row = self._folder_row(row_dy, label="")
        self._ytck_path = self.cfg.get("yt_cookies_file", "")
        if self._ytck_path:
            self._ytck_row.delete(0, tk.END)
            self._ytck_row.insert(0, self._ytck_path)
        tk.Label(cf, text="Netscape-format files (like browser 'Get cookies.txt' exports) \u2014 "
                          "used for downloads; leave empty to use browser auto-login",
                 bg=BG2, fg="#6b6b6b", font=FONT_SM).pack(anchor="w", padx=8)

        # -- Parallel / speed --
        wf = tk.Frame(card, bg=BG2)
        wf.pack(fill="x", pady=4)
        tk.Label(wf, text="Parallel downloads:", bg=BG2, fg=FG2,
                 font=FONT).pack(side="left", padx=8)
        self._workers = tk.Spinbox(wf, from_=1, to=10, width=4, bg=BG3,
                                   fg=FG, font=FONT, relief="flat")
        self._workers.delete(0, tk.END)
        self._workers.insert(0, str(self.cfg.get("max_workers", 4)))
        self._workers.pack(side="left")

        sf = tk.Frame(card, bg=BG2)
        sf.pack(fill="x", pady=4)
        tk.Label(sf, text="Speed limit (KB/s, 0=∞):", bg=BG2, fg=FG2,
                 font=FONT).pack(side="left", padx=8)
        self._speed = tk.Entry(sf, bg=BG3, fg=FG, font=FONT, relief="flat",
                               width=10)
        self._speed.insert(0, str(self.cfg.get("speed_limit_kbps", 0)))
        self._speed.pack(side="left", padx=8)

        schrow = tk.Frame(card, bg=BG2)
        schrow.pack(fill="x", pady=4)
        tk.Label(schrow, text="Auto-enhance nightly at (HH:MM, blank=off):",
                 bg=BG2, fg=FG2, font=FONT_SM).pack(side="left", padx=8)
        self._schentry = tk.Entry(schrow, bg=BG3, fg=FG, font=FONT_SM,
                                  relief="flat", width=8)
        self._schentry.insert(0, self.cfg.get("enhance_schedule", ""))
        self._schentry.pack(side="left")

        self._sbvar = tk.BooleanVar(
            value=bool(self.cfg.get("sponsorblock", True)))
        sbrow = tk.Frame(card, bg=BG2)
        sbrow.pack(fill="x", pady=4)
        tk.Checkbutton(sbrow, text="Save SponsorBlock skip segments (.segments.json)",
                       variable=self._sbvar, bg=BG2, fg=FG2, font=FONT_SM,
                       selectcolor=BG3, activebackground=BG2).pack(anchor="w", padx=8)

        self._subsvar = tk.BooleanVar(
            value=bool(self.cfg.get("fetch_subtitles", False)))
        subrow = tk.Frame(card, bg=BG2)
        subrow.pack(fill="x", pady=4)
        tk.Checkbutton(subrow, text="Download English subtitles (.vtt) when available",
                       variable=self._subsvar, bg=BG2, fg=FG2, font=FONT_SM,
                       selectcolor=BG3, activebackground=BG2).pack(anchor="w", padx=8)

        self._sdvar = tk.BooleanVar(value=bool(self.cfg.get("shutdown_when_done", False)))
        sdrow = tk.Frame(card, bg=BG2)
        sdrow.pack(fill="x", pady=4)
        tk.Checkbutton(sdrow, text="Shut down PC when downloads finish "
                       "(60s window to cancel)", variable=self._sdvar,
                       bg=BG2, fg=FG2, font=FONT_SM, selectcolor=BG3,
                       activebackground=BG2).pack(anchor="w", padx=8)

        if getattr(self, "_remote_url", ""):
            _mode = "LAN (paired)" if self.cfg.get("web_remote_lan") \
                else "this PC only (127.0.0.1)"
            tk.Label(card,
                     text=f"📱 Phone remote: {self._remote_url}  ({_mode})",
                     bg=BG2, fg="#22c55e", font=FONT_SM).pack(
                anchor="w", pady=(8, 0))
            if getattr(self, "_remote_pin", ""):
                tk.Label(card,
                         text=f"   Pairing PIN: {self._remote_pin} "
                              "(valid 5 min, one-time use)",
                         bg=BG2, fg="#fbbf24", font=FONT_B).pack(
                    anchor="w", pady=(2, 0))
            tk.Label(card,
                     text="   Remote is PIN-protected; unsafe LAN "
                          "exposure is off by default.",
                     bg=BG2, fg="#6b6b6b", font=FONT_SM).pack(
                anchor="w", pady=(2, 0))
        rr = tk.Frame(card, bg=BG2)
        rr.pack(fill="x", pady=(12, 4))
        self._btn(rr, "↺ Reset settings to defaults", self._reset_settings,
                  bg="#5c2b2b", fg="#ffffff").pack(side="left", padx=8)
        tk.Label(rr, text="(keeps library folder + browser choice)",
                 bg=BG2, fg="#6b6b6b", font=FONT_SM).pack(side="left", padx=6)

        # ===== Seal-parity: Format & Download =====
        card2 = tk.Frame(inner, bg=BG2, padx=14, pady=10)
        card2.pack(fill="x", padx=8, pady=8)
        tk.Label(card2, text="Format & Download (advanced)",
                 bg=BG2, fg=FG, font=FONT_B).pack(anchor="w")

        def _entry_row(label, var, width=44, hint=""):
            r = tk.Frame(card2, bg=BG2)
            r.pack(fill="x", pady=2)
            tk.Label(r, text=label, bg=BG2, fg=FG2).pack(side="left")
            ent = tk.Entry(r, bg="#1a1a1c", fg=FG, insertbackground=FG,
                           relief="flat", width=width)
            ent.pack(side="left", padx=(6, 0))
            ent.delete(0, "end")
            ent.insert(0, str(self.cfg.get(var, "") or ""))
            ent._cfgkey = var
            self._cfg_entries.append(ent)
            if hint:
                tk.Label(card2, text=hint, bg=BG2, fg="#666", font=FONT_SM).pack(anchor="w")
            return ent

        if not hasattr(self, "_cfg_entries"):
            self._cfg_entries = []

        _entry_row("Custom format (yt-dlp -f)", "custom_format",
                   hint='e.g. bestaudio[ext=m4a]/bestaudio, best[height<=720] — leave blank to use preset')
        _entry_row("Video height cap (px, e.g. 1080)", "max_video_height",
                   hint="0 = no cap. Applies to VIDEO downloads only.")
        _entry_row("Bitrate max (kbps, 32/64/128/192/256)", "audio_bitrate_max",
                   hint="Used by the 256k-opus branch")
        _entry_row("Max concurrent downloads", "max_workers", width=6,
                   hint="How many downloads run at once (default 3)")
        _entry_row("Proxy (http/https/socks5://host:port)", "proxy",
                   hint="leave blank for direct connection")
        _entry_row("Subtitle language (blank = auto en/en-US/en-GB)", "subtitle_lang")
        _entry_row("Playlist items (e.g. 1-10,15)", "playlist_items",
                   hint="download only these playlist items; blank = all")
        # Source selector: YouTube Music vs YouTube vs both
        _sm_vals = ["both", "ytmusic", "youtube"]
        _sm_cur = (self.cfg.get("source_mode", "") or "both").strip().lower()
        if _sm_cur not in _sm_vals:
            _sm_cur = "both"
        r3 = tk.Frame(card2, bg=BG2)
        r3.pack(fill="x", pady=2)
        tk.Label(r3, text="Search / download source", bg=BG2, fg=FG2).pack(side="left")
        _smvar = tk.StringVar(value=_sm_cur)
        self._source_var = _smvar
        _smcb = ttk.Combobox(r3, textvariable=_smvar, values=["both — YouTube + YouTube Music", "ytmusic — YouTube Music only", "youtube — YouTube only"], state="readonly", width=30)
        _smcb.pack(side="left", padx=(6, 0))
        _smcb.current(_sm_vals.index(_sm_cur))
        def _on_source(_e=None):
            _label = _smvar.get()
            _key = "ytmusic" if "Music only" in _label else ("youtube" if "YouTube only" in _label else "both")
            self.cfg["source_mode"] = _key
        _smcb.bind("<<ComboboxSelected>>", _on_source)
        tk.Label(card2, text="choose where searches resolve + downloads fetch from",
                 bg=BG2, fg="#666", font=FONT_SM).pack(anchor="w")
        ua = tk.BooleanVar(value=bool(self.cfg.get("use_aria2c", False)))
        self._ariavar = ua
        tk.Checkbutton(card2, text="Use aria2c for multi-connection download (if installed)",
                       variable=ua, bg=BG2, fg=FG2, selectcolor=BG2,
                       activebackground=BG2).pack(anchor="w")

        rf = tk.BooleanVar(value=bool(self.cfg.get("restrict_filenames", False)))
        self._rfnar = rf
        tk.Checkbutton(card2, text="Restrict filenames (no unicode in file names)",
                       variable=rf, bg=BG2, fg=FG2, selectcolor=BG2,
                       activebackground=BG2).pack(anchor="w", pady=(6, 0))
        st = tk.BooleanVar(value=bool(self.cfg.get("download_subtitles", False)))
        self._subvar = st
        tk.Checkbutton(card2, text="Download subtitles (.vtt) when available",
                       variable=st, bg=BG2, fg=FG2, selectcolor=BG2,
                       activebackground=BG2).pack(anchor="w")

        brow = tk.Frame(card, bg=BG2)
        brow.pack(fill="x", pady=(8, 0))
        self._btn(brow, "Save Settings", self._save).pack(side="left")
        self._btn(brow, "\U0001F4D6 What's new", self._show_changelog,
                  bg=BG3, fg=FG).pack(side="left", padx=6)
        self._watermark_label(card).pack(anchor="e")

        info = tk.Frame(inner, bg=BG2, padx=14, pady=10)
        info.pack(fill="x", padx=8, pady=4)
        tk.Label(info, text=f"SHUNGITE — {WATERMARK}", bg=BG2, fg=GOLD,
                 font=FONT_B).pack(anchor="w")
        tk.Label(info, text="ffmpeg bundled · deno auto-detected · cookies auto-pulled from your browser",
                 bg=BG2, fg=FG2, font=FONT_SM).pack(anchor="w")

    _watcher_state = {"thread": None, "seen": set()}

    def _ba_auto_start(self, _e=None):
        """Started quickly when a folder is picked; skips if off in settings."""
        if not self.cfg.get("batch_auto_start", True):
            return
        import threading, time
        def check():
            time.sleep(2)                    # tiny debounce
            try:
                pth = self._bdir.get().strip()
                if os.path.isdir(pth):
                    self._ui(lambda: self._do_batch())
            except Exception:
                pass
        threading.Thread(target=check, daemon=True).start()

    def _toggle_watch(self):
        on = bool(self._watchvar.get())
        self.cfg["watch_folder"] = on
        try:
            import json
            json.dump(self.cfg, open(r"C:\Users\Itsa\.peak_config.json", "w",
                                     encoding="utf-8"), indent=2)
        except Exception:
            pass
        if on:
            self._start_watcher()
            self._bstat.config(text="👁 watching CSV folder…", fg=GOLD)
        else:
            self._watcher_state["thread"] = None
            self._bstat.config(text="watching off", fg=FG2)

    def _start_watcher(self):
        if self._watcher_state["thread"] and self._watcher_state["thread"].is_alive():
            return

        def run():
            self._watcher_state["thread"] = threading.current_thread()
            import glob as _g
            # seed 'seen' with current csvs so existing files aren't re-ingested
            d = self._bdir.get().strip()
            for f in _g.glob(os.path.join(d, "*.csv")):
                self._watcher_state["seen"].add(os.path.abspath(f))
            while self._watcher_state["thread"] is threading.current_thread():
                time.sleep(30)
                try:
                    d = self._bdir.get().strip()
                    if not os.path.isdir(d):
                        continue
                    for f in _g.glob(os.path.join(d, "*.csv")):
                        ap = os.path.abspath(f)
                        if ap not in self._watcher_state["seen"]:
                            self._watcher_state["seen"].add(ap)
                            self._ui(lambda p=ap: self._bstat.config(
                                text=f"👁 new CSV: {os.path.basename(p)}",
                                fg=GOLD))
                            try:
                                self._run_batch_file(f)
                            except Exception:
                                pass
                except Exception:
                    continue

        threading.Thread(target=run, daemon=True).start()

    def _run_batch_file(self, path):
        """Ingest one Exportify CSV as a playlist (best-effort)."""
        import csv as _csv
        tracks = []
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as fh:
                rd = _csv.DictReader(fh)
                for row in rd:
                    name = (row.get("Track Name") or "").strip()
                    artist = (row.get("Artist Name(s)") or "").strip()
                    if name:
                        ms = int(row.get("Duration (ms)") or 0)
                        dur = f"{ms//60000}:{(ms//1000)%60:02d}" if ms else ""
                        tracks.append((name, artist, dur))
        except Exception:
            return
        if not tracks:
            return
        pl_name = os.path.splitext(os.path.basename(path))[0]
        index = self._load_lib_index()
        music = self._music_dir()
        file_norms = self._build_file_norms(music)
        missing = [(t, a) for t, a, _d in tracks
                   if not self._have_track(t, a, index, file_norms)]
        qual = self.qkey(self._bq.get())
        browser = self.browser()

        def status(msg):
            self._ui(lambda m=msg: self._blog.insert(tk.END, f"  {m}\n"))

        ok = 0
        for i, (track, artist) in enumerate(missing[:200]):
            q = f"{track} {artist}"
            try:
                results = search_youtube(q, 2, browser)
                for res in results or []:
                    s, _err = download_one(
                        res["title"], res["url"], music,
                        getattr(self, "_bfmt", None).get() if hasattr(self, "_bfmt") else "audio",
                        qual, 0, browser=browser,
                        cookiefile=self._yt_cookiefile(),
                        subs=self.cfg.get("fetch_subtitles", False))
                    if s == "ok":
                        actual_fn = re.sub(r'[<>:"/\\|?*]', "_",
                                          res["title"]).strip(" .")[:200] + ".opus"
                        with self._lib_lock:
                            index[actual_fn] = {"track": track, "artist": artist,
                                                "uri": _vid_from_url(res.get("url", ""))}
                        ok += 1
                        break
                else:
                    continue
                time.sleep(0.3)
            except Exception:
                continue
        self._save_lib_index(index)
        found, _, m3u_path = self._write_m3u(pl_name, tracks, index)
        status(f"👁 {pl_name[:30]}: {len(tracks)} rows · {ok} new · "
               f"{found} matched → {os.path.basename(m3u_path)}")


    _sched_state = {"last_run_date": None}

    _APP_VERSION = "7.68"
    UPDATE_URL = "https://kashifcloud.com/shungite/latest.txt"

    def _check_update_async(self):
        """Non-blocking version check against kashifcloud.com. Silent on any
        failure - only speaks up when a NEWER version exists."""
        import threading as _th
        import urllib.request as _ur

        def run():
            try:
                with _ur.urlopen(self.UPDATE_URL, timeout=6) as r:
                    latest = r.read().decode().strip()
                if latest and latest > self._APP_VERSION:
                    def ui():
                        try:
                            self._loginstat.config(
                                text=f"⬆ v{latest} available "
                                     f"(you have v{self._APP_VERSION}) — "
                                     f"ask Irteza for the new zip",
                                fg=GOLD)
                        except Exception:
                            pass
                    self._ui(ui)
            except Exception:
                pass

        _th.Thread(target=run, daemon=True).start()

    def _start_scheduler(self):
        """Nightly auto-enhance: runs _do_enhance silently at configured time."""
        def run():
            import datetime as _dt
            while True:
                try:
                    sched = (self.cfg.get("enhance_schedule") or "").strip()
                    if not sched or ":" not in sched:
                        time.sleep(60)
                        continue
                    hh, mm = sched.split(":")[:2]
                    now = _dt.datetime.now()
                    target = now.replace(hour=int(hh) % 24,
                                         minute=int(mm) % 60,
                                         second=0, microsecond=0)
                    if now >= target and _sched_last.get("date") != now.date():
                        if _sched_last.get("date") is None or \
                                _sched_last["date"] < now.date():
                            _sched_last["date"] = now.date()
                            try:
                                idx = os.path.join(self._music_dir(),
                                                   "_index.txt")
                                if os.path.exists(idx):
                                    ten.retrofit_index(
                                        idx, None, None,
                                        do_embed=self.cfg.get("embed_art",
                                                              True))
                            except Exception:
                                pass
                except Exception:
                    pass
                time.sleep(30)

        import threading as _th
        _th.Thread(target=run, daemon=True,
                   name="zincum-scheduler").start()



    def _maybe_shutdown(self):
        """If user enabled shutdown-on-finish, schedule it with a 60s abort."""
        if not self.cfg.get("shutdown_when_done"):
            return

        def run():
            import subprocess
            _f = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(["shutdown", "/s", "/t", "60"],
                           capture_output=True, creationflags=_f)
            self._ui(lambda: messagebox.showwarning(
                "Shutting down",
                "PC will shut down in 60 seconds.\n"
                "Run  shutdown /a  to abort."))

        threading.Thread(target=run, daemon=True).start()

    def _notify_done(self, title="SHUNGITE", msg="Downloads finished"):
        """Balloon toast; also flashes the taskbar if minimized."""
        try:
            self._ui(lambda: self._notify_done_now(title, msg))
        except Exception:
            pass

    def _notify_done_now(self, title, msg):
        try:
            root = self.master
            if root.state() == "iconic":
                root.deiconify()
            # use win notification if available, else a transient toplevel toast
            try:
                import ctypes
                ctypes.windll.user32.FlashWindowEx(
                    self._flash_hwnd, 0, 4, 0) if hasattr(self, "_flash_hwnd") else None
            except Exception:
                pass
            top = tk.Toplevel(self.master)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            l = tk.Label(top, text=f"{title} — {msg}",
                         bg=BG3, fg=FG, font=FONT_B, padx=20, pady=12)
            l.pack()
            sw = top.winfo_screenwidth(); sh = top.winfo_screenheight()
            top.update_idletasks()
            w = top.winfo_width(); h = top.winfo_height()
            top.geometry(f"+{sw-w-20}+{sh-h-60}")
            def close():
                try: top.destroy()
                except Exception: pass
            top.after(5000, close)
            l.bind("<Button-1>", lambda e: close())
        except Exception:
            pass

    def _record_failure(self, kind, title, q, url=None, playlist=None):
        """Track a failed download for the RETRY button."""
        try:
            self._last_failed.append({"kind": kind, "title": title,
                                      "q": q, "url": url or "",
                                      "playlist": playlist or ""})
        except Exception:
            pass

    def _sp_retry_failed(self):
        """Re-attempt everything that failed in the last Spotify run."""
        if not self._last_failed:
            messagebox.showinfo("Nothing to retry",
                                "No failures recorded from the last run.")
            return
        items = list(self._last_failed)
        self._last_failed = []
        ev = threading.Event()
        self._cancel["sp"] = ev
        music = self._music_dir()
        qual = self.qkey(self._spq.get())
        browser = self.browser()

        def status(msg):
            def do():
                self._spstat.config(text=f"♻ {msg}")
                try:
                    self._splog.insert(tk.END, f"  {msg}\n")
                    self._splog.see(tk.END)
                except Exception:
                    pass
            self._ui(do)

        def run():
            index = self._load_lib_index()
            ok = 0
            for i, it in enumerate(items):
                if ev.is_set():
                    break
                status(f"♻ [{i+1}/{len(items)}] {it['title'][:35]}")
                try:
                    results = search_youtube(it["q"], 2, browser)
                    got = False
                    for res in results or []:
                        s, _err = download_one(
                            res["title"], res["url"], music,
                            self._spfmt.get(), qual, 0, browser=browser,
                            cancel_event=ev,
                            cookiefile=self._yt_cookiefile(),
                            subs=self.cfg.get("fetch_subtitles", False))
                        if s == "ok":
                            actual_fn = re.sub(r'[<>:"/\\|?*]', "_",
                                              res["title"]).strip(" .")[:200] + ".opus"
                            with self._lib_lock:
                                index[actual_fn] = {"track": it["title"],
                                                    "artist": "",
                                                    "uri": _vid_from_url(res.get("url", ""))}
                            ok += 1
                            got = True
                            break
                        elif "unavailable" in str(_err).lower() or "403" in str(_err):
                            continue
                        else:
                            break
                    if not got and not ev.is_set():
                        self._record_failure("search", it["title"], it["q"])
                except Exception as e:
                    self._record_failure("search", it["title"], it["q"])
                time.sleep(0.3)
            self._save_lib_index(index)
            status(f"♻ retry complete — {ok}/{len(items)} recovered")

        threading.Thread(target=run, daemon=True).start()

    def _ytm_retry_failed(self):
        """Retry failed YT Music tracks (direct video ids)."""
        if not self._last_failed:
            messagebox.showinfo("Nothing to retry",
                                "No failures recorded from the last run.")
            return
        items = [f for f in self._last_failed if f.get("url")]
        if not items:
            messagebox.showinfo("Nothing direct",
                                "Failures were search-based; use the Spotify retry.")
            return
        self._last_failed = [f for f in self._last_failed if f.get("url") != ""]
        ev = threading.Event()
        self._cancel["ytm"] = ev

        def run():
            save_root = self._ytmout.get().strip() or r"D:\PEAK_downloads"
            b = self.browser()
            ok = 0
            for i, it in enumerate(items):
                if ev.is_set():
                    break
                folder = re.sub(r'[<>:"/\\|?*]', "_", it.get("playlist", "tracks"))[:80]
                save_dir = os.path.join(save_root, folder)
                os.makedirs(save_dir, exist_ok=True)
                s, m = download_one(it["title"], it["url"], save_dir,
                                    self._ytmfmt.get(),
                                    self.qkey(self._ytmq.get()), 0,
                                    browser=b, cancel_event=ev,
                                    subs=self.cfg.get("fetch_subtitles", False))
                if s == "ok":
                    ok += 1
                else:
                    self._record_failure("direct", it["title"], "", it["url"],
                                         it.get("playlist"))
            self._ui(lambda: self._ytmstat.config(
                text=f"♻ {ok}/{len(items)} recovered", fg="#ff6d00"))

        threading.Thread(target=run, daemon=True).start()

    def _manage_ids(self):
        """List / add / remove Spotify Client IDs. Any number can be stored;
        login tries them in order until one works."""
        import spotify_oauth as _so
        ids = _so.list_client_ids()
        lines = [f"  {i+1}. {r['label']:12s} {r['client_id'][:24]}…"
                 for i, r in enumerate(ids)] or ["  (none yet)"]
        dlg = tk.Toplevel(self.root)
        dlg.title("Spotify Client IDs")
        dlg.configure(bg=BG2)
        dlg.geometry("560x420")
        tk.Label(dlg, text="Registered Client IDs (tried top to bottom):",
                 bg=BG2, fg=GOLD, font=FONT_B).pack(anchor="w", padx=12,
                                                    pady=(12, 4))
        lst = tk.Listbox(dlg, bg=BG3, fg=FG, font=("Consolas", 10),
                         relief="flat", height=8)
        for ln in lines:
            lst.insert(tk.END, ln)
        lst.pack(fill="x", padx=12)

        row1 = tk.Frame(dlg, bg=BG2)
        row1.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(row1, text="New ID:", bg=BG2, fg=FG2, font=FONT).pack(side="left")
        ent = tk.Entry(row1, bg=BG3, fg=FG, font=FONT_SM, relief="flat",
                       width=34)
        ent.pack(side="left", padx=6)
        lbl = tk.Entry(row1, bg=BG3, fg=FG, font=FONT_SM, relief="flat",
                       width=10)
        lbl.insert(0, "label")
        lbl.pack(side="left")

        def do_add():
            cid = ent.get().strip()
            if not cid:
                return
            _so.add_client_id(cid, lbl.get().strip() or f"id-{len(ids)+1}")
            self._refresh_id_list(lst)
        self._btn(row1, "Add", do_add, bg="#3d5a80", fg="#fff").pack(side="left",
                                                                     padx=6)

        def do_del():
            sel = lst.curselection()
            idx = sel[0] - 0
            real = _so.list_client_ids()
            # listbox shows numbered lines; map back by index into real list
            # (lines were built from the same order)
            if 0 <= sel[0] < len(real):
                _so.remove_client_id(real[sel[0]]["client_id"])
                self._refresh_id_list(lst)
        self._btn(row1, "Remove selected", do_del, bg=BG3, fg=FG).pack(
            side="left", padx=6)

        txt = tk.Text(dlg, height=9, bg=BG3, fg=FG2, relief="flat",
                      font=FONT_SM, wrap="word")
        txt.pack(fill="both", expand=True, padx=12, pady=10)
        txt.insert("1.0",
            "Can friends use YOUR Client ID? YES — an app ID isn't tied to "
            "your account; anyone running TITANIUM can log in with it.\n"
            "Rate limits are shared per app though, so heavy users should "
            "add their OWN id below as backup — login auto-rotates.\n\n"
            "GET YOUR OWN (2 minutes):\n"
            "  1. Go to developer.spotify.com/dashboard and log in\n"
            "  2. Create app → any name (e.g. 'Titanium')\n"
            "  3. Redirect URI:  http://127.0.0.1:47821/callback   (exact!)\n"
            "  4. Check 'Web API' under which API/SDKs are used\n"
            "  5. Save → copy the Client ID → paste it above with Add\n"
            "No secret needed — TITANIUM uses the secure PKCE flow.")
        txt.config(state="disabled")

    def _refresh_id_list(self, lst):
        import spotify_oauth as _so
        lst.delete(0, tk.END)
        for i, r in enumerate(_so.list_client_ids()):
            lst.insert(tk.END,
                       f"  {i+1}. {r['label']:12s} {r['client_id'][:24]}…")

    def _show_id_guide(self):
        messagebox.showinfo(
            "Spotify Client ID — quick guide",
            "Your own ID (recommended, 2 minutes):\n\n"
            "1. developer.spotify.com/dashboard\n"
            "2. Create app → name it anything\n"
            "3. Redirect URI: http://127.0.0.1:47821/callback\n"
            "4. Enable Web API → Save → copy Client ID\n"
            "5. Paste it in Settings → Save ID (or Manage IDs → Add)\n\n"
            "Sharing: one ID works for everyone using TITANIUM — but rate "
            "limits are per-app, so each person can add their own too. "
            "TITANIUM tries every registered ID automatically.")

    def _save_folder_template(self):
        self.cfg["folder_template"] = self._ftentry.get().strip()
        self._save()
        self._ui(lambda: self._ftentry.config(fg=GREEN))

    def _save_lyric_lang(self):
        """Persist preferred lyrics language (blank = no preference)."""
        try:
            import titanium_enrich as _te3
            _te3.set_lyric_lang(self._langentry.get().strip().lower())
            messagebox.showinfo("Saved",
                                "Lyric language preference saved. "
                                "Future enhancement runs will prefer it.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _sub_creds(self):
        return (self._subsrv.get().strip(),
                self._subuser.get().strip(),
                self._subpass.get().strip())

    def _subsonic_test(self):
        """Ping the Subsonic/Navidrome server with current credentials."""
        srv, user, pw = self._sub_creds()
        if not (srv and user and pw):
            messagebox.showinfo("Missing info",
                                "Fill server, user and password first.")
            return
        try:
            import subsonic_push as sp
            ok, ver, typ = sp.ping(srv, user, pw)
            n = 0
            try:
                n = sp.get_artists_count(srv, user, pw)
            except Exception:
                pass
            st_txt = ""
            try:
                st = sp.get_scan_status(srv, user, pw)
                st_txt = (f"\nTracks indexed: {st['count']:,}"
                          f"\nLast scan: {st['lastScan'] or 'never'}")
                if st["scanning"]:
                    st_txt += "\n(scan running now…)"
            except Exception:
                pass
            messagebox.showinfo(
                "Connected",
                f"{typ or 'Subsonic server'} v{ver}\n"
                f"{n:,} artists indexed{st_txt}")
        except Exception as e:
            messagebox.showerror("Failed",
                                 f"Could not reach server:\n{e}")

    def _subsonic_browse(self):
        """Pull album list from the Subsonic/Navidrome server and offer to
        import an album's tracklist."""
        import subsonic_push as sp
        srv = self._subsrv.get().strip()
        u = self._subuser.get().strip()
        pw = self._subpass.get().strip()
        if not (srv and u):
            messagebox.showinfo("Subsonic", "enter server + user first")
            return
        try:
            albums = sp.get_album_list(srv, u, pw, "newest", 50)
        except Exception as e:
            messagebox.showerror("Subsonic", f"album list failed: {e}")
            return
        if not albums:
            messagebox.showinfo("Subsonic", "no albums returned")
            return
        win = tk.Toplevel(self.master)
        win.title("Albums on server")
        win.configure(bg=BG2)
        lb = tk.Listbox(win, bg=BG3, fg=FG, font=FONT,
                        width=60, height=20, selectmode="single")
        lb.pack(fill="both", expand=True, padx=8, pady=8)
        for a in albums:
            lb.insert(tk.END, f"{a['artist']} — {a['title']} ({a['year']})")
        def use():
            sel = lb.curselection()
            if not sel:
                return
            al = albums[sel[0]]
            try:
                tracks = sp.get_album_tracks(srv, u, pw, al["id"])
            except Exception as e:
                messagebox.showerror("Subsonic", str(e))
                return
            win.destroy()
            music = self._music_dir()
            qual = self._fmt_spotify.get() if hasattr(self, "_fmt_spotify") else "opus256"
            if hasattr(self, "_fmt_ytm"):
                qual = self._fmt_ytm.get()
            browser = self._browser.get().split(" ")[0] if hasattr(self, "_browser") and self._browser.get() else "auto"
            def run():
                n = ok = 0
                for t in tracks:
                    if not t.get("title") or t.get("title") == "?":
                        continue
                    n += 1
                    q = f"{t['title']} {t['artist']}".strip()
                    res = search_youtube(q, 2, browser)
                    got = False
                    for r in res or []:
                        s, e2 = download_one(
                            r["title"], r["url"], music, "audio", qual, 0,
                            browser=browser)
                        if s == "ok":
                            ok += 1
                            got = True
                            break
                        if "unavailable" in str(e2).lower():
                            continue
                    self._ui(lambda: self._spstat.config(
                        text=f"● {ok}/{n} from {al['title'][:20]}"))
                self._notify_done("SHUNGITE",
                                  f"Subsonic album {al['title'][:24]} done ({ok}/{n})")
            threading.Thread(target=run, daemon=True).start()
        tk.Button(win, text="Import selected album", command=use,
                  bg=GREEN, fg="#000", font=FONT).pack(pady=6)

    def _subsonic_scan(self):
        """Fire a library rescan on the Subsonic server (background)."""
        srv, user, pw = self._sub_creds()
        if not (srv and user and pw):
            return

        def run():
            try:
                import subsonic_push as sp
                sp.start_scan(srv, user, pw)
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def _show_changelog(self):
        """What's new — recent SHUNGITE releases."""
        msg = (
            "SHUNGITE v6.8\n\n"
            "\u25aa Loudness normalization (EBU R128 -16 LUFS)\n"
            "\u25aa Spotify 429 rate-limit backoff\n"
            "\u25aa Subsonic/Navidrome push + auto library scan\n"
            "\u25aa Smart playlists from tags (genre/artist/year rules)\n"
            "\u25aa Album URL support (OLAK5uy)\n"
            "\u25aa Queue: retry-failed + run-next bump (desktop & phone)\n"
            "\u25aa yt-dlp: 4-way parallel fragments + SB chapters embedded\n"
            "\u25aa Lyrics language preference + multi-record selection\n"
            "\u25aa Enhance history sparkline + report file\n"
            "\u25aa Phone remote PWA: icons, dedupe, queue management\n")
        messagebox.showinfo("What's new", msg)

    def _save_lfm_key(self):
        """Validate then store Last.fm API key for artist-image fallback."""
        try:
            import titanium_enrich as _te2
            k = self._lfmkey.get().strip()
            if k:
                # validate against a known artist before saving
                d = _te2._http_json(
                    "https://ws.audioscrobbler.com/2.0/?" +
                    __import__("urllib.parse", fromlist=["urlencode"]).
                    urlencode({"method": "artist.getinfo",
                               "artist": "Radiohead",
                               "api_key": k, "format": "json"}))
                if not isinstance(d, dict) or d.get("error"):
                    messagebox.showerror(
                        "Invalid key",
                        f"Last.fm rejected that key "
                        f"(error {d.get('error') if isinstance(d, dict) else '?'})."
                        "\nGet a free one at last.fm/api")
                    return
                open(_te2.LASTFM_KEY_FILE, "w",
                     encoding="utf-8").write(k)
                messagebox.showinfo("Saved",
                                    "Last.fm key validated & saved. "
                                    "Artist images will use it when "
                                    "Deezer misses.")
            else:
                try:
                    os.unlink(_te2.LASTFM_KEY_FILE)
                    messagebox.showinfo("Cleared",
                                        "Last.fm key removed.")
                except Exception:
                    pass
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _save_sp_cid(self):
        """Save the user's Spotify app client ID (PKCE - no secret needed)."""
        cid = self._spcid.get().strip()
        if not cid:
            messagebox.showinfo("Missing", "Paste your Client ID first.")
            return
        try:
            from spotify_oauth import CLIENT_ID_PATH
            open(CLIENT_ID_PATH, "w", encoding="utf-8").write(cid)
            import importlib
            import spotify_oauth as _so
            importlib.reload(_so)
            messagebox.showinfo("Saved",
                "Client ID saved.\n\nNow click \u2018Login to Spotify\u2019.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _run_on_main(self, fn, timeout=300):
        """Run fn on the Tk main thread and block the caller until it returns.

        pywebview's start() must run on the main thread; login handlers are
        dispatched from worker threads, so we marshal the webview call onto the
        main loop via root.after() and wait for it to finish."""
        import threading as _th
        done = _th.Event()
        holder = {}
        def _do():
            try:
                holder["ret"] = fn()
            except Exception as e:
                holder["err"] = e
            finally:
                done.set()
        try:
            self.root.after(0, _do)
        except Exception:
            # fallback: run inline if Tk isn't available
            _do()
        done.wait(timeout)
        if "err" in holder:
            raise holder["err"]
        return holder.get("ret")

    def _sp_oauth_login(self):
        """Official Spotify PKCE login: opens browser, user approves once."""
        if not spo:
            messagebox.showerror("Missing module",
                                 "spotify_oauth.py is missing from the app folder.")
            return

        def open_url(url):
            # use the embedded webview so cookies stay in our storage
            def _do():
                from login_window import open_login
                import os
                home = os.path.expanduser("~")
                cookies_path = os.path.join(home, ".spotify_cookies.txt")
                open_login(url, title="Spotify Login", timeout=300,
                           cookie_capture=cookies_path,
                           cookie_dir=os.path.join(home, ".peak_webview_spotify"),
                           allow_system_fallback=True)
                return True
            return self._run_on_main(_do, timeout=300)

        def run():
            try:
                self._ui(lambda: self._loginstat.config(
                    text="opening browser - approve PEAK in Spotify…", fg=GOLD))

                def interactive(fn):
                    done = threading.Event()
                    holder = {}
                    def target():
                        try:
                            holder["tok"] = fn(open_url)
                        finally:
                            done.set()
                    # must run in background; server waits for callback
                    threading.Thread(target=target, daemon=True).start()
                    done.wait(300)
                    return holder.get("tok")

                tok = spo.get_access_token(interactive)
                if tok:
                    me = spo.get_me(tok)
                    who = (me or {}).get("display_name") or "Spotify account"
                    self._ui(lambda w=who: self._loginstat.config(
                        text=f"✓ Spotify connected as {w}", fg="#1DB954"))
                else:
                    self._ui(lambda: self._loginstat.config(
                        text="✗ Spotify login not completed",
                        fg="#e63946"))
            except Exception as e:
                self._ui(lambda s=str(e)[:60]: self._loginstat.config(
                    text="error: " + s, fg="#e63946"))

        threading.Thread(target=run, daemon=True).start()

    def _sp_paste_callback(self):
        """Fallback: user pastes the full redirect URL from the browser bar."""
        if not spo:
            messagebox.showerror("Missing module", "spotify_oauth.py missing.")
            return
        url = simpledialog.askstring(
            "Paste callback URL",
            "Paste the FULL url from the browser address bar\n"
            "(it starts with 127.0.0.1:47821/callback?code=...)",
            parent=self.root)
        if not url:
            return
        code, state = spo.parse_callback_url(url)
        if not code:
            messagebox.showerror("Bad paste",
                                 "Couldn't find ?code= in what you pasted.")
            return
        tok = spo.manual_exchange(code)
        if tok:
            me = spo.get_me(tok) or {}
            who = me.get("display_name") or "Spotify"
            self._loginstat.config(text=f"✓ Spotify connected as {who}",
                                   fg="#1DB954")
        else:
            messagebox.showerror(
                "Exchange failed",
                "Code exchange failed. Note: a code only works once and "
                "expires in ~60s — do a fresh Login if this keeps failing.")

    def _sp_oauth_logout(self):
        try:
            if os.path.exists(spo.TOKEN_PATH):
                os.unlink(spo.TOKEN_PATH)
            self._ui(lambda: self._loginstat.config(
                text="Spotify logged out", fg=GOLD))
        except Exception:
            pass

    def _import_cookies(self):
        """Import a Netscape cookies.txt (YouTube/Google) so yt-dlp can use it."""
        import tkinter.filedialog as _fd, shutil as _sh
        f = _fd.askopenfilename(title="Select cookies.txt",
                                filetypes=[("Netscape cookies", "*.txt"), ("All files", "*.*")])
        if not f:
            return
        try:
            dst = _data_file(".peak_yt_cookies.txt")
            _sh.copyfile(f, dst)
            self.cfg["yt_cookies_file"] = dst
            save_config(self.cfg)
            self._loginstat.config(text="cookies imported — will apply on next download",
                                   fg="#1DB954")
            self.root.after(1000, self._check_logins)
        except Exception as e:
            self._loginstat.config(text="import failed: " + str(e)[:40], fg="#e63946")

    def _yt_login(self):
        """Open an EMBEDDED browser (inside the app) at Google/YouTube sign-in.
        The window keeps its own cookie profile; we harvest it for yt-dlp."""
        import os as _os, threading as _th, shutil as _sh
        profile = _data_file(".peak_webview")
        # Start fresh: remove any prior profile so Google shows the full
        # account chooser (instead of auto-logging the previously used account).
        try:
            if _os.path.isdir(profile):
                _sh.rmtree(profile, ignore_errors=True)
        except Exception:
            pass
        _os.makedirs(profile, exist_ok=True)

        def run():
            try:
                def _do_login():
                    from login_window import open_login
                    return open_login(
                        "https://accounts.google.com/ServiceLogin"
                        "?service=youtube&continue=https%3A%2F%2Fmusic.youtube.com%2F",
                        title="YouTube Music Login", timeout=600,
                        cookie_dir=profile,
                        allow_system_fallback=True)
                opened = self._run_on_main(_do_login, timeout=600)
                self._ui(lambda: self._loginstat.config(
                    text=("embedded YouTube login finished - reading cookies..."
                          if opened else "embedded login window closed"),
                    fg=GOLD))
                try:
                    import browser_cookies as _bc
                    n = _bc.export_webview_cookies(profile)
                    self._ui(lambda: self._loginstat.config(
                        text="YouTube logged in (%s cookies) - 256k ready" % n,
                        fg="#1DB954"))
                except Exception as e2:
                    _msg2 = str(e2)[:40]
                    self._ui(lambda m=_msg2: self._loginstat.config(
                        text="login done (cookie harvest skipped: %s)" % m,
                        fg=GOLD))
                self.root.after(1000, self._check_logins)
            except Exception as e:
                _msg = str(e)[:50]
                self._ui(lambda m=_msg: self._loginstat.config(
                    text="error: " + m, fg="#e63946"))

        _th.Thread(target=run, daemon=True).start()

    def _yt_logout(self):
        """Fully wipe ALL saved YouTube login state so the next login shows a
        clean Google account chooser (not the previously-logged-in account).

        Clears: the embedded WebView2 cookie profile, the exported cookies.txt,
        and the config pointer. Then opens Google logout inside the embedded
        webview to drop the session server-side."""
        import os as _os, shutil as _sh, threading as _th

        profile = _data_file(".peak_webview")
        cookie_file = _data_file(".peak_yt_cookies.txt")

        # 1) remove the exported cookies.txt
        for cf in (cookie_file,
                   _data_file(".peak_yt_cookies.txt")):
            try:
                if _os.path.exists(cf):
                    _os.remove(cf)
            except Exception:
                pass

        # 2) remove the whole WebView2 profile (forces a fresh account chooser)
        try:
            if _os.path.isdir(profile):
                _sh.rmtree(profile, ignore_errors=True)
        except Exception:
            pass

        # 3) reset the config pointer so downloads stop using stale cookies
        self.cfg["yt_cookies_file"] = ""
        try:
            cfgp = _data_file(".peak_config.json")
            import json as _json
            _c = {}
            if _os.path.exists(cfgp):
                try:
                    _c = _json.load(open(cfgp, encoding="utf-8"))
                except Exception:
                    _c = {}
            _c["yt_cookies_file"] = ""
            _json.dump(_c, open(cfgp, "w", encoding="utf-8"), indent=2)
        except Exception:
            pass

        def _do_logout():
            # open the Google logout in the embedded browser (new empty profile)
            from login_window import open_login
            open_login(
                "https://accounts.google.com/Logout"
                "?continue=https%3A%2F%2Faccounts.google.com%2F",
                title="YouTube Logout", timeout=120,
                cookie_dir=profile,
                allow_system_fallback=True)
            return True

        self._loginstat.config(text="wiping saved YouTube login…", fg=GOLD)

        def run():
            try:
                self._run_on_main(_do_logout, timeout=150)
            except Exception:
                pass
            self._ui(lambda: self._loginstat.config(
                text="YouTube login cleared — log in fresh to pick a different account",
                fg="#1DB954"))
            self.root.after(500, self._check_logins)

        _th.Thread(target=run, daemon=True).start()

    def _check_logins(self):
        """Probe every browser for live Spotify/YT Music cookies.
        Shows instant feedback and updates progressively per service."""
        # INSTANT feedback before any slow probing starts
        self._loginstat.config(text="⏳ probing browsers… (up to ~60s)", fg=GOLD)

        def setstat(txt, color):
            self._ui(lambda t=txt, c=color: self._loginstat.config(text=t, fg=c))

        def run():
            results = {}
            try:
                # 1. Spotify OAuth (instant - local token check only)
                who = ""
                if spo:
                    try:
                        t = spo._refresh_if_needed()
                        if t:
                            me = spo.get_me(t["access_token"]) or {}
                            who = me.get("display_name") or "Spotify"
                            results["oauth"] = True
                        else:
                            results["oauth"] = False
                    except Exception:
                        results["oauth"] = False
                setstat("⏳ Spotify cookies…", GOLD)

                # self-heal: drop configured cookie files that no longer exist
                for key in ("sp_cookies_file", "yt_cookies_file"):
                    p = self.cfg.get(key) or ""
                    if p and not os.path.exists(p):
                        self.cfg[key] = ""

                import browser_cookies as bc
                dc = bc.get_sp_dc()
                results["sp"] = bool(dc)
                ok_txt = f"OAuth as {who}" if results.get("oauth") else (
                    "OK" if dc else "not logged in")
                sp_color = "#1DB954" if (dc or results.get("oauth")) else "#e63946"
                setstat(f"Spotify: {ok_txt}  ·  ⏳ YouTube…",
                        "#1DB954" if (dc or results.get("oauth")) else GOLD)

                yt = bc.has_yt_auth()
                results["yt"] = bool(yt)

                parts = []
                parts.append(("✓" if (results.get("oauth") or results["sp"])
                              else "✗") + " Spotify" +
                             (f" ({who})" if results.get("oauth") else ""))
                parts.append(("✓" if yt else "✗") + " YouTube")
                all_ok = ((results.get("oauth") or results["sp"]) and yt)
                msg = "  ·  ".join(parts)
                setstat(msg, "#1DB954" if all_ok else GOLD)
            except Exception as e:
                setstat("error: " + str(e)[:50], "#e63946")
        threading.Thread(target=run, daemon=True).start()



    def _reset_settings(self):
        """Reset settings to defaults, keeping library folder + browser."""
        if not messagebox.askyesno(
                "Reset settings",
                "Reset everything except your library folder and browser choice?"):
            return
        keep = {"library_root": self.cfg.get("library_root"),
                "browser": self.cfg.get("browser")}
        self.cfg.clear()
        self.cfg.update(keep)
        save_config(self.cfg)
        messagebox.showinfo("Reset done",
                            "Settings reset. Restart the app for a clean slate.")

    def _save(self):
        self.cfg["output_dir"] = self._setdir.get()
        self.cfg["library_root"] = self._setdir.get()
        bsel = self._browser.get().split(" ")[0]   # strip " (no cookies)" etc.
        if bsel == "auto":
            bsel = "auto"                          # scan ALL installed browsers
        elif bsel == "none":
            bsel = "none"
        self.cfg["browser"] = bsel
        # custom manual cookie files (optional overrides)
        try:
            self.cfg["sp_cookies_file"] = self._spdc_row.get().strip()
        except Exception:
            self.cfg["sp_cookies_file"] = ""
        try:
            self.cfg["enhance_schedule"] = self._schentry.get().strip()
            if hasattr(self, "_normvar"):
                self.cfg["normalize_loudness"] = bool(self._normvar.get())
            if hasattr(self, "_rgvar"):
                self.cfg["replaygain"] = bool(self._rgvar.get())
            if hasattr(self, "_embonlyvar"):
                self.cfg["embed_only"] = bool(self._embonlyvar.get())
            try:
                import titanium_enrich as _te_emb
                _te_emb.set_embed_only(bool(self.cfg.get("embed_only", True)))
                if hasattr(self, "_cs_itunes"):
                    self.cfg["cover_itunes"] = bool(self._cs_itunes.get())
                    self.cfg["cover_deezer"] = bool(self._cs_deezer.get())
                    self.cfg["cover_lastfm"] = bool(self._cs_lastfm.get())
                    _te_emb.set_cover_source("itunes", bool(self._cs_itunes.get()))
                    _te_emb.set_cover_source("deezer", bool(self._cs_deezer.get()))
                    _te_emb.set_cover_source("lastfm", bool(self._cs_lastfm.get()))
            except Exception:
                pass
            if hasattr(self, "_premvar"):
                self.cfg["yt_premium"] = bool(self._premvar.get())
            if hasattr(self, "_enhvar"):
                self.cfg["enhance_audio"] = bool(self._enhvar.get())
            if hasattr(self, "_subsrv"):
                self.cfg["subsonic_server"] = self._subsrv.get().strip()
                self.cfg["subsonic_user"] = self._subuser.get().strip()
                self.cfg["subsonic_pass"] = self._subpass.get().strip()
                self.cfg["subsonic_push"] = bool(self._subpushvar.get())
        except Exception:
            pass
        try:
            self.cfg["sponsorblock"] = bool(self._sbvar.get())
        except Exception:
            pass
        try:
            self.cfg["fetch_subtitles"] = bool(self._subsvar.get())
        except Exception:
            pass
        try:
            self.cfg["shutdown_when_done"] = bool(self._sdvar.get())
        except Exception:
            pass
        for key, var in (("fmt_batch", getattr(self, "_bfmt", None)),
                         ("fmt_search", getattr(self, "_sfmt2", None)),
                         ("fmt_single", getattr(self, "_sfmt", None)),
                         ("fmt_playlist", getattr(self, "_pfmt", None)),
                         ("fmt_spotify", getattr(self, "_spfmt", None)),
                         ("fmt_ytm", getattr(self, "_ytmfmt", None))):
            if var is not None:
                self.cfg[key] = var.get()
        try:
            self.cfg["yt_cookies_file"] = self._ytck_row.get().strip()
        except Exception:
            self.cfg["yt_cookies_file"] = ""
        self.cfg["max_workers"] = int(self._workers.get() or 4)
        try:
            self.dq.resize(int(self._workers.get() or 4))
        except Exception:
            pass
        self.cfg["speed_limit_kbps"] = int(self._speed.get() or 0)
        if getattr(self, "_autodl", None) is not None:
            self.cfg["auto_dl_on_paste"] = bool(self._autodl.get())
        # Seal-style advanced settings (new Format & Download card)
        for ent in getattr(self, "_cfg_entries", []) or []:
            key = ent._cfgkey; v = ent.get().strip()
            if key in ("max_workers", "max_video_height", "audio_bitrate_max"):
                try:
                    v = int(v) if v else 0
                except Exception:
                    v = 0
            self.cfg[key] = v
        if getattr(self, "_rfnar", None) is not None:
            self.cfg["restrict_filenames"] = bool(self._rfnar.get())
        if getattr(self, "_subvar", None) is not None:
            self.cfg["download_subtitles"] = bool(self._subvar.get())
        if getattr(self, "_ariavar", None) is not None:
            self.cfg["use_aria2c"] = bool(self._ariavar.get())
        save_config(self.cfg)
        # Show where things will be saved
        music = os.path.join(self._setdir.get(), "Music")
        pls = os.path.join(self._setdir.get(), "Playlists")
        messagebox.showinfo("Saved",
            f"Settings saved.\n\n"
            f"Music folder:\n  {music}\n"
            f"Playlists folder:\n  {pls}\n\n"
            f"⚠ Keep Music/ and Playlists/ side-by-side\n"
            f"   for .m3u files to work on your phone.")


if __name__ == "__main__":
    root = tk.Tk()
    root.minsize(980, 640)
    # Bind the real root so the combobox popup inherits readable listbox colors
    import sys as _sys
    _sys.modules[__name__].ROOT_TK = root
    app = App(root)
    # restore window geometry + maximized state
    try:
        g = app.cfg.get("window_geom")
        if g and isinstance(g, dict):
            if g.get("maximized"):
                root.state("zoomed")
            else:
                root.geometry(g.get("geom", ""))
    except Exception:
        pass

    def _save_geom():
        try:
            app.cfg["window_geom"] = {
                "geom": root.geometry(),
                "maximized": bool(root.state() == "zoomed")}
            app._save()
        except Exception:
            pass

    def _toggle_fullscreen(e=None):
        root.attributes("-fullscreen",
                        not root.attributes("-fullscreen"))

    root.protocol("WM_DELETE_WINDOW", lambda: (_save_geom(), root.destroy()))
    root.bind("<F11>", _toggle_fullscreen)
    root.mainloop()

def _find_python_exe():
    """Locate a real python exe — never the frozen frozen exe."""
    import os
    candidates = [
        r"C:\Users\Itsa\AppData\Local\Programs\Python\Python314\python.exe",
        r"C:\Users\Itsa\AppData\Local\Programs\Python\Python311\python.exe",
        r"C:\Users\Itsa\whisperX\.venv\Scripts\python.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    import shutil
    return shutil.which("python.exe") or shutil.which("python")

