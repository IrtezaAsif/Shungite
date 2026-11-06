"""Spotify-library robustness tests (2026-11-06, v1.1.4 - Bug #1 D1-D4).

Mocked only: no network, no Tk, and the real token file is never touched
(every test redirects SP_DC_PATH into tmp_path). The live probe proved the
libraryV3 route is healthy (22/22/22 playlists); these tests pin the
ROBUSTNESS contract around it:

  D1 - the post-login sp_dc harvest never fails silently and falls back to
       one live browser-cookie scan when the webview capture is missing.
  D2 - every failure exit of get_my_playlists_dc records a reason
       (LIBV3_LAST_REASON); a clean fetch clears it; a genuinely-empty
       library (totalCount=0) leaves it empty - empty is not a failure.
  D3 - the empty-library UI distinguishes logged-out from fetch-failed
       from logged-in-but-empty.
  D4 - a stale (or missing) ~/.peak_sp_dc self-heals before a library
       refresh; a fresh one is never re-scanned.

Run:  cd D:/PEAK_Master && build_venv/Scripts/python.exe -m pytest tests/test_spotify_lib_robust.py -q
"""
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import PEAK_GITHUB_true as peak
import spotify_api as sp


# --------------------------------------------------------------- D2 reasons
def test_playlists_dc_no_spdc_sets_reason(monkeypatch):
    monkeypatch.setattr(sp, "stored_sp_dc", lambda: "")
    sp.LIBV3_LAST_REASON = ""
    assert sp.get_my_playlists_dc(limit=50) is None
    assert "logged out" in sp.LIBV3_LAST_REASON


def test_playlists_dc_bad_typename_sets_reason(monkeypatch):
    monkeypatch.setattr(sp, "stored_sp_dc", lambda: "x" * 32)
    monkeypatch.setattr(sp, "_libraryv3_hash", lambda: "h")

    def _bad_gql(op, variables, sha, force_new_token=False):
        return {"data": {"me": {"libraryV3":
                                {"__typename": "GenericError"}}}}

    monkeypatch.setattr(sp, "_gql", _bad_gql)
    sp.LIBV3_LAST_REASON = ""
    assert sp.get_my_playlists_dc(limit=50) is None
    assert "GenericError" in sp.LIBV3_LAST_REASON


def test_playlists_dc_exception_sets_reason(monkeypatch):
    monkeypatch.setattr(sp, "stored_sp_dc", lambda: "x" * 32)
    monkeypatch.setattr(sp, "_libraryv3_hash", lambda: "h")

    def _boom(op, variables, sha, force_new_token=False):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(sp, "_gql", _boom)
    sp.LIBV3_LAST_REASON = ""
    assert sp.get_my_playlists_dc(limit=50) is None
    assert "socket closed" in sp.LIBV3_LAST_REASON


def test_playlists_dc_success_clears_reason(monkeypatch):
    monkeypatch.setattr(sp, "stored_sp_dc", lambda: "x" * 32)
    monkeypatch.setattr(sp, "_libraryv3_hash", lambda: "h")

    def _good_gql(op, variables, sha, force_new_token=False):
        return {"data": {"me": {"libraryV3": {
            "__typename": "LibraryPage", "totalCount": 1,
            "items": [{"__typename": "Playlist"}]}}}}

    monkeypatch.setattr(sp, "_gql", _good_gql)
    monkeypatch.setattr(sp, "_parse_libv3_rows",
                        lambda rows: [{"name": "P1", "id": "1"}])
    sp.LIBV3_LAST_REASON = "stale"
    out = sp.get_my_playlists_dc(limit=50)
    assert out and out[0].get("name") == "P1"
    assert sp.LIBV3_LAST_REASON == ""


def test_playlists_dc_genuinely_empty_is_not_a_failure(monkeypatch):
    """An account with zero playlists is empty, not broken (totalCount=0
    must leave LIBV3_LAST_REASON empty so D3 says 'logged in but empty')."""
    monkeypatch.setattr(sp, "stored_sp_dc", lambda: "x" * 32)
    monkeypatch.setattr(sp, "_libraryv3_hash", lambda: "h")

    def _empty_gql(op, variables, sha, force_new_token=False):
        return {"data": {"me": {"libraryV3": {
            "__typename": "LibraryPage", "totalCount": 0,
            "items": []}}}}

    monkeypatch.setattr(sp, "_gql", _empty_gql)
    sp.LIBV3_LAST_REASON = ""
    assert sp.get_my_playlists_dc(limit=50) is None
    assert sp.LIBV3_LAST_REASON == ""


