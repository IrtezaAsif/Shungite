"""SHUNGITE fix regression suite (2026-11-04 session).

Run:  cd D:\\PEAK_Master && build_venv/Scripts/python.exe -m pytest tests -q
Covers: naming flip (Song - Artist), order-agnostic dedup, cookie-path heal,
native-language vtt ranking, strict matcher gates, Spotify-history zip parse.
"""
import importlib.util
import json
import os
import sys
import tempfile
import zipfile

import pytest

PEAK_DIR = r"D:\PEAK_Master"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


peak = _load("peak", os.path.join(PEAK_DIR, "PEAK_GITHUB_true.py"))
matchers = _load("matchers", os.path.join(PEAK_DIR, "matchers.py"))
ten = _load("titanium_enrich", os.path.join(PEAK_DIR, "titanium_enrich.py"))


class _Shim(object):
    """Minimal stand-in exposing App methods that touch no Tk widgets."""
    pass


def _shim():
    s = _Shim()
    s._normalize_match = lambda txt: peak.App._normalize_match(s, txt)
    return s


# ── 1. Naming: Song - Artist flip ────────────────────────────────────────────

def test_want_filename_new_order():
    assert peak.want_filename("Never Gonna Give You Up", "Rick Astley") == \
        "Never Gonna Give You Up - Rick Astley"


def test_want_filename_no_artist():
    assert peak.want_filename("Somebody") == "Somebody"
    assert peak.want_filename("Somebody", "") == "Somebody"


def test_want_filename_no_track():
    assert peak.want_filename("", "Artist") == "Artist"


def test_want_filename_sanitizes():
    assert peak.want_filename('a<b>c"d', "x/y") == "a_b_c_d - x_y"


def test_want_filename_cap():
    out = peak.want_filename("T" * 400, "A" * 400)
    assert len(out) <= 200


def test_looks_like_url_guard():
    assert peak.looks_like_url("https://www.youtube.com/watch?v=abc")
    assert peak.looks_like_url("youtu.be/abc")
    assert not peak.looks_like_url("Never Gonna Give You Up - Rick Astley")


# ── 2. Order-agnostic dedup (the flip must not re-download the old library) ──

def _make_library(tmpdir, fname):
    p = os.path.join(str(tmpdir), fname)
    open(p, "wb").write(b"x")
    return peak.App._build_file_norms(_shim(), str(tmpdir))


def test_check_have_old_order_still_detected(tmp_path):
    norms = _make_library(tmp_path, "rick astley - never gonna give you up.opus")
    got = peak.App._check_have(_shim(), "Never Gonna Give You Up",
                               "Rick Astley", {}, norms)
    assert got


def test_check_have_new_order_detected(tmp_path):
    norms = _make_library(tmp_path, "never gonna give you up - rick astley.opus")
    got = peak.App._check_have(_shim(), "Never Gonna Give You Up",
                               "Rick Astley", {}, norms)
    assert got


def test_check_have_no_false_positive(tmp_path):
    norms = _make_library(tmp_path, "other artist - other song.opus")
    got = peak.App._check_have(_shim(), "Never Gonna Give You Up",
                               "Rick Astley", {}, norms)
    assert not got


def test_check_have_index_hit_order_free(tmp_path):
    (tmp_path / "old.opus").write_bytes(b"x")
    index = {"old.opus": {"track": "Never Gonna Give You Up",
                          "artist": "Rick Astley", "uri": ""}}
    s = _shim()
    s._music_dir = lambda: str(tmp_path)
    got = peak.App._check_have(s, "Never Gonna Give You Up", "Rick Astley",
                               index, {})
    assert got == "old.opus"


# ── 3. Cookie-path heal (folder-shaped config poison) ────────────────────────

def test_heal_clears_folder_paths(tmp_path):
    folder = str(tmp_path / "a folder")
    os.makedirs(folder, exist_ok=True)
    cfg = {"sp_cookies_file": folder, "yt_cookies_file": folder}
    changed = peak.heal_cookie_paths(cfg, save=False)
    assert set(changed) == {"sp_cookies_file", "yt_cookies_file"}
    assert cfg["sp_cookies_file"] == "" and cfg["yt_cookies_file"] == ""


def test_heal_keeps_valid_file(tmp_path):
    f = tmp_path / "cookies.txt"
    f.write_text("# Netscape HTTP Cookie File\n" + "x" * 150, encoding="utf-8")
    cfg = {"sp_cookies_file": str(f)}
    changed = peak.heal_cookie_paths(cfg, save=False)
    assert changed == []
    assert cfg["sp_cookies_file"] == str(f)


def test_heal_missing_path_cleared():
    cfg = {"yt_cookies_file": r"C:\definitely\not\here.txt"}
    changed = peak.heal_cookie_paths(cfg, save=False)
    assert changed == ["yt_cookies_file"]


