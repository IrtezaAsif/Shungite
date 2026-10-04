"""Spotify OAuth (PKCE) for PEAK - no cookies needed, works everywhere.

Flow: app starts a localhost callback server, opens the browser, user
approves once, we exchange the code for tokens. Tokens cached in
~/.peak_spotify_auth.json; refresh handled automatically.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

_data_file_compat = None


def _data_dir():
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    low = os.path.normpath(os.path.join(os.path.dirname(base), "LocalLow"))
    d = os.path.join(low, "Shungite")
    os.makedirs(d, exist_ok=True)
    return d

def _data_file(name):
    new = os.path.join(_data_dir(), name)
    legacy = os.path.join(os.path.expanduser("~"), name)
    try:
        if not os.path.exists(new) and os.path.isfile(legacy):
            import shutil as _sh
            _sh.copy2(legacy, new)
    except Exception:
        pass
    return new

CLIENT_ID_PATH = _data_file(".peak_spotify_client_id")
CLIENT_IDS_PATH = os.path.join(os.path.expanduser("~"),
                               ".peak_spotify_client_ids.json")


def list_client_ids():
    """All registered client IDs: [{label, client_id}, ...]. Primary first."""
    ids = []
    try:
        ids = json.load(open(CLIENT_IDS_PATH, encoding="utf-8"))
    except Exception:
        ids = []
    # legacy single-id file becomes the primary entry
    try:
        legacy = open(CLIENT_ID_PATH, encoding="utf-8").read().strip()
        if legacy and not any(i.get("client_id") == legacy for i in ids):
            ids.insert(0, {"label": "primary", "client_id": legacy})
    except Exception:
        pass
    return ids


def add_client_id(client_id, label=""):
    client_id = client_id.strip()
    if not client_id:
        return False
    ids = list_client_ids()
    if any(i["client_id"] == client_id for i in ids):
        return True
    ids.append({"label": label or f"id-{len(ids)+1}",
                "client_id": client_id})
    json.dump(ids, open(CLIENT_IDS_PATH, "w", encoding="utf-8"), indent=2)
    return True


def remove_client_id(client_id):
    ids = [i for i in list_client_ids() if i["client_id"] != client_id]
    json.dump(ids, open(CLIENT_IDS_PATH, "w", encoding="utf-8"), indent=2)
    return True


def _client_id():
    """Primary client ID (first registered)."""
    ids = list_client_ids()
    if ids:
        return ids[0]["client_id"]
    try:
        v = open(CLIENT_ID_PATH, encoding="utf-8").read().strip()
        if v:
            return v
    except Exception:
        pass
    return DEFAULT_CLIENT_ID


DEFAULT_CLIENT_ID = "0e518b7396d24c4c8e397c26b0ef8514"


def _login_flow(open_url, client_id=None):
    """Run the PKCE login. Tries each registered ID until one works."""
    ids = [i["client_id"] for i in list_client_ids()] or           ([client_id] if client_id else []) or [DEFAULT_CLIENT_ID]
    last_err = None
    for cid in ids:
        try:
            tok = _login_flow_single(open_url, cid)
            if tok:
                return tok
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    return None


def _login_flow_single(open_url, client_id):
    """PKCE login with ONE client id."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    manual_exchange._verifier = verifier
REDIRECT_PORT = 47821
REDIRECT_URI = "http://127.0.0.1:" + str(REDIRECT_PORT) + "/callback"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = ("playlist-read-private playlist-read-collaborative "
         "user-library-read playlist-modify-public playlist-modify-private")

TOKEN_PATH = os.path.join(os.path.expanduser("~"), ".peak_spotify_auth.json")


def _load_tokens():
    try:
        return json.load(open(TOKEN_PATH, encoding="utf-8"))
    except Exception:
        return None


def _save_tokens(t):
    json.dump(t, open(TOKEN_PATH, "w", encoding="utf-8"))


