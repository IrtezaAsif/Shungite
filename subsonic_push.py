"""Subsonic/Navidrome client for SHUNGITE — push new downloads to a
personal music server. Token auth (md5(password+salt)) per Subsonic API
1.13+. Free, no key server-side."""
import hashlib
import secrets
import urllib.parse
import urllib.request
import json


def _auth_params(user, password):
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    return {"u": user, "t": token, "s": salt,
            "v": "1.16.1", "c": "SHUNGITE", "f": "json"}


def _call(server, endpoint, user, password, extra=None, timeout=10):
    base = server.rstrip("/")
    if not base.endswith("/rest"):
        base += "/rest"
    params = _auth_params(user, password)
    if extra:
        params.update(extra)
    url = base + "/" + endpoint + ".view?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "ZINCUM/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    sr = data.get("subsonic-response", {})
    if sr.get("status") != "ok":
        err = sr.get("error", {})
        raise RuntimeError(f"subsonic error {err.get('code')}: "
                           f"{err.get('message')}")
    return sr


def ping(server, user, password):
    """True + server version string if credentials work."""
    sr = _call(server, "ping", user, password)
    return True, sr.get("version", "?"), sr.get("type", "")


def start_scan(server, user, password):
    """Ask the server to rescan its library (Navidrome supports this)."""
    sr = _call(server, "startScan", user, password)
    scan = sr.get("scanStatus", {})
    return scan.get("scanning", False)


def get_artists_count(server, user, password):
    sr = _call(server, "getArtists", user, password)
    idx = sr.get("artists", {}).get("index", [])
    n = sum(len(a.get("artist", [])) for a in idx)
    return n


if __name__ == "__main__":
    import sys
    srv, u, p = sys.argv[1], sys.argv[2], sys.argv[3]
    ok, ver, typ = ping(srv, u, p)
    print(f"ping ok={ok} version={ver} type={typ}")


def get_scan_status(server, user, password):
    """Returns dict: scanning(bool), count(long), lastScan(str|''),
    folderCount(int). lastScan/folderCount are Navidrome extras."""
    sr = _call(server, "getScanStatus", user, password)
    ss = sr.get("scanStatus", {})
    return {
        "scanning": bool(ss.get("scanning", False)),
        "count": ss.get("count", 0),
        "lastScan": ss.get("lastScan", ""),
        "folderCount": ss.get("folderCount", 0),
    }


def wait_for_scan(server, user, password, timeout_s=300, poll=5,
                  progress=None):
    """Block until a started scan finishes; calls progress(count) each poll.
    Returns final track count."""
    import time as _t
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        st = get_scan_status(server, user, password)
        if progress:
            try:
                progress(st.get("count", 0))
            except Exception:
                pass
        if not st["scanning"]:
            return st["count"]
        _t.sleep(poll)
    return -1


def get_album_list(server, user, password, list_type="newest", size=100,
                   offset=0):
    """getAlbumList2 — fetch albums by type (newest/frequent/recent/random/
    alphabetical/byGenre/byYear/alphabeticalByName). Returns list of dicts
    {id, title, artist, year, genre, coverArt}."""
    d = _call(server, "getAlbumList2", user, password,
              extra={"type": list_type, "size": str(size),
                     "offset": str(offset)})
    albums = []
    try:
        items = (d.get("albumList2") or {}).get("album", [])
        if isinstance(items, dict):
            items = [items]
        for a in items:
            albums.append({
                "id": a.get("id", ""),
                "title": a.get("name") or a.get("title", ""),
                "artist": a.get("artist", ""),
                "year": a.get("year", ""),
                "genre": a.get("genre", ""),
                "coverArt": a.get("coverArt", ""),
            })
    except Exception:
        pass
    return albums


def get_album_tracks(server, user, password, album_id):
    """getMusicDirectory for an album id -> its track list (id/title/duration)."""
    d = _call(server, "getMusicDirectory", user, password,
              extra={"id": album_id})
    tracks = []
    try:
        ch = (d.get("directory") or {}).get("child", [])
        if isinstance(ch, dict):
            ch = [ch]
        for c in ch:
            if c.get("isDir") in (True, "true"):
                continue
            tracks.append({
                "id": c.get("id", ""),
                "title": c.get("title", ""),
                "artist": c.get("artist", ""),
                "duration": c.get("duration", 0),
            })
    except Exception:
        pass
    return tracks
