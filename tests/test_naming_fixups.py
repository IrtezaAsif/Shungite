"""Download-naming + fixups-extension robustness tests (2026-11-06, v1.1.4 -
the naming item's residual cycle).

Reframed honestly: the user's literal "file named as the URL" symptom was
already fixed by bfc0ed29 (Nov 5, in shipped v1.1.3 - the Single-tab
metadata resolve plus the looks_like_url backstop). What this cycle closes
is the residual class found in the recon:

1. _post_download_fixups hard-coded actual = want_fn + ".opus" - any MP3/M4A
   download silently got NO tags, NO genre, NO embedded lyrics, and every
   actual_fn = want_fn + ".opus" index key lied about the file on disk.
   The new _actual_audio_file() resolves the real file: glob-escaped (the
   want_filename sanitizer keeps [ ] chars, so a naive glob on a
   "[Official Video]" stem matches nothing), newest mtime wins (a format
   re-download must beat a stale same-stem file).
2. Three retry/missing paths passed raw YouTube titles to download_one and
   keyed the index on them; the new pure _dl_title_for() composes the
   canonical Song - Artist stem from real metadata first, then the video
   title via _split_video_title, and never lets a URL become a filename.
3. looks_like_url missed scheme-less URLs (youtube.com/watch?v=... would
   flow into outtmpl as a path).

HERMETICITY NOTE (test-hygiene lesson, 2026-11-06): the fixups test does
NOT rely on the socket tripwire - the fixups wraps its network calls in
bare except: pass, which would SWALLOW the tripwire's AssertionError
silently. Instead titanium_enrich is fake-injected, ensure_lyrics is
monkeypatched, and genre= is passed explicitly. The crafted minimal MP3
parses with mutagen (probed: parsed=True) but has info.length=None - the
duration-keyed LRClib branch is unreachable by design and nothing asserts
on it.
"""
import os
import socket
import sys
import time
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import PEAK_GITHUB_true as peak


_MIN_MP3 = (b"ID3\x03\x00\x00\x00\x00\x00\x00"
            + (b"\xff\xfb\x90\x00" + b"\x00" * 413) * 20)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _dead(*a, **k):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket, "socket", _dead)
    monkeypatch.setattr(socket, "create_connection", _dead)
    monkeypatch.setattr(socket, "getaddrinfo", _dead)
    yield


# ------------------------------------------- _actual_audio_file (pure)
def test_actual_audio_file_finds_m4a(tmp_path):
    (tmp_path / "Song - Artist.m4a").write_bytes(b"x")
    got = peak._actual_audio_file(str(tmp_path), "Song - Artist")
    assert got is not None and got.endswith("Song - Artist.m4a")


def test_actual_audio_file_newest_mtime_wins(tmp_path):
    # a stale opus and a fresh mp3 re-download: the FRESH file must win
    old = tmp_path / "Song - Artist.opus"
    new = tmp_path / "Song - Artist.mp3"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    old_t = time.time() - 3600
    os.utime(old, (old_t, old_t))
    got = peak._actual_audio_file(str(tmp_path), "Song - Artist")
    assert got.endswith("Song - Artist.mp3")


def test_actual_audio_file_bracket_stem(tmp_path):
    # want_filename keeps [ ] chars; a naive glob treats [Official Video]
    # as a character class and matches nothing - escape must be used
    stem = "Song [Official Video]"
    (tmp_path / (stem + ".opus")).write_bytes(b"x")
    got = peak._actual_audio_file(str(tmp_path), stem)
    assert got is not None and got.endswith(stem + ".opus")


def test_actual_audio_file_none(tmp_path):
    assert peak._actual_audio_file(str(tmp_path), "Missing Song") is None


# ------------------------------------------- _dl_title_for (pure)
def test_dl_title_for_real_metadata():
    assert peak._dl_title_for("Song", "Artist") == "Song - Artist"


def test_dl_title_for_parses_video_title():
    got = peak._dl_title_for("", "", "Artist - Song (Official Video)",
                             "Artist - Topic")
    assert got == "Song - Artist"


def test_dl_title_for_never_url():
    # a URL must NEVER become a filename, even via the res fallback
    got = peak._dl_title_for("Song", "",
                             "https://www.youtube.com/watch?v=abc", "")
    assert "youtube.com" not in got and got == "Song"


def test_dl_title_for_track_only():
    assert peak._dl_title_for("Song", "") == "Song"


# ------------------------------------------- looks_like_url hardening
def test_looks_like_url_schemeless():
    assert peak.looks_like_url("youtube.com/watch?v=abc") is True
    assert peak.looks_like_url("music.youtube.com/watch?v=abc") is True
    assert peak.looks_like_url("https://youtu.be/abc") is True
    assert peak.looks_like_url("Song - Artist") is False


# ------------------------------------------- fixups behavioral (RED core)
@pytest.fixture
def fake_ten(monkeypatch):
    fake = types.ModuleType("titanium_enrich")
    fake.__path__ = []
    fake.itunes_genre = lambda t, a: "TestGenre"
    fake.fetch_lyrics = lambda *a, **k: False
    monkeypatch.setitem(sys.modules, "titanium_enrich", fake)
    return fake


def test_fixups_tags_mp3_download(tmp_path, monkeypatch, fake_ten):
    # RED core: the .opus hard-code missed every non-opus download - the
    # MP3 file must get its tags. (mutagen parses the crafted MP3; the
    # duration-keyed branch is unreachable by design - length is None.)
    monkeypatch.setattr(peak, "ensure_lyrics", lambda *a, **k: None)
    music = str(tmp_path)
    (tmp_path / "Song - Artist.mp3").write_bytes(_MIN_MP3)
    peak._post_download_fixups(music, "Song - Artist", "Song", "Artist",
                               genre="TestGenre")
    import mutagen
    f = mutagen.File(os.path.join(music, "Song - Artist.mp3"), easy=True)
    assert f is not None
    assert f.get("title") == ["Song"]
    assert f.get("artist") == ["Artist"]


def test_fixups_missing_file_noop(tmp_path, monkeypatch, fake_ten):
    monkeypatch.setattr(peak, "ensure_lyrics", lambda *a, **k: None)
    peak._post_download_fixups(str(tmp_path), "Ghost - Song", "Ghost",
                               "Song", genre="TestGenre")
    assert not (tmp_path / "Ghost - Song.opus").exists()


def test_fixups_renames_yt_title_file(tmp_path, monkeypatch, fake_ten):
    # the file landed under the yt title (the %(title)s fallback): the
    # fixups must find it via res_title, rename it to Song - Artist, and
    # tag it - previously the tags ran before any rename and missed it.
    monkeypatch.setattr(peak, "ensure_lyrics", lambda *a, **k: None)
    music = str(tmp_path)
    (tmp_path / "Kroi - SPIN (Official Video).mp3").write_bytes(_MIN_MP3)
    peak._post_download_fixups(music, "SPIN - Kroi", "SPIN", "Kroi",
                               res_title="Kroi - SPIN (Official Video)",
                               genre="TestGenre")
    import mutagen
    f = mutagen.File(os.path.join(music, "SPIN - Kroi.mp3"), easy=True)
    assert f is not None
    assert f.get("title") == ["SPIN"]
    assert f.get("artist") == ["Kroi"]
    assert not (tmp_path / "Kroi - SPIN (Official Video).mp3").exists()
