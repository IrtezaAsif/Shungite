"""Strict Spotify→YouTube track matcher — spotDL-grade, v2.

Two ranked lists per query, fused by position (Borda count):
  * POPULARITY list: candidates sorted by view_count desc (log-scaled)
  * RELEVANCE list: candidates sorted by the search engine's own ordering
    (InnerTube / yt-dlp return results in relevance order) plus fuzzy
    title+artist score.
A candidate's final score = (pop_rank_weight) + (rel_rank_weight) − penalties.
The #1 on BOTH lists wins; anything failing the HARD gates is dropped first.

Hard gates:
  * must share at least one >2-char word with the target title
  * if dur caps enabled: seconds within [min_s, max_s] (defaults 40..720)
    — when a candidate has no duration we let it through but rank it lower.

No duration-MATCH gate anymore (user requested removal); only the caps.
"""
import re
import unicodedata

try:
    from rapidfuzz import fuzz as _fuzz
    def _ratio(a, b): return _fuzz.token_set_ratio(a, b)
    def _partial(a, b): return _fuzz.partial_ratio(a, b)
except Exception:
    import difflib
    def _ratio(a, b): return difflib.SequenceMatcher(None, a, b).ratio() * 100
    def _partial(a, b): return _ratio(a, b)

BAD_WORDS = ("karaoke", "nightcore", "8d", "8-d", "sped up", "slowed",
             "reverb", "remix", "mashup", "tribute", "instrumental",
             "acoustic", "parody", "fan made", "amv", "reaction",
             "dance practice", "teaser", "trailer", "snippet", "megamix",
             "compilation", "full album", "medley", "hour", "chill beats",
             "lofi girl", "radio", "lyric video", "lyrics", "8 bit",
             "cover version", "tribute to")


def _norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_mmss(s):
    if not s:
        return None
    try:
        p = [int(x) for x in str(s).split(":")]
        if len(p) == 2:
            return p[0] * 60 + p[1]
        if len(p) == 3:
            return p[0] * 3600 + p[1] * 60 + p[2]
    except Exception:
        return None
    return None


class _C:
    __slots__ = ("id", "url", "title", "channel", "seconds", "views",
                 "idx", "score")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)



_CJK_RE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\u3040-\u30ff]")


def _cjk_sim(a, b):
    """CJK coverage: how much of title-a's CJK chars appear in b. Recall-
    based so candidates with extra decoration (歌詞付き, official tags) don't
    get penalized. 0.0-1.0."""
    ca = set(_CJK_RE.findall(a or ""))
    if not ca:
        return 0.0
    cb = set(_CJK_RE.findall(b or ""))
    if not cb:
        return 0.0
    inter = len(ca & cb)
    return inter / len(ca)


def _sim(a_title, b_title):
    """Combined similarity: ascii-norm ratio OR CJK char overlap, whichever
    is higher. Handles 陷入爱情 vs 陷入愛情 (traditional) and 伴奏版 suffixes
    that _norm would strip to empty."""
    s1 = _ratio(_norm(a_title), _norm(b_title))
    s2 = _cjk_sim(a_title, b_title)
    return s1 if s1 >= s2 else s2


