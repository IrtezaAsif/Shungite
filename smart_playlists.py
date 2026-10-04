"""Smart playlist generator for SHUNGITE (MPD-smart-playlists inspired).
Rules-based playlists from embedded tags, e.g.:
  {"name": "Chill 2010s", "match": {"genre": "chill", "year_from": 2010}}
Writes standard .m3u files into <library>/Playlists.
"""
import glob
import os
import re


def _audio_files(library_root):
    files = []
    for ext in ("*.opus", "*.mp3", "*.m4a", "*.flac"):
        files += glob.glob(os.path.join(library_root, "Music", "**", ext),
                           recursive=True)
    return files


def _tag(path, reader):
    try:
        return (reader(path) or "").lower()
    except Exception:
        return ""


def generate(rules, library_root, mutagen_mod):
    """rules: {"name": str, "match": {field: value|list},
               "limit": int}
    fields: genre, artist, title, album, year_from, year_to
    Returns (playlist_path, matched_count)."""
    name = rules.get("name") or "smart"
    match = rules.get("match", {}) or {}
    limit = int(rules.get("limit") or 200)

    def field_of(path, f):
        try:
            audio = mutagen_mod.File(path, easy=True)
            if not audio:
                return ""
            if f == "genre":
                return " ".join(audio.get("genre", []))
            if f == "artist":
                return " ".join(audio.get("artist", []))
            if f == "title":
                return " ".join(audio.get("title", []))
            if f == "album":
                return " ".join(audio.get("album", []))
            if f == "date":
                return " ".join(audio.get("date", []))
        except Exception:
            pass
        return ""

    out = []
    for p in _audio_files(library_root):
        ok = True
        for f, want in match.items():
            if f in ("year_from", "year_to"):
                continue
            val = _tag(p, lambda x: field_of(x, f))
            wants = want if isinstance(want, list) else [want]
            if not any(str(w).lower() in val for w in wants):
                ok = False
                break
        if ok and ("year_from" in match or "year_to" in match):
            date = field_of(p, "date")
            m = re.search(r"(\d{4})", date)
            year = int(m.group(1)) if m else None
            if year is None:
                ok = False
            elif "year_from" in match and year < int(match["year_from"]):
                ok = False
            elif "year_to" in match and year > int(match["year_to"]):
                ok = False
        if ok:
            out.append(p)
            if len(out) >= limit:
                break

    pls_dir = os.path.join(library_root, "Playlists")
    os.makedirs(pls_dir, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*]', "_", name)[:80]
    m3u = os.path.join(pls_dir, safe + ".m3u")
    music_dir = os.path.normpath(os.path.join(library_root, "Music"))
    with open(m3u, "w", encoding="utf-8", newline="\n") as f:
        f.write("#EXTM3U\n#PLAYLIST:" + name + "\n\n")
        for p in out:
            rel = os.path.relpath(p, pls_dir).replace("\\", "/")
            f.write(rel + "\n")
    return m3u, len(out)


def generate_auto_collection(library_root, mutagen_mod, min_genre=50,
                             min_decade=30, limit=300):
    """One-shot set of auto playlists: By Genre (top genres), By Decade,
    Recently Added. Returns list of (playlist_name, matched_count)."""
    import collections, time
    files = _audio_files(library_root)
    music_dir = os.path.normpath(os.path.join(library_root, "Music"))
    pls_dir = os.path.join(library_root, "Playlists")
    os.makedirs(pls_dir, exist_ok=True)
    results = []

    def field(p, f, m=mutagen_mod):
        try:
            a = m.File(p, easy=True)
            if not a:
                return ""
            return " ".join(a.get(f, []) or [])
        except Exception:
            return ""

    # --- By Genre ---
    genres = collections.Counter()
    for p in files:
        g = field(p, "genre").strip()
        if g:
            genres[g] += 1
    written = {}
    for g, cnt in genres.most_common():
        if cnt < min_genre:
            break
        safe = re.sub(r'[<>:"/\\|?*]', "_", g)[:60] or "Unknown"
        m3u = os.path.join(pls_dir, "Genre - " + safe + ".m3u")
        with open(m3u, "w", encoding="utf-8", newline="\n") as f:
            f.write("#EXTM3U\n#PLAYLIST:Genre - " + g + "\n\n")
            n = 0
            for p in files:
                if field(p, "genre").strip() == g:
                    rel = os.path.relpath(p, pls_dir).replace("\\", "/")
                    f.write(rel + "\n")
                    n += 1
                    if n >= limit:
                        break
        results.append(("Genre - " + g, n))

    # --- By Decade (from date tag) ---
    decades = collections.defaultdict(list)
    for p in files:
        d = field(p, "date")
        m = re.search(r"(\d{4})", d)
        if m:
            dec = (int(m.group(1)) // 10) * 10
            decades[f"{dec}s"].append(p)
    for dec in sorted(decades):
        if len(decades[dec]) < min_decade:
            continue
        m3u = os.path.join(pls_dir, "Decade - " + dec + ".m3u")
        with open(m3u, "w", encoding="utf-8", newline="\n") as f:
            f.write("#EXTM3U\n#PLAYLIST:Decade - " + dec + "\n\n")
            for p in decades[dec][:limit]:
                rel = os.path.relpath(p, pls_dir).replace("\\", "/")
                f.write(rel + "\n")
        results.append(("Decade - " + dec, len(decades[dec])))

    # --- Recently Added (by mtime) ---
    recent = sorted(files, key=lambda p: os.path.getmtime(p), reverse=True)
    if recent:
        m3u = os.path.join(pls_dir, "Recently Added.m3u")
        with open(m3u, "w", encoding="utf-8", newline="\n") as f:
            f.write("#EXTM3U\n#PLAYLIST:Recently Added\n\n")
            for p in recent[:limit]:
                rel = os.path.relpath(p, pls_dir).replace("\\", "/")
                f.write(rel + "\n")
        results.append(("Recently Added", len(recent[:limit])))

    return results
