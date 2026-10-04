"""
Spotify access for PEAK_PREMIUM — reverse-engineered web-player flow
( same approach as the Spotube plugin: anonymous TOTP token + GQL API ).

NO LOGIN NEEDED for recommendations / browse / search / playlist tracks:
  1. TOTP secret from the public nuances.json (rotated by the community)
  2. anonymous access token from open.spotify.com/api/token
  3. GQL queries against api-partner.spotify.com/pathfinder/v2/query
     with persisted-query hashes (from the open-source spotube plugin)

Optional OAuth login (PKCE) still exists for "Your playlists" (personal
library needs a real session). Shared community client ID embedded.
"""

import base64
import hashlib
import hmac
import http.cookiejar
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

def detect_deno():
    for c in [
        os.path.expanduser("~/.deno/bin/deno.exe"),
        os.path.expanduser("~/.deno/bin/deno"),
        "/usr/local/bin/deno",
        "deno",
    ]:
        if os.path.exists(c):
            return c
    return None


TOKEN_PATH = os.path.join(os.path.expanduser("~"), ".peak_spotify_token.json")
AUTH_PORT = 43017
REDIRECT_URI = f"http://127.0.0.1:{AUTH_PORT}/callback"
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read user-read-email"
API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
GQL = "https://api-partner.spotify.com/pathfinder/v2/query"
NUANCES_URL = "https://gist.githubusercontent.com/raw/22ed9c6ba463899e933427f7de1f0eef/nuances.json"
WEB_CLIENT_ID = "d8a5ed958d274a2e8ee717e6a4b0941d"   # web-player public id (GQL header)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0"

# persisted-query hashes (from spotube-plugin-spotify, AGPL)
HASH_HOME = "3357ffed7961629ba92b4e0a41514e4d5004a14355c964c23ce442205c9e44a1"
HASH_HOME_SECTION = "d62af2714f2623c923cc9eeca4b9545b4363abaa9188a9e94e2b63b823419a2c"
HASH_PLAYLIST = "cd2275433b29f7316176e7b5b5e098ae7744724e1a52d63549c76636b3257749"
HASH_SEARCH = "d9f785900f0710b31c07818d617f4f7600c1e21217e80f5b043d1e78d74e6026"

DEFAULT_CLIENT_ID = "99e1d0f6770d406ba1e3aa5b37c5d0b1"   # shared community ID (Exportify's public one)
SP_DC_PATH = os.path.join(os.path.expanduser("~"), ".peak_sp_dc")


def get_sp_dc_from_cookies_file(path=None):
    """Extract sp_dc from a Netscape cookies.txt export (like the user's
    'Get cookies.txt LOCALLY' Firefox plugin output)."""
    import glob as _glob
    candidates = [path]
    # newest-first scan of EVERY cookies*.txt export in Downloads
    # (covers cookies.txt, cookies(2)...cookies(N).txt from any browser plugin)
    found = sorted(_glob.glob(os.path.expanduser("~/Downloads/cookies*.txt")),
                   key=os.path.getmtime, reverse=True)
    candidates += found
    for p in candidates:
        if not p or not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "\tsp_dc\t" in line:
                        val = line.strip().split("\t")[6]
                        if val:
                            return val
        except Exception:
            continue
    return None


def get_sp_dc_from_firefox():
    """Read the sp_dc cookie from Firefox's cookie DB (user must be logged
    into spotify.com in Firefox). Returns the cookie value or None."""
    import sqlite3, glob, shutil, tempfile
    for cookies in glob.glob(os.path.expanduser(
            "~/AppData/Roaming/Mozilla/Firefox/Profiles/*/cookies.sqlite")):
        tmp = tempfile.mktemp(suffix=".sqlite")
        shutil.copy2(cookies, tmp)
        try:
            db = sqlite3.connect(tmp)
            rows = db.execute(
                "SELECT value FROM moz_cookies WHERE host LIKE '%spotify.com%' "
                "AND name='sp_dc' AND expires > strftime('%s','now')"
            ).fetchall()
            db.close()
            if rows:
                return rows[0][0]
        except Exception:
            pass
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    return None


