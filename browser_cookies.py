"""Universal browser-cookie extraction for Windows v2 (PEAK).

Covers EVERY major browser, no manual exports, no admin rights:
  Firefox / forks        : plaintext values in cookies.sqlite
  Chromium family        : Chrome, Edge, Brave, Opera, Opera GX, Vivaldi,
                           Chromium, Arc - discovered generically, values
                           decrypted via DPAPI-protected os_crypt key
                           (v10/v11) with best-effort app-bound (v20) support
  CDP oracle fallback    : for stubborn v20 profiles, the browser itself is
                           launched headless on a COPY of its profile and
                           asked for its cookies over the DevTools protocol -
                           the browser decrypts its own cookies, so no key
                           cracking, no SYSTEM tricks, no UAC prompt.
"""
import base64
import glob
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request

# ---------------------------------------------------------------- discovery
# no console flash for child processes (windowed exe)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _firefox_dbs():
    pats = [
        "~/AppData/Roaming/Mozilla/Firefox/Profiles/*/cookies.sqlite",
        "~/AppData/Roaming/Waterfox/Profiles/*/cookies.sqlite",
        "~/AppData/Roaming/librewolf/Profiles/*/cookies.sqlite",
        "~/AppData/Local/Pale Moon/Profiles/*/cookies.sqlite",
    ]
    out = []
    for p in pats:
        out += glob.glob(os.path.expanduser(p))
    return out

def _exe_candidates(*subpaths):
    """Full search list for one browser exe: Program Files + LocalAppData."""
    home = os.path.expanduser("~")
    local = os.path.join(home, "AppData", "Local")
    out = []
    for base in (r"C:\Program Files", r"C:\Program Files (x86)", local):
        for sub in subpaths:
            out.append(os.path.join(base, sub))
    return out

def _chromium_roots():
    """[(name, user_data_root, exe_candidates)] - explicit + generic sweep."""
    home = os.path.expanduser("~")
    local = os.path.join(home, "AppData", "Local")
    roam = os.path.join(home, "AppData", "Roaming")
    named = [
        ("chrome",   os.path.join(local, r"Google\Chrome\User Data"),
         _exe_candidates(r"Google\Chrome\Application\chrome.exe")),
        ("edge",     os.path.join(local, r"Microsoft\Edge\User Data"),
         _exe_candidates(r"Microsoft\Edge\Application\msedge.exe")),
        ("brave",    os.path.join(local, r"BraveSoftware\Brave-Browser\User Data"),
         _exe_candidates(r"BraveSoftware\Brave-Browser\Application\brave.exe")),
        ("opera",    os.path.join(roam, r"Opera Software\Opera Stable"),
         _exe_candidates(r"Opera Software\Opera Stable\opera.exe")),
        ("opera_gx", os.path.join(roam, r"Opera Software\Opera GX Stable"),
         _exe_candidates(r"Opera Software\Opera GX Stable\opera.exe")),
        ("vivaldi",  os.path.join(local, r"Vivaldi\User Data"),
         _exe_candidates(r"Vivaldi\Application\vivaldi.exe")),
        ("chromium", os.path.join(local, r"Chromium\User Data"),
         _exe_candidates(r"Chromium\Application\chrome.exe")),
        ("arc",      os.path.join(local, r"Packages\TheBrowserCompany.Arc\tmp\Local\Arc\User Data"),
         []),
    ]
    roots = [(n, r, e) for n, r, e in named if os.path.isdir(r)]
    # generic sweep for other Chromium forks: <root>\User Data with Local State
    for base in (local, roam):
        for ud in glob.glob(os.path.join(base, "*", "User Data")):
            if any(r == ud for _, r, _ in roots):
                continue
            if os.path.exists(os.path.join(ud, "Local State")):
                roots.append((os.path.basename(os.path.dirname(ud)).lower(),
                              ud, []))
    return roots

def _profile_credential_sets(root):
    """[(cookies_db_path, profile_dir_name)] newest first."""
    out = []
    for pat in (os.path.join(root, "*", "Network", "Cookies"),
                os.path.join(root, "Default", "Cookies")):
        for db in glob.glob(pat):
            prof = os.path.basename(os.path.dirname(
                os.path.dirname(db) if "Network" in db else db))
            out.append((db, prof))
    seen, uniq = set(), []
    for db, prof in sorted(out, key=lambda x: os.path.getmtime(x[0]),
                           reverse=True):
        if db not in seen:
            seen.add(db)
            uniq.append((db, prof))
    return uniq

