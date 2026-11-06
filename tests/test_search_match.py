"""Search-matcher regression tests (2026-11-06, v1.1.4 increment).

Fixtures transcribed from the user's real 50-track Spotify->YTM transfer
log (2026-11-06): the observed wrong picks are ground truth for what
pick_best must STOP returning, and the correct candidates must win when
present. No network: pick_best is called with enrich_top=0 (the default),
so it never touches yt-dlp in these tests.

Run:  cd D:/PEAK_Master && build_venv/Scripts/python.exe -m pytest tests/test_search_match.py -q
"""
import os
import sys

sys.path.insert(0, r"D:\PEAK_Master")

import PEAK_GITHUB_true as peak


def _res(rid, title, channel, seconds, views):
    return {"id": rid, "title": title, "channel": channel,
            "seconds": seconds, "views": views}


# ---------------------------------------------------------------- case 1
# User log: 'Chemical Rabbit Hole' (Demi the Daredevil, kroh) -> matched
# Adam Lambert's 'Down The Rabbit Hole' (48M views). The right track, when
# present, must win; when absent, pick_best must return None (no confident
# match), never the wrong song.
WRONG_RABBIT = _res("adam", "Down The Rabbit Hole", "Adam Lambert",
                    244, 48_000_000)
RIGHT_RABBIT = _res("demi",
                    "Demi the Daredevil - Chemical Rabbit Hole (Official Audio)",
                    "Demi the Daredevil - Topic", 241, 12_400)


def test_rabbit_right_candidate_wins():
    best = peak.pick_best([WRONG_RABBIT, RIGHT_RABBIT], target_seconds=242,
                          artist="Demi the Daredevil, kroh",
                          title="Chemical Rabbit Hole")
    assert best is not None and best["id"] == "demi"


def test_rabbit_wrong_only_returns_none():
    best = peak.pick_best([WRONG_RABBIT], target_seconds=242,
                          artist="Demi the Daredevil, kroh",
                          title="Chemical Rabbit Hole")
    assert best is None


# ---------------------------------------------------------------- case 2
# User log: 'clavar la espada' (Spanish-titled JP track) -> 'La Espada'
# (Release - Topic, 2:14 vs a ~3:50 target). The duration gate rejects the
# candidate, and the old `return best or results[0]` then returned the
# rejected candidate anyway. Must be None.
def test_espada_duration_reject_survives_no_fallback():
    wrong = _res("espada", "La Espada", "Release - Topic", 134, 210_000)
    best = peak.pick_best([wrong], target_seconds=230,
                          artist="Bleach", title="clavar la espada")
    assert best is None


# ---------------------------------------------------------------- case 3
# User log: 'NUMBER' (Elliot Sutton) -> 'Golden Brown x Love Story by
# Elliot Sutton' (1:10 medley). Artist tokens appear in the title so
# containment passes; the duration gate must reject it and the function
# must NOT fall back to it.
def test_number_medley_not_returned():
    wrong = _res("gb", "Golden Brown x Love Story by Elliot Sutton",
                 "RouteNote_Asia", 70, 89_000)
    best = peak.pick_best([wrong], target_seconds=196,
                          artist="Elliot Sutton", title="NUMBER")
    assert best is None


# ---------------------------------------------------------------- case 4
# User log: 'ice - slowed' -> 'ice - super slowed'. The exact title must
# outrank the 'super' variant when both exist (a capped views score stops
# popularity from deciding).
def test_ice_exact_beats_super_slowed():
    sup = _res("ss", "ice - super slowed", "ZERTAL - Topic", 173, 1_200_000)
    exact = _res("ex", "ice - slowed", "ZERTAL - Topic", 165, 340_000)
    best = peak.pick_best([sup, exact], target_seconds=166,
                          artist="ZERTAL", title="ice - slowed")
    assert best is not None and best["id"] == "ex"


# ---------------------------------------------------------------- case 5
# Positive controls: the correct official uploads must still win.
def test_billie_jean_official_wins():
    mj = _res("mj", "Michael Jackson - Billie Jean (Official Video)",
              "Michael Jackson", 294, 1_400_000_000)
    best = peak.pick_best([mj], target_seconds=294,
                          artist="Michael Jackson", title="Billie Jean")
    assert best is not None and best["id"] == "mj"


def test_bling_bang_born_topic_wins():
    cn = _res("cn", "Creepy Nuts - Bling-Bang-Bang-Born",
              "Creepy Nuts - Topic", 170, 500_000_000)
    best = peak.pick_best([cn], target_seconds=170,
                          artist="Creepy Nuts", title="Bling-Bang-Bang-Born")
    assert best is not None and best["id"] == "cn"


# ---------------------------------------------------------------- case 6
# Topic channels are often the CORRECT official audio: never blanket-
# penalize them; containment may be satisfied by the artist embedded in
# the channel name.
def test_topic_channel_counts_as_containment():
    r = _res("t1", "Some Obscure Song", "Some Obscure Artist - Topic",
             200, 90_000)
    best = peak.pick_best([r], target_seconds=199,
                          artist="Some Obscure Artist",
                          title="Some Obscure Song")
    assert best is not None and best["id"] == "t1"


# ---------------------------------------------------------------- case 7
# Accented artist: _artist_tokens normalizes via _norm_txt (NFKD) and the
# haystack via the same normalizer - containment must survive accents
# (regression guard for the _loose_match normalizer question).
def test_accented_artist_containment():
    r = _res("v1", "VOILÀ - Something Blue", "VOILÀ - Topic",
             187, 50_000)
    best = peak.pick_best([r], target_seconds=187, artist="VOILÀ",
                          title="Something Blue")
    assert best is not None and best["id"] == "v1"


# ---------------------------------------------------------------- case 8
# One-character artist: no content tokens survive -> the containment gate
# is skipped entirely and the similarity gates decide (never a crash).
def test_one_char_artist_does_not_crash():
    r = _res("x1", "Kiss", "X", 200, 1000)
    best = peak.pick_best([r], target_seconds=200, artist="X", title="Kiss")
    assert best is None or best.get("id") == "x1"
