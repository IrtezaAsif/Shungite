# SHUNGITE

**YouTube Music + Spotify Downloader & Library Builder** — a Windows desktop app
that downloads your entire Spotify library and playlists from YouTube as
best-quality Opus, names every file `Artist - Title`, and embeds cover art,
synced lyrics, real genres, and the source YouTube link into the file tags.

![Python 3.12](https://img.shields.io/badge/python-3.12-blue) ![License: MIT](https://img.shields.io/badge/license-MIT-green) ![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey)

> **Download** → grab `Shungite_win64.zip` from [**Releases**](https://github.com/IrtezaAsif/Shungite/releases), extract anywhere, run `Shungite.exe`. No console windows, no installer needed — a first-run wizard walks you through picking your music folder and logging into YouTube.

---

## Features — what they do and how they work

### 🎵 Smart track matching (every download tab)
When you ask for a song, SHUNGITE searches YouTube and picks the RIGHT upload through a four-stage pipeline (`_match_track()` in `PEAK_GITHUB_true.py`, scoring in `matchers.py`):
1. **Strict match** — search results are scored by title/artist text similarity, channel trust (official/Topic channels), view-count popularity (log-scaled, so the official 250M-view upload beats a 900-view cover), and a duration sanity window (40–720s by default, kills "1 hour mix" uploads). A token-overlap gate rejects results that share no words with your query (no more *Don't Be A Hero → Don't Run*).
2. **Popularity re-rank** — among plausible matches, the most-viewed correct one wins.
3. **Loose match** — if strict scoring rejects everything, a conservative fallback takes the most-viewed hit where the artist name genuinely appears in the title/channel — with an Official-Topic bridge for OST/cast tracks uploaded by label channels.
4. **Rescue query** — if the full Spotify title poisons the YouTube search (parenthetical `(From 'X')`, feat-lists, remix tags), the search is retried with a simplified title and artist first.
5. **CJK bridge** — Chinese/Japanese/Korean titles are matched by character overlap, so 陷入爱情 finds 陷入愛情 (traditional variants) and romanized titles find the CJK upload from the same artist.

### 🎧 Best-quality audio
- **Best Opus** (default): the highest-bitrate Opus stream — 256 kbps target when your logged-in YouTube account has Premium, ~130 kbps otherwise. Real Opus, never a lossy re-encode. Also: **Best M4A**, **Best MP3**.
- Premium-only format IDs (774/141) are tried first when available and **auto-demote** with a full fallback chain, so a format hiccup never fails a download.

### 📎 Embedded everything (automatic on every download)
Each downloaded `.opus` gets, inside the file itself:
- **Cover art** — the video thumbnail, with iTunes/Last.fm/Deezer fallbacks (`titanium_enrich.py`)
- **Synced lyrics** — three layers: YouTube's own timed subtitles converted to `.lrc` when present → LRClib (duration-keyed exact match) → **WhisperX speech transcription on your GPU** for everything else. Embedded in the USLT/LYRICS tag so every player shows timed lyrics.
- **Real genre** — looked up per-track from the iTunes Search API (or Spotify artist genres when connected), cached per artist (`itunes_genre()`).
- **YouTube source link** — the `purl` tag holds the exact video you downloaded from.
- **Correct title/artist tags** — always the Spotify metadata, never the video title.
- Leftover `.vtt`/`.jpg` sidecars are cleaned up automatically — your Music folder stays just `.opus` files + m3us.

### 📁 Spotify library & playlists
- Log in once (embedded PKCE OAuth — a shared community client ID is **built in**, no setup required).
- Your playlists appear in a tree — pick one or *Select All* (100+), and SHUNGITE downloads every track into a **flat `Music/` folder** and writes **one `.m3u` per playlist** into `Playlists/`.
- Personalized Spotify mixes (Daily Mixes, daylist, radios) work too, via the same GQL route the Spotify web client uses.

### 📊 Batch CSV
Point it at exported Spotify CSVs (Track / Artist / Duration columns — the GDPR export or Exportify format) and it fills your library the same way: `Artist - Title.opus`, resumable (an `_index.txt` means *Sync Missing Only* only fetches what you don't have), per-track progress `[i/total] ✓new ↷skipped ✗failed`, and a `failed_downloads.txt` that names **why** every failure failed (with the actual search results it saw).

### 🎤 Lyric Syncer tab
For tracks no lyrics database has: **WhisperX** transcribes the audio on your GPU (CUDA auto-detected, float16), converts the result to a timed `.lrc`, and embeds it. Per-file progress, cancel button, background execution — no console windows.

### 🖥 Embedded login, no browser tabs
The YouTube login opens **inside the app** (pywebview/WebView2), stores cookies in the app data folder, and every download reuses that logged-in session — which is what keeps YouTube's "confirm you're not a bot" walls away during bulk runs.

### 🛡 Resilient engine
Bot-checks trigger an embedded-client switch, saved-cookie fallback, format demotion, and paced retries. Every failure path is logged with a reason. Parallel downloads (default 6 workers) with a working Cancel that kills in-flight ffmpeg process trees.

## Project layout — every file, what it does

| File | Purpose |
|---|---|
| `PEAK_GITHUB_true.py` | The application: all tabs, batch engine, `_match_track()` unified search pipeline, `_post_download_fixups()` embed/cleanup guarantee, `download_one()` yt-dlp wrapper with retry/fallback chain, first-run wizard, LocalLow data dir |
| `matchers.py` | `strict_match()` scoring: token gates, duration bands, popularity fusion, junk penalties, CJK bridge |
| `titanium_enrich.py` | Enrichment: iTunes/last.fm/Deezer/MusicBrainz covers, `fetch_lyrics()` (LRClib duration-keyed), `vtt_to_lrc()`, `itunes_genre()`, tag embedding, ReplayGain/loudness |
| `spotify_api.py` | Cookie/GQL Spotify client (home sections, playlist tracks, search) — works without the official API |
| `spotify_oauth.py` | OAuth PKCE login, token refresh, playlist/liked paging, batch artist-genre lookup; built-in default client ID |
| `innertube.py` | YouTube InnerTube search client (fast metadata + durations) |
| `login_window.py` | Embedded pywebview/WebView2 YouTube login (persistent cookie profile) |
| `browser_cookies.py` | Cookie extraction from installed browsers |
| `download_queue.py` | Job queue with cancel events and progress |
| `history_db.py` | SQLite history for the library index |
| `musicbrainz.py` | MusicBrainz release/tracklist lookups |
| `sponsorblock.py` | SponsorBlock chapter marking via yt-dlp |
| `smart_playlists.py` | Auto-playlist generation (top-listened, recent…) |
| `acoustic_dedup.py` | Acoustic fingerprinting to detect duplicate downloads |
| `subsonic_push.py` | Push finished library to a Subsonic server |
| `web_remote.py` | Tiny web remote control for running downloads |
| `audit.py` | 20-check self-test suite |
| `PEAK_GITHUB_true.spec` | PyInstaller spec — bundles ffmpeg + deno, webview/pythonnet, `console=False`, output `Shungite.exe` |
| `build.bat` | One-command build |
| `requirements.txt` | Python dependencies |

## Data & settings location

Everything the app writes lives in **`%USERPROFILE%\AppData\LocalLow\Shungite`** —
config, cookies, login profiles, and (by default) the `Music Library` folder.
Legacy `~/.peak_*` files are migrated there automatically on first run.
No registry, no Program Files pollution.

## Run from source

```bat
git clone https://github.com/IrtezaAsif/Shungite.git
cd Shungite
py -3.12 -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python PEAK_GITHUB_true.py
```

Requires **Python 3.12**, **ffmpeg** on PATH (or bundled by the spec),
**WebView2 Runtime** (preinstalled on Windows 11). Optional: **deno** for
yt-dlp JS challenges; a **CUDA GPU** makes the Lyric Syncer ~10× faster.

## Build the standalone exe

```bat
venv\Scripts\pip install pyinstaller
build.bat
```

Output: `dist3\Shungite\Shungite.exe` (one-folder build, no console).

## Usage quick-start

1. Run `Shungite.exe` → the **setup wizard** asks for your music folder and offers the YouTube login.
2. **Single tab** — paste any YouTube/YT Music link or search.
3. **Spotify tab** — log in, select playlists, Download (or M3U-only).
4. **Batch CSV tab** — point at your exported CSVs, *Sync Missing Only*.
5. **Lyric Syncer tab** — sync lyrics for obscure tracks on GPU.

### Troubleshooting

- **"Requested format is not available"** — handled automatically; make sure you're logged in via Settings.
- **Slow downloads / bot-checks** — log in to YouTube inside the app.
- **Lyric Syncer "venv missing"** — it expects a whisperX venv, or use the source install (`pip install whisperx "numpy<2"`; torch matching your CUDA).
- **Spotify API 403/429** — the app falls back to the cookie/GQL client automatically.

## License

MIT — see [LICENSE](LICENSE). Downloaded content is for personal use; respect
YouTube's Terms of Service and copyright law.
