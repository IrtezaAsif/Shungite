"""YTM cookie-chain robustness tests (2026-11-06, v1.1.4 - YTM library item).

The audit found the tier chain's silent paths: (1) the picker reads
innertube._LAST_ERROR without resetting it - a tier whose fetch raises
inherits the PREVIOUS tier's oracle (and across invocations, the previous
load's final state) and can be classified "alive but empty" while its
fetch actually crashed; (2) innertube's "browse failed: ..." (transport
failure) lands in the generic branch and is reported to the user as
"session expired - re-login"; (3) a throwing cookie tier vanishes from the
candidate list with no diagnostic; (4) the jar path split-brain:
yt_cookies_dict_from_file() and export_webview_cookies() default to
~/.peak_yt_cookies.txt while every _data_file consumer (yt-dlp cookie args,
_import_cookies, _yt_cookiefile) uses LocalLow\\Shungite - confirmed live
by two coexisting jars with different sizes/mtimes. The fix canonicalizes
LocalLow via explicit paths at every call site, clears BOTH jars on
logout, and gives every tier a first-class diagnostic.

HERMETIC: fake innertube via monkeypatch.setitem(sys.modules, ...) -
immune to module-identity drift (the test_caption_pipeline.py bare-install
root cause) - plus an autouse network tripwire. No real cookie files, no
user data dirs, no sockets. Cookie values used in tests are marked with
the SECRET_ prefix and asserted absent from every diagnostic.
"""
import os
import socket
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import PEAK_GITHUB_true as peak