def test_heal_migrates_dead_manual_sp_dc(tmp_path, monkeypatch):
    dc_file = tmp_path / "sp_dc"
    monkeypatch.setattr(peak.sp, "SP_DC_PATH", str(dc_file))
    cfg = {"manual_sp_dc": "TESTDC_dummy_value_for_migration"}
    changed = peak.heal_cookie_paths(cfg, save=False)
    assert any("manual_sp_dc" in c for c in changed)
    assert "manual_sp_dc" not in cfg
    assert dc_file.read_text(encoding="utf-8") == "TESTDC_dummy_value_for_migration"


# ── 4. Native-language vtt ranking ───────────────────────────────────────────

def test_rank_vtt_prefers_native_over_english():
    base = "Artist - Song"
    vtts = [base + ".en.vtt", base + ".ja.vtt", base + ".und.vtt"]
    ranked = ten.rank_vtt_candidates(vtts, want_lang="ja")
    assert ranked[0].endswith(".ja.vtt")
    assert ranked[-1].endswith(".en.vtt")


def test_rank_vtt_english_last_when_no_native():
    base = "Some Song"
    vtts = [base + ".en-US.vtt", base + ".de.vtt", base + ".und.vtt"]
    ranked = ten.rank_vtt_candidates(vtts, want_lang="ja")
    assert ranked[-1].endswith((".en.vtt", ".en-US.vtt"))
    assert ranked[0].endswith((".de.vtt", ".und.vtt"))


def test_rank_vtt_no_want_lang_still_native_first():
    base = "X"
    vtts = [base + ".en.vtt", base + ".ko.vtt"]
    ranked = ten.rank_vtt_candidates(vtts, want_lang="")
    assert ranked[0].endswith(".ko.vtt")


# ── 5. Matcher gates (wrong song / cover / live) ─────────────────────────────

def _c(id_, title, channel, secs=210, views=1_000_000):
    return {"id": id_, "url": "https://www.youtube.com/watch?v=" + id_,
            "title": title, "channel": channel, "seconds": secs,
            "views": views, "duration": f"{secs // 60}:{secs % 60:02d}"}


def test_strict_match_prefers_official_over_big_cover():
    res = [_c("cover1", "Blinding Lights - cover by CoverCat", "CoverCat",
              views=90_000_000),
           _c("off1", "The Weeknd - Blinding Lights (Official Audio)",
              "The Weeknd", views=3_000_000_000)]
    best = matchers.strict_match(res, "Blinding Lights", "The Weeknd")
    assert best and best["id"] == "off1"


def test_strict_match_rejects_live_not_in_query():
    res = [_c("live1", "Blinding Lights (Live from LA)", "The Weeknd"),
           _c("off1", "The Weeknd - Blinding Lights (Official Audio)",
              "The Weeknd")]
    best = matchers.strict_match(res, "Blinding Lights", "The Weeknd")
    assert best and best["id"] == "off1"


def test_strict_match_query_live_still_allows_live():
    res = [_c("live1", "Blinding Lights (Live from LA)", "The Weeknd")]
    best = matchers.strict_match(res, "Blinding Lights Live", "The Weeknd")
    assert best and best["id"] == "live1"


# ── 6. Spotify history zip parse (JSON naming source) ────────────────────────

def test_parse_spotify_history_zip(tmp_path):
    zp = tmp_path / "my_spotify_data.zip"
    recs = [
        {"master_metadata_track_name": "Sonder", "episode_name": None,
         "master_metadata_album_artist_name": "Dora Waller",
         "spotify_track_uri": "spotify:track:1"},
        {"master_metadata_track_name": "Sonder",  # duplicate play
         "master_metadata_album_artist_name": "Dora Waller",
         "spotify_track_uri": "spotify:track:1"},
        {"master_metadata_track_name": None,      # podcast episode
         "episode_name": "Ep 12", "master_metadata_album_artist_name": None},
        {"master_metadata_track_name": "Why The Rush",
         "master_metadata_album_artist_name": "528 Hz"},  # artist w/ ' - '
    ]
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("Spotify Extended Streaming History/"
                   "Streaming_History_Audio_2026.json",
                   json.dumps(recs))
    out = peak._parse_spotify_history_zip(str(zp))
    names = sorted(out)
    assert ("Sonder", "Dora Waller") in out
    assert ("Why The Rush", "528 Hz") in out
    assert len(names) == 2  # episode dropped, dupes collapsed


def test_parse_spotify_history_folder(tmp_path):
    d = tmp_path / "Spotify Extended Streaming History"
    d.mkdir()
    (d / "Streaming_History_Audio_2026.json").write_text(
        json.dumps([{"master_metadata_track_name": "T1",
                     "master_metadata_album_artist_name": "A1"}]),
        encoding="utf-8")
    out = peak._parse_spotify_history_zip(str(d))
    assert ("T1", "A1") in out


