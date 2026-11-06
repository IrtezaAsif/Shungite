"""Caption-pipeline regression suite (2026-11-06 session).

Run:  cd D:\\PEAK_Master && build_venv/Scripts/python.exe -m pytest tests -q
Covers: decoupled single-track caption fetch (never fatal), narrowed
caption language list (no en.* wildcard), inventory-based caption picker,
vtt_to_lrc artifact stripping + rolling dedupe, translation demotion in
vtt ranking, orphan sidecar sweep on failure paths.
"""
import importlib.util
import inspect
import os
import sys

import pytest

PEAK_DIR = r"D:\PEAK_Master"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


peak = _load("peak_cap", os.path.join(PEAK_DIR, "PEAK_GITHUB_true.py"))
ten = _load("titanium_enrich_cap", os.path.join(PEAK_DIR, "titanium_enrich.py"))


# -- 1. the audio download never requests subtitles inline ----------------
def test_download_one_has_no_inline_subtitle_requests():
    src = inspect.getsource(peak.download_one)
    assert "writesubtitles" not in src
    assert "subtitleslangs" not in src
    assert "writeautomaticsub" not in src


# -- 2. the caption fetch requests exactly ONE non-wildcard code -----------
def test_caption_fetch_requests_exactly_one_code(monkeypatch, tmp_path):
    import yt_dlp

    recorded = {}

    class _Rec(object):
        def __init__(self, opts=None, *a, **k):
            recorded.update(opts or {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, *a, **k):
            return {"subtitles": {"ja": [{"url": "x"}],
                                  "en": [{"url": "y"}]},
                    "automatic_captions": {"ja-orig": [{"url": "z"}],
                                           "en": [{"url": "w"}]},
                    "language": "ja"}

        def download(self, *a, **k):
            return 0

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _Rec)
    base = os.path.join(str(tmp_path), "song")
    peak.fetch_caption_vtt("https://www.youtube.com/watch?v=abc", base,
                           "SPIN", "Kroi")
    langs = recorded.get("subtitleslangs")
    assert isinstance(langs, list) and len(langs) == 1
    assert "*" not in langs[0]
    assert langs[0] == "ja"          # native manual track, never a translation


# -- 3. inventory picker: one code, native first, never a translation ------
def test_pick_caption_code_none_when_no_native_track():
    # Real shape (Ado usseewa, Qp3b-RXtz4w): uploader added ar/zh/en/ko/ru/es
    # manual translations, no ja manual, no ja-orig ASR -> return None so
    # LRClib provides Japanese lyrics instead of a wrong translation.
    info = {"subtitles": {k: [{}] for k in
                          ("ar", "zh", "en", "ko", "ru", "es", "live_chat")},
            "automatic_captions": {k: [{}] for k in
                                   ("ar", "zh", "en", "ko", "ru", "es")},
            "language": None}   # real payload: Ado reports language=None
    assert ten.pick_caption_code(info, want_lang="ja") == (None, False)


def test_pick_caption_code_video_language_manual_first():
    info = {"subtitles": {"ja": [{}], "en": [{}]},
            "automatic_captions": {"ja-orig": [{}], "en": [{}]},
            "language": "ja"}
    code, is_auto = ten.pick_caption_code(info, want_lang="ja")
    assert code == "ja" and is_auto is False


def test_pick_caption_code_asr_original_when_no_manual():
    info = {"subtitles": {},
            "automatic_captions": {"ja-orig": [{}], "en": [{}],
                                   "es": [{}]},
            "language": None}
    code, is_auto = ten.pick_caption_code(info, want_lang="ja")
    assert code == "ja-orig" and is_auto is True


def test_pick_caption_code_english_video_gets_english():
    info = {"subtitles": {"en": [{}]},
            "automatic_captions": {"en": [{}]},
            "language": "en"}
    code, is_auto = ten.pick_caption_code(info, want_lang="")
    assert code == "en" and is_auto is False


# -- 4. vtt_to_lrc strips every artifact the user saw ----------------------
_RAW_VTT = (
    "WEBVTT\n"
    "Kind: captions\n"
    "Language: en\n"
    "\n"
    "NOTE this is a note block\n"
    "that spans lines\n"
    "\n"
    "1\n"
    "00:00:01.000 --> 00:00:03.000 align:start position:0%\n"
    "\n"
    "<00:00:01.000><c>Hello</c>\n"
    "\n"
    "2\n"
    "00:00:03.000 --> 00:00:05.500 align:start position:0%\n"
    "\n"
    "Hello<00:00:03.400><c>World</c>\n"
    "\n"
    "3\n"
    "00:00:05.500 --> 00:00:07.000\n"
    "\n"
    "[Music]\n"
    "\n"
    "4\n"
    "00:00:07.000 --> 00:00:09.000\n"
    "\n"
    "The world is beautiful &amp; wide\n"
    "\n"
    "5\n"
    "00:00:09.000 --> 00:00:11.000\n"
    "\n"
    "The world is beautiful &amp; wide\n"
)


def test_vtt_to_lrc_strips_artifacts_and_rolls(tmp_path):
    v = tmp_path / "song.ja.vtt"
    v.write_text(_RAW_VTT, encoding="utf-8")
    lrc = tmp_path / "song.lrc"
    assert ten.vtt_to_lrc(str(v), str(lrc)) is True
    txt = lrc.read_text(encoding="utf-8")
    for bad in ("<c>", "align:", "NOTE", "&amp;", "<", ">", "&#39;",
                "[Music]", "WEBVTT"):
        assert bad not in txt, bad
    assert "Hello" in txt and "World" in txt
    assert "beautiful & wide" in txt           # entities decoded, text kept
    lines = [l for l in txt.splitlines() if l.strip()]
    assert len(lines) == len(set(lines))        # no duplicated lines
    assert len(lines) <= 3                      # rolling cues collapsed