def _refresh_if_needed():
    t = _load_tokens()
    if not t or "refresh_token" not in t:
        return None
    if t.get("expires_at", 0) > time.time() + 60:
        return t
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": t["refresh_token"],
        "client_id": _client_id(),
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.load(r)
    t["access_token"] = resp["access_token"]
    t["expires_at"] = time.time() + resp.get("expires_in", 3600)
    if "refresh_token" in resp:
        t["refresh_token"] = resp["refresh_token"]
    _save_tokens(t)
    return t


def get_access_token(interactive_cb=None):
    """Return a valid access token, or None.
    interactive_cb(login_fn) is called when the user must log in;
    login_fn(url) opens the browser and blocks until approved/denied."""
    t = _refresh_if_needed()
    if t:
        return t["access_token"]
    if interactive_cb:
        return interactive_cb(_login_flow)
    return None


def _login_flow_single(open_url, client_id):
    """PKCE login with ONE client id."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    manual_exchange._verifier = verifier

    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(q.query)
            if q.path == "/callback" and "code" in params:
                result["code"] = params["code"][0]
                result["state"] = params.get("state", [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>TITANIUM: Spotify connected! "
                                 b"You can close this tab.</h2>")
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        qs = urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        open_url(AUTH_URL + "?" + qs)
        deadline = time.time() + 300
        while time.time() < deadline and "code" not in result:
            time.sleep(0.5)
        if "code" not in result or result.get("state") != state:
            return None
        data = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        }).encode()
        req = urllib.request.Request(TOKEN_URL, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.load(r)
        tok["expires_at"] = time.time() + tok.get("expires_in", 3600)
        _save_tokens(tok)
        return tok.get("access_token")
    finally:
        server.shutdown()
        server.server_close()


API_BASE = "https://api.spotify.com/v1"


def _api_get(path, token, params=None):
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except Exception:
        return None


def get_me(token):
    return _api_get("/me", token)


def get_my_playlists(token, limit=50):
    out = []
    url_params = {"limit": limit, "offset": 0}
    while True:
        d = _api_get("/me/playlists", token, url_params)
        if not d or not d.get("items"):
            break
        for it in d["items"]:
            out.append({
                "name": it.get("name", "?"),
                "id": (it.get("id") or "").split(":")[-1],
                "owner": (it.get("owner") or {}).get("display_name", ""),
                "tracks": it.get("tracks", {}).get("total", 0),
            })
        if d.get("next") and len(out) < 500:
            url_params["offset"] = url_params.get("offset", 0) + limit
        else:
            break
    return out


def get_artists_genres(token, artist_ids):
    """Batch genre lookup: /artists?ids=csv (50 per call).
    Returns {artist_id: [genres]}."""
    out = {}
    ids = [i for i in artist_ids if i]
    for k in range(0, len(ids), 50):
        d = _api_get("/artists", token, {"ids": ",".join(ids[k:k+50])})
        for a in (d or {}).get("artists", []) or []:
            if a and a.get("id"):
                out[a["id"]] = a.get("genres") or []
    return out


def _api_post(path, token, payload):
    """Authorized POST against api.spotify.com. Returns parsed JSON or None."""
    req = urllib.request.Request(API_BASE + path,
                                 data=json.dumps(payload).encode(),
                                 headers={
                                     "Authorization": "Bearer " + token,
                                     "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception:
        return None


def create_playlist(token, user_id, name, description=""):
    """Create a Spotify playlist for the user. Returns the playlist id or None."""
    d = _api_post("/users/" + user_id + "/playlists", token, {
        "name": name, "description": description or "Transfer via SHUNGITE",
        "public": False})
    return d.get("id") if d else None


def add_items(token, playlist_id, uris):
    """Add track URIs to a Spotify playlist. uris = list of 'spotify:track:...'."""
    d = _api_post("/playlists/" + playlist_id + "/tracks", token,
                  {"uris": uris})
    return d is not None


def search_track(token, query, limit=1):
    """Find a Spotify track URI by 'title artist'. Returns spotify URI or None."""
    import urllib.parse as _up
    url = (API_BASE + "/search?" +
           _up.urlencode({"q": query, "type": "track", "limit": limit}))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        items = data.get("tracks", {}).get("items", [])
        return items[0]["uri"] if items else None
    except Exception:
        return None


def get_playlist_tracks(token, playlist_id, limit=500):
    """Returns [(name, artists, duration_str, genre), ...] — genre comes from
    a batched /artists genre lookup over the playlist's unique artists."""
    out = []
    artist_ids = []
    offset = 0
    while len(out) < limit:
        d = _api_get("/playlists/" + playlist_id + "/tracks", token,
                     {"limit": 100, "offset": offset})
        if not d or not d.get("items"):
            break
        for it in d["items"]:
            tr = it.get("track") or {}
            if not tr:
                continue   # local-file / removed entries
            name = tr.get("name")
            if not name:
                continue
            arts = tr.get("artists") or []
            artists = ", ".join(a.get("name", "") for a in arts)
            ms = tr.get("duration_ms") or 0
            dur = f"{ms // 60000}:{(ms // 1000) % 60:02d}" if ms else ""
            first_id = (arts[0].get("id") if arts else "") or ""
            if first_id:
                artist_ids.append(first_id)
            out.append((name, artists, dur, first_id))
        if not d.get("next"):
            break
        offset += 100
    genres = {}
    try:
        genres = get_artists_genres(token, set(artist_ids)) or {}
    except Exception:
        genres = {}
    return [(n, a, du, (genres.get(aid) or [""])[0] if (genres.get(aid)) else "")
            for (n, a, du, aid) in out]