LOGGED_OUT = "logged-out (server sign-in CTA)"
ALIVE_EMPTY = "alive but empty library - nothing saved or liked yet"
BROWSE_FAIL = "browse failed: URLError: timed out"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any real socket access in these tests is a hermeticity bug."""
    def _dead(*a, **k):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket, "socket", _dead)
    monkeypatch.setattr(socket, "create_connection", _dead)
    monkeypatch.setattr(socket, "getaddrinfo", _dead)
    yield


class _FakeItb(types.ModuleType):
    """Module-shaped innertube stand-in: the picker does
    'import innertube as _itb' and reads _LAST_ERROR off it."""

    def __init__(self):
        super().__init__("innertube")
        self._LAST_ERROR = ""


@pytest.fixture
def fake_itb(monkeypatch):
    itb = _FakeItb()
    monkeypatch.setitem(sys.modules, "innertube", itb)
    return itb


def _ck(tag):
    """A cookie dict carrying recognizably SECRET values."""
    return {"SAPISID": "SECRET_%s" % tag,
            "__Secure-1PSIDTS": "SECRET_TS_%s" % tag}


def _fetch_sets(itb, state, rows=None, raise_exc=None, sentinel=False):
    """A fake fetch closure: sets the oracle state like the real
    get_library_playlists does, then returns rows or raises."""
    def _f(_ck_dict):
        if sentinel:
            raise AssertionError("this tier must never be fetched")
        if state is not None:
            itb._LAST_ERROR = state
        if raise_exc is not None:
            raise raise_exc
        return list(rows or [])
    return _f


def _run(sources, fetch):
    return peak._pick_ytm_cookie_source(sources, fetch)


# ------------------------------------------------- picker: happy paths
def test_first_tier_rows_win(fake_itb):
    rows, ck, tag, diag, dead, net = _run(
        [("webview", _ck("w"))], _fetch_sets(fake_itb, "", rows=["r1", "r2"]))
    assert rows == ["r1", "r2"]
    assert tag == "webview"
    assert ck == _ck("w")
    assert dead is False and net is False
    assert diag == ["webview: ok rows=2"]


def test_logged_out_falls_through_to_next_tier(fake_itb):
    tiers = [("webview", _ck("w")), ("jar", _ck("j"))]
    fetches = [_fetch_sets(fake_itb, LOGGED_OUT, rows=[]),
               _fetch_sets(fake_itb, "", rows=["r1"])]
    it = iter(fetches)
    rows, ck, tag, diag, dead, net = _run(tiers, lambda c: next(it)(c))
    assert rows == ["r1"]
    assert tag == "jar"
    assert dead is False and net is False
    assert len(diag) == 2


def test_alive_but_empty_short_circuits(fake_itb):
    tiers = [("webview", _ck("w")), ("jar", _ck("j"))]
    fetches = [_fetch_sets(fake_itb, ALIVE_EMPTY, rows=[]),
               _fetch_sets(fake_itb, "", sentinel=True)]
    it = iter(fetches)
    rows, ck, tag, diag, dead, net = _run(tiers, lambda c: next(it)(c))
    assert rows == []
    assert tag == "webview"
    assert ck == _ck("w")
    assert dead is False and net is False


def test_empty_sources_returns_clean(fake_itb):
    rows, ck, tag, diag, dead, net = _run([], _fetch_sets(fake_itb, ""))
    assert (rows, ck, tag, diag, dead, net) == ([], None, "", [], False, False)


# ------------------------------------------------- picker: failure taxonomy
def test_fetch_error_not_inherited_from_previous_tier(fake_itb):
    # tier1 logged-out (dead, continue) -> tier2's fetch CRASHES. The old
    # picker read the stale global and labeled tier2 "logged-out" too; the
    # fixed picker resets the oracle before every fetch, so tier2's diag
    # says fetch error, not logged-out.
    tiers = [("webview", _ck("w")), ("jar", _ck("j"))]
    fetches = [_fetch_sets(fake_itb, LOGGED_OUT, rows=[]),
               _fetch_sets(fake_itb, None, raise_exc=ValueError("boom"))]
    it = iter(fetches)
    rows, ck, tag, diag, dead, net = _run(tiers, lambda c: next(it)(c))
    assert rows == []
    assert ck is None  # benign: the caller falls back to its own jar dict
    assert dead is True and net is False
    assert "jar: fetch error: ValueError" in diag
    assert not any("logged-out" in d for d in diag if d.startswith("jar:"))


def test_stale_state_not_inherited_across_invocations(fake_itb):
    # _LAST_ERROR is a module global: a PREVIOUS load left "alive but
    # empty" behind. The new load's first tier crashes -> the old picker
    # returned "alive but empty" for a tier whose fetch never ran.
    fake_itb._LAST_ERROR = ALIVE_EMPTY
    rows, ck, tag, diag, dead, net = _run(
        [("webview", _ck("w"))],
        _fetch_sets(fake_itb, None, raise_exc=ValueError("boom")))
    assert rows == []
    assert dead is True
    assert diag == ["webview: fetch error: ValueError"]
    assert "alive but empty" not in " ".join(diag)


def test_browse_failed_is_net_not_dead(fake_itb):
    # A transport failure must NOT tell the user to re-login (the D2
    # lesson in a new coat).
    tiers = [("webview", _ck("w")), ("jar", _ck("j"))]
    fetches = [_fetch_sets(fake_itb, BROWSE_FAIL, rows=[]),
               _fetch_sets(fake_itb, BROWSE_FAIL, rows=[])]
    it = iter(fetches)
    rows, ck, tag, diag, dead, net = _run(tiers, lambda c: next(it)(c))
    assert rows == []
    assert dead is False and net is True


def test_mixed_dead_and_net(fake_itb):
    tiers = [("webview", _ck("w")), ("jar", _ck("j"))]
    fetches = [_fetch_sets(fake_itb, LOGGED_OUT, rows=[]),
               _fetch_sets(fake_itb, BROWSE_FAIL, rows=[])]
    it = iter(fetches)
    rows, ck, tag, diag, dead, net = _run(tiers, lambda c: next(it)(c))
    assert dead is True and net is True


def test_empty_ck_tier_skipped_with_diag(fake_itb):
    tiers = [("webview", {}), ("jar", _ck("j"))]
    rows, ck, tag, diag, dead, net = _run(
        tiers, _fetch_sets(fake_itb, "", rows=["r1"]))
    assert tag == "jar"
    assert "webview: no cookies" in diag


def test_diag_never_contains_cookie_values(fake_itb):
    tiers = [("webview", _ck("w")), ("jar", {}), ("browser", _ck("b"))]
    fetches = [_fetch_sets(fake_itb, LOGGED_OUT, rows=[]),
               _fetch_sets(fake_itb, BROWSE_FAIL, rows=[])]
    it = iter(fetches)
    def _drive(c):
        f = next(it)
        return f(c)
    rows, ck, tag, diag, dead, net = _run(tiers, _drive)
    blob = "\n".join(diag)
    assert "SECRET_" not in blob


# ------------------------------------------------- source builder
class _FakeBc:
    """browser_cookies stand-in; records the paths PEAK passes explicitly."""

    def __init__(self, fail_webview=False, fail_jar=False, fail_browser=False):
        self.calls = []
        self.fail_webview = fail_webview
        self.fail_jar = fail_jar
        self.fail_browser = fail_browser

    def yt_cookies_dict_webview(self, profile=None):
        self.calls.append(("webview", profile))
        if self.fail_webview:
            raise RuntimeError("dpapi gone")
        return {"SAPISID": "SECRET_wv"}

    def yt_cookies_dict_from_file(self, path=None):
        self.calls.append(("jar", path))
        if self.fail_jar:
            raise RuntimeError("bad jar")
        return {"SAPISID": "SECRET_jar"}

    def yt_cookies_dict_complete(self):
        self.calls.append(("browser", None))
        if self.fail_browser:
            raise RuntimeError("no browser")
        return {"SAPISID": "SECRET_browser"}


def test_build_sources_all_ok_ordered():
    bc = _FakeBc()
    sources, tier_diag = peak._build_ytm_sources(
        bc, jar_path="J:/jar.txt", webview_profile="W:/prof")
    assert [t for t, _ in sources] == ["webview", "jar", "browser"]
    assert tier_diag == []
    assert bc.calls == [("webview", "W:/prof"), ("jar", "J:/jar.txt"),
                        ("browser", None)]


def test_build_sources_webview_throws_is_diagnosed():
    bc = _FakeBc(fail_webview=True)
    sources, tier_diag = peak._build_ytm_sources(
        bc, jar_path="J:/jar.txt", webview_profile="W:/prof")
    assert [t for t, _ in sources] == ["jar", "browser"]
    assert tier_diag == ["webview: source error: RuntimeError"]


def test_build_sources_all_throw_is_diagnosed():
    bc = _FakeBc(fail_webview=True, fail_jar=True, fail_browser=True)
    sources, tier_diag = peak._build_ytm_sources(
        bc, jar_path="J:/jar.txt", webview_profile="W:/prof")
    assert sources == []
    assert len(tier_diag) == 3
    assert all("source error" in d for d in tier_diag)


# ------------------------------------------------- canonical jar paths
def test_cookie_paths_cover_both_locations():
    paths = peak._yt_cookie_paths()
    norm = [p.replace("\\", "/") for p in paths]
    assert len(set(norm)) == 2  # distinct: logout used to clear one twice
    assert any("LocalLow" in p for p in norm)  # the canonical DATA_DIR jar
    home = os.path.join(os.path.expanduser("~"),
                        ".peak_yt_cookies.txt").replace("\\", "/")
    assert home in norm  # the legacy jar the old logout left alive