def test_vtt_to_lrc_rejects_junk_instead_of_embedding_it(tmp_path):
    v = tmp_path / "x.en.vtt"
    v.write_text("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\n\n[Music]\n",
                 encoding="utf-8")
    lrc = tmp_path / "x.lrc"
    assert ten.vtt_to_lrc(str(v), str(lrc)) is False
    assert not lrc.exists() or lrc.stat().st_size == 0


# -- 5. ranking: native/und first, translations last -----------------------
def test_rank_vtt_demotes_translations_below_english():
    paths = ["stem.en-ja.vtt", "stem.ja.vtt", "stem.en.vtt", "stem.und.vtt"]
    ranked = ten.rank_vtt_candidates(paths, want_lang="")
    assert ranked[0] in ("stem.ja.vtt", "stem.und.vtt")
    assert ranked.index("stem.en-ja.vtt") > ranked.index("stem.en.vtt")


def test_rank_vtt_want_language_wins_over_translations():
    paths = ["stem.en-ja.vtt", "stem.ja.vtt", "stem.en.vtt"]
    ranked = ten.rank_vtt_candidates(paths, want_lang="ja")
    assert ranked[0] == "stem.ja.vtt"


# -- 6. caption fetch is best-effort: a 429 can never raise out ------------
def test_caption_fetch_never_raises(monkeypatch, tmp_path):
    import yt_dlp

    class _Boom(object):
        def __init__(self, *a, **k):
            raise RuntimeError("HTTP Error 429: Too Many Requests")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _Boom)
    out = peak.fetch_caption_vtt("https://www.youtube.com/watch?v=abc",
                                 os.path.join(str(tmp_path), "s"),
                                 "SPIN", "Kroi")
    assert out == ""


# -- 6b. lyric pipeline order: LRClib first, captions only as fallback -----
def test_ensure_lyrics_lrclib_win_skips_captions(monkeypatch, tmp_path):
    import titanium_enrich as ten_canon
    calls = []
    monkeypatch.setattr(ten_canon, "fetch_lyrics",
                        lambda *a, **k: calls.append("lrclib") or True)
    monkeypatch.setattr(peak, "fetch_caption_vtt",
                        lambda *a, **k: calls.append("caption") or "")
    out = peak.ensure_lyrics(os.path.join(str(tmp_path), "song"),
                             "https://youtu.be/x", "SPIN", "Kroi")
    assert calls == ["lrclib"]


def test_ensure_lyrics_falls_back_to_captions(monkeypatch, tmp_path):
    import titanium_enrich as ten_canon
    calls = []
    monkeypatch.setattr(ten_canon, "fetch_lyrics",
                        lambda *a, **k: calls.append("lrclib") or False)
    monkeypatch.setattr(peak, "fetch_caption_vtt",
                        lambda *a, **k: calls.append("caption")
                        or os.path.join(str(tmp_path), "song.ja.vtt"))
    with open(os.path.join(str(tmp_path), "song.ja.vtt"), "w",
              encoding="utf-8") as f:
        f.write("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\n\nharu ga kuru\n"
                "\n2\n00:00:03.000 --> 00:00:04.000\n\nkimi to natsu\n")
    out = peak.ensure_lyrics(os.path.join(str(tmp_path), "song"),
                             "https://youtu.be/x", "SPIN", "Kroi")
    assert calls == ["lrclib", "caption"]
    assert os.path.exists(os.path.join(str(tmp_path), "song.lrc"))


# -- 7. orphan sweep: partials/art die, audio+lrc+m3u+txt live -------------
def test_sweep_orphan_sidecars(tmp_path):
    m = tmp_path / "Music"
    m.mkdir()
    (m / "Song A.opus").write_bytes(b"x")
    (m / "Song A.lrc").write_text("[00:01.00] hi\n", encoding="utf-8")
    (m / "Song A.ja.vtt").write_text("WEBVTT\n", encoding="utf-8")  # lrc exists
    (m / "Broken.webm.part").write_bytes(b"x")
    (m / "Broken.webp").write_bytes(b"x")
    (m / "Broken.en-ja.vtt").write_text("WEBVTT\n", encoding="utf-8")
    (m / "Broken.info.json").write_text("{}", encoding="utf-8")
    (m / "playlist.m3u").write_text("", encoding="utf-8")
    (m / "notes.txt").write_text("", encoding="utf-8")
    n = peak.sweep_orphan_sidecars(str(m))
    assert not (m / "Broken.webm.part").exists()
    assert not (m / "Broken.webp").exists()
    assert not (m / "Broken.en-ja.vtt").exists()
    assert not (m / "Broken.info.json").exists()
    assert not (m / "Song A.ja.vtt").exists()       # lrc already made
    for keep in ("Song A.opus", "Song A.lrc", "playlist.m3u", "notes.txt"):
        assert (m / keep).exists(), keep
    assert n >= 5


# -- 8. duration reaches LRClib on every path ------------------------------
def test_ensure_lyrics_passes_real_duration(monkeypatch, tmp_path):
    import struct
    import wave
    import titanium_enrich as ten_canon
    wav = tmp_path / "song.wav"
    with wave.open(str(wav), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * 8000)      # exactly 1 second
    seen = {}

    def fake_fetch(artist, title, audio_path, duration_s=None):
        seen["dur"] = duration_s
        return True

    monkeypatch.setattr(ten_canon, "fetch_lyrics", fake_fetch)
    out = peak.ensure_lyrics(os.path.join(str(tmp_path), "song"),
                             "", "X", "Y")
    assert out == "lrclib"
    assert seen["dur"] == 1
