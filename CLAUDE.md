# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A personal anime library sync tool. It scans a Plex-organized anime folder, checks AniList for how many episodes have aired, finds missing episodes on Nyaa.si, and adds them directly to qBittorrent via its Web UI API. It can also interactively **add a new anime** (search → pick show/season → verify a trusted source exists → create the folder → download) and **delete an anime** (remove from qBittorrent + wipe the folder from disk).

## Project Layout

The **code lives in its own git repo** (this folder, e.g. `…\Torrent\anime-sync\`), kept **separate from the Plex library**. The anime media and `anilist_cache.json` live in the Plex directory (`ANIME_LIBRARY_PATH`, default `…\Torrent\Anime\`) — a *sibling* of this folder, not inside it. So the library scanner never sees the code, and Plex never sees the scripts. `LIBRARY_PATH` is an absolute path (overridable via `ANIME_LIBRARY_PATH`); nothing here assumes the code and media share a directory.

## Running the Script

Always use the venv Python, not the system Python:

```bash
# Interactive menu: [1] Sync library  [2] Add a new anime  [3] Delete an anime  [Q] Quit
.venv\Scripts\python.exe anime_downloader.py

# Flags skip the menu and run the sync directly:
.venv\Scripts\python.exe anime_downloader.py --sync             # sync, no menu
.venv\Scripts\python.exe anime_downloader.py --dry-run          # preview, no downloads
.venv\Scripts\python.exe anime_downloader.py --series "Kill Ao" # single series
```

Double-clicking `Sync Anime.bat` runs with no args via the venv Python → lands on the menu.

**Quality rule (strict, everywhere):** only **trusted** Nyaa releases are ever selected. Resolution follows the preference ladder `RESOLUTION_LADDER` (default **1080p → 720p → 480p**): each episode gets the highest-resolution trusted release that exists; lower rungs are only searched for episodes with nothing above. Only when no rung has a trusted release is an episode reported as unavailable.

**Batch/box fallback:** when a season has **no episodes on disk** *and* no trusted per-episode release exists at any resolution (e.g. older shows with no SubsPlease release like *Medaka Box*), the tool falls back to a single trusted **whole-season box** from any group. It is **season-aware** — matched against each season's own romaji + English titles (`token_sort_ratio ≥ BATCH_FUZZY_THRESHOLD`), excluding movies/spin-offs/single-episode files (`BATCH_EXCLUDE_KEYWORDS`), walking the same resolution ladder. So *Medaka Box* S1 resolves to its 720p box while *Medaka Box Abnormal* (S2) resolves to its 1080p box. It fires **only** when the per-episode search came up empty, so the common SubsPlease case costs no extra requests. Trusted-only is preserved. Implemented by `search_nyaa_batch_ladder` / `pick_batch_torrent` / `check_batch_availability`; wired into `prepare_sync` (so sync and add-confirm both use it) and surfaced by `/api/add/check` (`mode: "batch"`). Offline regression tests: `tests/test_batch_fallback.py`.

## Web App

`webapp.py` is a Flask server exposing the same flows as a single-page web UI — the **"Sakura" editorial/risograph design** (cream paper, ink, riso spot colors) with real AniList cover art (gradient fallback when a cover is missing), a headline ticker, a featured spread, count-up stats, per-season episode chips, sync with live progress, add/delete flows, and a download monitor. Double-clicking `Anime Web.bat` loads `.env`, starts the server on port **8765** (`ANIME_WEB_PORT` overrides; qBittorrent owns 8080), and opens the browser. Manual start: `.venv\Scripts\python.exe webapp.py` (`--no-browser` to suppress the auto-open).

- The frontend lives in **`.webui/`** (`index.html`, `app.js`, `style.css`, `tweaks-panel.jsx`) — dot-prefixed deliberately so the library scanner and the delete picker skip it; no `IGNORE_DIRS` entry needed. No Node/build step; Flask serves it statically at `/ui`. `app.js` is vanilla JS: it fetches `/api/library` and runs a `normalizeSeries()` adapter that maps the API shape onto the renderers' view model. The **Tweaks panel** (`tweaks-panel.jsx`) is a small React island loaded from the **unpkg CDN with in-browser Babel** — purely cosmetic (accent color / motion / ticker toggles, applied via CSS vars + body classes, persisted to `localStorage`); it needs internet, but the tool already does (AniList/Nyaa).
- **Endpoints** (all JSON; folder names travel in POST bodies, never URL path segments): `GET /api/library` (cache-only, never blocks on AniList), `POST /api/sync` + `GET /api/sync/status` (background-thread job, frontend polls 1 s), `GET /api/downloads`, `POST /api/anilist/search`, `/api/add/seasons`, `/api/add/check` (calls the shared `check_availability` — same trusted-only resolution-ladder policy as the CLI — then `check_batch_availability` when per-episode is empty, returning `mode: "episodes" | "batch" | "none"` plus `batch_title`/`batch_resolution`), `/api/add/confirm`, `/api/delete` (traversal-guarded, requires `confirm: true`).
- **One job at a time:** `SYNC_LOCK` serializes sync jobs, and `/api/delete` takes the same lock (never rmtree a folder mid-scan). Concurrent starts get 409.
- Each sync job uses a **fresh `QBittorrent` instance** (dedup snapshot is once-per-run, matching the CLI); monitor/delete share a long-lived instance that is replaced after a failed connection so the UI recovers when qBittorrent comes up.
- `debug=False` always — the Flask reloader would fork a second process and duplicate job state.

## Dependencies & Environment

Dependencies are isolated in `.venv/` — do not install them globally.

```bash
# Set up from scratch
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

`requirements.txt` has pinned versions. After adding/upgrading a package, update it:
```bash
.venv\Scripts\pip freeze | findstr /i "flask requests rapidfuzz qbittorrent" > requirements.txt
```

## Configuration

Credentials and paths are read from **environment variables** — they are not hardcoded in source. `Sync Anime.bat` sets them automatically. To run from the terminal manually:

```powershell
$env:QBIT_USERNAME="yourusername"; $env:QBIT_PASSWORD="yourpasswordhere"; .\.venv\Scripts\python.exe anime_downloader.py
```

| Env var | Default | Purpose |
|---|---|---|
| `ANIME_LIBRARY_PATH` | hardcoded Windows path | Root anime folder |
| `QBIT_HOST` | `localhost` | qBittorrent Web UI host |
| `QBIT_PORT` | `8080` | qBittorrent Web UI port |
| `QBIT_USERNAME` | `admin` | qBittorrent Web UI username |
| `QBIT_PASSWORD` | _(empty)_ | qBittorrent Web UI password |
| `ANIME_WEB_PORT` | `8765` | Web app port (`PORT` is honored as a fallback) |

Other config constants at the top of `anime_downloader.py`:

| Variable | Purpose |
|---|---|
| `SKIP_SERIES` | Folders to never process (completed BD releases, duplicate-name folders) |
| `IGNORE_DIRS` | Non-series folders skipped by the scanner and the delete picker (e.g. `.claude`, `__pycache__`, `codebase-health`, recycle bin) |
| `PREFERRED_GROUP` | Nyaa release group preference (default: SubsPlease) |
| `RESOLUTION_LADDER` | Resolution preference order (default: 1080p, 720p, 480p); `RESOLUTION` is its first rung |
| `FUZZY_THRESHOLD` | AniList title match strictness (0–100, default: 70) |
| `CACHE_TTL_HOURS` | Hours before AniList data is re-fetched (default: 6) |
| `ANILIST_RATE_LIMIT_SECS` | Courtesy delay between sequential AniList requests (default: 0.4) |
| `MAX_PROBE_EPISODES` | Episode ceiling when total count is unknown in add-anime flow (default: 50) |
| `BATCH_FUZZY_THRESHOLD` | Min `token_sort_ratio` of a whole-season box title vs the season's AniList title for the batch/box fallback (default: 80) |
| `BATCH_EXCLUDE_KEYWORDS` | Substrings that disqualify a batch as a TV-season box: movie/spin-off/recap markers (default: movie, gekijou, gekijouban, recap, side story, specials) |

## Architecture

All core logic lives in `anime_downloader.py`; `webapp.py` (see **Web App** above) is a thin Flask layer over the same functions and contains no scraping/selection logic of its own. `main()` parses args, builds one `QBittorrent` instance, then either runs `run_sync(...)` (flags) or `interactive_menu(...)` (no args).

**Data models** (`@dataclass`): `Torrent` (a Nyaa result: title/link/seeders/trusted/remake/resolution/infohash), `Candidate` (an AniList search hit for the add flow), `SeasonOption` (one season in a show's sequel chain). The AniList "info" dict and the JSON cache are intentionally left as plain dicts (the cache serializes them directly).

**`class QBittorrent`** wraps the Web UI client; it connects lazily, caches the existing-torrent name+hash sets once per run (`_load_existing()`), and exposes `has()` / `add()` / `delete()`, plus fresh-fetch queries `hashes_under()` / `torrents_under()` (save_path-prefix matching, used by delete flows and the web monitor). Degrades gracefully if qBittorrent is down.

The sync is split into **compute** and **present**: `prepare_sync()` does everything with no console output — concurrent network-fetch phases, then per-episode classification via `classify_episodes()` — and returns a plan dict (one entry per season with `missing` / `in_qbit` / `to_download` / `no_source`). It accepts an optional `progress(event, payload)` callback (fired from worker threads for `*_progress` events — callbacks must be thread-safe and must never print). `run_sync()` is a thin presenter over it: prints the plan sequentially, performs the `qbit.add()` calls, and returns the `set[str]` of added infohashes, which the interactive menu uses to offer `monitor_downloads()`. The web app's job runner consumes `prepare_sync()` directly. Never print from worker threads.

All HTTP traffic goes through a single module-level `requests.Session` (`SESSION`) so connections to AniList and Nyaa are reused (keep-alive/pooling) instead of re-handshaking per call. It's shared safely across the thread pools.

1. **Scanner** (`scan_library`) — walks `LIBRARY_PATH`, finds `Season NN` subfolders, extracts episode numbers from filenames via regex. Also picks up root-level orphan `.mkv` files and merges them into the correct season. Records a per-season `style` (`subsplease_simple` / `subsplease_stag` / `plex`) used to build Nyaa queries. The root directory is listed once and reused across both passes; folders in `IGNORE_DIRS` (and any dot-prefixed folder) are skipped.

2. **AniList lookup** (`anilist_lookup`) — queries `https://graphql.anilist.co` (free, no auth) for episode count and airing status. For Season N > 1, calls `walk_sequel_chain()`, which delegates to `_walk_chain()` — a shared generator that walks the `SEQUEL` relation chain with rate-limited fetches. Run concurrently across all series via `ThreadPoolExecutor`. Results cached in `anilist_cache.json` with a 6-hour TTL. The `info` dict's airing `status` feeds `format_season_status()`, which the print phase renders as a human-readable **Season status** line (e.g. "Finished airing" / "Still airing (ep 9 of 12 aired)").

3. **Duplicate-folder merge** — after AniList lookups, units are grouped by `(anilist_id, season_num)`. If two folders are the same show under different titles (e.g. English vs romaji), their present-episode sets are merged into the one with the most episodes; the others are marked `duplicate_of` and skipped.

4. **Gap finder** (`find_missing_episodes`) — compares present episodes against what has aired. Uses `nextAiringEpisode.episode - 1` as the ceiling for currently-airing shows so it never tries to download episodes that don't exist yet.

5. **Nyaa search** (`search_nyaa_ladder` → `search_nyaa_series`) — run concurrently per series. Queries are resolution-tagged, so the ladder fetches the preferred rung's feed first (**one** RSS request per series in the common case) and only fetches the next rung's feed for episodes that had no trusted match above — `search_nyaa_ladder` returns `[(resolution, prefiltered_pool), ...]` in ladder order. Each rung tries the folder-name query then the AniList romaji title; both are SubsPlease-tagged and season-specific. **No broad untagged fallback** — it would return other seasons' episodes. Selection is split so episode loops don't redo work: `prefilter_torrents` applies the episode-independent filters once per rung (requires `trusted` + that rung's `resolution`, filters to the requested `season_num` via `detect_torrent_season`), then `pick_episode_ladder` walks the rungs calling `pick_episode`, which matches a specific episode via `torrent_has_episode` (the Plex branch requires a full `S##E##` prefix so it won't false-match hex hashes like `[39E1D3C2]`) and picks the best by group preference then `(trusted, seeders)` — a match at a higher rung always beats any match below it. `pick_best_torrent` is a single-resolution wrapper for single-shot callers. The `Torrent.infohash` field is populated from `<nyaa:infoHash>` in the RSS feed.

6. **Classify + add** — each missing episode is sorted into: already in qBittorrent (hash-first dedup via `qbit.has(title, infohash)` — not re-added), to-download (`qbit.add(...)`), or no-source. Torrents are added as **magnet links** (`build_magnet(infohash, title)`) rather than `.torrent` file URLs, which avoids qBittorrent having to fetch from Nyaa directly (Nyaa rejects those requests). `qbit.add` sets `save_path` to the exact season folder.

### Sequel chain walking

`_walk_chain(base_media, max_hops)` is the single implementation that follows the AniList SEQUEL relation chain. Both `walk_sequel_chain` (returns the media dict for a target season number) and `enumerate_seasons` (returns all seasons as `SeasonOption` list) delegate to it. Any change to sequel-selection logic (e.g. split-cour tiebreaking, TV format filtering) belongs in `_walk_chain` only.

### Add-anime flow (`add_anime_interactive`)

Search AniList (`_anilist_search_candidates`) → show numbered `Candidate`s → user picks a show → `enumerate_seasons` walks the SEQUEL chain → user picks a season → availability check via the shared `check_availability` (ladder Nyaa search, then per-episode probing — returns `{episode: best resolution}`) confirms trusted episodes exist **before** creating anything → on confirm, create `LIBRARY_PATH/<romaji>/Season NN/` and download by calling `run_sync(series_filter=<sanitized folder name>)`. Folders use the **romaji** title (run through `safe_folder_name`) to match SubsPlease filenames and avoid the English/romaji duplicate trap.

### Monitor (`monitor_downloads`)

Called after sync when the user opts in. Polls `client.torrents_info()` every 5 seconds, printing a live status line (count remaining, speed, names) until all added infohashes reach a terminal state. Exits cleanly on Ctrl+C.

### Delete flow (`delete_anime_interactive`)

Lists all series folders, user picks one, confirms, then: removes matching torrents from qBittorrent by matching `save_path` prefix (with `delete_files=False` since we handle disk deletion ourselves), then `shutil.rmtree`s the entire series folder. Plex picks up the removal on its next scan.

## Filename Patterns Supported

- `[SubsPlease] Title - 07 (1080p) [HASH].mkv` — simple SubsPlease
- `[SubsPlease] Title S2 - 07 (1080p) [HASH].mkv` — SubsPlease with season tag
- `[SubsPlease] Title - 05v2 (1080p) [HASH].mkv` — v2 re-releases (counted as ep 5 present)
- `Title - S01E07 - Episode Name [HDTV-1080p].mkv` — Plex/HDTV format

## Claude Code Skills

Project-scoped skills live in `.claude/skills/<name>/SKILL.md` (auto-discovered by Claude Code). Currently: `codebase-health` — an architecture/tech-debt audit skill. Skills must NOT sit at the library root, or the scanner would treat them as anime folders; `.claude` is in `IGNORE_DIRS` so anything under it is safely skipped.

## Cache File

`anilist_cache.json` is auto-created and human-editable. To manually fix a bad AniList match, add the correct `anilist_id` for a series directly in the cache. To force a re-fetch, delete the series entry or the whole file.

## Gotchas / Things to Know

- **Same show, two folders.** If a show exists under both its English and Japanese titles, downloads (identical infohash) only land in one folder; the other looks "missing". The duplicate-folder merge handles this automatically, but the intended fix is to keep one folder and add the other to `SKIP_SERIES`.
- **qBittorrent connection** uses the Web UI API (`Tools > Options > Web UI`), not magnet handlers. If it can't connect, `--dry-run` still works (dedup just treats everything as new).
- **Magnet links only** — the script never passes `.torrent` file URLs to qBittorrent. It always constructs `magnet:?xt=urn:btih:HASH` from the infohash in the Nyaa RSS feed, with a set of public trackers appended. This avoids qBittorrent fetching from Nyaa directly (which Nyaa rejects).
- **Output altitude:** all `print`s happen in the final sequential phase. Don't add prints inside the `ThreadPoolExecutor` workers — output would interleave.
- **Plex naming note:** SubsPlease files are named by the romaji title (e.g. `Tongari Boushi no Atelier`), so downloads into an English-named folder won't match Plex's expected episode naming. This is cosmetic, not a script bug.
