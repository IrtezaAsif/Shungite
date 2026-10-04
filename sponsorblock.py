"""SponsorBlock sidecar segments for ZINCUM (research feature).
Fetches skip-segment data from sponsor.ajay.app (free, no key) and stores
it as <song>.segments.json next to the audio. Players that support it can
skip sponsors/intros; the data also powers future auto-skip.
"""
import json
import os
import urllib.request

API = "https://sponsor.ajay.app/api/skipSegments"
UA = {"User-Agent": "ZINCUM/1.0"}


def _http_json(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def video_id_from_url(url):
    import re
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})",
                  url or "")
    return m.group(1) if m else None


def hash_path(video_id):
    """SHA256 prefix path per SponsorBlock API spec (first 4 hex chars)."""
    import hashlib
    return hashlib.sha256(video_id.encode()).hexdigest()[:4]


def fetch_segments(video_id):
    """Return raw segment list for a video, or None (404 = none submitted)."""
    if not video_id or len(video_id) != 11:
        return None
    cats = json.dumps(["sponsor", "intro", "outro", "selfpromo",
                       "interaction"])
    url = API + "?" + urllib.parse.urlencode({"videoID": video_id,
                                              "categories": cats})
    segs = _http_json(url)
    return segs or None


def save_segments(video_id, audio_path):
    """Write <audio>.segments.json beside the file. True if segments found."""
    base = os.path.splitext(audio_path)[0]
    out_path = base + ".segments.json"
    segs = fetch_segments(video_id)
    if not segs:
        return False
    cleaned = [{"category": s.get("category"),
                "start": s.get("segment", [0])[0],
                "end": s.get("segment", [0, 0])[-1]}
               for s in segs if s.get("segment")]
    if not cleaned:
        return False
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"videoId": video_id, "segments": cleaned},
                      f, indent=1)
        return True
    except Exception:
        return False
