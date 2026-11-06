"""Caption-lyric script-guard tests (2026-11-06, v1.1.4 - lyrics item).

The user's bug: a Japanese song (Kroi - SPIN) got ENGLISH caption lyrics
when LRClib missed and YouTube's en-orig ASR served a wrong-language
transcription. The guard rejects a caption-derived lyric whose script
contradicts the native script carried by the title/artist, so the
pipeline falls through to the description or an honest miss. LRClib still
wins first; English stays acceptable when there is no non-Latin evidence.

HERMETIC BY CONSTRUCTION (2026-11-06 incident fix): the wiring tests
inject a fake titanium_enrich module via sys.modules - immune to module
identity drift between test files (ROOT CAUSE, recon-proven:
tests/test_caption_pipeline.py installs spec-loaded modules with a BARE
sys.modules[name] = mod - no monkeypatch, no restore - so tests running
after it inherit those objects, and the real LRClib stage then won before
the caption stage was ever reached) - and an autouse network tripwire
makes any real socket access fail loudly instead of silently passing.

Run:  cd D:/PEAK_Master && build_venv/Scripts/python.exe -m pytest tests/test_lyrics_script_guard.py -q
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


EN_LYRIC = ("[00:01.00]Walking down the avenue alone tonight\n"
            "[00:04.00]Everything I wanted is standing in the light\n"
            "[00:08.00]Tell me how it feels to be the one who stays\n")
JA_LYRIC = ("[00:01.00]愛して 愛して 愛して\n"
            "[00:04.00]頭の中が潰れても\n"
            "[00:08.00]叫んで 叫んで 叫んで\n")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any real socket access in these tests is a hermeticity bug."""
    def _dead(*a, **k):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket, "socket", _dead)
    monkeypatch.setattr(socket, "create_connection", _dead)
    monkeypatch.setattr(socket, "getaddrinfo", _dead)
    yield


# ------------------------------------------------- helper: pure decisions
def test_ja_title_en_lyric_rejected():
    assert peak._lyric_script_mismatch(EN_LYRIC, "愛して 愛して 愛して",
                                       "Kikuo") is True


def test_ja_title_ja_lyric_accepted():
    assert peak._lyric_script_mismatch(JA_LYRIC, "愛して 愛して 愛して",
                                       "Kikuo") is False


def test_mixed_ja_lyric_with_english_words_accepted():
    mixed = JA_LYRIC + "[00:12.00]baby don't go baby don't go\n"
    assert peak._lyric_script_mismatch(mixed, "愛して 愛して 愛して",
                                       "Kikuo") is False


def test_ko_title_en_lyric_rejected():
    assert peak._lyric_script_mismatch(EN_LYRIC, "사랑하지 않아",
                                       "artist") is True


def test_ru_title_en_lyric_rejected():
    assert peak._lyric_script_mismatch(EN_LYRIC, "Катя держи",
                                       "artist") is True


def test_en_title_en_lyric_accepted():
    assert peak._lyric_script_mismatch(EN_LYRIC, "Billie Jean",
                                       "Michael Jackson") is False


def test_romanized_title_residue_accepted():
    # SPIN (Kroi): no non-Latin evidence in title/artist - the guard cannot
    # help here; LRClib-first + duration keying already fixed the live case.
    assert peak._lyric_script_mismatch(EN_LYRIC, "SPIN", "Kroi") is False


def test_too_short_lyric_not_judged():
    assert peak._lyric_script_mismatch("[00:01.00]la\n",
                                       "愛して", "Kikuo") is False


# ------------------------------------- wiring: ensure_lyrics falls through
def _stage(tmp_path, monkeypatch, lyric_text, fetch_lyrics_result=False,
           desc_result=False):
    base = str(tmp_path / "track")
    calls = []

    fake = types.ModuleType("titanium_enrich")
    fake.__path__ = []
    fake._last_dur = None

    def _fake_fetch_lyrics(*a, **k):
        calls.append(a)
        return fetch_lyrics_result
    fake.fetch_lyrics = _fake_fetch_lyrics
    fake.lyric_lang = lambda: ""
    fake._detect_lang_from_title = lambda t, a: "en"
    fake.rank_vtt_candidates = lambda vtts, wl: list(vtts)

    def _fake_vtt_to_lrc(path, out):
        with open(out, "w", encoding="utf-8") as f:
            f.write(lyric_text)
        return True
    fake.vtt_to_lrc = _fake_vtt_to_lrc
    fake.fetch_lyrics_youtube = lambda *a, **k: desc_result

    monkeypatch.setitem(sys.modules, "titanium_enrich", fake)
    monkeypatch.setattr(peak, "fetch_caption_vtt",
                        lambda *a, **k: str(tmp_path / "fake.vtt"))
    return base, calls


def test_rejected_caption_is_deleted_and_falls_through(tmp_path, monkeypatch):
    base, calls = _stage(tmp_path, monkeypatch, EN_LYRIC)
    assert peak.ensure_lyrics(base, "https://youtu.be/x",
                              "愛して 愛して 愛して", "Kikuo",
                              captions=True) == ""
    assert not os.path.exists(base + ".lrc")
    assert len(calls) == 1  # the LRClib stage ran and was skipped by the fake


def test_accepted_caption_returns_caption(tmp_path, monkeypatch):
    base, calls = _stage(tmp_path, monkeypatch, JA_LYRIC)
    assert peak.ensure_lyrics(base, "https://youtu.be/x",
                              "愛して 愛して 愛して", "Kikuo",
                              captions=True) == "caption"
    assert os.path.exists(base + ".lrc")


def test_rejection_reaches_description_fallback(tmp_path, monkeypatch):
    base, calls = _stage(tmp_path, monkeypatch, EN_LYRIC, desc_result=True)
    assert peak.ensure_lyrics(base, "https://youtu.be/x",
                              "愛して 愛して 愛して", "Kikuo",
                              captions=True) == "youtube-desc"


def test_lrclib_success_short_circuits_guard(tmp_path, monkeypatch):
    base, calls = _stage(tmp_path, monkeypatch, JA_LYRIC,
                         fetch_lyrics_result=True)
    assert peak.ensure_lyrics(base, "https://youtu.be/x",
                              "愛して 愛して 愛して", "Kikuo",
                              captions=True) == "lrclib"
    assert len(calls) == 1
    assert calls[0][1] == "愛して 愛して 愛して"  # title passed through
    assert not os.path.exists(base + ".lrc")  # the caption stage never ran
