"""MusicBrainz release lookup for ZINCUM - free, no key.
Fills album/year when iTunes misses (beets/Picard approach)."""
import json
import urllib.parse
import urllib.request

UA = {"User-Agent": "ZINCUM/1.0 (music library enricher; personal use)"}


def _get(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def lookup_release(title, artist=""):
    """Best-match recording. Returns {album, year, artist} or {}."""
    q = f'recording:"{title}"'
    if artist:
        q += f' AND artist:"{artist}"'
    url = ("https://musicbrainz.org/ws/2/recording/?query=" +
           urllib.parse.quote(q) +
           "&fmt=json&limit=3")
    d = _get(url)
    if not d or not d.get("recordings"):
        return {}
    tl = (title or "").lower().strip()
    for rec in d["recordings"]:
        if tl:
            tr = (rec.get("title") or "").lower()
            # accept if the query title appears in the result or vice versa
            if tl[:20] not in tr and tr[:20] not in tl:
                continue
        out = {}
        releases = rec.get("releases") or []
        if releases:
            rel = releases[0]
            out["album"] = rel.get("title")
            date = rel.get("date") or ""
            if date:
                out["year"] = date[:4]
            acs = rec.get("artist-credit") or []
            if acs:
                out["artist"] = acs[0].get("artist", {}).get("name")
        # recording-level year fallback
        if "year" not in out and rec.get("first-release-date"):
            out["year"] = rec["first-release-date"][:4]
        if out:
            return out
    return {}


def lookup_tracklist(title, artist=""):
    """Full album tracklist via MusicBrainz release lookup. Returns
    {album, year, artist, tracks: [(track_no, track_title, duration_ms)]}
    or {} on failure."""
    # 1) find the release id for this recording
    q = f'recording:"{title}"'
    if artist:
        q += f' AND artist:"{artist}"'
    url = ("https://musicbrainz.org/ws/2/recording/?query=" +
           urllib.parse.quote(q) + "&fmt=json&limit=3")
    d = _get(url)
    if not d or not d.get("recordings"):
        return {}
    tl = (title or "").lower().strip()
    relid = None
    for rec in d["recordings"]:
        if tl:
            tr = (rec.get("title") or "").lower()
            if tl[:20] not in tr and tr[:20] not in tl:
                continue
        rels = rec.get("releases") or []
        if rels and rels[0].get("id"):
            relid = rels[0].get("id")
            break
    if not relid:
        return {}
    # 2) fetch the release's full track listing
    rel_url = ("https://musicbrainz.org/ws/2/release/" + relid +
               "?inc=recordings&fmt=json")
    rd = _get(rel_url)
    if not rd:
        return {}
    out = {"album": rd.get("title", ""),
           "year": (rd.get("date") or "")[:4],
           "tracks": []}
    ac = rd.get("artist-credit") or []
    if ac:
        out["artist"] = ac[0].get("artist", {}).get("name")
    media = rd.get("media") or []
    for med in media:
        for t in med.get("tracks", []):
            out["tracks"].append((
                int(t.get("position") or 0),
                t.get("title", ""),
                t.get("length") or t.get("recording", {}).get("length") or 0))
    return out
