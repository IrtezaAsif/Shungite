# SHUNGITE

**YouTube Music + Spotify Downloader & Library Builder** — a Windows desktop app
that downloads your entire Spotify library and playlists from YouTube as
best-quality Opus, names every file `Artist - Title`, and embeds cover art,
synced lyrics, real genres, and the source YouTube link into the file tags.

![Python 3.12](https://img.shields.io/badge/python-3.12-blue) ![License: MIT](https://img.shields.io/badge/license-MIT-green) ![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey)

## What it does

- **Spotify library & playlists** — log in once (embedded WebView2 window, no
  browser tab juggling), pick one or all playlists, and SHUNGITE downloads
  every track into a flat `Music/` folder, writing one `.m3u` per playlist.
- **Smart track matching** — every search goes through a four-stage pipeline:
  1. **strict match** — token-overlap gates, duration sanity bands (40–720s),
     junk penalties (mix / reaction / nightcore / karaoke / 1-hour uploads)
  2. **popularity scoring** — log-scaled view counts favor the *official* upload
  3. **loose match** — artist-name verification + title-similarity floor
  4. **rescue query** — re-search with a simplified title (`(From 'X')`,
     feat-lists, remix tags stripped) when the full title poisons the search
- **Best-quality audio** — "Best Opus" grabs the highest-bitrate Opus stream
  (256 kbps target with Premium cookies, ~130 kbps otherwise — real Opus, never
  re-encoded junk). Also Best M4A / Best MP3. Premium format IDs (774/141)
  auto-demote with a full format-fallback chain so downloads never hard-fail.
- **Embedded everything** — each downloaded file gets:
  - cover art (YouTube thumbnail → iTunes fallback)
  - **synced lyrics** — timed YouTube subtitles → `.lrc` conversion, LRClib
    duration-keyed lookup, and (for the rest) on-device **WhisperX** speech
    transcription on your GPU, all embedded in the USLT/LYRICS tag
  - **real genre** from the iTunes Search API (Spotify OAuth genre when
    available), cached per artist
  - the **YouTube source URL** in the `purl` tag
  - title/artist tags from the Spotify metadata (never the video title)
- **Batch CSV** — point it at exported Spotify CSVs (Track / Artist / Duration
  columns) and it fills your library the same way, with per-track progress
  `[i/total] ✓new ↷skipped ✗failed` logging in every tab.
- **Lyric Syncer tab** — GPU-accelerated WhisperX (auto-detects CUDA,
  float16) transcribes any track that has no lyrics available online, writes
  a timed `.lrc`, and embeds it. Runs per-file with cancel support.
- **Resilient downloads** — your logged-in YouTube cookies are used
  automatically; bot-checks ("Sign in to confirm you're not a bot") trigger
  an embedded-client switch, cookie fallback, format demotion, and paced
  retries. Every failure is logged with reasons to `failed_downloads.txt`.
- **One engine everywhere** — Single, Playlist, Search, Batch CSV, Spotify,
  and YT Music tabs all use the same match pipeline and the same
  post-download guarantee (tags / genre / lyrics / cleanup).

## Project layout — every file, what it does

| File | Purpose |
|---|---|
| `PEAK_GITHUB_true.py` | The application: Tkinter GUI, all tabs, the batch engine, `_match_track()` unified search pipeline, `_post_download_fixups()` embed/cleanup guarantee, `download_one()` yt-dlp wrapper with the full retry/fallback chain |
| `matchers.py` | `strict_match()` scoring: token-overlap gate, duration soft bands, popularity fusion, junk-word penalties |
| `titanium_enrich.py` | Enrichment library: iTunes/last.fm/Deezer/MusicBrainz covers, `fetch_lyrics()` (LRClib duration-keyed), YouTube-subtitle `vtt_to_lrc()`, `itunes_genre()` real-genre lookup, tag embedding, ReplayGain/loudness tools |
| `spotify_api.py` | Cookie/GQL client for Spotify (home sections, playlist tracks, search) — works without the official API |
| `spotify_oauth.py` | Official OAuth PKCE login, token refresh, playlist/liked-songs paging, batch artist-genre lookup; ships a built-in default client ID |
| `innertube.py` | YouTube InnerTube search client (fast metadata + durations) |
| `login_window.py` | Embedded **pywebview / WebView2** login window for YouTube (persistent cookie profile — no system-browser fallback) |
| `browser_cookies.py` | Extract cookies from installed browsers (Firefox/Chrome/Edge) |
| `download_queue.py` | Job queue with cancel events and progress reporting |
| `history_db.py` | SQLite history of downloads/skips for the library index |
| `musicbrainz.py` | MusicBrainz release/tracklist lookups |
| `sponsorblock.py` | SponsorBlock chapter marking via yt-dlp |
| `smart_playlists.py` | Auto-playlist generation (top-listened, recent, etc.) |
| `acoustic_dedup.py` | Acoustic fingerprinting to detect duplicate downloads |
| `subsonic_push.py` | Push finished library to a Subsonic server |
| `web_remote.py` | Tiny web remote control for running downloads |
| `audit.py` | 20-check self-test suite (engine dry-init, live search, matcher caps, multi-track batch, cancel speed) |
| `PEAK_GITHUB_true.spec` | PyInstaller spec — bundles ffmpeg + deno, `webview`/pythonnet for the embedded login |
| `build.bat` | One-command build of the standalone `.exe` |
| `requirements.txt` | Python dependencies |

## Install (run from source)

```bat
git clone https://github.com/IrtezaAsif/Shungite.git
cd Shungite
py -3.12 -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python PEAK_GITHUB_true.py
```

Requirements: **Python 3.12**, **ffmpeg** on PATH (or bundled by the spec),
**WebView2 Runtime** (preinstalled on Windows 11) for the embedded login, and
optionally **deno** for yt-dlp JS challenges.

## Build the standalone exe

```bat
venv\Scripts\pip install pyinstaller
build.bat
```

Output lands in `dist3\PEAK_GITHUB_true\PEAK_GITHUB_true.exe` (one-folder build).

## Usage quick-start

1. **Settings → Login to YouTube** — the embedded window opens; log in once.
   Cookies persist in `~/.peak_yt_cookies.txt` and every download uses them.
2. **Settings → Login to Spotify** (PKCE OAuth) — or paste a playlist URL in
   the Spotify tab. Your library playlists appear in the tree; *Select All*
   then *Download* builds `Music/` + one `.m3u` per playlist.
3. **Batch CSV tab** — for Spotify GDPR exports: point at the folder, choose
   *Sync Missing Only*, and it fills the gaps.
4. **Lyric Syncer tab** — for tracks no online database has lyrics for. Pick
   a model (e.g. `small`), press Sync; GPU is auto-detected.

### Config (`~/.peak_config.json`)

| Key | Meaning |
|---|---|
| `library_root` | Where `Music/` and `Playlists/` live |
| `login_method` | `embedded` (WebView2 in-app) or `system` browser |
| `yt_cookies_file` | Optional manual cookies.txt — must be a **file** (a folder here is auto-cleared) |
| `download_subtitles` | Grab timed YouTube subs → `.lrc` + embed |
| `sponsorblock` | Mark sponsor/intro/outro chapters |
| `audio_bitrate_max` | Target Opus bitrate when Premium cookies present |

### Troubleshooting

- **"Requested format is not available"** — handled automatically (format
  demotion); make sure you're logged in (Settings → Login to YouTube).
- **Downloads slow / bot-checks** — log in to YouTube inside the app; the
  logged-in session avoids most walls. Avoid more than ~10 parallel workers.
- **Lyric Syncer "venv missing"** — it expects a whisperX venv at
  `C:\Users\<you>\whisperX\.venv` (install with `pip install whisperx
  "numpy<2"`; torch must match your CUDA).
- **Spotify API 403/429** — the app falls back to the cookie/GQL client and
  the `libraryV3` partner query automatically.

## License

MIT — see [LICENSE](LICENSE). Downloaded content is for personal use; respect
YouTube's Terms of Service and copyright law.
