# anime-sync

A personal anime library sync tool. It scans a Plex-organized anime folder, checks **AniList** for how many episodes have aired, finds the missing ones on **Nyaa.si**, and adds them straight to **qBittorrent** via its Web UI API — as a CLI or a single-page web app.

It can also **add a new anime** (search → pick show/season → verify a trusted source exists → create the folder → download) and **delete an anime** (remove from qBittorrent + wipe the folder from disk).

> Personal project, Windows-first. Credentials and paths are read from environment variables — nothing sensitive is hardcoded.

## Features

- **Sync** — for every season on disk, work out which aired episodes are missing and queue them.
- **Trusted-only, quality-first** — only **trusted** Nyaa releases are ever selected. Each episode gets the highest-resolution trusted release available, walking a preference ladder (**1080p → 720p → 480p**); lower rungs are only searched for episodes with nothing above.
- **Batch/box fallback** — when a season has no episodes on disk *and* no per-episode trusted release exists at any resolution (e.g. older shows with no SubsPlease release), it falls back to a single trusted **whole-season box**, matched season-aware against that season's romaji/English titles and excluding movies/recaps/specials.
- **Add a new anime** — AniList search, season picker (walks the sequel chain), availability check *before* creating anything, then folder creation + download.
- **Delete an anime** — removes matching torrents from qBittorrent and `rmtree`s the series folder; Plex picks it up on its next scan.
- **Web UI** — the "Sakura" editorial/risograph design with real AniList cover art, per-season episode chips, live sync progress, add/delete flows, and a download monitor.
- **Concurrent & polite** — AniList lookups and Nyaa searches run in thread pools over a shared keep-alive HTTP session; AniList results are cached to `anilist_cache.json` with a 6-hour TTL.

## How it works

```
scan library  →  AniList lookup  →  merge duplicate folders  →  find missing episodes
              →  search Nyaa (resolution ladder, trusted-only)  →  classify  →  qBittorrent
```

1. **Scanner** — walks the library, finds `Season NN` subfolders, extracts episode numbers from filenames.
2. **AniList lookup** — queries the free GraphQL API for episode count and airing status (for sequels, walks the `SEQUEL` relation chain).
3. **Gap finder** — compares episodes on disk against what has aired (never tries to grab episodes that haven't aired yet).
4. **Nyaa search** — resolution-tagged, season-specific RSS queries; picks the best trusted release by group preference and seeders.
5. **Classify + add** — each missing episode is already-in-qBittorrent / to-download / no-source. Downloads are added as **magnet links** built from the infohash in the RSS feed, with `save_path` set to the exact season folder.

All scraping/selection logic lives in `anime_downloader.py`; `webapp.py` is a thin Flask layer over the same functions.

## Filename patterns supported

```
[SubsPlease] Title - 07 (1080p) [HASH].mkv            # simple SubsPlease
[SubsPlease] Title S2 - 07 (1080p) [HASH].mkv         # SubsPlease with season tag
[SubsPlease] Title - 05v2 (1080p) [HASH].mkv          # v2 re-releases (counted as ep 5)
Title - S01E07 - Episode Name [HDTV-1080p].mkv        # Plex / HDTV format
```

## Requirements

- **Python 3.10+** (uses `X | None` type syntax)
- **qBittorrent** with the Web UI enabled (`Tools > Options > Web UI`)
- A Plex-organized anime library with `Season NN` subfolders
- Internet access (AniList + Nyaa)

## Setup

```bash
# From the repo folder
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Then create a `.env` (copy `.env.example`) and fill in your qBittorrent credentials:

```ini
QBIT_USERNAME=admin
QBIT_PASSWORD=yourpasswordhere
# QBIT_HOST=localhost
# QBIT_PORT=8080
# ANIME_LIBRARY_PATH=C:\path\to\your\anime
```

`.env` is gitignored and never committed.

## Configuration

All credentials and paths come from **environment variables** (`.env`, or set them in your shell). The `.bat` launchers load `.env` automatically.

| Env var | Default | Purpose |
|---|---|---|
| `ANIME_LIBRARY_PATH` | a local Windows path | Root anime folder |
| `QBIT_HOST` | `localhost` | qBittorrent Web UI host |
| `QBIT_PORT` | `8080` | qBittorrent Web UI port |
| `QBIT_USERNAME` | `admin` | qBittorrent Web UI username |
| `QBIT_PASSWORD` | *(empty)* | qBittorrent Web UI password |
| `ANIME_WEB_PORT` | `8765` | Web app port (`PORT` honored as a fallback) |

Tunable constants at the top of `anime_downloader.py` include `RESOLUTION_LADDER`, `PREFERRED_GROUP` (default SubsPlease), `FUZZY_THRESHOLD`, `CACHE_TTL_HOURS`, `SKIP_SERIES`, `IGNORE_DIRS`, and the batch-fallback thresholds.

## Usage

### CLI

Always use the venv Python.

```bash
# Interactive menu: [1] Sync  [2] Add a new anime  [3] Delete an anime  [Q] Quit
.venv\Scripts\python.exe anime_downloader.py

# Flags skip the menu:
.venv\Scripts\python.exe anime_downloader.py --sync               # sync, no menu
.venv\Scripts\python.exe anime_downloader.py --dry-run            # preview, no downloads
.venv\Scripts\python.exe anime_downloader.py --series "Kill Ao"   # single series
```

On Windows, double-clicking **`Sync Anime.bat`** loads `.env` and lands on the menu.

### Web app

```bash
.venv\Scripts\python.exe webapp.py            # serves on http://localhost:8765
.venv\Scripts\python.exe webapp.py --no-browser
```

Or double-click **`Anime Web.bat`**. (qBittorrent owns port 8080; the web app uses 8765.)

## Caching

`anilist_cache.json` is auto-created (in your library directory) and human-editable. To fix a bad AniList match, set the correct `anilist_id` for a series directly in the cache. To force a re-fetch, delete that series entry or the whole file.

## Notes

- **qBittorrent connection** uses the Web UI API. If it can't connect, `--dry-run` still works (dedup just treats everything as new).
- **Magnet links only** — the tool never hands `.torrent` URLs to qBittorrent (Nyaa rejects those fetches); it builds `magnet:?xt=urn:btih:...` from the RSS infohash with public trackers appended.
- **Code and media are kept separate** — this repo lives in its own folder, a *sibling* of the Plex library, so the scanner never sees the code and Plex never sees the scripts.

## Project layout

```
anime_downloader.py    # all core logic (scan / AniList / Nyaa / qBittorrent)
webapp.py              # Flask layer over the same functions
.webui/                # static frontend (HTML/JS/CSS) — dot-prefixed so the scanner skips it
tests/                 # offline regression tests
Sync Anime.bat         # CLI launcher (loads .env)
Anime Web.bat          # web app launcher (loads .env)
.env.example           # template — copy to .env
```