# ------------------------------------------------------------- crypto utils

def _dpapi(data):
    import ctypes
    import ctypes.wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    bi = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    bo = BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo)):
        return None
    out = ctypes.string_at(bo.pbData, bo.cbData)
    ctypes.windll.kernel32.LocalFree(bo.pbData)
    return out

def _local_state_path(root):
    """Return the Chromium/WebView2 'Local State' path, searching the profile
    root and one level down (EBWebView keeps it in its own subfolder)."""
    import glob as _g
    candidates = [os.path.join(root, "Local State")]
    candidates += [os.path.join(root, d, "Local State")
                   for d in ("EBWebView", "User Data", "Default")]
    for c in candidates:
        if os.path.isfile(c):
            return c
    hits = _g.glob(os.path.join(root, "**", "Local State"), recursive=True)
    return hits[0] if hits else None


def _os_crypt_key(root):
    try:
        ls = _local_state_path(root)
        if not ls:
            return None
        with open(ls, encoding="utf-8") as f:
            data = json.load(f)
        blob = base64.b64decode(data["os_crypt"]["encrypted_key"])
        return _dpapi(blob[5:]) if blob[:5] == b"DPAPI" else None
    except Exception:
        return None

def _app_bound_key_best_effort(root):
    """Try user-level unwrap of app_bound_encrypted_key (works on some
    Edge/Brave builds; Chrome usually needs the CDP oracle instead)."""
    try:
        ls = _local_state_path(root)
        if not ls:
            return None
        with open(ls, encoding="utf-8") as f:
            data = json.load(f)
        blob = base64.b64decode(data["os_crypt"]["app_bound_encrypted_key"])
        out = _dpapi(blob)
        if not out:
            return None
        if out[:4] == b"APPB":
            out = out[4:]
        return out[-32:] if len(out) >= 32 else None
    except Exception:
        return None

def _aesgcm(key, nonce, ct, tag=None):
    from Crypto.Cipher import AES
    c = AES.new(key, AES.MODE_GCM, nonce=nonce)
    if tag:
        return c.decrypt_and_verify(ct, tag)
    return c.decrypt(ct)

def _looks_like_value(pt, domain_hash_expected=False):
    if not pt:
        return False
    body = pt[32:] if domain_hash_expected else pt
    if not body:
        return False
    printable = sum(1 for b in body if 32 <= b < 127)
    return printable / len(body) > 0.85

def _decrypt(enc, key, ab_key=None):
    if not enc:
        return ""
    if enc[:3] in (b"v10", b"v11"):
        try:
            pt = _aesgcm(key, enc[3:15], enc[15:-16], enc[-16:])
            # Chrome/Edge "application-bound" protection prepends a 32-byte
            # SHA256(domain) hash to the plaintext of v10/v11 cookies. Strip it
            # when present; otherwise leave the (already clean) value intact.
            _allowed = set(
                b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                b"0123456789_./-=|%:;")
            if len(pt) > 32 and all(b in _allowed for b in pt[32:]):
                pt = pt[32:]
            # Purge any value that still carries non-ASCII/garbage (a failed
            # or partial decrypt). The essential auth cookies (SAPISID/SID/
            # HSID/SSID/__Secure-*PSID) decrypt cleanly; junk ones are dropped
            # so yt-dlp never sees an invalid byte sequence.
            if any(b < 32 or b > 126 for b in pt):
                return ""
            return pt.decode("utf-8", "replace")
        except Exception:
            return ""
    if enc[:3] == b"v20":
        # app-bound: try best-effort key; plaintext may carry a 32-byte
        # SHA256(domain) prefix - accept either layout if printable.
        for k in (ab_key, None):
            if not k:
                continue
            for strip_hash in (True, False):
                try:
                    pt = _aesgcm(k, enc[3:15], enc[15:-16], enc[-16:])
                    cand = pt[32:] if strip_hash else pt
                    if _looks_like_value(pt, strip_hash):
                        return cand.decode("utf-8", "replace")
                except Exception:
                    continue
        raise _V20()          # signal caller: needs the oracle
    if enc[:1] == b"\x01":
        out = _dpapi(enc)
        return out.decode("utf-8", "replace") if out else ""
    return enc.decode("utf-8", "replace")

class _V20(Exception):
    pass

# ------------------------------------------------------------ raw db access