# ------------------------------------------------- D1 harvest (not silent)
def test_harvest_uses_capture_file(tmp_path, monkeypatch):
    cap = tmp_path / ".spotify_cookies.txt"
    cap.write_text("x" * 150, encoding="utf-8")
    dcp = tmp_path / "test_dc"
    monkeypatch.setattr(sp, "SP_DC_PATH", str(dcp))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))
    monkeypatch.setattr(sp, "get_sp_dc_from_cookies_file",
                        lambda p=None: "DCVALUE123"
                        if (p and os.path.isfile(p)) else "")
    assert peak.harvest_sp_dc_post_login() is True
    assert dcp.read_text(encoding="utf-8").strip() == "DCVALUE123"


def test_harvest_falls_back_to_browser_scan(tmp_path, monkeypatch):
    dcp = tmp_path / "test_dc"
    monkeypatch.setattr(sp, "SP_DC_PATH", str(dcp))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))
    monkeypatch.setattr(sp, "get_sp_dc_from_cookies_file", lambda p=None: "")
    import browser_cookies as bc
    monkeypatch.setattr(bc, "get_sp_dc", lambda: "BROWSERDC456")
    assert peak.harvest_sp_dc_post_login() is True
    assert dcp.read_text(encoding="utf-8").strip() == "BROWSERDC456"


def test_harvest_failure_is_reported(tmp_path, monkeypatch):
    dcp = tmp_path / "test_dc"
    monkeypatch.setattr(sp, "SP_DC_PATH", str(dcp))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))
    monkeypatch.setattr(sp, "get_sp_dc_from_cookies_file", lambda p=None: "")
    import browser_cookies as bc
    monkeypatch.setattr(bc, "get_sp_dc", lambda: "")
    assert peak.harvest_sp_dc_post_login() is False
    assert not dcp.exists()


# ---------------------------------------------- D4 stale self-heal refresh
def test_refresh_stale_sp_dc_rewrites_when_old(tmp_path, monkeypatch):
    dcp = tmp_path / "test_dc"
    dcp.write_text("OLD", encoding="utf-8")
    old_t = time.time() - 7 * 3600
    os.utime(dcp, (old_t, old_t))
    monkeypatch.setattr(sp, "SP_DC_PATH", str(dcp))
    import browser_cookies as bc
    monkeypatch.setattr(bc, "get_sp_dc", lambda: "NEW")
    assert peak.refresh_stale_sp_dc(max_age_s=21600) is True
    assert dcp.read_text(encoding="utf-8") == "NEW"


def test_refresh_skips_fresh_sp_dc(tmp_path, monkeypatch):
    dcp = tmp_path / "test_dc"
    dcp.write_text("FRESH", encoding="utf-8")
    monkeypatch.setattr(sp, "SP_DC_PATH", str(dcp))
    import browser_cookies as bc
    scanned = []

    def _scan():
        scanned.append(1)
        return "SHOULDNOTBEUSED"

    monkeypatch.setattr(bc, "get_sp_dc", _scan)
    assert peak.refresh_stale_sp_dc(max_age_s=21600) is False
    assert scanned == []
    assert dcp.read_text(encoding="utf-8") == "FRESH"


def test_refresh_missing_file_triggers_scan(tmp_path, monkeypatch):
    dcp = tmp_path / "test_dc"
    monkeypatch.setattr(sp, "SP_DC_PATH", str(dcp))
    import browser_cookies as bc
    monkeypatch.setattr(bc, "get_sp_dc", lambda: "MISSINGSCAN")
    assert peak.refresh_stale_sp_dc(max_age_s=21600) is True
    assert dcp.read_text(encoding="utf-8") == "MISSINGSCAN"


# ---------------------------------------------- D3 empty-library UI states
def test_sp_empty_message_logged_out(monkeypatch):
    monkeypatch.setattr(sp, "stored_sp_dc", lambda: "")
    assert "not logged in" in peak._sp_empty_message()


def test_sp_empty_message_fetch_failed(monkeypatch):
    monkeypatch.setattr(sp, "stored_sp_dc", lambda: "x" * 32)
    sp.LIBV3_LAST_REASON = "libraryV3 typename=GenericError after hash refresh"
    msg = peak._sp_empty_message()
    assert "fetch failed" in msg and "GenericError" in msg


def test_sp_empty_message_logged_in_empty(monkeypatch):
    monkeypatch.setattr(sp, "stored_sp_dc", lambda: "x" * 32)
    sp.LIBV3_LAST_REASON = ""
    assert "logged in but empty" in peak._sp_empty_message()