def test_have_track_batch_path_old_order(tmp_path):
    """_have_track is the ACTUAL batch skip path — must detect legacy files."""
    norms = _make_library(tmp_path, "rick astley - never gonna give you up.opus")
    assert peak.App._have_track(_shim(), "Never Gonna Give You Up",
                                "Rick Astley", {}, norms)


def test_load_lib_index_order_by_mtime(tmp_path):
    """Unindexed files: post-flip files parse as Song - Artist, legacy as
    Artist - Song (mtime against the flip date)."""
    import time as _t
    old_f = tmp_path / "rick astley - never gonna give you up.opus"
    old_f.write_bytes(b"x")
    os.utime(old_f, (1_000_000_000, 1_000_000_000))   # 2001 = legacy order
    new_f = tmp_path / "never gonna give you up - rick astley.opus"
    new_f.write_bytes(b"x")
    future = _t.mktime((2026, 11, 5, 12, 0, 0, 0, 0, -1))
    os.utime(new_f, (future, future))                 # post-flip (midnight anchor)
    s = _shim()
    s._music_dir = lambda: str(tmp_path)
    idx = peak.App._load_lib_index(s)
    assert idx[old_f.name]["artist"] == "rick astley"
    assert idx[new_f.name]["artist"] == "rick astley"


def test_get_sp_dc_from_cookies_file(tmp_path):
    """Fresh-install harvest chain: Netscape cookies.txt with an sp_dc line
    must yield the cookie value (the post-login harvest persists it)."""
    cf = tmp_path / "spotify_cookies.txt"
    cf.write_text(
        "# Netscape HTTP Cookie File\n"
        ".open.spotify.com\tTRUE\t/\tTRUE\t0\tsp_dc\tTESTDCVALUE123\n")
    val = peak.sp.get_sp_dc_from_cookies_file(str(cf))
    assert val == "TESTDCVALUE123"


def test_split_video_title_basic_and_topic():
    assert peak._split_video_title(
        "Rick Astley - Never Gonna Give You Up (Official Video)", "") == \
        ("Never Gonna Give You Up", "Rick Astley")
    assert peak._split_video_title("Some Song", "Artist Name - Topic") == \
        ("Some Song", "Artist Name")


def test_parse_libv3_rows_real_shape():
    rows = [{"item": {"_uri": "spotify:playlist:ABC123",
                      "data": {"__typename": "Playlist", "name": "My List"}}},
            {"garbage": True},
            None,
            {"item": {"_uri": "spotify:playlist:XYZ789",
                      "data": {"__typename": "Playlist", "name": "Second"}}}]
    out = peak.sp._parse_libv3_rows(rows)
    assert [p["id"] for p in out] == ["ABC123", "XYZ789"]
    assert out[0]["name"] == "My List"


def test_get_my_playlists_dc_needs_spdc(monkeypatch):
    monkeypatch.setattr(peak.sp, "stored_sp_dc", lambda: "")
    assert peak.sp.get_my_playlists_dc(limit=5) is None


def test_native_sub_langs_user_override_and_native_first(monkeypatch):
    monkeypatch.setattr(peak, "_cfg_get",
                        lambda k, d=None: "ja" if k == "subtitle_lang" else d)
    assert peak._native_sub_langs("Some Song", "Artist") == ["ja"]
    monkeypatch.setattr(peak, "_cfg_get", lambda k, d=None: None)
    langs = peak._native_sub_langs("夜に駆ける (Official Video)", "YOASOBI")
    assert langs[0] == "und"
    assert "ja" in langs[:2]
    assert langs[-1] in ("en", "en.*")


def test_sp_api_playlists_dc_first(monkeypatch):
    s = _shim()
    monkeypatch.setattr(peak.sp, "get_my_playlists_dc",
                        lambda limit=50: [{"name": "DC List", "id": "dc1",
                                           "owner": "you", "tracks": 3}])

    class _Spo:
        def get_access_token(self, x):
            raise AssertionError("OAuth must not be tried when DC succeeds")

    monkeypatch.setattr(peak, "spo", _Spo())
    out = peak.App._sp_api_playlists(s)
    assert out and out[0]["id"] == "dc1"


def test_split_video_title_descriptor_segment():
    song, artist = peak._split_video_title("Song Name - Official Video",
                                           "Some Channel")
    assert song == "Song Name"
    assert artist == "Some Channel"


# ── 8. YTM library: dead-session oracle + fallback chain (2026-11-05) ────────