def _raw_copy(src):
    tmp = tempfile.mktemp(suffix=".db")
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        if os.path.getsize(tmp) > 0:
            return tmp
    except Exception:
        pass
    try:
        os.unlink(tmp)
    except Exception:
        pass
    return None

_VSS_PS = r'''
$class = [WMICLASS]"root\cimv2:Win32_ShadowCopy"
$null = $class.Create("C:\", "ClientAccessible")
Start-Sleep -Seconds 2
$sc = Get-CimInstance Win32_ShadowCopy | Sort-Object InstallDate -Descending | Select-Object -First 1
$src = "$($sc.DeviceObject)\__SRC__"
try {
    [System.IO.File]::Copy($src, "__DST__", $true)
    Write-Host "COPIED"
} catch {
    Write-Host "ERR: $($_.Exception.Message)"
}
Remove-CimInstance -InputObject $sc -ErrorAction SilentlyContinue
'''

def _vss_copy(src):
    """Copy an exclusively-locked file via a Volume Shadow Copy snapshot.
    Requires elevation (the app usually runs elevated on user machines).
    `src` must be on C:. Returns a temp-file path or None."""
    # __SRC__ is relative to the VOLUME ROOT (no drive letter!)
    rel = os.path.normpath(src)
    if len(rel) > 2 and rel[1:3] == ":\\":
        rel = rel[3:]                      # strip "C:\"
    rel = rel.replace("\\", "\\")
    dst = tempfile.mktemp(suffix=".db")
    ps = _VSS_PS.replace("__SRC__", rel).replace("__DST__", dst)
    try:
        r = subprocess.run(["powershell", "-Command", ps],
                           capture_output=True, text=True, timeout=150,
                           creationflags=CREATE_NO_WINDOW)
        if os.path.exists(dst) and os.path.getsize(dst) > 0 and \
                "COPIED" in (r.stdout or ""):
            return dst
    except Exception:
        pass
    try:
        os.unlink(dst)
    except Exception:
        pass
    return None

def _copy_any(src):
    """Raw copy first; VSS fallback for exclusively-locked files."""
    tmp = _raw_copy(src)
    return tmp if tmp else _vss_copy(src)

def _ff_read(db, host_substr, name):
    tmp = _raw_copy(db)
    if not tmp:
        return None
    try:
        con = sqlite3.connect(tmp)
        row = con.execute(
            "SELECT value FROM moz_cookies WHERE host LIKE ? AND name=? "
            "AND (expiry=0 OR expiry>strftime('%s','now'))",
            (f"%{host_substr}%", name)).fetchone()
        con.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass

# ------------------------------------------------------------- CDP oracle

_WS_RECV_BUF = 1 << 22

class _MiniWS:
    """Smallest possible websocket client for one request/response."""

    def __init__(self, host, port, path):
        self.s = socket.create_connection((host, port), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               "Origin: http://localhost\r\n\r\n")
        self.s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.s.recv(4096)
            if not chunk:
                break
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError("ws upgrade refused")
        self.buf = resp.split(b"\r\n\r\n", 1)[1]

    def send_text(self, text):
        payload = text.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += int(n).to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += int(n).to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.s.sendall(bytes(header) + mask + masked)

    def recv_msg(self):
        while True:
            hdr = self._read(2)
            fin_op = hdr[0]
            ln = hdr[1] & 0x7F
            if ln == 126:
                ln = int.from_bytes(self._read(2), "big")
            elif ln == 127:
                ln = int.from_bytes(self._read(8), "big")
            payload = self._read(ln)
            op = fin_op & 0x0F
            if op == 0x9:            # close
                raise RuntimeError("closed")
            if op == 0x8:
                continue
            if op in (0x1, 0x2) or op == 0x0:
                self.buf += payload
                if fin_op & 0x80:
                    msg, self.buf = self.buf, b""
                    return msg

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(min(_WS_RECV_BUF, max(4096, n - len(self.buf))))
            if not chunk:
                raise RuntimeError("eof")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

_ORACLE_CACHE = {}

def _browser_running(exe):
    try:
        name = os.path.basename(exe)
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        return name.lower() in (r.stdout or "").lower()
    except Exception:
        return False

_CLOSE_TASK_PS = r'''
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c taskkill /IM __EXE__ /T /F"
Register-ScheduledTask -TaskName "__TASK__" -Action $action -Force | Out-Null
Start-ScheduledTask -TaskName "__TASK__"
Start-Sleep -Seconds 3
Unregister-ScheduledTask -TaskName "__TASK__" -Confirm:$false
'''