def strict_match(candidates, title, artist, target_seconds=None,
                 min_fuzz=65,
                 caps_on=True, cap_min_s=40, cap_max_s=720,
                 return_all=False):
    """Dual-rank fusion matcher.

    Args:
        candidates: iterable of dicts (id,url,title,channel,seconds|duration,
                    views). Position in the iterable = relevance rank (the
                    search engine's own ordering).
        caps_on / cap_min_s / cap_max_s: per-tab adjustable length window.

    Returns best candidate dict, or None. With return_all=True returns
    (best, [(score, dict), ...]) for debugging.
    """
    q_t = _norm(title)
    q_a = _norm(artist)
    q_full = _norm(f"{artist} {title}")
    q_tokens = set(w for w in q_t.split() if len(w) > 2)

    # build candidate objects with the engine's relevance index preserved
    cands = []
    for i, c in enumerate(candidates or []):
        secs = c.get("seconds") or parse_mmss(c.get("duration"))
        cands.append(_C(id=c.get("id"), url=c.get("url", ""),
                        title=c.get("title") or "",
                        channel=c.get("channel") or "",
                        seconds=secs, views=c.get("views") or 0,
                        idx=i, score=0.0))

    # HARD GATES -------------------------------------------------------
    clean = []
    for c in cands:
        if not c.id:
            continue
        t_norm = _norm(c.title)
        t_tokens = set(w for w in t_norm.split() if len(w) > 2)
        # word-overlap requirement (kills "Don't Be A Hero" -> "Don't Run").
        # CJK bridges: (a) CJK query title covered by candidate title,
        # (b) CJK ARTIST appears verbatim in candidate title/channel — the
        # uploader named the artist, so it's the right song even when the
        # title itself is romanized.
        if q_tokens and not (q_tokens & t_tokens):
            _t_cjk_ok = (_CJK_RE.search(title)
                         and _cjk_sim(title, c.title) >= 0.5)
            _a_cjk_ok = (_CJK_RE.search(artist)
                         and _cjk_sim(artist, c.title + " " + c.channel)
                         >= 0.6)
            if not (_t_cjk_ok or _a_cjk_ok):
                continue
        if caps_on and c.seconds is not None:
            if c.seconds < cap_min_s or c.seconds > cap_max_s:
                continue
        clean.append(c)
    if not clean:
        return (None, []) if return_all else None

    # RELEVANCE rank — search engine position + fuzzy title/artist score
    rel_ranked = sorted(clean, key=lambda c: (
        -(c.idx * -1),      # engine order (lower idx = more relevant)
    ))
    # boost relevance by fuzzy similarity so an exact-title beat an early
    # mismatched result
    def rel_key(c):
        fz_t = _partial(q_t, _norm(c.title))
        fz_full = _ratio(q_full, _norm(f"{c.channel} {c.title}"))
        return (-(fz_t * 0.6 + fz_full * 0.4) + c.idx * 0.5)
    rel_ranked = sorted(clean, key=rel_key)
    rel_pos = {c.id: i for i, c in enumerate(rel_ranked)}

    # POPULARITY rank — view count (log scale for stability)
    pop_ranked = sorted(clean, key=lambda c: -(c.views or 0))
    pop_pos = {c.id: i for i, c in enumerate(pop_ranked)}

    # FUSE + penalties --------------------------------------------------
    n = max(len(clean), 1)
    for c in clean:
        rel = (n - rel_pos[c.id]) / n
        pop = (n - pop_pos[c.id]) / n
        c.score = rel * 0.55 + pop * 0.45
        # fuzzy floors — anything too far from the query gets zeroed.
        # CJK queries: the ascii fuzz is meaningless; use the CJK bridge.
        fz_full = _ratio(q_full, _norm(f"{c.channel} {c.title}"))
        cjk_ok = ((_CJK_RE.search(title)
                   and _cjk_sim(title, c.title) >= 0.5)
                  or (_CJK_RE.search(artist)
                      and _cjk_sim(artist, c.title + " " + c.channel)
                      >= 0.6))
        if fz_full < min_fuzz and not cjk_ok:
            c.score -= 0.8
        # junk word penalties (unless in query)
        t_norm = _norm(c.title)
        pen = 0
        for w in BAD_WORDS:
            if w in t_norm and w not in q_full:
                pen += 0.25
        c.score -= pen
        # official/topic boost
        ch = _norm(c.channel)
        if (q_a and (q_a in ch)) or "topic" in ch:
            c.score += 0.08

    clean.sort(key=lambda c: -c.score)
    best = clean[0]
    if best.score < 0.25:
        return (None, [(c.score, _as_dict(c)) for c in clean]) \
            if return_all else None
    bd = _as_dict(best)
    if return_all:
        return bd, [(c.score, _as_dict(c)) for c in clean]
    return bd


def _as_dict(c):
    return {"id": c.id, "url": c.url, "title": c.title,
            "channel": c.channel, "seconds": c.seconds,
            "views": c.views}
