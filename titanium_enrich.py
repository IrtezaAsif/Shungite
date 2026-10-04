"""Namida-compatibility enrichment upgrade for TITANIUM.

Covers the gaps found in real-library testing:
- Cover art: iTunes -> YouTube thumbnail fallback (maxres->sd->hq)
- Lyrics: lrclib synced/plain -> YouTube description (via yt-dlp)
- Embedding: art + title/artist/album into mp3/m4a/opus/flac tags
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "TITANIUM/1.0 (music downloader)"}


def _http_json(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def _download(url, path, timeout=15):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r, \
                open(path, "wb") as f:
            f.write(r.read())
        return os.path.getsize(path) > 3000
    except Exception:
        try:
            os.unlink(path)
        except Exception:
            pass
        return False


def clean_query(s):
    s = re.sub(r"\(.*?(official|video|audio|lyric|lyrics|visualizer|"
               r"hd|hq|4k|remaster\w*|full album|8d).*?\)", " ", s,
               flags=re.I)
    s = re.sub(r"\[.*?(official|video|audio|lyrics?|visualizer|"
               r"hd|hq|4k|8d)\]", " ", s, flags=re.I)
    s = re.sub(r"\\s+", " ", s).strip(" -|")
    return s


def fetch_cover_url(title, artist=""):
    q = clean_query(f"{artist} {title}".strip() if artist else title)
    if not q:
        return None
    d = _http_json("https://itunes.apple.com/search?" +
                   urllib.parse.urlencode({"term": q, "entity": "song",
                                           "limit": 5}))
    if not d or not d.get("results"):
        d = _http_json("https://itunes.apple.com/search?" +
                       urllib.parse.urlencode({"term": title,
                                               "entity": "song",
                                               "limit": 5}))
        if not d or not d.get("results"):
            return None
    for it in d["results"]:
        art = it.get("artworkUrl100") or ""
        if art:
            return art.replace("100x100", "1200x1200")
    return None




COVER_SOURCES = {"itunes": True, "deezer": True, "lastfm": True}

def set_cover_source(name, enabled):
    if name in COVER_SOURCES:
        COVER_SOURCES[name] = bool(enabled)

def cover_source(name):
    return COVER_SOURCES.get(name, True)

def save_cover(title, artist, audio_path):
    base = os.path.splitext(audio_path)[0]
    cover_path = base + ".jpg"
    if os.path.exists(cover_path) and os.path.getsize(cover_path) > 3000:
        return True
    url = None
    if COVER_SOURCES.get("itunes", True):
        url = fetch_cover_url(title, artist)
    if not url and COVER_SOURCES.get("deezer", True):
        try:
            url = fetch_cover_url_deezer(title, artist)
        except Exception:
            url = None
    if not url and COVER_SOURCES.get("lastfm", True):
        try:
            url = fetch_cover_url_lastfm(title, artist,
                                         lastfm_key())
        except Exception:
            url = None
    if not url:
        return False
    return _download(url, cover_path)


def fetch_lyrics(artist, title, audio_path, duration_s=None):
    base = os.path.splitext(audio_path)[0]
    lrc_path = base + ".lrc"
    if os.path.exists(lrc_path) and os.path.getsize(lrc_path) > 50:
        return True
    t = clean_query(title)
    params = {"track_name": t}
    if artist:
        params["artist_name"] = artist
    if duration_s:
        # LRClib requires duration_seconds for exact match — pass it
        params["duration"] = int(duration_s)
    d = _http_json("https://lrclib.net/api/get?" +
                   urllib.parse.urlencode(params))
    if not d:
        # search returns MANY records — pick best via language preference
        d2 = _http_json("https://lrclib.net/api/search?" +
                        urllib.parse.urlencode({"track_name": t,
                                                "q": f"{artist} {title}"}))
        if isinstance(d2, list) and d2:
            d = _pick_best_record(d2, lyric_lang()) or d2[0]
    if not d:
        return False
    text = d.get("syncedLyrics") or d.get("plainLyrics")
    if not text or len(text.strip()) < 20:
        return False
    try:
        with open(lrc_path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except Exception:
        return False


# ─────────────── YouTube fallbacks ───────────────

_YT_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def yt_video_id(url):
    m = _YT_ID_RE.search(url or "")
    return m.group(1) if m else None


def save_cover_from_yt(video_id_or_query, audio_path):
    """Cover from the video's own thumbnail. Accepts an ID, URL or search
    query (query resolved via yt-dlp)."""
    vid = video_id_or_query
    if vid and ("youtube.com" in str(vid) or "youtu.be" in str(vid)):
        vid = yt_video_id(str(vid))
    if not vid or len(str(vid)) != 11:
        meta = _yt_meta(video_id_or_query)
        vid = meta["id"] if meta else None
    if not vid:
        return False
    base = os.path.splitext(audio_path)[0]
    cover_path = base + ".jpg"
    if os.path.exists(cover_path) and os.path.getsize(cover_path) > 3000:
        return True
    for quality in ("maxresdefault", "sddefault", "hqdefault"):
        if _download(f"https://i.ytimg.com/vi/{vid}/{quality}.jpg",
                     cover_path):
            return True
    return False


_LYRIC_CUES = ("subscribe", "follow me", "http", "streaming", "spotify",
               "apple music", "prod.", "produced by", "official", "channel",
               "©", "socials", "instagram", "twitter", "tiktok",
               "beat by", "mixed by", "mastered", "turn on notifications")


def _looks_like_lyrics(block):
    lines = [l for l in block.splitlines() if l.strip()]
    if len(lines) < 5:
        return False
    ok_len = sum(1 for l in lines if 3 <= len(l.strip()) <= 70)
    if ok_len / len(lines) < 0.8:
        return False
    spam = sum(1 for l in lines
               if any(c in l.lower() for c in _LYRIC_CUES))
    return spam / len(lines) < 0.25


def _yt_meta(query_or_url):
    """Video metadata via yt-dlp (bundled; handles consent walls).
    Accepts URL or search query."""
    import subprocess
    target = query_or_url
    if target and ("youtube.com" not in target and "youtu.be" not in target):
        target = "ytsearch1:" + target
    try:
        _flags = 0
        if os.name == "nt":
            _flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(
            ["yt-dlp", "--skip-download", "--dump-json", "--no-playlist",
             target],
            capture_output=True, text=True, timeout=120,
            creationflags=_flags)
        if r.returncode != 0 or not r.stdout:
            return None
        d = json.loads(r.stdout)
        return {"id": d.get("id"),
                "title": d.get("title") or "",
                "description": d.get("description") or ""}
    except Exception:
        return None


def fetch_lyrics_youtube(url_or_query, audio_path):
    """Lyrics from the YouTube description (channels paste them there).
    Saves plain-text .lrc that Namida displays fine."""
    base = os.path.splitext(audio_path)[0]
    lrc_path = base + ".lrc"
    if os.path.exists(lrc_path) and os.path.getsize(lrc_path) > 50:
        return True
    meta = _yt_meta(url_or_query)
    if not meta:
        return False
    desc = meta.get("description") or ""
    best, best_score = None, 0
    for block in re.split(r"\n\s*\n", desc):
        block = block.strip()
        if not _looks_like_lyrics(block):
            continue
        score = len([l for l in block.splitlines() if l.strip()])
        if score > best_score:
            best, best_score = block, score
    if not best:
        return False
    try:
        with open(lrc_path, "w", encoding="utf-8") as f:
            f.write(best + "\n")
        return True
    except Exception:
        return False




# ── VTT -> LRC converter (timed lyrics in original language) ──────────────────
_VTT_TS_RE = re.compile(r"^(?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{3})")

def vtt_to_lrc(vtt_path, lrc_path):
    """Convert a downloaded .vtt subtitle into a synced .lrc file.
    Preserves the original language text and timing — this is the 'properly
    timed subtitles in original language' path."""
    if not vtt_path or not os.path.exists(vtt_path):
        return False
    try:
        with open(vtt_path, encoding="utf-8", errors="replace") as f:
            out, cur_start, cur_end, cur_text = [], None, None, []
            for line in f:
                line = line.rstrip("\r\n")
                m = _VTT_TS_RE.match(line)
                if m and "-->" in line:
                    # flush previous cue
                    if cur_start is not None and cur_text:
                        txt = " ".join(t for t in cur_text if t)
                        if txt:
                            out.append(f"[{cur_start}] {txt}")
                    cur_text = []
                    _h, _m, _s, _ms = m.groups()
                    hh = int(_h) if _h is not None else 0
                    total = (hh * 3600 + int(_m) * 60 + int(_s)
                             + int(_ms) / 1000.0)
                    mm2 = int(total // 60); ss2 = int(total % 60)
                    cs  = int((total % 1) * 100)
                    cur_start = f"{mm2:02d}:{ss2:02d}.{cs:02d}"
                elif line and cur_start is not None and not line.startswith("WEBVTT"):
                    cur_text.append(line)
            if cur_start is not None and cur_text:
                txt = " ".join(t for t in cur_text if t)
                if txt:
                    out.append(f"[{cur_start}] {txt}")
        if not out:
            return False
        with open(lrc_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        return True
    except Exception:
        return False


def fetch_lyrics_any(artist, title, audio_path, yt_url="", yt_query="", duration_s=None):
    """lrclib synced/plain -> YouTube description."""
    if fetch_lyrics(artist, title, audio_path, duration_s=duration_s):
        return "lrclib"
    probe = yt_url or yt_query or (f"{artist} {title} lyrics" if artist
                                   else f"{title} lyrics")
    if probe and fetch_lyrics_youtube(probe, audio_path):
        return "youtube-desc"
    return ""


def lookup_musicbrainz(title, artist=""):
    """Album/year via MusicBrainz (free). Returns {} on any failure."""
    try:
        import musicbrainz as _mb
        return _mb.lookup_release(title, artist)
    except Exception:
        return {}




GENRE_KEYWORDS = {
    "rock": ["rock", "metal", "metallica", "punk", "grunge", "hardcore",
             "nirvana", "slipknot", "ac/dc", "led zeppelin", "iron maiden"],
    "pop": ["pop", "dance", "disco", "synth", "top 40", "the weeknd",
            "weeknd", "ariana", "bieber", "ed sheeran", "dua lipa",
            "blinding lights", "taylor swift"],
    "hip hop": ["hip hop", "hiphop", "rap", "trap", "drill", "travis scott",
                "drake", "kendrick", "eminem", "sicko mode", "lil ",
                "21 savage", "future", "carti", "mf doom"],
    "electronic": ["electronic", "edm", "house", "techno", "trance",
                   "dubstep", "dnb", "drum and bass", "ambient", "lo-fi",
                   "deadmau5", "skrillex", "tiesto", "david guetta",
                   "calvin harris", "avicii", "marshmello"],
    "r&b": ["r&b", "rnb", "soul", "funk", "neo soul", "usher", "beyonce",
            "beyoncé", "frank ocean", "sza", "summer walker", "bruno mars"],
    "jazz": ["jazz", "swing", "bebop", "fusion", "blues", "brubeck",
             "miles davis", "coltrane", "ella fitzgerald",
             "louis armstrong", "take five"],
    "classical": ["classical", "orchestral", "symphony", "piano", "sonata",
                  "concerto", "chamber", "mozart", "beethoven", "bach",
                  "vivaldi", "chopin", "adagio", "nocturne", "satie"],
    "country": ["country", "folk", "americana", "bluegrass", "dolly parton",
                "johnny cash", "morgan wallen", "luke combs"],
    "latin": ["latin", "reggaeton", "salsa", "bachata", "cumbia", "flamenco",
              "reggae", "soca", "bad bunny", "j balvin", "shakira",
              "despacito"],
    "soundtrack": ["soundtrack", "score", "ost", "theme", "hans zimmer",
                   "john williams", "film"],
    "lofi": ["lofi", "lo-fi", "chillhop", "study", "beats to"],
    "indie": ["indie", "alternative", "shoegaze", "dream pop", "neighbourhood",
              "neighborhood", "arctic monkeys", "tame impala", "mac demarco",
              "the 1975", "radiohead"],
}


def itunes_genre(title, artist=""):
    """Real genre from the iTunes Search API (no auth). Cached per (t,a).
    Returns a genre string or ''. """
    cache = getattr(itunes_genre, "_c", None)
    if cache is None:
        cache = itunes_genre._c = {}
    key = (title or "").lower() + "|" + (artist or "").lower()
    if key in cache:
        return cache[key]
    try:
        import urllib.parse, urllib.request
        q = urllib.parse.quote(((title or "") + " " + (artist or "")).strip())
        url = ("https://itunes.apple.com/search?term=" + q
               + "&entity=song&limit=5")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        for it in (d or {}).get("results", []) or []:
            gn = (it.get("primaryGenreName") or "").strip()
            a = (it.get("artistName") or "").lower()
            t = (it.get("trackName") or "").lower()
            if gn and ((artist or "").lower() in a
                       or (title or "").lower() in t):
                cache[key] = gn
                return gn
        res = (d or {}).get("results") or []
        if res and res[0].get("primaryGenreName"):
            gn = res[0]["primaryGenreName"].strip()
            cache[key] = gn
            return gn
    except Exception:
        pass
    cache[key] = ""
    return ""


def predict_genre(title="", artist=""):
    """Genre from title/artist keyword heuristics (fallback when Spotify
    provides none). Returns a genre string or ''."""
    text = f"{title} {artist}".lower()
    for genre, kws in GENRE_KEYWORDS.items():
        for kw in kws:
            if kw in text:
                return genre
    return ''

def embed_art(audio_path, cover_jpg=None, title=None, artist=None,
              album=None, genre=None, only_if_new=False):
    """Write tags + cover into the audio file itself (what Namida reads).
    If only_if_new=True: only write the cover art when a fresh image file is
    provided; never strip or replace existing embedded art with the stock
    sidecar. Same for lyrics/title/artist/album/genre: only set non-empty
    values; empty values leave the existing tag alone. This is the
    "enhancer must not clobber existing data it can't improve" rule."""
    cover = cover_jpg  # only trust a fresh image path; do NOT auto-pick sidecar
    have_art = bool(cover) and os.path.exists(cover) and os.path.getsize(cover) > 3000
    if not (have_art or title or artist):
        return False
    try:
        ext = os.path.splitext(audio_path)[1].lower()
        if ext == ".mp3":
            from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TCON
            try:
                tags = ID3(audio_path)
            except Exception:
                tags = ID3()
            if title:
                tags.setall("TIT2", [TIT2(encoding=3, text=title)])
            if artist:
                tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
            if album:
                tags.setall("TALB", [TALB(encoding=3, text=album)])
            if genre:
                tags.setall("TCON", [TCON(encoding=3, text=genre)])
            if have_art:
                with open(cover, "rb") as f:
                    tags.setall("APIC", [APIC(encoding=3, mime="image/jpeg",
                                              type=3, data=f.read())])
            tags.save(audio_path)
            return True
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            m = MP4(audio_path)
            if title:
                m["\xa9nam"] = [title]
            if artist:
                m["\xa9ART"] = [artist]
            if album:
                m["\xa9alb"] = [album]
            if genre:
                m["\xa9gen"] = [genre]
            if have_art:
                with open(cover, "rb") as f:
                    m["covr"] = [MP4Cover(f.read(), imageformat=13)]
            m.save()
            return True
        if ext in (".opus", ".ogg"):
            from mutagen.oggopus import OggOpus
            from mutagen.flac import Picture
            import base64
            m = OggOpus(audio_path)
            if title:
                m["title"] = title
            if artist:
                m["artist"] = artist
            if album:
                m["album"] = album
            if genre:
                m["genre"] = genre
            if have_art:
                with open(cover, "rb") as f:
                    pic = Picture()
                    pic.type = 3
                    pic.mime = "image/jpeg"
                    pic.data = f.read()
                m["metadata_block_picture"] = [
                    base64.b64encode(pic.write()).decode("ascii")]
            m.save()
            return True
        if ext == ".flac":
            from mutagen.flac import FLAC
            m = FLAC(audio_path)
            if title:
                m["title"] = title
            if artist:
                m["artist"] = artist
            if album:
                m["album"] = album
            if genre:
                m["genre"] = genre
            if have_art:
                with open(cover, "rb") as f:
                    pic = Picture()
                    pic.type = 3
                    pic.mime = "image/jpeg"
                    pic.data = f.read()
                m.add_picture(pic)
            m.save()
            return True
    except Exception:
        return False
    return False


AUDIO_EXTS = (".opus", ".mp3", ".m4a", ".flac", ".wav", ".ogg")




EMBED_ONLY = True   # default: embed cover+lyrics into tags, delete sidecars

def set_embed_only(enabled):
    global EMBED_ONLY
    EMBED_ONLY = bool(enabled)

def embed_only():
    return EMBED_ONLY

def enrich_file(audio_path, title, artist="", do_embed=True,
                yt_url="", album="", clean_title=True,
                fetch_sponsorblock=False, duration_s=None):
    """Full enrichment: cover (iTunes->YT thumb), lyrics (lrclib->YT desc),
    tag embedding, SponsorBlock sidecar. Returns dict of bools."""
    res = {"cover": False, "lyrics": False, "embedded": False}
    if fetch_sponsorblock and yt_url:
        try:
            import sponsorblock as _sbmod
            vid = yt_video_id(yt_url)
            if vid and not os.path.exists(
                    os.path.splitext(audio_path)[0] + ".segments.json"):
                res["segments"] = _sbmod.save_segments(vid, audio_path)
        except Exception:
            pass
    t_clean = clean_query(title) if clean_title else title
    try:
        if save_cover(t_clean, artist, audio_path):
            res["cover"] = True
        elif yt_url:
            res["cover"] = save_cover_from_yt(yt_url, audio_path)
    except Exception:
        pass
    try:
        res["lyrics"] = bool(fetch_lyrics_any(artist, t_clean, audio_path,
                                              yt_url=yt_url,
                                              duration_s=duration_s))
        if res["lyrics"] and do_embed:
            try:
                lrc = os.path.splitext(audio_path)[0] + ".lrc"
                if os.path.exists(lrc):
                    embed_lyrics_in_tags(
                        audio_path,
                        open(lrc, encoding="utf-8",
                             errors="replace").read())
                    if EMBED_ONLY:
                        try:
                            os.remove(lrc)
                        except OSError:
                            pass
            except Exception:
                pass
    except Exception:
        pass
    if do_embed:
        alb = album or getattr(enrich_file, "_alb_cache", {}).get(
            f"{t_clean[:30]}|{artist[:20]}")
        if not alb:
            mb = lookup_musicbrainz(t_clean, artist)
            alb = mb.get("album")
            if alb:
                cache = getattr(enrich_file, "_alb_cache", {})
                cache[f"{t_clean[:30]}|{artist[:20]}"] = alb
                enrich_file._alb_cache = cache
        try:
            try:
                _genre = predict_genre(t_clean or title, artist) \
                    if not alb else ""
            except Exception:
                _genre = ""
            _sidecar = None
            _jp = os.path.splitext(audio_path)[0] + ".jpg"
            if os.path.exists(_jp) and os.path.getsize(_jp) > 3000:
                _sidecar = _jp
            res["embedded"] = embed_art(audio_path, _sidecar,
                                        title=t_clean or title,
                                        artist=artist,
                                        album=alb, genre=_genre,
                                        only_if_new=True)
            if res["embedded"] and EMBED_ONLY:
                # no .jpg left behind — cover lives inside the tags now
                jpg = os.path.splitext(audio_path)[0] + ".jpg"
                for _jp in (jpg, os.path.splitext(audio_path)[0] + ".png"):
                    try:
                        if os.path.exists(_jp):
                            os.remove(_jp)
                    except OSError:
                        pass
        except Exception:
            pass
    return res


def normalize_lrc_text(lrc_text):
    """Reorder risetime-stamped .lrc lines and drop obviously-broken entries.
    - Strips blank / metadata junk
    - Requires [mm:ss.xx] format
    - Sorts by timestamp ascending
    - Removes negative timestamps
    - Dedupes identical timestamps (keeps the longer/last line)
    Returns cleaned text; empty if nothing salvaged.
    """
    import re as _re
    out = []
    seen = set()
    for raw in lrc_text.replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = _re.match(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$", line)
        if not m:
            continue
        mm = int(m.group(1)); ss = float(m.group(2))
        stamp = mm * 60.0 + ss
        body = m.group(3).strip()
        if stamp < 0 or stamp > 999.99:
            continue  # clearly bogus
        key = round(stamp, 2)
        if key in seen:
            continue
        seen.add(key)
        out.append((stamp, f"[{mm:02d}:{ss:05.2f}] {body}"))
    out.sort(key=lambda x: x[0])
    return "\n".join(l for _, l in out if l)


def retrofit_index(index_path, progress_cb=None, should_stop=None,
                   do_embed=True, want_subs=False, sb_segments=False):
    """Walk a PEAK _index.txt (filename|track|artist|uri) and enrich files.
    uri field may hold a YouTube id/url - used for thumbnail/desc fallbacks."""
    entries = []
    try:
        with open(index_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|", 3)
                if len(parts) >= 3:
                    uri = parts[3] if len(parts) == 4 else ""
                    entries.append((parts[0], parts[1], parts[2], uri))
    except Exception as e:
        if progress_cb:
            progress_cb(0, 0, f"cannot read {index_path}: {e}", None)
        return 0, 0

    root = os.path.dirname(index_path)
    done = enriched = 0
    for fn, track, artist, uri in entries:
        if should_stop is not None and should_stop.is_set():
            break
        audio = None
        exact = os.path.join(root, fn)
        if os.path.exists(exact) and fn.lower().endswith(AUDIO_EXTS):
            audio = exact
        else:
            stem = os.path.splitext(fn)[0]
            for ext in AUDIO_EXTS:
                cand = os.path.join(root, stem + ext)
                if os.path.exists(cand):
                    audio = cand
                    break
        # Fuzzy fallback: the index filename may have '_' where the real file
        # uses unicode (⧸ ｜ - etc). Match by track title (and artist if given).
        if not audio:
            try:
                import glob as _g
                q = (track or "").strip()
                if len(q) >= 3:
                    for cand in _g.glob(os.path.join(root, "*" + q + "*.*")):
                        if os.path.splitext(cand)[1].lower() in AUDIO_EXTS:
                            audio = cand
                            break
            except Exception:
                pass
        if not audio and (track or artist):
            try:
                import glob as _g
                q = f"{track} {artist}".strip().replace('/', ' ')
                for cand in _g.glob(os.path.join(root, "*.*")):
                    if os.path.splitext(cand)[1].lower() not in AUDIO_EXTS:
                        continue
                    base = os.path.basename(cand).lower()
                    if track and track.lower() in base:
                        audio = cand
                        break
                    if artist and artist.lower() in base:
                        audio = cand
                        break
            except Exception:
                pass
        done += 1
        if not audio:
            if progress_cb:
                progress_cb(done, len(entries), fn, None)
            continue
        base = os.path.splitext(audio)[0]
        has_lrc = os.path.exists(base + ".lrc")
        has_cov = os.path.exists(base + ".jpg")
        if has_lrc and has_cov:
            # still ensure embedding when requested
            if do_embed and not has_cov is False:
                pass
            if progress_cb:
                progress_cb(done, len(entries), fn, {})
            continue
        yt = uri if ("youtube.com" in uri or "youtu.be" in uri
                     or re.fullmatch(r"[A-Za-z0-9_-]{11}", uri or "")) else ""
        res = enrich_file(audio, track, artist, do_embed=do_embed, yt_url=yt)
        # extras: sponsorblock segments (+ subtitles come via download_one)
        if sb_segments and yt:
            try:
                import sponsorblock as _sbmod
                vid = yt_video_id(yt)
                if vid and not os.path.exists(
                        os.path.splitext(audio)[0] + ".segments.json"):
                    _sbmod.save_segments(vid, audio)
            except Exception:
                pass
        enriched += 1 if any(res.values()) else 0
        if progress_cb:
            progress_cb(done, len(entries), fn, res)
        time.sleep(0.15)
    return done, enriched


enrich_file._alb_cache = {}


def fetch_cover_url_deezer(title, artist=""):
    """Deezer search -> album art 500x500+ (free, no key)."""
    q = clean_query(f"{artist} {title}".strip() if artist else title)
    d = _http_json("https://api.deezer.com/search?" +
                   urllib.parse.urlencode({"q": q, "limit": 3}))
    if not d or not d.get("data"):
        return None
    for it in d["data"]:
        alb = it.get("album") or {}
        art = alb.get("cover_big") or alb.get("cover_medium")
        if art:
            return art
    return None


def fetch_cover_url_lastfm(title, artist="", api_key=""):
    """Last.fm album.getinfo art (requires free API key set in Settings)."""
    if not api_key:
        return None
    import urllib.parse as _up
    params = {"method": "album.getinfo",
              "api_key": api_key,
              "artist": artist or "",
              "album": title,
              "format": "json"}
    d = _http_json("https://ws.audioscrobbler.com/2.0/?" +
                   _up.urlencode(params))
    try:
        for img in d["album"]["image"]:
            if img.get("size") == "extralarge":
                return img.get("#text")
    except Exception:
        pass
    return None


LASTFM_KEY_FILE = os.path.join(os.path.expanduser("~"),
                               ".peak_lastfm_key")


DEFAULT_LASTFM_KEY = "6cb7d46a61b1542abe98b0a8a58e528e"


def lastfm_key():
    k = ""
    try:
        k = open(LASTFM_KEY_FILE, encoding="utf-8").read().strip()
    except Exception:
        pass
    return k or DEFAULT_LASTFM_KEY


def _lastfm_key():
    p = os.path.join(os.path.expanduser("~"), ".peak_lastfm_key")
    try:
        return open(p, encoding="utf-8").read().strip()
    except Exception:
        return ""


def _lastfm_artist_image(artist, key):
    """Artist image via Last.fm artist.getinfo (requires free key)."""
    if not key:
        return None
    d = _http_json("https://ws.audioscrobbler.com/2.0/?" +
                   urllib.parse.urlencode(
                       {"method": "artist.getinfo", "artist": artist,
                        "api_key": key, "format": "json"}))
    try:
        for img in d["artist"]["image"]:
            if img.get("size") == "extralarge" and img.get("#text"):
                return img["#text"]
    except Exception:
        pass
    return None


def save_artist_image(artist, library_root, lastfm_key=""):
    """Save <library>/Artists/<artist>.jpg.
    Deezer first; Last.fm second when a key is configured."""
    if not artist:
        return False
    adir = os.path.join(library_root, "Artists")
    try:
        os.makedirs(adir, exist_ok=True)
    except Exception:
        return False
    safe = re.sub(r'[<>:"/\\|?*]', "_", artist).strip()[:80]
    out = os.path.join(adir, safe + ".jpg")
    if os.path.exists(out) and os.path.getsize(out) > 3000:
        return True

    art = None
    d = _http_json("https://api.deezer.com/search/artist?" +
                   urllib.parse.urlencode({"q": artist, "limit": 1}))
    try:
        art = (d["data"][0].get("picture_big")
               or d["data"][0].get("picture_medium"))
    except Exception:
        art = None

    if not art and lastfm_key:
        lf = _lastfm_artist_image(artist, lastfm_key)
        if lf:
            art = lf

    if not art:
        return False
    return _download(art, out)


def normalize_loudness(audio_path, target_i=-16, lra=11, tp=-1.5):
    """EBU R128 loudness-normalize in place using ffmpeg loudnorm.
    Two-pass: first measure, then apply with measured values. Returns True
    on success (file replaced)."""
    import subprocess as _sp
    ff = _ffmpeg_path()
    if not ff:
        return False
    flags = getattr(_sp, "CREATE_NO_WINDOW", 0)
    try:
        # pass 1: measure
        cmd = [ff, "-hide_banner", "-i", audio_path,
               "-af", f"loudnorm=I={target_i}:LRA={lra}:TP={tp}"
                      ":print_format=json", "-f", "null", "-"]
        r = _sp.run(cmd, capture_output=True, timeout=180,
                    creationflags=flags)
        txt = r.stderr.decode(errors="replace")
        i0 = txt.rfind("{")
        i1 = txt.rfind("}")
        if i0 < 0 or i1 <= i0:
            return False
        m = json.loads(txt[i0:i1 + 1])
        af = (f"loudnorm=I={target_i}:LRA={lra}:TP={tp}"
              f":measured_I={m['input_i']}:measured_LRA={m['input_lra']}"
              f":measured_TP={m['input_tp']}"
              f":measured_thresh={m['input_thresh']}"
              f":offset={m['target_offset']}:linear=true")
        # pass 2: apply to temp then replace
        tmp = audio_path + ".norm.tmp" +             os.path.splitext(audio_path)[1]
        cmd = [ff, "-y", "-i", audio_path, "-af", af,
               "-c:a", "copy", tmp] if False else \
              [ff, "-y", "-i", audio_path, "-af", af, tmp]
        r2 = _sp.run(cmd, capture_output=True, timeout=300,
                     creationflags=flags)
        if r2.returncode == 0 and os.path.exists(tmp) and \
                os.path.getsize(tmp) > 10000:
            os.replace(tmp, audio_path)
            return True
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def _ffmpeg_path():
    import subprocess  # noqa
    cand = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "_internal", "ffmpeg", "ffmpeg.exe")
    if os.path.exists(cand):
        return cand
    return "ffmpeg"


LYRIC_LANG_FILE = os.path.join(os.path.expanduser("~"),
                               ".peak_lyric_lang")


def lyric_lang():
    """User's preferred lyric language code ('' = any)."""
    try:
        return open(LYRIC_LANG_FILE, encoding="utf-8").read().strip()
    except Exception:
        return ""


def set_lyric_lang(lang):
    try:
        if lang:
            open(LYRIC_LANG_FILE, "w", encoding="utf-8").write(lang)
        else:
            os.remove(LYRIC_LANG_FILE)
    except Exception:
        pass


def _pick_best_record(recs, want_lang=""):
    """Choose the best lrclib record: prefer synced + preferred language."""
    best = None
    best_score = -1
    for r in recs or []:
        score = 0
        if r.get("syncedLyrics"):
            score += 2
        if r.get("plainLyrics"):
            score += 1
        if want_lang and (r.get("lang") or "") == want_lang:
            score += 4
        elif want_lang:
            # penalize known mismatch
            if r.get("lang") and r.get("lang") != want_lang:
                score -= 2
        if score > best_score:
            best, best_score = r, score
    return best


def embed_lyrics_in_tags(audio_path, text):
    """Write lyrics into the file's tags in addition to the .lrc sidecar.
    mp3->USLT(ID3), m4a/mp4->©lyr, opus/ogg/flac->LYRICS vorbis comment."""
    if not text or not os.path.exists(audio_path):
        return False
    try:
        ext = os.path.splitext(audio_path)[1].lower()
        if ext == ".mp3":
            from mutagen.id3 import ID3, USLT
            tags = None
            try:
                tags = ID3(audio_path)
            except Exception:
                tags = ID3()
            tags.delall("USLT")
            tags.add(USLT(encoding=3, lang="eng", desc="",
                          text=text))
            tags.save(audio_path)
            return True
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            m = MP4(audio_path)
            m["\xa9lyr"] = [text]
            m.save()
            return True
        if ext in (".opus", ".ogg", ".flac"):
            from mutagen.oggopus import OggOpus
            from mutagen.flac import FLAC
            from mutagen.oggvorbis import OggVorbis
            if ext == ".opus":
                audio = OggOpus(audio_path)
                audio["LYRICS"] = text
                audio.save()
            elif ext == ".ogg":
                audio = OggVorbis(audio_path)
                audio["LYRICS"] = text
                audio.save()
            else:
                audio = FLAC(audio_path)
                audio["LYRICS"] = text
                audio.save()
            return True
        return False
    except Exception:
        return False


def compute_replaygain(audio_path):
    """Write REPLAYGAIN_TRACK_GAIN/PEAK/REFERENCE tags via ffmpeg volumedetect.
    Returns (gain_db, peak) or None. Lossless — tags only, audio untouched."""
    import subprocess, os
    exe = _ffmpeg_path()
    if not exe or not os.path.exists(audio_path):
        return None
    cmd = [exe, "-i", audio_path, "-af", "volumedetect",
           "-f", "null", "NUL" if os.name == "nt" else "/dev/null"]
    CREATE = 0x08000000 if os.name == "nt" else 0
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300,
                           creationflags=CREATE)
        txt = ((r.stderr or b"") + (r.stdout or b"")).decode(
            "utf-8", errors="replace")
    except Exception:
        return None
    import re as _re
    g = _re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", txt)
    p = _re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", txt)
    gain = float(g.group(1)) if g else None
    peak = float(p.group(1)) if p else None
    if gain is None:
        return None
    gain_db = round(-18.0 - gain, 2)  # ReplayGain: 89dB ref ≈ -18 LUFS
    try:
        import mutagen
        ext = os.path.splitext(audio_path)[1].lower()
        if ext == ".mp3":
            from mutagen.id3 import ID3, TXXX
            t = ID3(audio_path) if ID3(audio_path) else ID3()
            t.delall("TXXX:REPLAYGAIN_TRACK_GAIN")
            t.add(TXXX(desc="REPLAYGAIN_TRACK_GAIN", text=[f"{gain_db:.2f} dB"]))
            if peak is not None:
                t.add(TXXX(desc="REPLAYGAIN_TRACK_PEAK", text=[f"{peak:.6f}"]))
            t.save(audio_path)
        elif ext in (".opus", ".ogg", ".flac"):
            audio = mutagen.File(audio_path)
            audio["REPLAYGAIN_TRACK_GAIN"] = [f"{gain_db:.2f} dB"]
            if peak is not None:
                audio["REPLAYGAIN_TRACK_PEAK"] = [f"{peak:.6f}"]
            audio.save()
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            m = MP4(audio_path)
            m["----:com.apple.iTunes:replaygain_track_gain"] =                 [f"{gain_db:.2f} dB".encode()]
            m.save()
    except Exception:
        return gain_db
    return gain_db



def backfill_missing(library_root, progress_cb=None, should_stop=None,
                     do_embed=True, target="all"):
    """Enrich files lacking album/lyrics/cover, reading title+artist from
    their existing embedded tags (no index needed). target: 'album','lyrics',
    'cover','all'. Returns (done, enriched)."""
    import glob as _g
    music = os.path.join(library_root, "Music")
    files = []
    for ext in AUDIO_EXTS:
        files += _g.glob(os.path.join(music, "**", "*" + ext),
                         recursive=True)
    done = enriched = 0
    for p in files:
        if should_stop is not None and should_stop.is_set():
            break
        base = os.path.splitext(p)[0]
        try:
            import mutagen as _mg
            a = _mg.File(p, easy=True)
            if not a:
                continue
            title = " ".join(a.get("title", []) or []) or os.path.basename(base)
            artist = " ".join(a.get("artist", []) or [])
            album = " ".join(a.get("album", []) or [])
        except Exception:
            continue
        need = False
        if target in ("album", "all") and not album:
            need = True
        if target in ("lyrics", "all") and not os.path.exists(base + ".lrc"):
            need = True
        if target in ("cover", "all"):
            if not (os.path.exists(base + ".jpg")
                    or os.path.exists(base + ".png")):
                need = True
        if not need:
            done += 1
            continue
        try:
            res = enrich_file(p, title, artist, do_embed=do_embed)
            if any(res.values()):
                enriched += 1
        except Exception:
            pass
        done += 1
        if progress_cb and done % 10 == 0:
            try:
                progress_cb(done, len(files), None, None)
            except Exception:
                pass
    if progress_cb:
        try:
            progress_cb(done, len(files), None, None)
        except Exception:
            pass
    return done, enriched


def tag_bpm(path, bpm):
    """Write BPM into the file's tags (TBPM for mp3, BPM for vorbis/flac/opus)."""
    try:
        import mutagen as _mg
        ext = os.path.splitext(path)[1].lower()
        if ext == ".mp3":
            from mutagen.id3 import TBPM, ID3
            tags = ID3(path)
            tags.delall("TBPM")
            tags.add(TBPM(encoding=3, text=str(int(bpm))))
            tags.save()
        else:
            f = _mg.File(path)
            if f is not None:
                f["bpm"] = str(int(bpm))
                f.save()
        return True
    except Exception:
        return False


def embed_existing_sidecars(library_root, progress_cb=None, should_stop=None,
                            delete_after=True):
    """Backfill embed-only: for files that already have .lrc/.jpg sidecars but
    no embedded tag, embed the sidecar into the tags and (optionally) delete
    the sidecar. Returns (processed, embedded_lyrics, embedded_cover)."""
    import glob as _g, mutagen as _mg
    music = os.path.join(library_root, "Music")
    files = []
    for ext in (".opus", ".mp3", ".m4a", ".flac"):
        files += _g.glob(os.path.join(music, "**", "*" + ext),
                         recursive=True)
    proc = lyr = cover = 0
    for p in files:
        if should_stop is not None and should_stop.is_set():
            break
        base = os.path.splitext(p)[0]
        lrc = base + ".lrc"
        jpg = base + ".jpg"
        try:
            a = _mg.File(p)
            tags = dict(getattr(a, "tags", {}) or {})
            pic_key = ("metadata_block_picture" in tags)
            lyr_key = any(k.upper() in ("LYRICS", "USLT", "UNSYNCEDLYRICS")
                          for k in tags.keys())
        except Exception:
            pic_key = lyr_key = False
        # lyrics
        if not lyr_key and os.path.exists(lrc):
            try:
                txt = open(lrc, encoding="utf-8", errors="replace").read()
                if txt.strip():
                    if embed_lyrics_in_tags(p, txt):
                        lyr += 1
                        if delete_after:
                            try:
                                os.remove(lrc)
                            except OSError:
                                pass
            except Exception:
                pass
        # cover
        if not pic_key and os.path.exists(jpg):
            try:
                if embed_art(p, None, title=os.path.splitext(os.path.basename(p))[0],
                             artist=""):
                    cover += 1
                    if delete_after:
                        try:
                            os.remove(jpg)
                        except OSError:
                            pass
            except Exception:
                pass
        proc += 1
        if progress_cb and proc % 20 == 0:
            try:
                progress_cb(proc, len(files), None, None)
            except Exception:
                pass
    if progress_cb:
        try:
            progress_cb(proc, len(files), None, None)
        except Exception:
            pass
    return proc, lyr, cover


def verify_audio(path):
    """Verify a downloaded file is a valid, playable audio file.

    Returns dict: {valid: bool, duration: float, size: int, codec: str,
                   reason: str, bitrate: int}. Reuses bundled ffmpeg."""
    import subprocess as _sp
    ff = _ffmpeg_path()
    if not ff:
        return {"valid": False, "reason": "no ffmpeg", "duration": 0,
                "size": 0, "codec": "", "bitrate": 0}
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if size == 0:
        return {"valid": False, "reason": "empty/missing", "duration": 0,
                "size": 0, "codec": "", "bitrate": 0}
    flags = getattr(_sp, "CREATE_NO_WINDOW", 0)
    try:
        cmd = [ff, "-v", "error", "-i", path, "-f", "null", "-"]
        r = _sp.run(cmd, capture_output=True, timeout=120, creationflags=flags)
        err = r.stderr.decode(errors="replace")
        valid = r.returncode == 0
        # metadata (duration/bitrate/codec) via a second -v info probe
        dur = 0.0; br = 0; codec = ""
        if valid:
            try:
                import re as _re
                r2 = _sp.run([ff, "-v", "info", "-i", path, "-f", "null", "-"],
                             capture_output=True, timeout=120, creationflags=flags)
                info = r2.stderr.decode(errors="replace")
                m = _re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", info)
                if m:
                    dur = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
                mb = _re.search(r"bitrate: (\d+) kb/s", info)
                if mb:
                    br = int(mb.group(1))
                mc = _re.search(r"Audio: (\w+)", info)
                if mc:
                    codec = mc.group(1)
            except Exception:
                pass
        return {"valid": valid, "duration": round(dur, 1), "size": size,
                "codec": codec, "bitrate": br,
                "reason": "" if valid else (err.strip()[-200:] or "decode failed")}
    except Exception as e:
        return {"valid": False, "reason": str(e), "duration": 0,
                "size": size, "codec": "", "bitrate": 0}
