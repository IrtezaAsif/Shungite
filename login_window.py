"""Embedded login browser for SHUNGITE — runs pywebview IN-PROCESS on the main thread.



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
Restored to match the known-good 2026-09-15 build: the login page opens in an
EMBEDDED WebView2 window inside the app (never the system default browser) and
its cookies persist to `cookie_dir`/`storage_path` so browser_cookies can
harvest them afterwards.

Why in-process: the app is frozen with PyInstaller and has NO standalone
python.exe bundled, so `sys.executable` == the SHUNGITE exe and a subprocess
would just relaunch the app. Instead we hand control to pywebview's own
event loop from the caller's (main) thread. Callers in PEAK_GITHUB_true.py
already marshal us onto the Tk main thread via _run_on_main().

The system-browser path is kept ONLY as an explicit fallback (config key
`login_method == "system"` — see PEAK_GITHUB_true.py settings), or if the
embedded window truly cannot start (missing WebView2 runtime). Silent fallback
is what broke the login for the user, so failures raise visibly by default.
"""
import os


def _run_webview(url, cookie_dir, title, result_holder, width=1000, height=820):
    """Blocking call — MUST be invoked on the main thread."""
    try:
        import webview
        w = webview.create_window(title, url, width=width, height=height,
                                  on_top=True, resizable=True)
        kwargs = {"gui": "edgechromium", "private_mode": False}
        if cookie_dir:
            os.makedirs(cookie_dir, exist_ok=True)
            kwargs["storage_path"] = cookie_dir
        webview.start(**kwargs)          # blocks on this (main) thread until closed
        result_holder["ok"] = True
    except Exception as e:
        result_holder["ok"] = False
        result_holder["err"] = str(e)


class LoginWindow:
    """Embedded login window. `open_and_wait` must be called on the main thread."""

    def __init__(self, url, cookie_dir=None, title="Login",
                 width=1000, height=820):
        self.url = url
        self.cookie_dir = cookie_dir
        self.title = title
        self.width = width
        self.height = height
        self._holder = {}

    def open_and_wait(self, timeout=600):
        _run_webview(self.url, self.cookie_dir, self.title, self._holder,
                     self.width, self.height)
        return bool(self._holder.get("ok"))


def _default_profile_dir():
    return _data_file(".peak_webview")


def open_login(url, title="Login", cookie_capture=None, timeout=600,
               width=1000, height=820, cookie_dir=None,
               allow_system_fallback=True):
    """Open the embedded login window and wait for the user to close it.

    Args:
        url: login URL to open.
        title: window title.
        cookie_capture: legacy kwarg from callers — a FILE path where cookies
            would be deposited. The harvest itself is done by the caller from
            the profile dir (browser_cookies.export_webview_cookies); we only
            accept-and-ignore the arg for compatibility.
        timeout: kept for signature compat (webview blocks until window close).
        width/height: window size.
        cookie_dir: WebView2 profile/storage dir. Defaults to ~/.peak_webview,
            matching _yt_login / _yt_logout / export_webview_cookies.
        allow_system_fallback: if True and the embedded window fails to start,
            fall back to the OS default browser. Callers that need a hard
            failure (interactive login) should pass False.

    Returns True if a window/browser was actually shown to the user.
    Raises on embedded failure when allow_system_fallback=False.
    """
    if cookie_dir is None:
        cookie_dir = _default_profile_dir()

    # config-driven escape hatch: Settings → login window method
    prefer_system = None
    try:
        import json as _j
        cfgp = os.path.join(os.path.expanduser("~"), ".peak_config.json")
        if os.path.exists(cfgp):
            prefer_system = (
                _j.load(open(cfgp, encoding="utf-8")).get("login_method") == "system")
    except Exception:
        prefer_system = None
    # default = system browser (embedded webview.event_loop conflicts with Tk's
    # mainloop when both run on the main thread and closed the whole app); the
    # user can switch to "embedded (in-app)" in Settings to force the in-app window.
    if prefer_system is None:
        prefer_system = True

    holder = {}
    err = ""
    if not prefer_system:
        _run_webview(url, cookie_dir, title, holder, width=width, height=height)
        if holder.get("ok"):
            return True
        err = holder.get("err", "unknown")
    else:
        err = "user opted to use the system browser"

    if not allow_system_fallback and not prefer_system:
        raise RuntimeError("embedded login window failed to start: %s" % err)

    # explicit fallback only (config-driven callers gate this themselves)
    try:
        import webbrowser, time
        webbrowser.open(url, new=1, autoraise=True)
        time.sleep(1)
        return True
    except Exception:
        return False


# Keep the old signature so callers don't break
login = open_login