def _graceful_close(bname):
    """Close a running browser so its v20 cookies can be driven natively.
    Tries direct taskkill first (works when elevated); falls back to a
    scheduled task that runs in the user's interactive session."""
    exe_name = {"brave": "brave.exe", "chrome": "chrome.exe",
                "edge": "msedge.exe", "opera": "opera.exe",
                "opera_gx": "opera.exe", "vivaldi": "vivaldi.exe",
                "chromium": "chrome.exe"}.get(bname, f"{bname}.exe")
    try:
        subprocess.run(["taskkill", "/IM", exe_name, "/T", "/F"],
                       capture_output=True, text=True, timeout=30,
                       creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass
    time.sleep(3)
    if not _browser_running_by_name(exe_name):
        return True
    # fallback: scheduled task (runs in user's session)
    task = f"PEAK_CK_{int(time.time())}"
    ps = _CLOSE_TASK_PS.replace("__EXE__", exe_name).replace("__TASK__", task)
    try:
        subprocess.run(["powershell", "-Command", ps],
                       capture_output=True, text=True, timeout=45,
                       creationflags=CREATE_NO_WINDOW)
        time.sleep(2)
        return not _browser_running_by_name(exe_name)
    except Exception:
        return False

def _browser_running_by_name(exe_name):
    try:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        return exe_name.lower() in (r.stdout or "").lower()
    except Exception:
        return False

def _relaunch_browser(exe):
    try:
        subprocess.Popen([exe], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass

def _cdp_oracle_cookies_real(exe, root, host_filters, timeout_s=50):
    """Drive the REAL profile dir headlessly (browser must be closed).
    Returns {name: [(host, name, value), ...]}."""
    ckey = ("real", root, tuple(sorted(host_filters)))
    if ckey in _ORACLE_CACHE:
        return _ORACLE_CACHE[ckey]
    proc = None
    result = {}
    try:
        try:   # clear crash flag that can block headless start
            os.unlink(os.path.join(root, "CrashpadMetrics-active.pma"))
        except Exception:
            pass
        s = socket.socket(); s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]; s.close()
        start_url = f"https://{host_filters[0].lstrip('.')}/"
        cmd = [exe, "--headless=new", f"--remote-debugging-port={port}",
               "--remote-allow-origins=*", "--no-first-run", "--disable-gpu",
               "--disable-extensions", f"--user-data-dir={root}", start_url]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW)
        ws_url = None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                    targets = json.load(r)
                page = next((t for t in targets if t.get("type") == "page"),
                            None)
                if page:
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:
                time.sleep(1)
        if not ws_url:
            return {}
        path = "/" + ws_url.split("/", 3)[3]
        ws = _MiniWS("127.0.0.1", port, path)
        time.sleep(3)          # let the start page pull cookies into the jar
        ws.send_text(json.dumps({
            "id": 1, "method": "Network.getCookies",
            "params": {"urls": [f"https://{f.lstrip('.')}" for f in
                                host_filters] +
                      [f"https://www.{f.lstrip('.')}" for f in
                       host_filters] +
                      [f"https://accounts.{f.lstrip('.')}" for f in
                       host_filters]}}))
        deadline = time.time() + timeout_s
        cookies = []
        while time.time() < deadline:
            d = json.loads(ws.recv_msg().decode("utf-8", "replace"))
            if d.get("id") == 1:
                cookies = d.get("result", {}).get("cookies", [])
                break
        ws.close()
        for c in cookies:
            h = c.get("domain", "")
            if any(h.endswith(f) or h == f.lstrip(".")
                   for f in host_filters):
                result.setdefault(c.get("name", ""), []).append(
                    (h, c.get("name"), c.get("value")))
        _ORACLE_CACHE[ckey] = result
        return result
    except Exception:
        return result
    finally:
        if proc:
            try:
                proc.kill()
            except Exception:
                pass