def login_via_browser(timeout=300):
    """Embedded browser login (exactly like Spotube):
    Opens spotify.com in an in-app webview. User logs in with Google/email/
    Facebook/Apple. Polls the webview's NATIVE cookie store (get_cookies)
    for the httpOnly sp_dc cookie. document.cookie can't see httpOnly
    cookies — we MUST use window.get_cookies()."""
    stored = stored_sp_dc()
    if stored:
        return True
    # Try cookies.txt files first (most reliable — user already has them)
    dc = get_sp_dc_from_cookies_file()
    if dc:
        with open(SP_DC_PATH, "w") as f:
            f.write(dc)
        return True
    # Then Firefox's cookie DB
    dc = get_sp_dc_from_firefox()
    if dc:
        with open(SP_DC_PATH, "w") as f:
            f.write(dc)
        return True

    try:
        import webview
    except ImportError:
        return _login_via_system_browser(timeout)

    result = {"sp_dc": None, "done": False}

    def poll_cookies(window):
        """Poll native cookie store every 2s — get_cookies() returns a list of
        {cookie_name: Morsel} dicts. The Morsel's .value holds the actual value.
        This sees httpOnly cookies (which document.cookie cannot)."""
        import time as _t
        while not result["done"]:
            _t.sleep(2)
            try:
                for c in window.get_cookies():
                    for key, morsel in c.items():
                        if key == "sp_dc":
                            val = getattr(morsel, "value", "") or str(morsel).split("=")[1].split(";")[0] if "=" in str(morsel) else ""
                            if val:
                                result["sp_dc"] = val
                                result["done"] = True
                                try:
                                    window.destroy()
                                except Exception:
                                    pass
                                return
            except Exception:
                result["done"] = True
                return

    window = webview.create_window(
        "Login to Spotify — Google · Email · Facebook · Apple",
        "https://accounts.spotify.com/en/login",
        width=500, height=700,
    )

    import threading as _th
    _th.Thread(target=poll_cookies, args=(window,), daemon=True).start()
    webview.start()

    if result["sp_dc"]:
        with open(SP_DC_PATH, "w") as f:
            f.write(result["sp_dc"])
        return True
    return False


def _login_via_system_browser(timeout=180):
    """Fallback: open spotify.com in system browser, poll all browser cookie
    stores (Firefox, Chrome, Edge, Brave) for sp_dc."""
    webbrowser.open("https://accounts.spotify.com/en/login")

    def check_all_browsers():
        dc = get_sp_dc_from_firefox()
        if dc:
            return dc
        import sqlite3, shutil, tempfile
        for path in _chromium_cookie_paths():
            if not os.path.exists(path):
                continue
            tmp = tempfile.mktemp(suffix=".sqlite")
            try:
                shutil.copy2(path, tmp)
                db = sqlite3.connect(tmp)
                rows = db.execute(
                    "SELECT encrypted_value FROM cookies "
                    "WHERE host_key LIKE '%spotify.com%' AND name='sp_dc'"
                ).fetchall()
                db.close()
                if rows:
                    dec = _dpapi_decrypt(rows[0][0])
                    if dec:
                        return dec
            except Exception:
                pass
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        val = check_all_browsers()
        if val:
            with open(SP_DC_PATH, "w") as f:
                f.write(val)
            return True
    return False


def _chromium_cookie_paths():
    """Cookie DB paths for Chrome, Edge, Brave (all profiles)."""
    home = os.path.expanduser("~")
    patterns = [
        # Chrome
        f"{home}/AppData/Local/Google/Chrome/User Data/*/Network/Cookies",
        f"{home}/AppData/Local/Google/Chrome/User Data/*/Cookies",
        # Edge
        f"{home}/AppData/Local/Microsoft/Edge/User Data/*/Network/Cookies",
        f"{home}/AppData/Local/Microsoft/Edge/User Data/*/Cookies",
        # Brave
        f"{home}/AppData/Local/BraveSoftware/Brave-Browser/User Data/*/Network/Cookies",
    ]
    out = []
    for pat in patterns:
        out.extend(glob.glob(pat))
    return out


