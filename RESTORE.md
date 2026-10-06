# SHUNGITE — Full Backup Restore Guide

This backup captures the **complete working state** of SHUNGITE as of
**2026-10-06** — the build where everything works (downloads, batch CSV,
Spotify tab, lyric syncer, all verified end-to-end).

## What's inside

| Folder | Contents |
|---|---|
| `source/` | Every app .py file, the PyInstaller spec, requirements.txt, version resource, Inno Setup installer script, GitHub Actions workflow, README, LICENSE, screenshots |
| `build/` | The known-good frozen build (`Shungite.exe` + `_internal/` — ffmpeg, deno, all deps) |
| `release/` | `Shungite_Setup_v1.0.0.exe` — the Inno Setup installer (wizard + uninstaller) |
| `state/` | App config (`AppData\LocalLow\Shungite` — config, login state; cookies EXCLUDED, see below) |
| `dev/` | The full `D:\PEAK_Master` snapshot: source + repair scripts + build logs + test scripts (documents every bug fixed and how) |

## Restoring

### 1. App itself
- **Portable**: copy `build/Shungite/` anywhere, run `Shungite.exe`.
- **Installed**: run `release/Shungite_Setup_v1.0.0.exe`.
- **From source**:
  ```
  py -3.12 -m venv venv
  venv\Scripts\pip install -r source/requirements.txt
  venv\Scripts\python source/PEAK_GITHUB_true.py
  ```

### 2. App data (config + logins)
Copy `state/` contents into `%USERPROFILE%\AppData\LocalLow\Shungite\`.
**YouTube cookies are not in this backup** (session secret — don't put
credentials in archives). After restoring, open the app → Settings →
**Log in to YouTube** once; the app re-harvests fresh cookies into
`.peak_yt_cookies.txt`. Spotify login likewise re-auths in one click.

### 3. Rebuilding from this source
```
:: from source/, inside a Python 3.12 venv with requirements installed:
:: CRITICAL: clean environment — a stray PYTHONPATH poisons the build
:: (that bug shipped broken numpy into several builds once)
venv\Scripts\python -m PyInstaller PEAK_GITHUB_true.spec --noconfirm --clean ^
    --distpath dist3 --workpath build3\b
:: installer:
"C:\Program Files (x86)\Inno Setup 7\ISCC.exe" installer\Shungite.iss
```

## The known-good state (what "works" means)

- Downloads succeed on fresh installs: `remote_components: ["ejs:github"]`
  (yt-dlp's JS-challenge solver), TV-client bot-check fallback, harvested
  cookie policy
- Startup self-test writes `SELFTEST ok` into `search_debug.log`
- Batch CSV: searches return 8 entries, matcher picks official uploads,
  `Artist - Title.opus` files with embedded cover/lyrics/genre/purl
- Formats: Best Opus (default) + Best M4A only
- Matcher: hard DQ gates (slowed/nightcore/live/lyric-channels/covers),
  ±25% duration window from CSV durations, Song/Topic channel preference,
  official-audio rescue
- GUI: full log lines (queue-pump fix), `--tab=` deep link, no console
  windows anywhere

## Diagnosing a future breakage

`%USERPROFILE%\AppData\LocalLow\Shungite\search_debug.log` records every
search, match, and download failure with its real yt-dlp error. Compare
against `dev/` logs from the working era to see what changed.