inn = _load("innertube_test", os.path.join(PEAK_DIR, "innertube.py"))
bcook = _load("browser_cookies_test", os.path.join(PEAK_DIR, "browser_cookies.py"))
sys.modules["innertube"] = inn          # lazy `import innertube` in helpers
sys.modules["browser_cookies"] = bcook


def _shell_logged_out():
    """The exact shape YouTube serves a dead session (v3 probe, live).

    Built programmatically: the deep literal once miscounted braces and
    broke the whole module's import — construction beats brace-counting.
    """
    message = {
        "text": {"runs": [{"text": "Looking for what you've liked?"}]},
        "button": {"buttonRenderer": {"navigationEndpoint": {"signInEndpoint": {}}}},
    }
    item_section = {"contents": [{"messageRenderer": message}]}
    section = {"contents": [{"itemSectionRenderer": item_section}]}
    tab = {"tabRenderer": {"content": {"sectionListRenderer": section}}}
    return {"contents": {"singleColumnBrowseResultsRenderer": {"tabs": [tab]}}}


def _grid_alive():
    """Canonical logged-in shape (ytmusicapi GRID): two-row items in a grid."""
    def row(pid, title):
        return {"musicTwoRowItemRenderer": {
            "navigationEndpoint": {"browseEndpoint": {"browseId": pid}},
            "title": {"runs": [{"text": title}]}}}

    grid = {"items": [row("VLPLaaa111", "Anime Mix"),
                      row("VLPLbbb222", "Gym Pump"),
                      row("MPREb_album1", "Some Album")]}
    section = {"contents": [{"gridRenderer": grid}]}
    tab = {"tabRenderer": {"content": {"sectionListRenderer": section}}}
    return {"contents": {"singleColumnBrowseResultsRenderer": {"tabs": [tab]}}}


def test_ytm_library_detects_logged_out_shell(monkeypatch):
    monkeypatch.setattr(inn, "_post", lambda ep, body, ck: _shell_logged_out())
    assert inn.get_library_playlists({"SID": "x"}) == []
    assert "logged-out" in inn._LAST_ERROR


def test_ytm_library_parses_alive_grid_rows(monkeypatch):
    monkeypatch.setattr(inn, "_post", lambda ep, body, ck: _grid_alive())
    out = inn.get_library_playlists({"SID": "x"})
    assert [p["playlistId"] for p in out] == ["VLPLaaa111", "VLPLbbb222"]
    assert inn._LAST_ERROR == ""


def test_ytm_library_alive_empty_vs_logged_out(monkeypatch):
    monkeypatch.setattr(inn, "_post", lambda ep, body, ck: {"contents": {}})
    assert inn.get_library_playlists({"SID": "x"}) == []
    assert "empty" in inn._LAST_ERROR
    assert "logged-out" not in inn._LAST_ERROR


def test_ytm_library_error_state(monkeypatch):
    def boom(ep, body, ck):
        raise RuntimeError("http 500")
    monkeypatch.setattr(inn, "_post", boom)
    assert inn.get_library_playlists({"SID": "x"}) == []
    assert "failed" in inn._LAST_ERROR


def test_yt_cookie_names_include_rotating():
    for n in ("__Secure-1PSIDTS", "__Secure-3PSIDTS", "LOGIN_INFO"):
        assert n in bcook._YT_NAMES


def test_ytm_source_fallback_jar_dead_browser_alive():
    calls = []

    def fetch(ck):
        calls.append(ck.get("src"))
        if ck.get("src") == "jar":
            inn._LAST_ERROR = "logged-out (server sign-in CTA)"
            return []
        inn._LAST_ERROR = ""
        return [{"playlistId": "VLPLx", "title": "Anime Mix"}]

    rows, ck, tag, diag, dead, _net = peak._pick_ytm_cookie_source(
        [("jar", {"src": "jar"}), ("browser", {"src": "browser"})], fetch)
    assert calls == ["jar", "browser"]
    assert tag == "browser" and len(rows) == 1 and dead is False
    assert ck.get("src") == "browser"


def test_ytm_source_fallback_both_dead():
    def fetch(ck):
        inn._LAST_ERROR = "logged-out (server sign-in CTA)"
        return []
    rows, ck, tag, diag, dead, _net = peak._pick_ytm_cookie_source(
        [("jar", {"src": "jar"}), ("browser", {"src": "browser"})], fetch)
    assert rows == [] and tag == "" and dead is True
    assert len(diag) == 2


def test_ytm_source_chain_stops_on_alive_empty():
    def fetch(ck):
        inn._LAST_ERROR = "alive but empty library"
        return []
    rows, ck, tag, diag, dead, _net = peak._pick_ytm_cookie_source(
        [("jar", {"src": "jar"}), ("browser", {"src": "browser"})], fetch)
    assert rows == [] and dead is False and tag == "jar"
