<div align="center">

# ◆ SHUNGITE

**YouTube Music + Spotify Downloader & Library Builder**

Downloads your entire Spotify library — every playlist, every mix, and **every
song you've ever streamed in your account's history** — from YouTube as
best-quality Opus, named `Artist - Title`, with cover art, synced lyrics,
genres, and the source link embedded into the file itself.

![Python 3.12](https://img.shields.io/badge/python-3.12-blue) ![License: MIT](https://img.shields.io/badge/license-MIT-green) ![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey)

**⬇ [Download the installer from Releases](https://github.com/IrtezaAsif/Shungite/releases)** — setup wizard, desktop shortcut, uninstaller included.

</div>

---

![The Batch CSV tab](docs/screenshots/04_batch.png)

## What it does

Paste a link, pick a playlist, or drop in your Spotify export — SHUNGITE
searches YouTube for each song, picks **the most-viewed correct upload**
(never a cover, slowed-down edit, nightcore, lyric-video reupload, or live
version), downloads its best audio as Opus, and writes into the file's own
tags:

- **Cover art** — the upload's own artwork
- **Synced lyrics** — timed, original language, embedded as a real lyric track
- **Genre** — per-track lookup from iTunes
- **The exact YouTube link** — every file knows where it came from
- **Clean tags** — always the true song title and artist, never the video title

Every tab uses the same pipeline, so a download behaves identically whether
it comes from the Single tab, a Spotify playlist, the YT Music library, or a
CSV of 6,700 songs.

---

## Installation

### Option A — installer (recommended)

Grab **`Shungite_Setup_v1.0.0.exe`** from
[Releases](https://github.com/IrtezaAsif/Shungite/releases) and run it. A
setup wizard walks you through license → install location → shortcuts, and
installs a proper uninstaller. No console windows ever appear.

### Option B — portable

Grab `Shungite_win64.zip` from the same page, extract anywhere, run
`Shungite.exe`.

### First run

![Setup wizard](docs/screenshots/00_wizard.png)

A built-in wizard (above) picks your music folder and offers the embedded
YouTube login. All data lives in
`%USERPROFILE%\AppData\LocalLow\Shungite` — config, cookies, logins, and by
default the `Music Library` folder. Nothing touches Program Files or the
registry.

> **Log in to YouTube inside the app** (Settings → YouTube login). A logged-in
> session is what keeps YouTube's bot walls away during bulk downloads.

---

## Every tab, in depth

### 🎵 Single

![Single tab](docs/screenshots/01_single.png)

Paste any YouTube / YT Music URL (or a search query) and hit Download.
Auto-download on paste is supported. Quality picker offers exactly two
formats:

- **Best Opus** (default) — the highest Opus stream YouTube serves, copied
  into `.opus` with zero re-encode when possible
- **Best M4A** — AAC stream copy into `.m4a`

### 📃 Playlist

![Playlist tab](docs/screenshots/02_playlist.png)

Paste a YouTube playlist URL. Every entry runs through the same
match → download → embed pipeline with per-track log lines, in parallel,
cancellable.

### 🔎 Search

![Search tab](docs/screenshots/03_search.png)

Interactive YouTube search with a library search fallback — click a result to
download it, or find what you already have.

### 📊 Batch CSV — including your entire Spotify history

![Batch CSV tab](docs/screenshots/04_batch.png)

Point it at a folder of CSVs (Exportify format, or the GDPR
`Spotify_history_all.csv` it writes itself) and it bulk-fills your library:

- **Every CSV in the folder** is parsed (`Track Name`, `Artist Name(s)`,
  `Duration (ms)` columns), grouped per playlist.
- **Resumable by design** — an `_index.txt` tracks what you have; *Sync
  Missing Only* fetches only the gaps.
- **Parallel workers** (default 6) with a working Cancel that kills in-flight
  ffmpeg trees.
- **`failed_downloads.txt`** records *why* each failure failed — with the
  actual search results it saw.
- **One `.m3u` per playlist** into `Playlists/`.

**📦 Import Spotify data.zip — download your entire listening history.**
This is the headline feature: request your GDPR account export from Spotify
(Settings → Privacy → Download your data), then click *Import Spotify
data.zip*. SHUNGITE extracts **every single track you have ever streamed**
(`Streaming_History_Audio*.json`), deduplicates it into a unique track list,
looks up real durations from iTunes, writes `Spotify_history_all.csv`, and
drops you straight into the batch — so your **complete Spotify history
becomes a downloadable library**, not just your saved playlists.

### 🟢 Spotify

![Spotify tab](docs/screenshots/05_spotify.png)

Log in with the embedded PKCE OAuth window (a shared community client ID is
built in — zero setup). Then:

- Your playlists, liked songs, albums, and **personalized mixes** (Daily
  Mixes, daylist, Discovery/Release Radar — same GQL route the Spotify web
  client uses) appear in a tree.
- *Select All* downloads 100+ playlists in one go; a **merge mode**
  cross-deduplicates tracks shared between playlists.
- Downloads land in a flat `Music/` folder; one `.m3u` per playlist is
  written automatically.
- The tab falls back to the cookie/GQL client automatically if the official
  API rate-limits (429s) — downloads keep going.

### 🎧 YT Music

![YT Music tab](docs/screenshots/06_ytmusic.png)

Sign in with the same embedded window and sync your YouTube Music library —
uploads, liked songs, and playlists, all through the fast InnerTube API.

### 📈 Stats

![Stats tab](docs/screenshots/07_stats.png)

Library statistics from the built-in SQLite history: counts, sizes, top
artists, failure reports.

### 🎤 Lyric Syncer

![Lyric Syncer tab](docs/screenshots/08_lyricsyncer.png)

For tracks no lyrics database covers, **WhisperX** runs forced alignment on
your NVIDIA GPU (CUDA auto-detected, float16): the known lyric text is
aligned against the actual audio *phrase by phrase*, producing a genuinely
synced `.lrc` embedded into the tags. Runs in the background with per-file
progress and a cancel button.

### ⚙ Settings

![Settings tab](docs/screenshots/09_settings.png)

Everything in one place: YouTube login (embedded WebView2 window, cookies
stored in app data), Spotify login, cookie source (harvested jar / browser /
manual file), default quality, Subsonic/Navidrome push, last.fm scrobbling,
the LAN web remote, and the debug log location.

---

## How the matching engine works

Every download tab funnels through one pipeline: `_match_track()` in
`PEAK_GITHUB_true.py`, scoring in `matchers.py`. It finds the **most-viewed
correct upload** in five stages:

1. **Search** — YouTube InnerTube (YT Music's own API) first, with a
   cookie-authenticated ytsearch fallback and a ytmusicsearch last resort.
2. **Hard gates** — junk uploads are rejected outright, never merely
   down-ranked: `slowed` / `sped up` / `nightcore` / `8d` / `karaoke` /
   `reverb` / `mashup` / `amv` / covers / remixes / `live at` / `THE FIRST
   TAKE` / Dolby-binaural reuploads / lyric-translation channels. Unless your
   query itself asks for one, these can never win.
3. **Duration window** — when the CSV/Spotify metadata provides the real
   duration, the window tightens to ±25% around it, killing live versions
   and radio edits.
4. **Fusion scoring** — fuzzy title/artist similarity + search-rank +
   log-scaled view count, with big bonuses for official channels: YT Music's
   auto `Song`/`Topic` channels (the real original upload), and any channel
   whose name is the artist. A CJK bridge matches Japanese/Korean/Chinese
   titles by character overlap, so romanized queries still find the original
   upload.
5. **Rescue queries** — if the survivor is unofficial or duration-less, the
   search retries with `"… official audio"`; poisoned titles (feat-lists,
   `(From 'X')`) get simplified and retried.

The result: you get the official upload the artist actually published — the
one with the real cover, the real audio, and the real captions SHUNGITE can
turn into synced lyrics.

## How enrichment works (automatic on every download)

`_post_download_fixups()` guarantees, for every file:

| What | How |
|---|---|
| **Cover art** | The video's thumbnail, re-encoded and embedded as a proper front-cover tag; iTunes/Last.fm/Deezer artwork as fallbacks (`titanium_enrich.py`) |
| **Synced lyrics** | Three layers: ① YouTube's own timed captions for that exact video (original language preferred — Japanese songs get Japanese lyrics, never machine-translated English), converted to `.lrc`; ② LRClib exact-match keyed by real duration and language; ③ the Lyric Syncer's WhisperX alignment for everything else |
| **Genre** | Per-track iTunes Search API lookup, cached per artist |
| **Source link** | The exact YouTube URL in the `purl` tag |
| **True tags** | Title/artist always from Spotify metadata, never the video title |
| **Cleanup** | `.vtt`, `.jpg`, `.info.json`, sidecars deleted after embedding (with `glob.escape` so names like `[English Ver]` can't dodge the cleanup) |

## The resilient download engine

`download_one()` wraps yt-dlp with a full recovery chain:

- **JS-challenge solver** — `remote_components: ["ejs:github"]` fetches
  yt-dlp's challenge-solver script from GitHub on first use. Fresh installs
  need this or YouTube walls every download with *"The page needs to be
  reloaded."* It's built in and enabled by default.
- **Bot-check fallback** — on a bot wall the client switches to the **TV
  player client** (serves real audio formats without a PO token) and paces
  retries; on unavailable videos it retries with the harvested cookie jar.
- **Cookie policy** — searches and downloads always attach the harvested
  login cookies from app data, so bulk runs ride a logged-in session.
- **Format demotion** — premium format IDs (774/141) auto-demote to
  `bestaudio/best` instead of failing.
- **No consoles, no windows** — the app is a `windows` GUI subsystem binary
  and every child process (ffmpeg, deno, WhisperX) inherits
  `CREATE_NO_WINDOW`. The only thing on your screen is the app.
- **Diagnostics** — every search and failed download is logged with its real
  reason to `%USERPROFILE%\AppData\LocalLow\Shungite\search_debug.log`, and
  a startup self-test performs one real download so a broken environment is
  visible immediately.

### Command line

```
Shungite.exe --tab=batch        # open on a specific tab
```
Names: `single playlist search batch spotify ytm stats enhance lyric settings` (or 0–9).

---

## Build from source

```bat
git clone https://github.com/IrtezaAsif/Shungite.git
cd Shungite
py -3.12 -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python PEAK_GITHUB_true.py
```

Requirements: **Python 3.12**, ffmpeg on PATH (bundled by the build),
WebView2 Runtime (preinstalled on Windows 11), optionally deno for JS
challenges and a CUDA GPU for the Lyric Syncer.

### Build the exe + installer

```bat
venv\Scripts\pip install pyinstaller
:: IMPORTANT: clean environment — a stray PYTHONPATH poisons the build
venv\Scripts\python -m PyInstaller PEAK_GITHUB_true.spec --noconfirm --clean ^
    --distpath dist3 --workpath build3\b
```

The spec bundles ffmpeg, ffprobe, and deno, and outputs `dist3\Shungite\`.
For the setup installer: `installer\Shungite.iss` (Inno Setup) produces
`dist\Shungite_Setup_v1.0.0.exe` with the full wizard + uninstaller, and
`.github/workflows/build-installer.yml` builds it automatically on every
`v*` tag push.

## Project layout

| File | Purpose |
|---|---|
| `PEAK_GITHUB_true.py` | The app: all tabs, `_match_track()` pipeline, `download_one()` engine, `_post_download_fixups()` embed guarantee, wizard, LocalLow data dir, `--tab=` deep link |
| `matchers.py` | `strict_match()` scoring: hard DQ gates, duration bands, popularity fusion, official-channel bonuses, CJK bridge |
| `titanium_enrich.py` | Enrichment: covers (thumbnail → iTunes/last.fm/Deezer), `fetch_lyrics()` (LRClib, language-aware), `vtt_to_lrc()`, genre, tag embedding |
| `spotify_api.py` | Cookie/GQL Spotify client — playlists, mixes, home sections without the official API |
| `spotify_oauth.py` | PKCE OAuth login + refresh, playlist paging, artist genres |
| `innertube.py` | YouTube InnerTube search client |
| `login_window.py` | Embedded WebView2 YouTube login window |
| `browser_cookies.py` | Cookie extraction from installed browsers |
| `download_queue.py` | Job queue with cancel events |
| `history_db.py` | SQLite download history |
| `musicbrainz.py`, `sponsorblock.py`, `smart_playlists.py`, `acoustic_dedup.py`, `subsonic_push.py`, `web_remote.py` | MusicBrainz lookups, SponsorBlock marking, auto-playlists, dupe fingerprinting, Subsonic/Navidrome push, LAN web remote |
| `audit.py` | 20-check self-test suite |
| `PEAK_GITHUB_true.spec` | PyInstaller spec (ffmpeg + deno bundled, GUI subsystem) |
| `installer/Shungite.iss` | Inno Setup installer script |
| `.github/workflows/build-installer.yml` | CI: build + installer + release asset on tags |
| `version_info.txt` | Windows version resource (1.0.0.0) |

## Troubleshooting

- **Downloads say "Requested format is not available"** — handled
  automatically by demotion; make sure you're logged in to YouTube inside
  the app.
- **"The page needs to be reloaded" / everything fails on a fresh install** —
  fixed: the build fetches yt-dlp's JS-challenge solver via
  `remote_components`. Re-download the release.
- **Diagnosing anything** — open
  `%USERPROFILE%\AppData\LocalLow\Shungite\search_debug.log`; every search,
  match, and download failure is there with its real error. A startup
  `SELFTEST ok` line confirms the download chain is healthy.
- **Lyric Syncer wants a whisperX venv** — `pip install whisperx "numpy<2"`
  with a CUDA-matched torch, or use the source install.
- **Spotify 403/429** — the cookie/GQL client takes over automatically.

## License

MIT — see [LICENSE](LICENSE). Downloaded content is for personal use;
respect YouTube's Terms of Service and copyright law.

---

<div align="center">

**SHUNGITE** — *Made by Irteza* ◆

</div>