def _cdp_oracle_cookies(exe, root, prof, host_filters, timeout_s=45):
    """Launch the browser headless on a COPY of its profile and dump cookies.
    Returns {name_lower: [(host, name, value), ...]} merged dict."""
    ckey = (exe, root, prof, tuple(sorted(host_filters)))
    if ckey in _ORACLE_CACHE:
        return _ORACLE_CACHE[ckey]

    tmp_ud = tempfile.mkdtemp(prefix="peak_ck_")
    proc = None
    result = {}
    try:
        # stage minimal profile
        dst_root = os.path.join(tmp_ud, "User Data")
        os.makedirs(os.path.join(dst_root, "Default", "Network"), exist_ok=True)
        src_ls = os.path.join(root, "Local State")
        if os.path.exists(src_ls):
            shutil.copyfile(src_ls, os.path.join(dst_root, "Local State"))
        src_db = os.path.join(root, prof, "Network", "Cookies")
        if not os.path.exists(src_db):
            src_db = os.path.join(root, prof, "Cookies")
        if not os.path.exists(src_db):
            return {}
        staged = _copy_any(src_db)          # raw first, VSS for exclusive locks
        if not staged:
            return {}
        shutil.move(staged, os.path.join(dst_root, "Default", "Network",
                                         "Cookies"))
        # free port
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        cmd = [exe, "--headless=new", f"--remote-debugging-port={port}",
               "--remote-allow-origins=*", "--no-first-run",
               "--disable-gpu", "--disable-extensions",
               f"--user-data-dir={dst_root}", "about:blank"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW)

        ws_url = None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                    info = json.load(r)
                ws_url = info.get("webSocketDebuggerUrl")
                if ws_url:
                    break
            except Exception:
                time.sleep(0.5)
        if not ws_url:
            return {}

        path = "/" + ws_url.split("/", 3)[3]
        host, sport = "127.0.0.1", port
        ws = _MiniWS(host, sport, path)
        ws.send_text(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        deadline = time.time() + timeout_s
        cookies = []
        while time.time() < deadline:
            msg = ws.recv_msg()
            try:
                d = json.loads(msg.decode("utf-8", "replace"))
            except Exception:
                continue
            if d.get("id") == 1:
                cookies = d.get("result", {}).get("cookies", [])
                break
        ws.close()

        for c in cookies:
            h = c.get("domain", "")
            if any(h.endswith(f) or h == f.lstrip(".") for f in host_filters):
                result.setdefault(c.get("name", ""), []).append(
                    (h, c.get("name"), c.get("value")))
        _ORACLE_CACHE[ckey] = result
        return result
    except Exception:
        return result
    finally:
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(tmp_ud, ignore_errors=True)

# ---------------------------------------------------------------- public API

def get_cookie(host_substr, name):
    """Cookie value from any installed browser, or None."""
    dotname = host_substr.lstrip(".")
    filters = [dotname, "." + dotname]

    # 1. Firefox family (plaintext)
    for db in _firefox_dbs():
        v = _ff_read(db, host_substr, name)
        if v:
            return v

    # 2. Chromium family: direct decrypt (v10/v11, best-effort v20)
    roots = _chromium_roots()
    pending_v20 = []
    for bname, root, exes in roots:
        ab_key = _app_bound_key_best_effort(root)
        for db, prof in _profile_credential_sets(root):
            tmp = _copy_any(db)          # raw first; VSS for exclusive locks
            if not tmp:
                continue
            try:
                con = sqlite3.connect(tmp)
                row = con.execute(
                    "SELECT encrypted_value, value FROM cookies "
                    "WHERE host_key LIKE ? AND name=?",
                    (f"%{host_substr}%", name)).fetchone()
                con.close()
                if not row:
                    continue
                plain, enc = row[1], row[0]
                if isinstance(plain, bytes):
                    plain = plain.decode("latin-1", "ignore")
                if plain and not plain.startswith(("v10", "v11", "v20")):
                    return plain
                key = _os_crypt_key(root)
                try:
                    val = _decrypt(enc or (plain.encode("latin-1", "ignore")
                                           if plain else b""), key, ab_key)
                    if val and not val.startswith(("v10", "v11", "v20")):
                        return val
                except _V20:
                    if exes:
                        pending_v20.append((bname, root, prof, exes))
            except Exception:
                continue
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    # 3. Chromium v20 profiles: CDP oracle with the browser's own exe.
    #    - If the browser is RUNNING we close it via a scheduled task (works
    #      from services/SSH since the task runs in the user's session),
    #      drive the REAL profile headlessly (app-bound key decrypts in
    #      place), then relaunch it. This is invisible to the user beyond a
    #      brief browser restart.
    for bname, root, prof, exes in pending_v20:
        exe = next((e for e in exes if os.path.exists(e)), None)
        if not exe:
            continue
        was_running = _browser_running(exe)
        if was_running and not _graceful_close(bname):
            continue                      # couldn't close; skip this browser
        try:
            got = _cdp_oracle_cookies_real(
                exe, root,
                [host_substr.lstrip("."), "." + host_substr.lstrip(".")])
            for _h, _n, v in got.get(name, []):
                if v:
                    return v
        finally:
            if was_running:
                _relaunch_browser(exe)
    return None

def get_sp_dc():
    return get_cookie("spotify.com", "sp_dc")

_YT_NAMES = ["SID", "HSID", "SSID", "APISID", "SAPISID",
             "__Secure-1PSID", "__Secure-3PSID",
             "__Secure-1PAPISID", "__Secure-3PAPISID"]

def youtube_cookie_header():
    pairs = []
    for n in _YT_NAMES:
        v = get_cookie("youtube.com", n)
        if v:
            pairs.append(f"{n}={v}")
    return "; ".join(pairs)

def has_yt_auth():
    return any(get_cookie("youtube.com", n) for n in ("SAPISID", "__Secure-1PSID"))


def yt_cookies_dict_from_file(path=None):
    """Read the exported Netscape cookie file into a {name: value} dict for
    authenticating InnerTube write endpoints (create playlist, add video)."""
    if not path:
        path = os.path.join(os.path.expanduser("~"), ".peak_yt_cookies.txt")
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    out[parts[5]] = parts[6]
    except Exception:
        pass
    return out



def _webview_cookie_dbs(profile):
    """Find Chromium-style Cookies DBs under a WebView2/pywebview profile."""
    import glob
    hits = []
    for pat in ("**/Network/Cookies", "**/Cookies", "Network/Cookies", "Cookies"):
        for db in glob.glob(os.path.join(profile, pat), recursive=True):
            if os.path.isfile(db) and os.path.getsize(db) > 0:
                hits.append((db, os.path.dirname(db)))
    # de-dup preserving order
    seen = set()
    out = []
    for db, d in hits:
        if db not in seen:
            seen.add(db)
            out.append((db, d))
    return out


def export_webview_cookies(profile, out_file=None, host_filter=("youtube.com", "google.com")):
    """Read cookies from an embedded-browser (WebView2/Chromium) profile and
    write a Netscape cookies.txt for yt-dlp. Returns count written."""
    import sqlite3, tempfile
    key = _os_crypt_key(profile)
    ab_key = _app_bound_key_best_effort(profile)

    rows = []   # list of (host, secure, path, name, value)
    for db, d in _webview_cookie_dbs(profile):
        tmp = _raw_copy(db)
        if not tmp:
            continue
        try:
            con = sqlite3.connect(tmp)
            cur = con.execute(
                "SELECT host_key, name, encrypted_value, value, is_secure, path "
                "FROM cookies")
            for host, name, enc, plain, secure, path in cur.fetchall():
                if not host:
                    continue
                if host_filter and not any(h in host.lower() for h in host_filter):
                    continue
                val = None
                if isinstance(plain, str) and plain and not plain.startswith(("v10", "v11", "v20")):
                    val = plain
                else:
                    try:
                        raw = enc if enc else (plain.encode("latin-1", "ignore") if isinstance(plain, str) else plain)
                        val = _decrypt(raw, key, ab_key)
                    except Exception:
                        val = None
                if val:
                    rows.append((host, "TRUE" if secure else "FALSE", path or "/", name, val))
            con.close()
        except Exception:
            pass
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    if not rows:
        return 0

    # collapse to first value per (host, name)
    dedup = {}
    for host, sec, path, name, val in rows:
        dedup.setdefault((host, name), (sec, path, val))
    rows = [(h, n, sec, path, val) for (h, n), (sec, path, val) in dedup.items()]

    if out_file is None:
        out_file = os.path.join(os.path.expanduser("~"), ".peak_yt_cookies.txt")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for host, name, sec, path, val in rows:
            # Netscape column 2 = include_subdomains: TRUE only for
            # domain cookies (host starts with '.'), FALSE for host-only.
            _inc = "TRUE" if host.startswith(".") else "FALSE"
            _exp = "0"
            f.write("\t".join([host, _inc, path, sec, _exp, name, val]) + "\n")
    # update the config's yt_cookies_file so download_one picks it up
    try:
        import json as _json
        cfgp = os.path.join(os.path.expanduser("~"), ".peak_config.json")
        if os.path.exists(cfgp):
            cfg = _json.load(open(cfgp, encoding="utf-8"))
        else:
            cfg = {}
        cfg["yt_cookies_file"] = out_file
        _json.dump(cfg, open(cfgp, "w", encoding="utf-8"), indent=2)
    except Exception:
        pass
    return len(rows)
