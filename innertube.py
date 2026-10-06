"""InnerTube (YouTube Music) search for ZINCUM - NewPipe-style, no subprocess.

Uses the same public API the YouTube Music web app / NewPipe use.
Returns real artist names + videoIds. Falls back gracefully on any error.
"""
import json
import os
import urllib.request

API = ("https://music.youtube.com/youtubei/v1/search"
       "?key=AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30&prettyPrint=false")
CONTEXT = {
    "context": {"client": {
        "clientName": "WEB_REMIX",
        "clientVersion": "1.20240101.01.00",
        "hl": "en", "gl": "US",
    }}
}
HDRS = {"Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _walk(node, key):
    """Depth-first yield of every dict under `key`."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _walk(v, key)
    elif isinstance(node, list):
        for it in node:
            yield from _walk(it, key)


def search_music(query, limit=10):
    """Search YT Music. Returns [{title, artist, videoId, seconds}, ...]."""
    body = dict(CONTEXT)
    body["query"] = query
    try:
        req = urllib.request.Request(
            API, data=json.dumps(body).encode(), headers=HDRS)
        # frozen-exe fix: PyInstaller builds can miss CA certs -> SSL
        # verify fails -> every request dies silently. Build an explicit
        # SSL context: certifi if present, else system store, else
        # unverified (search is public data, acceptable last resort).
        _ctx = None
        try:
            import ssl
            try:
                import certifi
                _ctx = ssl.create_default_context(
                    cafile=certifi.where())
            except ImportError:
                _ctx = ssl.create_default_context()
        except Exception:
            _ctx = None
        if _ctx is not None:
            try:
                _ctx.check_hostname = True
            except Exception:
                pass
            with urllib.request.urlopen(req, timeout=12,
                                        context=_ctx) as r:
                data = json.load(r)
        else:
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.load(r)
    except Exception as _e:
        try:
            _lf = os.path.join(os.path.expandvars(
                r"%LOCALAPPDATA%\Shungite"), "search_debug.log")
            # LocalLow actually lives under AppData\LocalLow
            _lf = os.path.join(os.path.expanduser("~"),
                               "AppData", "LocalLow", "Shungite",
                               "search_debug.log")
            with open(_lf, "a", encoding="utf-8") as _f:
                _f.write("innertube EXC detail: %s\n" % str(_e)[:150])
        except Exception:
            pass
        return []

    out = []
    seen = set()
    for renderer in _walk(data, "musicResponsiveListItemRenderer"):
        try:
            flex = renderer["flexColumns"]
            title = "".join(r.get("text", {}).get("runs", [{}])[0].get("text", "")
                            for r in [flex[0]] if "text" in r) or \
                    (flex[0].get("musicResponsiveListItemFlexColumnRenderer", {})
                         .get("text", {}).get("runs", [{}])[0].get("text", ""))
            # simpler: gather all runs text per column
            def col_text(i):
                try:
                    runs = (flex[i]["musicResponsiveListItemFlexColumnRenderer"]
                            ["text"]["runs"])
                    return "".join(x.get("text", "") for x in runs)
                except Exception:
                    return ""
            title = col_text(0)
            sub = col_text(1)          # "Artist • Album • duration" style
            vid = (renderer.get("playlistItemData", {})
                            .get("videoId") or
                   renderer.get("overlay", {}))
            # robust id extraction
            vid2 = None
            try:
                vid2 = (renderer["overlay"]["musicItemThumbnailOverlayRenderer"]
                        ["content"]["musicPlayButtonRenderer"]
                        ["playNavigationEndpoint"]["watchEndpoint"]["videoId"])
            except Exception:
                pass
            vid = renderer.get("playlistItemData", {}).get("videoId") or vid2
            if not vid or not title:
                continue
            if vid in seen:
                continue
            seen.add(vid)
            artist = sub.split("•")[0].strip() if sub else ""
            secs = None
            m = None
            import re as _re
            mm = _re.search(r"(\d+):(\d{2})", sub or "")
            if mm:
                secs = int(mm.group(1)) * 60 + int(mm.group(2))
            out.append({"title": title.strip(),
                        "artist": artist,
                        "videoId": vid,
                        "seconds": secs})
            if len(out) >= limit:
                break
        except Exception:
            continue
    return out


if __name__ == "__main__":
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    res = search_music("blinding lights weeknd", 6)
    for r in res:
        print(f"{r['videoId']}  {r['artist']:30s}  {r['title']}")


def _authorization(cookies_dict):
    """Build the SAPISIDHASH Authorization header (ytmusicapi-compatible).

    Requires the __Secure-3PAPISID cookie; hash = sha1(ts + " " + sapisid +
    " " + origin), prefixed "SAPISIDHASH ts_".
    """
    import hashlib, time
    sapisid = cookies_dict.get("__Secure-3PAPISID") or cookies_dict.get("SAPISID") or ""
    origin = "https://music.youtube.com"
    ts = int(time.time())
    h = hashlib.sha1(("%d %s %s" % (ts, sapisid, origin)).encode("utf-8")).hexdigest()
    return "SAPISIDHASH %d_%s" % (ts, h)


# Extract the InnerTube API key already used by the search endpoint.
_API_KEY = ""
try:
    import re as _re
    _m = _re.search(r"key=([A-Za-z0-9_-]+)", API)
    if _m:
        _API_KEY = _m.group(1)
except Exception:
    pass


def _post(endpoint, body, cookies_dict):
    """POST to an InnerTube endpoint with correct auth (key + SAPISIDHASH)."""
    import urllib.request as _ur
    url = "https://music.youtube.com/youtubei/v1/" + endpoint
    url += "?alt=json" + ("&key=" + _API_KEY if _API_KEY else "")
    body = dict(body)
    body.setdefault("context", {"client": {"clientName": "WEB_REMIX",
                                           "clientVersion": "1.20240101.01.00"}})
    hdrs = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Cookie": "; ".join("%s=%s" % (k, v) for k, v in cookies_dict.items() if v),
        "Authorization": _authorization(cookies_dict),
        "X-Goog-AuthUser": "0",
        "Origin": "https://music.youtube.com",
    }
    req = _ur.Request(url, data=json.dumps(body).encode("utf-8"), headers=hdrs)
    last = None
    for _try in range(3):
        try:
            with _ur.urlopen(req, timeout=25) as r:
                return json.load(r)
        except _ur.HTTPError as e:
            last = e
            # transient 429/503 or rate-limit -> backoff and retry
            if e.code in (429, 503):
                import time as _t
                _t.sleep(2 * (_try + 1))
                # rebuild request fresh (some servers want a new body stream)
                req = _ur.Request(url, data=json.dumps(body).encode("utf-8"), headers=hdrs)
                continue
            raise
    if last:
        raise last
    return {}


def create_playlist(title, cookies_dict, description=""):
    """Create a YT Music playlist via InnerTube. Returns playlistId or None."""
    title = title.replace("<", "").replace(">", "")
    body = {"title": title, "description": description or "",
            "privacyStatus": "PRIVATE"}
    try:
        data = _post("playlist/create", body, cookies_dict)
        return data.get("playlistId") or data.get("id") or None
    except Exception:
        return None


def add_to_playlist(playlist_id, video_id, cookies_dict):
    """Add one video to a YT Music playlist. Returns True on success."""
    body = {
        "playlistId": playlist_id,
        "actions": [{"action": "ACTION_ADD_VIDEO", "addedVideoId": video_id}],
    }
    try:
        import time as _t
        _t.sleep(0.4)   # gentle spacing so YouTube doesn't throttle a batch
        data = _post("browse/edit_playlist", body, cookies_dict)
        ok = data.get("status") in ("STATUS_SUCCEEDED", "OK", None)
        return ok or ("error" not in json.dumps(data).lower())
    except Exception:
        return False


def _walk(node, key):
    """Depth-first yield of every value under `key`."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _walk(v, key)
    elif isinstance(node, list):
        for it in node:
            yield from _walk(it, key)


def get_library_playlists(cookies_dict):
    """Return [{playlistId, title, count}] for the user's YT Music library."""
    body = {"browseId": "FEmusic_liked_playlists"}
    try:
        data = _post("browse", body, cookies_dict)
    except Exception:
        return []
    out = []
    seen = set()
    for renderer in _walk(data, "musicTwoRowItemRenderer"):
        try:
            pid = (renderer.get("navigationEndpoint", {})
                   .get("browseEndpoint", {})
                   .get("browseId"))
            title = "".join(r.get("text", "") for r in
                            renderer.get("title", {}).get("runs", []))
            if pid and pid.startswith("VL") and title and pid not in seen:
                seen.add(pid)
                out.append({"playlistId": pid, "title": title})
        except Exception:
            continue
    return out