def get_liked(token, limit=2000):
    """User's saved (liked) tracks. Returns [(name, artists, dur, genre)]"""
    out = []
    artist_ids = []
    offset = 0
    while len(out) < limit:
        d = _api_get("/me/tracks", token, {"limit": 50, "offset": offset})
        if not d or not d.get("items"):
            break
        for it in d["items"]:
            tr = it.get("track") or {}
            name = tr.get("name")
            if not name:
                continue
            arts = tr.get("artists") or []
            artists = ", ".join(a.get("name", "") for a in arts)
            ms = tr.get("duration_ms") or 0
            dur = f"{ms // 60000}:{(ms // 1000) % 60:02d}" if ms else ""
            first_id = (arts[0].get("id") if arts else "") or ""
            if first_id:
                artist_ids.append(first_id)
            out.append((name, artists, dur, first_id))
        if not d.get("next"):
            break
        offset += 50
    genres = {}
    try:
        genres = get_artists_genres(token, set(artist_ids)) or {}
    except Exception:
        genres = {}
    return [(n, a, du, (genres.get(aid) or [""])[0] if (genres.get(aid)) else "")
            for (n, a, du, aid) in out]


def parse_callback_url(url_or_code):
    """Parse a full redirect URL the user pasted (or a bare code).
    Returns (code, state) or (None, None)."""
    url_or_code = url_or_code.strip()
    if "code=" not in url_or_code:
        # maybe a bare code
        if len(url_or_code) > 20 and " " not in url_or_code:
            return url_or_code, None
        return None, None
    try:
        q = urllib.parse.urlparse(url_or_code)
        params = urllib.parse.parse_qs(q.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        return code, state
    except Exception:
        return None, None


def manual_exchange(code):
    """Exchange an authorization code obtained outside the local server.
    Returns access token or None."""
    verifier = getattr(manual_exchange, "_verifier", None)
    if not verifier:
        return None
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": _client_id(),
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.load(r)
        tok["expires_at"] = time.time() + tok.get("expires_in", 3600)
        _save_tokens(tok)
        return tok.get("access_token")
    except Exception:
        return None
