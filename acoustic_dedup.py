"""Acoustic duplicate finder for SHUNGITE (beets/chromaprint-inspired).

Uses ffmpeg to decode a 120s mono 11025Hz slice of each track, hashes the
raw PCM (fast, dependency-free), and groups tracks whose audio fingerprints
collide. Not a true chromaprint but catches re-uploads/8D remixes of the
same recording with zero extra installs.
"""
import hashlib
import os
import json
import subprocess

_FFMPEG_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "_internal", "ffmpeg", "ffmpeg.exe"),
    "ffmpeg",
)


def _ffmpeg():
    for c in _FFMPEG_CANDIDATES:
        if os.path.exists(c):
            return c
    return "ffmpeg"


def audio_fingerprint(path, seconds=90):
    """Hash of decoded PCM — stable per recording, ignores tags."""
    try:
        cmd = [_ffmpeg(), "-v", "error", "-i", path,
               "-t", str(seconds), "-ac", "1", "-ar", "8000",
               "-f", "s16le", "-"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(cmd, capture_output=True, timeout=60,
                             creationflags=flags)
        pcm = out.stdout
        if len(pcm) < 10000:
            return None
        # coarse-hash in 4KB blocks so tiny bitrate diffs don't matter
        h = hashlib.sha1()
        for i in range(0, min(len(pcm), 3_000_000), 4096):
            h.update(pcm[i:i + 2048])   # first half of each block
        return h.hexdigest()
    except Exception:
        return None


def find_duplicates(files, progress=None):
    """files: list of paths. Returns list of duplicate-groups (lists).
    Uses the (mtime,size) cache so unchanged files skip the ffmpeg decode."""
    cache = _load_cache()
    fp = {}
    meta = {}
    cached_n = 0
    for n, p in enumerate(files):
        if progress and n % 10 == 0:
            progress(n, len(files))
        f, was_cached = fingerprint_cached(p, cache=cache)
        if was_cached:
            cached_n += 1
        if f:
            fp.setdefault(f, []).append(p)
            meta[p] = f
    groups = [v for v in fp.values() if len(v) > 1]
    global _LAST_CACHE_HITS
    _LAST_CACHE_HITS = cached_n
    return groups


import os

_LAST_CACHE_HITS = 0

_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "_fp_cache.json")

def _load_cache():
    try:
        if os.path.exists(_CACHE_PATH):
            with open(_CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_cache(cache):
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass

def fingerprint_cached(path, seconds=90, cache=None):
    """Fingerprint with an (mtime,size) cache: untouched files return instantly."""
    if cache is None:
        cache = _load_cache()
    try:
        st = os.stat(path)
        key = f"{st.st_mtime_ns}:{st.st_size}"
        if path in cache and cache[path][0] == key:
            return cache[path][1], True  # (hash, was_cached)
    except Exception:
        pass
    h = audio_fingerprint(path, seconds=seconds)
    try:
        st = os.stat(path)
        cache[path] = [f"{st.st_mtime_ns}:{st.st_size}", h]
        _save_cache(cache)
    except Exception:
        pass
    return h, False


def detect_bpm(path, seconds=30):
    """Estimate BPM from decoded mono PCM via onset peak-interval analysis.
    Zero dependencies (reuses bundled ffmpeg). Returns rounded int BPM or 0."""
    import collections, statistics
    try:
        cmd = [_ffmpeg(), "-v", "error", "-i", path,
               "-t", str(seconds), "-ac", "1", "-ar", "8000",
               "-f", "s16le", "-"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(cmd, capture_output=True, timeout=60,
                             creationflags=flags)
        pcm = out.stdout
        if len(pcm) < 8000:
            return 0
        # 16-bit PCM -> samples
        n = len(pcm) // 2
        samples = []
        for i in range(0, n, 2):
            v = int.from_bytes(pcm[i:i+2], "little", signed=True)
            samples.append(abs(v))
        rate = 8000.0
        # energy envelope in 20ms windows
        win = int(rate * 0.02)
        env = []
        for i in range(0, len(samples) - win, win):
            seg = samples[i:i+win]
            env.append(sum(seg) / len(seg))
        if len(env) < 20:
            return 0
        # detect onsets: local energy peaks above a rolling mean
        peaks = []
        mean = statistics.mean(env) if env else 0
        if mean <= 0:
            return 0
        thresh = mean * 1.4
        for i in range(1, len(env) - 1):
            if env[i] > thresh and env[i] >= env[i-1] and env[i] > env[i+1]:
                peaks.append(i)
        if len(peaks) < 3:
            return 0
        # intervals between peaks -> bpm
        intervals = []
        for a, b in zip(peaks[:-1], peaks[1:]):
            dt = (b - a) * 0.02  # seconds
            if 0.2 < dt < 2.0:   # 30-300 bpm sane range
                intervals.append(dt)
        if not intervals:
            return 0
        med = statistics.median(intervals)
        bpm = round(60.0 / med)
        # clamp to sane range
        if bpm < 40 or bpm > 220:
            return 0
        return bpm
    except Exception:
        return 0