def _dpapi_decrypt(encrypted_bytes):
    """Decrypt Chromium DPAPI-encrypted cookie value on Windows."""
    try:
        import ctypes
        import ctypes.wintypes
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]
        buf = ctypes.create_string_buffer(encrypted_bytes, len(encrypted_bytes))
        blob_in = DATA_BLOB(len(encrypted_bytes), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        if ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
            data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return data.decode("utf-8", errors="replace").rstrip("\x00")
    except Exception:
        pass
    return None


def stored_sp_dc():
    """Load stored sp_dc; if absent/stale, extract LIVE from any logged-in
    browser (Firefox plaintext / Chromium decrypted) - no manual export."""
    # 1. cached copy
    try:
        dc = open(SP_DC_PATH).read().strip()
        if dc:
            return dc
    except Exception:
        pass
    # 2. live browser scan (all browsers, no user action needed)
    try:
        import browser_cookies as bc
        dc = bc.get_sp_dc()
        if not dc:
            # fall back to the Downloads cookies*.txt exports (newest first)
            dc = get_sp_dc_from_cookies_file()
        if dc:
            with open(SP_DC_PATH, "w") as f:
                f.write(dc)
            return dc
    except Exception:
        pass
    # 3. legacy Firefox-only path
    dc = get_sp_dc_from_firefox()
    if dc:
        with open(SP_DC_PATH, "w") as f:
            f.write(dc)
    return dc or ""


def logout_sp_dc():
    try:
        os.remove(SP_DC_PATH)
    except Exception:
        pass


def logged_in_token():
    """Get a LOGGED-IN access token (personal recommendations) using sp_dc.
    Falls back to anonymous if no sp_dc stored."""
    dc = stored_sp_dc()
    if not dc:
        return anon_token()  # anonymous fallback

    # refresh from Firefox if possible (sp_dc rotates)
    fresh = get_sp_dc_from_firefox()
    if fresh:
        dc = fresh
        with open(SP_DC_PATH, "w") as f:
            f.write(dc)

    nuances = json.loads(_get(NUANCES_URL).decode())
    n = max(nuances, key=lambda x: x["v"])
    st = json.loads(_get("https://open.spotify.com/api/server-time").decode())["serverTime"]
    tp = _totp(n["s"], st)
    url = (f"https://open.spotify.com/api/token?reason=transport&productType=web-player"
           f"&totp={tp}&totpServer={tp}&totpVer={n['v']}")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Cookie": f"sp_dc={dc};",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            tok = json.load(r)
        _anon["token"] = tok["accessToken"]
        _anon["expires"] = tok.get("accessTokenExpirationTimestampMs", 0) / 1000
        _anon["logged_in"] = not tok.get("isAnonymous", True)
        return tok["accessToken"]
    except Exception:
        return anon_token()


class SpotifyError(Exception):
    pass


def _get(url, headers=None, retries=3):
    """GET with 429 Retry-After + 5xx exponential backoff."""
    import time as _t
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url, headers=headers or {"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = e.headers.get("Retry-After")
                delay = float(wait) if (wait or "").replace(".", "",
                                                           1).isdigit() \
                    else min(30.0, 2 ** attempt * 2)
                _t.sleep(delay)
                continue
            if 500 <= e.code < 600:
                _t.sleep(min(15.0, 2 ** attempt))
                continue
            raise
    raise last_err


# ══════════════════════════════════════════════════════════════════════════════
#  ANONYMOUS MODE — no login, reverse-engineered web-player flow
# ══════════════════════════════════════════════════════════════════════════════

_anon = {"token": None, "expires": 0.0, "sp_t": ""}


def _totp(secret_b32, server_time):
    key = base64.b32decode(secret_b32)
    dig = hmac.new(key, (server_time // 30).to_bytes(8, "big"), hashlib.sha1).digest()
    o = dig[-1] & 0x0F
    return str((int.from_bytes(dig[o:o + 4], "big") & 0x7FFFFFFF) % 10**6).zfill(6)


def anon_token(force=False):
    """Anonymous web-player access token (TOTP-signed). Cached until expiry."""
    if not force and _anon["token"] and time.time() < _anon["expires"] - 60:
        return _anon["token"]
    nuances = json.loads(_get(NUANCES_URL).decode())
    n = max(nuances, key=lambda x: x["v"])
    server_time = json.loads(_get("https://open.spotify.com/api/server-time").decode())["serverTime"]
    tp = _totp(n["s"], server_time)
    url = (f"https://open.spotify.com/api/token?reason=transport&productType=web-player"
           f"&totp={tp}&totpServer={tp}&totpVer={n['v']}")
    tok = json.loads(_get(url).decode())
    _anon["token"] = tok["accessToken"]
    _anon["expires"] = tok.get("accessTokenExpirationTimestampMs", 0) / 1000
    if not _anon["sp_t"]:
        try:
            cj = http.cookiejar.CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            op.open(urllib.request.Request("https://open.spotify.com/",
                                           headers={"User-Agent": UA}), timeout=25).read(200)
            _anon["sp_t"] = next((c.value for c in cj if c.name == "sp_t"), "")
        except Exception:
            pass
    return _anon["token"]


def _gql(operation, variables, sha, force_new_token=False):
    body = {
        "operationName": operation,
        "variables": variables,
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha}},
    }
    # Use logged-in token when available (personal recommendations)
    bearer = logged_in_token() if stored_sp_dc() else anon_token(force=force_new_token)
    h = {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": "https://open.spotify.com",
        "Referer": "https://open.spotify.com/",
        "client-id": WEB_CLIENT_ID,
    }
    req = urllib.request.Request(GQL, data=json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401 and not force_new_token:
            return _gql(operation, variables, sha, force_new_token=True)  # token went stale
        raise SpotifyError(f"GQL {e.code}: {e.read(150).decode(errors='replace')}")


def _extract_playlist(item):
    """Normalize a GQL playlist entity -> dict(id, name, owner, tracks, uri)."""
    d = item.get("data") or item
    uri = d.get("uri") or ""
    pid = d.get("id") or (uri.split(":")[-1] if uri else "")
    if not pid:
        return None
    return {
        "id": pid,
        "uri": uri or f"spotify:playlist:{pid}",
        "name": d.get("name") or "(unnamed)",
        "owner": ((d.get("owner") or {}).get("name")
                  or ((d.get("owner") or {}).get("data") or {}).get("name") or ""),
        "tracks": (d.get("trackCount")
                   or (((d.get("tracks") or {}).get("totalCount"))
                       if isinstance(d.get("tracks"), dict) else None) or ""),
        "description": d.get("description") or "",
    }


def home_sections(limit=20):
    """The REAL Spotify home feed — recommendation rows, no login needed.
    Returns [(section_title, [playlist_dict, ...]), ...] (sections with
    playlists only; pure-track rows are skipped)."""
    tz = time.tzname[0] or "UTC"
    data = _gql("home", {"timeZone": tz, "sp_t": _anon["sp_t"], "facet": "",
                         "sectionItemsLimit": limit}, HASH_HOME)
    try:
        items = data["data"]["home"]["sectionContainer"]["sections"]["items"]
    except (KeyError, TypeError):
        return []
    out = []
    for s in items:
        title_data = (s.get("data") or {}).get("title") or {}
        title = (title_data.get("transformedLabel")
                 or title_data.get("text")
                 or title_data.get("translatedBaseText")
                 or "More")
        rows = []
        for it in (s.get("sectionItems") or {}).get("items", []):
            content = it.get("content") or {}
            tn = content.get("__typename", "")
            d = content.get("data") or {}
            if "Playlist" in tn or d.get("__typename") == "Playlist":
                pl = _extract_playlist(d)
                if pl:
                    rows.append(pl)
        if rows:
            out.append((title, rows))
    return out


def browse_all():
    """All home sections flattened + tagged."""
    return home_sections(limit=30)


def playlist_tracks(pl_uri_or_id, limit=500):
    """Track list of any public playlist — [(title, artists, duration), ...]."""
    uri = (pl_uri_or_id if pl_uri_or_id.startswith("spotify:")
           else f"spotify:playlist:{pl_uri_or_id}")
    out = []
    offset = 0
    while True:
        data = _gql("fetchPlaylist",
                    {"uri": uri, "offset": offset, "limit": 50,
                     "enableWatchFeedEntrypoint": False},
                    HASH_PLAYLIST)
        try:
            pl = data["data"]["playlistV2"]
        except (KeyError, TypeError):
            raise SpotifyError("Playlist not found")
        content = pl.get("content") or {}
        raw_items = content.get("items") or []
        if isinstance(raw_items, dict):          # some variants wrap in a page object
            raw_items = raw_items.get("items") or []
        got = 0
        for it in raw_items:
            t = (((it.get("itemV2") or {}).get("data"))
                 or ((it.get("content") or {}).get("data")) or {})
            if t.get("__typename") not in ("Track", "LocalTrack"):
                continue
            name = t.get("name") or "?"
            arts = (t.get("artists") or [])
            if isinstance(arts, dict):
                arts = arts.get("items", [])
            artists = ", ".join((a.get("profile") or {}).get("name") or a.get("name", "")
                                for a in arts)
            ms = (t.get("trackDuration") or {}).get("totalMilliseconds", 0)
            m, s = divmod(ms // 1000, 60)
            out.append((name, artists, f"{m}:{s:02d}"))
            got += 1
        offset += 50
        total = content.get("totalCount") or 0
        if got == 0 or offset >= min(total, limit):
            break
    return out


def search_playlists(q, limit=20):
    data = _gql("searchDesktop",
                {"searchTerm": q, "offset": 0, "limit": limit, "numberOfTopResults": 5,
                 "includeAudiobooks": False, "includeArtistHasConcertsField": False,
                 "includePreReleases": False, "includeLocalConcertsField": False},
                HASH_SEARCH)
    try:
        tabs = data["data"]["searchV2"]["playlists"] \
                    .get("items", [])
    except (KeyError, TypeError):
        return []
    out = []
    for it in tabs:
        d = (it.get("data") or {})
        pl = _extract_playlist(d)
        if pl:
            out.append(pl)
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  OPTIONAL OAUTH (for "Your playlists" — personal library)
# ══════════════════════════════════════════════════════════════════════════════

def load_tokens():
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_tokens(t):
    try:
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump(t, f)
    except Exception:
        pass


def logout():
    try:
        os.remove(TOKEN_PATH)
    except Exception:
        pass


def _pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def login(client_id: str = "", blocking: bool = True, timeout: int = 240):
    """Optional PKCE login (for personal playlists). Always uses the shared
    community ID (any custom ID without our redirect registered fails)."""
    client_id = DEFAULT_CLIENT_ID
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    auth_url = (f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": SCOPES,
    }))

    code_holder = {}

    class CB(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if self.path.startswith("/callback"):
                code_holder["code"] = (qs.get("code") or [None])[0]
                code_holder["state"] = (qs.get("state") or [None])[0]
                code_holder["error"] = (qs.get("error") or [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>Login captured - you can close this tab.</h2>")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", AUTH_PORT), CB)
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()

    webbrowser.open(auth_url)
    t.join(timeout=timeout)
    srv.server_close()

    if not code_holder.get("code"):
        raise SpotifyError("Login timed out or was cancelled."
                        + (f" ({code_holder.get('error')})" if code_holder.get("error") else ""))
    if code_holder.get("state") != state:
        raise SpotifyError("State mismatch - aborting.")

    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code_holder["code"],
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(f"{ACCOUNTS}/api/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.load(r)
    tok["client_id"] = client_id
    tok["obtained_at"] = time.time()
    save_tokens(tok)
    return tok


def _access_token():
    tok = load_tokens()
    if not tok:
        raise SpotifyError("Not logged in (optional — recommendations work without it)")
    if time.time() - tok.get("obtained_at", 0) > tok.get("expires_in", 3600) - 60:
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": tok.get("refresh_token"),
            "client_id": tok.get("client_id"),
        }).encode()
        req = urllib.request.Request(f"{ACCOUNTS}/api/token", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            new = json.load(r)
        new.setdefault("refresh_token", tok.get("refresh_token"))
        new["client_id"] = tok.get("client_id")
        new["obtained_at"] = time.time()
        save_tokens(new)
        tok = new
    return tok["access_token"]


def me():
    req = urllib.request.Request(API + "/me", headers={
        "Authorization": f"Bearer {_access_token()}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def my_playlists(limit=50):
    req = urllib.request.Request(
        API + "/me/playlists?" + urllib.parse.urlencode({"limit": limit}),
        headers={"Authorization": f"Bearer {_access_token()}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("items", [])


def _oauth_get_json(path, params=None):
    """Authorized GET against api.spotify.com using stored token."""
    import urllib.parse as _up
    tok = None
    try:
        import spotify_oauth as _so
        tok = _so.get_access_token(None)
    except Exception:
        tok = None
    if not tok:
        return None
    url = "https://api.spotify.com/v1/" + path
    if params:
        url += "?" + _up.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + tok,
        "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except Exception:
        return None


def album_tracks(album_id, limit=50):
    """Returns list of {track, artists} for an album id (OAuth)."""
    d = _oauth_get_json(f"albums/{album_id}/tracks",
                        {"limit": limit, "market": "from_token"})
    out = []
    for it in (d or {}).get("items", []):
        arts = ", ".join(a.get("name", "") for a in it.get("artists", []))
        out.append({"title": it.get("name", "?"), "artist": arts,
                    "seconds": int((it.get("duration_ms") or 0) // 1000)})
    return out


def artist_top_tracks(artist_id, market="US", limit=10):
    """Returns list of {title, artist, seconds} for an artist's top tracks."""
    d = _oauth_get_json(f"artists/{artist_id}/top-tracks",
                        {"market": market})
    out = []
    for it in (d or {}).get("tracks", [])[:limit]:
        arts = ", ".join(a.get("name", "") for a in it.get("artists", []))
        out.append({"title": it.get("name", "?"), "artist": arts,
                    "seconds": int((it.get("duration_ms") or 0) // 1000)})
    return out
