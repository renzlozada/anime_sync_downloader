"""
anime_downloader.py

Scans your Plex-organized anime library, checks AniList for current episode
counts, finds missing episodes on Nyaa.si, and opens the magnet links in
qBittorrent.

Usage:
    python anime_downloader.py             # scan + open magnet links
    python anime_downloader.py --dry-run   # preview only, no downloads
    python anime_downloader.py --series "Kill Ao"  # single series

Dependencies:
    pip install requests rapidfuzz qbittorrent-api
"""

import sys

# Anime titles from AniList contain non-ASCII (curly quotes, Japanese). The default
# Windows console code page (cp1252) would crash on those; force UTF-8 output and
# replace anything unrenderable instead of raising.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
    import qbittorrentapi
    from rapidfuzz import process as fuzz_process, fuzz
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install requests rapidfuzz qbittorrent-api")

import argparse
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# ============================================================
# USER CONFIGURATION — edit these to match your setup
# ============================================================

LIBRARY_PATH = os.environ.get(
    "ANIME_LIBRARY_PATH",
    r"C:\Users\Renz Lozada\Downloads\Torrent\Anime",
)

PREFERRED_GROUP = "SubsPlease"

# Resolution preference ladder. Every episode gets the highest-resolution trusted
# release available: rungs are tried in order, and a lower rung is only searched
# for episodes that have no trusted release at the rungs above it. Only if no
# rung has a trusted release is an episode reported as unavailable.
RESOLUTION_LADDER = ["1080p", "720p", "480p"]
RESOLUTION        = RESOLUTION_LADDER[0]  # preferred rung (default for queries)

# Folders to skip entirely (completed BD releases, duplicate-name folders, etc.)
SKIP_SERIES = [
    "[Tenrai-Sensei] Fate Zero [BD][1080p][HEVC 10bit x265][Dual Audio]",
    # Same show as "Tongari Boushi no Atelier" (AniList 147105); that folder has all episodes.
    "Witch Hat Atelier",
]

# Fuzzy match threshold (0-100). Lower = more permissive title matching.
FUZZY_THRESHOLD = 70

# qBittorrent Web UI credentials — read from environment variables so they're
# never stored in source. Set QBIT_USERNAME / QBIT_PASSWORD before running,
# or create a .env file and load it (e.g. with python-dotenv).
QBIT_HOST     = os.environ.get("QBIT_HOST",     "localhost")
QBIT_PORT     = int(os.environ.get("QBIT_PORT", "8080"))
QBIT_USERNAME = os.environ.get("QBIT_USERNAME", "admin")
QBIT_PASSWORD = os.environ.get("QBIT_PASSWORD", "")

# How many hours before re-checking AniList for a cached series
CACHE_TTL_HOURS = 6

# Courtesy delay between back-to-back AniList HTTP requests (their free API has no
# published rate limit, but 0.4 s keeps us well clear of any undocumented throttle)
ANILIST_RATE_LIMIT_SECS = 0.4

# When a show's total episode count is unknown, probe this many episodes in the
# availability check (local filtering only — no extra network requests)
MAX_PROBE_EPISODES = 50

# Batch/box fallback: when no per-episode trusted release exists for an empty
# season, fall back to a single trusted whole-season box from any group.
BATCH_FUZZY_THRESHOLD = 80  # min token_sort_ratio of a box title vs the AniList title
BATCH_EXCLUDE_KEYWORDS = ("movie", "gekijou", "gekijouban", "recap",
                          "side story", "specials")

# ============================================================
# CONSTANTS
# ============================================================

CACHE_FILE       = os.path.join(LIBRARY_PATH, "anilist_cache.json")
ANILIST_ENDPOINT = "https://graphql.anilist.co"
NYAA_RSS_BASE    = "https://nyaa.si/?f=0&c=1_2&page=rss&q="
NYAA_NS          = "https://nyaa.si/xmlns/nyaa"

ANILIST_THREAD_WORKERS = 5  # concurrent AniList lookups
NYAA_THREAD_WORKERS    = 8  # concurrent Nyaa searches

# Shared HTTP session: reuses TCP/TLS connections (keep-alive + pooling) across all
# AniList and Nyaa requests instead of opening a fresh connection each call. urllib3's
# pooled session is thread-safe for issuing requests, so the thread pools share it.
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "anime-downloader/1.0"})

# Folders that are not anime series
IGNORE_DIRS = {".claude", "$RECYCLE.BIN", "System Volume Information",
               "__pycache__", "codebase-health"}


# ============================================================
# REGEX PATTERNS for episode number extraction
# ============================================================

# [SubsPlease] Title S2 - 06 (1080p) [HASH].mkv
RE_SP_STAG   = re.compile(r'\[SubsPlease\] .+ S(\d+) - (\d+)(?:v\d+)? \(\d+p\)', re.IGNORECASE)
# [SubsPlease] Title - 06 (1080p) [HASH].mkv
RE_SP_SIMPLE = re.compile(r'\[SubsPlease\] .+ - (\d+)(?:v\d+)? \(\d+p\)', re.IGNORECASE)
# Title - S01E06 - Episode Name.mkv  (Plex format)
RE_PLEX      = re.compile(r'S(\d+)E(\d+)', re.IGNORECASE)

# Resolution token in a torrent title (480p / 720p / 1080p / 2160p)
RE_RESOLUTION = re.compile(r'(\d{3,4})p', re.IGNORECASE)

# Batch detection helpers.
RE_SINGLE_EP  = re.compile(r'(?:\s-\s\d{1,4}(?:v\d+)?(?=\s|\[|\()|[Ss]\d{1,2}[Ee]\d{1,3})')
RE_EP_RANGE   = re.compile(r'\b\d{1,4}\s*[-~]\s*\d{1,4}\b')
RE_BATCH_WORD = re.compile(r'\b(batch|complete|seasons?)\b', re.IGNORECASE)


# ============================================================
# DATA MODELS
# ============================================================

@dataclass
class Torrent:
    """One Nyaa search result."""
    title: str
    link: str
    seeders: int
    trusted: bool
    remake: bool
    resolution: str | None  # e.g. "1080p", or None if not detected
    infohash: str | None = None  # from nyaa:infoHash in RSS — used for reliable dedup


@dataclass
class Candidate:
    """An AniList search result shown to the user when adding a new anime."""
    anilist_id: int
    romaji: str
    english: str | None
    year: int | None
    format: str | None
    episodes: int | None
    status: str | None
    raw_media: dict  # full AniList media node (carries relations for season walking)


@dataclass
class SeasonOption:
    """One selectable season in the add-anime flow (an entry in the SEQUEL chain)."""
    season_num: int
    anilist_id: int
    title: str
    episodes: int | None
    status: str | None
    next_airing: int | None
    format: str | None
    english: str | None = None  # season-specific English title (for batch matching)


# ============================================================
# ANILIST GRAPHQL QUERIES
# ============================================================

ANILIST_SEARCH_QUERY = """
query ($search: String) {
  Page(perPage: 8) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english native }
      episodes
      status
      format
      seasonYear
      coverImage { extraLarge large color }
      bannerImage
      description(asHtml: false)
      genres
      averageScore
      relations {
        edges {
          relationType
          node {
            id
            title { romaji english }
            seasonYear
            episodes
            status
            format
          }
        }
      }
      nextAiringEpisode { episode airingAt }
    }
  }
}
"""

ANILIST_ID_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english }
    episodes
    status
    format
    seasonYear
    coverImage { extraLarge large color }
    bannerImage
    description(asHtml: false)
    genres
    averageScore
    relations {
      edges {
        relationType
        node {
          id
          title { romaji english }
          seasonYear
          episodes
          status
          format
        }
      }
    }
    nextAiringEpisode { episode airingAt }
  }
}
"""


# ============================================================
# CACHE
# ============================================================

# Serializes cache file access so a web request reading the cache never sees a
# half-written file from a concurrently-finishing sync. The CLI is single-threaded
# through these functions, so this changes nothing for terminal use.
CACHE_LOCK = threading.Lock()


def load_cache() -> dict:
    with CACHE_LOCK:
        if not os.path.exists(CACHE_FILE):
            return {"version": 1, "series": {}}
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data.get("series"), dict):
                raise ValueError("bad structure")
            return data
        except Exception as e:
            print(f"  [warn] Cache file corrupt ({e}), starting fresh.")
            return {"version": 1, "series": {}}


def save_cache(cache: dict):
    with CACHE_LOCK:
        try:
            # Write-then-rename so a crash mid-write can't corrupt the cache.
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2)
            os.replace(tmp, CACHE_FILE)
        except Exception as e:
            print(f"  [warn] Could not save cache: {e}")


def cache_is_fresh(entry: dict) -> bool:
    last = entry.get("last_checked")
    if not last:
        return False
    try:
        checked = datetime.fromisoformat(last)
        age_hours = (datetime.now() - checked).total_seconds() / 3600
        return age_hours < CACHE_TTL_HOURS
    except Exception:
        return False


# ============================================================
# LIBRARY SCANNER
# ============================================================

def detect_filename_style(filename: str) -> str:
    if RE_SP_STAG.search(filename):
        return "subsplease_stag"
    if RE_SP_SIMPLE.search(filename):
        return "subsplease_simple"
    if RE_PLEX.search(filename):
        return "plex"
    return "unknown"


def parse_episode_number(filename: str) -> int | None:
    """Return episode number from a filename, or None if not detectable."""
    m = RE_SP_STAG.search(filename)
    if m:
        return int(m.group(2))
    m = RE_SP_SIMPLE.search(filename)
    if m:
        return int(m.group(1))
    m = RE_PLEX.search(filename)
    if m:
        return int(m.group(2))
    # Fallback: last standalone number in stem
    stem = Path(filename).stem
    nums = re.findall(r'(?<!\d)(\d{1,3})(?!\d)', stem)
    if nums:
        return int(nums[-1])
    return None


def parse_season_number(folder_name: str) -> int | None:
    """Extract season number from a folder name like 'Season 01' or 'Season 4'."""
    m = re.match(r'[Ss]eason\s*(\d+)', folder_name)
    if m:
        return int(m.group(1))
    return None


def parse_stag_season(filename: str) -> int | None:
    """For SubsPlease S-tag files, extract the season number from the filename."""
    m = RE_SP_STAG.search(filename)
    if m:
        return int(m.group(1))
    return None


RE_ILLEGAL_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*]')

def safe_folder_name(title: str) -> str:
    """Strip characters Windows forbids in folder names (the ':' in 'Re:Zero...'
    would make mkdir fail) plus trailing dots/spaces, which Windows also rejects."""
    return RE_ILLEGAL_FOLDER_CHARS.sub("", title).strip().rstrip(". ")


def scan_library(library_path: str, series_filter: str | None = None) -> list[dict]:
    """
    Returns a list of series dicts:
    {
      "folder_name": str,
      "folder_path": Path,
      "seasons": {
        1: {"path": Path, "episodes": set[int], "style": str}
      }
    }
    """
    root = Path(library_path)
    results = []
    orphan_eps: dict[tuple, set[int]] = {}  # (series_name, season) -> set of ep nums

    # List the root once and reuse for both passes (avoids a second filesystem scan).
    root_entries = list(root.iterdir())

    # Pass 1: collect root-level orphan .mkv files
    for item in root_entries:
        if item.is_file() and item.suffix.lower() == ".mkv":
            ep = parse_episode_number(item.name)
            season_in_name = parse_stag_season(item.name)
            if ep is not None:
                # Try to extract series name from filename
                # Remove group tag, hash, resolution, extension
                sname = item.stem
                sname = re.sub(r'^\[SubsPlease\]\s*', '', sname, flags=re.IGNORECASE)
                sname = re.sub(r'\s*S\d+\s*-\s*\d+.*$', '', sname).strip()
                sname = re.sub(r'\s*-\s*\d+.*$', '', sname).strip()
                s = season_in_name if season_in_name else 1
                key = (sname, s)
                orphan_eps.setdefault(key, set()).add(ep)

    # Pass 2: scan series folders
    for item in sorted(root_entries):
        if not item.is_dir():
            continue
        if item.name in IGNORE_DIRS or item.name.startswith("."):
            continue
        if item.name in SKIP_SERIES:
            continue
        if series_filter and series_filter.lower() not in item.name.lower():
            continue

        series: dict = {
            "folder_name": item.name,
            "folder_path": item,
            "seasons": {}
        }

        # Look for Season XX subfolders
        for sub in sorted(item.iterdir()):
            if not sub.is_dir():
                continue
            snum = parse_season_number(sub.name)
            if snum is None:
                continue  # skip Specials, OVA, etc.

            episodes: set[int] = set()
            style_votes: list[str] = []

            for ep_file in sub.iterdir():
                if ep_file.suffix.lower() not in (".mkv", ".mp4", ".avi"):
                    continue
                ep = parse_episode_number(ep_file.name)
                if ep is not None:
                    episodes.add(ep)
                style_votes.append(detect_filename_style(ep_file.name))

            # Majority style
            style = max(set(style_votes), key=style_votes.count) if style_votes else "unknown"

            # Merge any orphan episodes that belong to this series+season
            for (oname, oseas), oeps in orphan_eps.items():
                if oseas == snum and (
                    oname.lower() in item.name.lower() or
                    item.name.lower() in oname.lower()
                ):
                    episodes.update(oeps)

            series["seasons"][snum] = {
                "path": sub,
                "episodes": episodes,
                "style": style,
            }

        if series["seasons"]:
            results.append(series)
        else:
            # Series folder with no recognizable Season subfolders — skip silently
            pass

    return results


# ============================================================
# ANILIST API
# ============================================================

def anilist_request(query: str, variables: dict) -> dict | None:
    try:
        resp = SESSION.post(
            ANILIST_ENDPOINT,
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            print(f"  [anilist] GraphQL error: {data['errors'][0].get('message','?')}")
            return None
        return data
    except requests.RequestException as e:
        print(f"  [anilist] Request failed: {e}")
        return None


def titles_from_media(m: dict) -> list[str]:
    t = m.get("title", {})
    return [v for v in [t.get("romaji"), t.get("english"), t.get("native")] if v]


def best_anilist_match(folder_name: str, candidates: list[dict]) -> dict | None:
    """Fuzzy-match folder_name against AniList result candidates."""
    scored = []
    for c in candidates:
        best_score = 0
        for title in titles_from_media(c):
            score = fuzz.token_sort_ratio(folder_name.lower(), title.lower())
            if score > best_score:
                best_score = score
        scored.append((best_score, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] >= FUZZY_THRESHOLD:
        return scored[0][1]
    return None


def _walk_chain(base_media: dict, max_hops: int = 12):
    """Yield (season_num, media_dict) tuples by following the SEQUEL relation chain
    from base_media. Stops when the chain breaks, cycles, or max_hops is reached.
    Fetches each hop's full data from AniList with rate-limit courtesy sleep."""
    current = base_media
    seen_ids: set[int] = set()
    season_num = 1

    while current and current["id"] not in seen_ids and season_num <= max_hops:
        seen_ids.add(current["id"])
        yield season_num, current

        sequels = [
            edge["node"]
            for edge in current.get("relations", {}).get("edges", [])
            if edge.get("relationType") == "SEQUEL"
            and edge["node"].get("format") in ("TV", "TV_SHORT", None)
        ]
        if not sequels:
            break
        # If split-cour produces multiple sequels, prefer the later year
        sequels.sort(key=lambda x: x.get("seasonYear") or 0)
        next_id = sequels[-1]["id"]
        time.sleep(ANILIST_RATE_LIMIT_SECS)
        data = anilist_request(ANILIST_ID_QUERY, {"id": next_id})
        current = data["data"]["Media"] if data else None
        season_num += 1


def walk_sequel_chain(base_media: dict, target_season: int) -> dict | None:
    """Return the AniList media entry for target_season (1-indexed) by walking
    the SEQUEL chain from base_media. Season 1 = base_media itself."""
    for season_num, media in _walk_chain(base_media, max_hops=target_season):
        if season_num == target_season:
            return media
    return None


def enumerate_seasons(base_media: dict, max_seasons: int = 12) -> list[SeasonOption]:
    """Follow the SEQUEL chain from base_media and return one SeasonOption per season
    (season 1 = base_media). Used by the interactive add-anime flow to list seasons."""
    seasons: list[SeasonOption] = []
    for season_num, current in _walk_chain(base_media, max_hops=max_seasons):
        title = current.get("title", {})
        nae = current.get("nextAiringEpisode")
        seasons.append(SeasonOption(
            season_num=season_num,
            anilist_id=current["id"],
            title=title.get("romaji") or title.get("english") or f"id {current['id']}",
            episodes=current.get("episodes"),
            status=current.get("status"),
            next_airing=nae["episode"] if nae else None,
            format=current.get("format"),
            english=title.get("english"),
        ))
    return seasons


def _clean_description(html: str | None) -> str | None:
    """AniList descriptions arrive with light HTML even when asHtml is false
    (<br>, <i>, <b>); convert to plain text so the UI can render via textContent."""
    if not html:
        return None
    text = re.sub(r'<br\s*/?>', '\n', html)
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip() or None


def anilist_lookup(folder_name: str, season_num: int, cache: dict) -> tuple[dict | None, int | None]:
    """
    Returns (episode_info, base_anilist_id) for a series+season, using cache when fresh.
    episode_info: {anilist_id, total_episodes, is_airing, next_airing_episode, ...}
    base_anilist_id: id of season-1 entry (used by caller to update cache safely).

    Does NOT write to cache — caller must do that sequentially to avoid data races
    when this function is called from multiple threads.
    """
    series_cache = cache["series"].get(folder_name, {})
    season_cache = series_cache.get("seasons", {}).get(str(season_num))

    # Entries written before cover art was fetched lack "cover_image"; treat them
    # as stale once so they pick up the new fields (no cache version bump — that
    # would wipe manually-fixed anilist_id entries, a documented workflow).
    if season_cache and cache_is_fresh(season_cache) and "cover_image" in season_cache:
        return season_cache, series_cache.get("anilist_id")

    # --- AniList search ---
    data = anilist_request(ANILIST_SEARCH_QUERY, {"search": folder_name})
    if not data:
        # Fallback to stale cache if available
        if season_cache:
            print("  [anilist] Using stale cache (API unavailable).")
            return season_cache, series_cache.get("anilist_id")
        return None, None

    candidates = data["data"]["Page"]["media"]
    best = best_anilist_match(folder_name, candidates)
    if not best:
        # Try stripping parenthetical / year suffixes and retry
        stripped = re.sub(r'\s*[\(\[].*', '', folder_name).strip()
        if stripped != folder_name:
            best = best_anilist_match(stripped, candidates)

    if not best:
        print(f"  [warn] Could not match '{folder_name}' on AniList (score too low).")
        print(f"         Add its AniList ID manually to {CACHE_FILE} to fix this.")
        return None, None

    # --- Walk sequel chain to reach the right season ---
    if season_num > 1:
        # Check if we already know the base anilist_id
        base_id = series_cache.get("anilist_id") or best["id"]
        if base_id != best["id"]:
            time.sleep(ANILIST_RATE_LIMIT_SECS)
            data2 = anilist_request(ANILIST_ID_QUERY, {"id": base_id})
            base_media = data2["data"]["Media"] if data2 else best
        else:
            base_media = best

        media = walk_sequel_chain(base_media, season_num)

        if not media:
            # Fallback: search explicitly for season N
            fallback_query = f"{folder_name} Season {season_num}"
            time.sleep(ANILIST_RATE_LIMIT_SECS)
            data3 = anilist_request(ANILIST_SEARCH_QUERY, {"search": fallback_query})
            if data3:
                candidates3 = data3["data"]["Page"]["media"]
                media = best_anilist_match(fallback_query, candidates3) or best
            else:
                media = best
    else:
        base_media = best
        media = best

    # --- Extract info ---
    total = media.get("episodes")
    status = media.get("status", "")
    nae = media.get("nextAiringEpisode")
    next_ep = nae["episode"] if nae else None
    is_airing = status in ("RELEASING", "NOT_YET_RELEASED")

    cover = media.get("coverImage") or {}
    result = {
        "anilist_id": media["id"],
        "total_episodes": total,
        "is_airing": is_airing,
        "next_airing_episode": next_ep,
        "status": status,
        "romaji_title": media.get("title", {}).get("romaji"),
        "english_title": media.get("title", {}).get("english"),
        # Presentation fields for the web UI. Always present (possibly None) so the
        # freshness gate's "cover_image" check never refetches an already-new entry.
        "cover_image": cover.get("large"),
        "cover_image_xl": cover.get("extraLarge"),
        "cover_color": cover.get("color"),
        "banner_image": media.get("bannerImage"),
        "description": _clean_description(media.get("description")),
        "genres": media.get("genres") or [],
        "average_score": media.get("averageScore"),
        "last_checked": datetime.now().isoformat(timespec="seconds"),
    }

    return result, best["id"]


# ============================================================
# GAP FINDER
# ============================================================

def format_season_status(info: dict) -> str:
    """Human-readable label for whether this season has finished airing."""
    status = (info.get("status") or "").upper()
    total = info.get("total_episodes")
    next_ep = info.get("next_airing_episode")
    if status == "FINISHED":
        return "Finished airing"
    if status == "RELEASING":
        aired = (next_ep - 1) if next_ep else None
        if aired and total:
            return f"Still airing (ep {aired} of {total} aired)"
        if aired:
            return f"Still airing (ep {aired} aired)"
        return "Still airing"
    if status == "NOT_YET_RELEASED":
        return "Not yet aired"
    if status == "CANCELLED":
        return "Cancelled"
    if status == "HIATUS":
        return "On hiatus"
    return "Unknown"


def find_missing_episodes(present: set[int], info: dict) -> list[int]:
    total = info.get("total_episodes")
    next_ep = info.get("next_airing_episode")
    is_airing = info.get("is_airing", False)

    # Determine ceiling: what is the highest episode that could exist right now?
    if next_ep is not None:
        # Episodes 1 through next_ep-1 have aired
        ceiling = next_ep - 1
    elif total is not None:
        ceiling = total
    elif is_airing:
        # No data at all for ongoing show — skip
        return []
    else:
        return []

    if ceiling <= 0:
        return []

    # Start from 1 (episode 0 prologues are bonus content, not required)
    expected = set(range(1, ceiling + 1))
    # Remove episode 0 from expected if present set has no ep 0 context
    missing = sorted(expected - present)
    return missing


# ============================================================
# NYAA SEARCH
# ============================================================

def build_magnet(infohash: str, title: str) -> str:
    """Construct a magnet URI from an infohash. Avoids having qBittorrent fetch
    the .torrent file from Nyaa (which Nyaa sometimes rejects with a remote-content error)."""
    dn = urllib.parse.quote(title)
    trackers = "&tr=".join([
        "http://nyaa.tracker.wf:7777/announce",
        "udp://open.stealth.si:80/announce",
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://tracker.torrent.eu.org:451/announce",
    ])
    return f"magnet:?xt=urn:btih:{infohash}&dn={dn}&tr={trackers}"


def build_nyaa_series_query(name: str, season_num: int, style: str,
                            resolution: str = RESOLUTION) -> str:
    """Query for a whole series (no episode number) — one feed returns every episode."""
    if style == "subsplease_stag" and season_num > 1:
        return f"[{PREFERRED_GROUP}] {name} S{season_num} ({resolution})"
    else:
        return f"[{PREFERRED_GROUP}] {name} ({resolution})"


def search_nyaa_series(name: str, romaji: str | None, season_num: int, style: str,
                       resolution: str = RESOLUTION) -> list[Torrent]:
    """
    Fetch all torrents for a series in a single Nyaa request. Falls back to the
    AniList romaji title if the folder name yields nothing. Both queries are
    group-tagged and season-specific (S<N> for sequels); we deliberately do NOT
    do a broad untagged search, which would return OTHER seasons' episodes and
    cause cross-season false matches (e.g. a Season 1 file matching a Season 3 query).
    Returns the full result list; per-episode matching happens locally afterward.
    """
    results = search_nyaa(build_nyaa_series_query(name, season_num, style, resolution))
    if results:
        return results

    if romaji and romaji.lower() != name.lower():
        results = search_nyaa(build_nyaa_series_query(romaji, season_num, style, resolution))
        if results:
            return results

    return []


def search_nyaa(query: str) -> list[Torrent]:
    url = NYAA_RSS_BASE + urllib.parse.quote(query)
    try:
        resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"    [nyaa] Request failed: {e}")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"    [nyaa] XML parse error: {e}")
        return []

    ns = {"nyaa": NYAA_NS}
    items: list[Torrent] = []
    for item in root.findall(".//item"):
        link = item.findtext("link", "")
        # Prefer enclosure (magnet), fall back to link
        enc = item.find("enclosure")
        if enc is not None:
            link = enc.get("url", link)
        title = item.findtext("title", "")
        seeders_text = item.findtext("nyaa:seeders", "0", ns)
        trusted_text  = item.findtext("nyaa:trusted", "No", ns)
        remake_text   = item.findtext("nyaa:remake",  "No", ns)
        try:
            seeders = int(seeders_text)
        except ValueError:
            seeders = 0

        res_match = RE_RESOLUTION.search(title)
        resolution = f"{res_match.group(1)}p" if res_match else None
        infohash_text = item.findtext("nyaa:infoHash", "", ns)

        items.append(Torrent(
            title=title,
            link=link,
            seeders=seeders,
            trusted=trusted_text == "Yes",
            remake=remake_text == "Yes",
            resolution=resolution,
            infohash=infohash_text.lower() if infohash_text else None,
        ))
    return items


RE_TORRENT_SEASON = re.compile(r'\sS(\d+)\s*-\s*\d')

def detect_torrent_season(title: str) -> int:
    """Season number from a SubsPlease title. 'Title S2 - 01' -> 2; 'Title - 01' -> 1.
    Used to keep a Season-1 query from matching Season-2 releases (and vice-versa),
    since the untagged 'Title (1080p)' feed returns both."""
    m = RE_TORRENT_SEASON.search(title)
    return int(m.group(1)) if m else 1


def torrent_has_episode(title: str, episode: int) -> bool:
    """Check that a torrent title contains the specific episode number (not a different ep).
    Matches SubsPlease ' - 07 ' / ' - 07v2 ' style and Plex 'S01E07' style. The Plex branch
    requires the full S##E## prefix so it does NOT false-match hex hashes like [39E1D3C2]."""
    ep = str(episode)
    return bool(re.search(
        r'(?:[-\s_]0*' + ep + r'(?:v\d+)?[\s_\(\[]|[Ss]\d+[Ee]0*' + ep + r'(?:\D|$))',
        title,
        re.IGNORECASE,
    ))


def looks_like_single_episode(title: str) -> bool:
    """True if the title names exactly ONE episode (' - 07 ' or 'S01E07'), as
    opposed to a whole-season batch/box. Episode ranges (01-12) and explicit
    'batch'/'complete'/'season' markers are batches, not single episodes."""
    if RE_EP_RANGE.search(title) or RE_BATCH_WORD.search(title):
        return False
    return bool(RE_SINGLE_EP.search(title))


def _clean_torrent_title(title: str) -> str:
    """Strip group tags, bracketed/parenthesized metadata, codec/source noise and
    a trailing '| alt-title', leaving a bare show title for fuzzy matching."""
    t = title.split("|")[0]
    t = re.sub(r'\[[^\]]*\]', ' ', t)
    t = re.sub(r'\([^)]*\)', ' ', t)
    t = re.sub(r'\b(BD|BDRip|BluRay|Blu-ray|Remux|HEVC|AVC|x265|x264|10bit|8bit|'
               r'FLAC|AAC|LPCM|Dual Audio|Multi(?:-?Audio)?|Batch|Complete)\b',
               ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'\b\d{3,4}p\b', ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'\b\d{1,4}\s*[-~]\s*\d{1,4}\b', ' ', t)  # episode ranges (e.g. 1-12)
    return re.sub(r'\s+', ' ', t).strip()


def pick_batch_torrent(results: list[Torrent], romaji: str | None, english: str | None,
                       season_format: str | None = None, *,
                       resolution: str = RESOLUTION) -> Torrent | None:
    """Pick the best trusted whole-season box from broad (untagged) Nyaa results.
    Anchored to the AniList title via token_sort_ratio, so movies, spin-offs and
    wrong shows are excluded. Trusted-only: returns None if nothing qualifies."""
    refs = [r for r in (romaji, english) if r]
    is_tv = (season_format or "TV").upper() in ("TV", "TV_SHORT", "ONA", "OVA")
    best, best_key = None, None
    for t in results:
        if not t.trusted or not t.link or t.resolution != resolution:
            continue
        low = t.title.lower()
        if is_tv and any(k in low for k in BATCH_EXCLUDE_KEYWORDS):
            continue
        if looks_like_single_episode(t.title):
            continue
        core_title = _clean_torrent_title(t.title)
        score = max((fuzz.token_sort_ratio(core_title, ref) for ref in refs), default=0)
        if score < BATCH_FUZZY_THRESHOLD:
            continue
        key = (score, t.seeders)
        if best_key is None or key > best_key:
            best, best_key = t, key
    return best


def build_nyaa_batch_query(title: str, resolution: str = RESOLUTION) -> str:
    """Untagged broad query for a whole-season box (no group tag)."""
    return f"{title} {resolution}"


def search_nyaa_batch_ladder(romaji: str | None, english: str | None,
                             season_format: str | None = None) -> Torrent | None:
    """Resolution-ladder broad search for a trusted season box. Tries the
    preferred rung first and only drops to a lower rung if no trusted box matched.
    One Nyaa request per (title, rung) until a box is found; short-circuits. Only
    call this when the per-episode ladder found nothing (keeps the common case at
    one request per series)."""
    for reso in RESOLUTION_LADDER:
        for title in (english, romaji):
            if not title:
                continue
            results = search_nyaa(build_nyaa_batch_query(title, reso))
            box = pick_batch_torrent(results, romaji, english, season_format,
                                     resolution=reso)
            if box:
                return box
    return None


def prefilter_torrents(results: list[Torrent], *,
                       season_num: int | None = None,
                       require_trusted: bool = True,
                       resolution: str = RESOLUTION) -> list[Torrent]:
    """Apply the episode-independent filters (remake/link/resolution/trusted/season)
    once. Callers that loop over many episodes share this result instead of redoing
    the same filtering per episode."""
    # Hard requirements: not a remake, has a link, exact resolution, and (if strict) trusted.
    filtered = [
        t for t in results
        if not t.remake and t.link and t.resolution == resolution
        and (t.trusted or not require_trusted)
    ]
    # Keep only the requested season (a Season-1 query also returns Season-2 releases).
    if season_num is not None:
        filtered = [t for t in filtered if detect_torrent_season(t.title) == season_num]
    return filtered


def pick_episode(pool: list[Torrent], episode: int | None = None, *,
                 group: str = PREFERRED_GROUP) -> Torrent | None:
    """Pick the best torrent for a specific episode from an already season/resolution/
    trusted-filtered pool. Returns None if nothing matches that episode."""
    # If episode number given, keep ONLY torrents matching that episode. Strict:
    # the pool may be a whole-series feed, so never fall back to a different episode.
    if episode is not None:
        pool = [t for t in pool if torrent_has_episode(t.title, episode)]

    if not pool:
        return None

    # Prefer the configured group, then highest seeders.
    group_matches = [t for t in pool if group.lower() in t.title.lower()]
    candidates = group_matches if group_matches else pool

    return max(candidates, key=lambda t: (t.trusted, t.seeders))


def pick_best_torrent(results: list[Torrent], episode: int | None = None, *,
                      season_num: int | None = None,
                      require_trusted: bool = True,
                      resolution: str = RESOLUTION,
                      group: str = PREFERRED_GROUP) -> Torrent | None:
    """Pick the best torrent. Strict by default: only trusted releases at the exact
    resolution. Returns None if nothing qualifies (caller reports it unavailable).
    Convenience wrapper over prefilter_torrents + pick_episode for single-shot callers."""
    pool = prefilter_torrents(results, season_num=season_num,
                              require_trusted=require_trusted, resolution=resolution)
    return pick_episode(pool, episode, group=group)


def search_nyaa_ladder(name: str, romaji: str | None, season_num: int, style: str,
                       episodes) -> list[tuple[str, list[Torrent]]]:
    """Resolution-ladder Nyaa search: fetch the preferred rung's feed first; only
    if some episode in `episodes` has no trusted match there is the next rung's
    feed fetched, and so on. Returns [(resolution, prefiltered_pool), ...] in
    ladder order — callers pick per episode with pick_episode_ladder(). In the
    common all-1080p case this is still exactly one Nyaa request per series."""
    pools: list[tuple[str, list[Torrent]]] = []
    remaining = set(episodes)
    for reso in RESOLUTION_LADDER:
        if not remaining:
            break
        results = search_nyaa_series(name, romaji, season_num, style, resolution=reso)
        pool = prefilter_torrents(results, season_num=season_num, resolution=reso)
        pools.append((reso, pool))
        remaining = {ep for ep in remaining if pick_episode(pool, ep) is None}
    return pools


def pick_episode_ladder(pools: list[tuple[str, list[Torrent]]], episode: int | None = None, *,
                        group: str = PREFERRED_GROUP) -> Torrent | None:
    """Best torrent for an episode honoring the resolution ladder: any match at a
    higher-preference rung beats every match at the rungs below it."""
    for _reso, pool in pools:
        best = pick_episode(pool, episode, group=group)
        if best:
            return best
    return None


def check_availability(romaji: str, season: SeasonOption) -> dict[int, str]:
    """Trusted-source availability check for a season before anything is created:
    ladder Nyaa search, then local per-episode probing. Returns {episode: best
    available resolution} (empty = no trusted release at any rung). Shared by the
    CLI add flow and the web add flow so the policy can't drift."""
    style = "subsplease_stag" if season.season_num > 1 else "subsplease_simple"
    # Ceiling = episodes aired so far (mirror find_missing_episodes): for an airing show
    # only 1..next_airing-1 exist; for a finished show, 1..episodes.
    if season.next_airing:
        ceiling = season.next_airing - 1
    elif season.episodes:
        ceiling = season.episodes
    else:
        ceiling = MAX_PROBE_EPISODES  # unknown total; probe a generous range (local filter, no extra requests)
    episodes = range(1, ceiling + 1)
    pools = search_nyaa_ladder(romaji, season.title, season.season_num, style, episodes)
    out: dict[int, str] = {}
    for ep in episodes:
        best = pick_episode_ladder(pools, ep)
        if best:
            out[ep] = best.resolution or "?"
    return out


def check_batch_availability(season: SeasonOption) -> Torrent | None:
    """Add-flow preview: is there a trusted whole-season box for THIS season?
    Returns the box Torrent or None. Matches on the season's own romaji + English
    titles so sequels (e.g. 'Medaka Box Abnormal') don't grab Season 1's box.
    Meaningful only when per-episode check_availability is empty."""
    return search_nyaa_batch_ladder(season.title, season.english, season.format)


def format_availability(avail: dict[int, str]) -> str:
    """'1-9 @ 1080p, 10 @ 720p' — episode ranges grouped by resolution in ladder order."""
    if not avail:
        return "(none)"
    parts = []
    for reso in RESOLUTION_LADDER:
        eps = {ep for ep, r in avail.items() if r == reso}
        if eps:
            parts.append(f"{format_ep_range(eps)} @ {reso}")
    other = {ep for ep, r in avail.items() if r not in RESOLUTION_LADDER}
    if other:
        parts.append(f"{format_ep_range(other)} @ other")
    return ", ".join(parts)


# ============================================================
# QBITTORRENT CLIENT
# ============================================================

def _normalize_torrent_name(name: str) -> str:
    """Lowercase and strip any video extension so candidate and qBit names compare equal.
    qBittorrent keeps the .mkv on single-file torrents, but Nyaa titles also end in .mkv —
    normalizing both sides the same way is what makes dedup reliable."""
    name = name.lower().strip()
    for ext in (".mkv", ".mp4", ".avi"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name



class QBittorrent:
    """Thin wrapper over the qBittorrent Web UI client.

    Deduplicates by infohash (extracted from the magnet link) rather than by
    name. qBittorrent identifies torrents internally by infohash, so this is the
    only check that is guaranteed to match queued, downloading, and finished
    torrents regardless of how qBittorrent displays the name. Falls back to
    name-based dedup when no hash is available. Degrades gracefully if
    qBittorrent is unreachable — every method is a safe no-op and --dry-run works.
    """

    def __init__(self, host: str, port: int, username: str, password: str):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._client = None
        self._connect_failed = False
        self._existing_names: set[str] | None = None
        self._existing_hashes: set[str] | None = None

    def _client_or_none(self):
        if self._client is not None or self._connect_failed:
            return self._client
        try:
            client = qbittorrentapi.Client(
                host=self._host, port=self._port,
                username=self._username, password=self._password,
            )
            client.auth_log_in()
            self._client = client
        except Exception as e:
            self._connect_failed = True
            print(f"  [error] Could not connect to qBittorrent Web UI: {e}")
            print(f"          Make sure qBittorrent is open and Web UI is enabled.")
        return self._client

    def _load_existing(self):
        """Fetch all torrent info once and populate both the name and hash sets."""
        if self._existing_hashes is not None:
            return
        client = self._client_or_none()
        if client is None:
            self._existing_names = set()
            self._existing_hashes = set()
            return
        try:
            torrents = client.torrents_info()
            self._existing_names  = {_normalize_torrent_name(t.name) for t in torrents}
            self._existing_hashes = {t.hash.lower() for t in torrents if t.hash}
        except Exception:
            self._existing_names  = set()
            self._existing_hashes = set()

    def existing_names(self) -> set[str]:
        self._load_existing()
        return self._existing_names

    def existing_hashes(self) -> set[str]:
        self._load_existing()
        return self._existing_hashes

    def has(self, title: str, infohash: str | None = None) -> bool:
        if infohash and infohash in self.existing_hashes():
            return True
        return _normalize_torrent_name(title) in self.existing_names()

    def add(self, torrent_url: str, save_path: str, title: str, dry_run: bool,
            infohash: str | None = None) -> bool:
        if dry_run:
            print(f"    [dry-run] Would add: {title}")
            print(f"              -> {save_path}")
            return True

        # Hash-first dedup: catches queued/downloading/completed torrents regardless of name
        if self.has(title, infohash):
            print(f"    Already in qBittorrent, skipping.")
            return False

        client = self._client_or_none()
        if client is None:
            return False

        try:
            client.torrents_add(urls=torrent_url, save_path=save_path, is_paused=False)
            # Track locally so subsequent duplicates in the same run are also caught
            if infohash:
                self.existing_hashes().add(infohash)
            self.existing_names().add(_normalize_torrent_name(title))
            print(f"    Added -> {save_path}")
            return True
        except Exception as e:
            print(f"    [error] Failed to add torrent: {e}")
            return False

    def delete(self, infohashes: list[str]) -> int:
        """Remove torrents by infohash from qBittorrent (does NOT delete files on disk).
        Returns the number successfully removed."""
        if not infohashes:
            return 0
        client = self._client_or_none()
        if not client:
            return 0
        try:
            client.torrents_delete(delete_files=False, torrent_hashes=infohashes)
            return len(infohashes)
        except Exception as e:
            print(f"  [error] qBittorrent delete failed: {e}")
            return 0

    def hashes_under(self, path_prefix: str) -> list[str]:
        """Hashes of torrents whose save_path starts with path_prefix
        (case-insensitive). Fresh fetch, not the cached dedup snapshot, so it
        sees torrents added after this instance was created."""
        client = self._client_or_none()
        if not client:
            return []
        try:
            prefix = path_prefix.lower()
            return [
                t.hash for t in client.torrents_info()
                if t.save_path and t.save_path.lower().startswith(prefix)
            ]
        except Exception as e:
            print(f"  [warn] qBittorrent error: {e}")
            return []

    def torrents_under(self, path_prefix: str) -> list[dict]:
        """Live status of torrents saved under path_prefix, serialized to plain
        dicts (for the web UI's download monitor). [] when unreachable."""
        client = self._client_or_none()
        if not client:
            return []
        try:
            prefix = path_prefix.lower()
            return [
                {
                    "name": t.name,
                    "hash": t.hash,
                    "state": t.state,
                    "progress": t.progress,
                    "dlspeed": t.dlspeed,
                    "eta": t.eta,
                    "save_path": t.save_path,
                }
                for t in client.torrents_info()
                if t.save_path and t.save_path.lower().startswith(prefix)
            ]
        except Exception:
            return []


# ============================================================
# MAIN
# ============================================================

def format_ep_range(episodes: set[int]) -> str:
    if not episodes:
        return "(none)"
    sorted_eps = sorted(episodes)
    ranges = []
    start = end = sorted_eps[0]
    for ep in sorted_eps[1:]:
        if ep == end + 1:
            end = ep
        else:
            ranges.append(f"{start}-{end}" if start != end else str(start))
            start = end = ep
    ranges.append(f"{start}-{end}" if start != end else str(start))
    return ", ".join(ranges)


def classify_episodes(pools: list[tuple[str, list[Torrent]]], missing: list[int],
                      qbit: QBittorrent) -> tuple[list[int], list[tuple[int, Torrent]], list[int]]:
    """Classify each missing episode against the resolution-ladder pools:
    already in qBittorrent, ready to download (with its picked Torrent), or no source."""
    in_qbit: list[int] = []
    to_download: list[tuple[int, Torrent]] = []
    no_source: list[int] = []
    for ep_num in missing:
        best = pick_episode_ladder(pools, ep_num) if pools else None
        if not best:
            no_source.append(ep_num)
        elif qbit.has(best.title, best.infohash):
            in_qbit.append(ep_num)
        else:
            to_download.append((ep_num, best))
    return in_qbit, to_download, no_source


def prepare_sync(qbit: QBittorrent, series_filter: str | None = None,
                 progress=None) -> dict:
    """All the compute behind a sync, no console output: scan the library, look
    up AniList concurrently, merge duplicate folders, find gaps, search Nyaa
    concurrently, and classify every missing episode. Returns a plan dict the
    caller presents (CLI) or serializes (web):

      {"series_count", "season_count", "seasons": [
        {"folder_name", "season_num", "path": Path, "present": set[int], "style",
         "duplicate_of", "info", "season_status", "missing": [int],
         "in_qbit": [int], "to_download": [(int, Torrent)], "no_source": [int]}]}

    progress, if given, is called as progress(event, payload) — from worker
    threads for the *_progress events, so it must be thread-safe and must not
    print (the workers-don't-print rule).
    Events: scan_start, scan_done {series, seasons}, anilist_progress
    {done, total}, nyaa_progress {done, total}, classify_done.
    """
    def emit(event: str, payload: dict | None = None):
        if progress:
            progress(event, payload or {})

    emit("scan_start", {"library_path": LIBRARY_PATH})
    series_list = scan_library(LIBRARY_PATH, series_filter=series_filter)
    total_seasons = sum(len(s["seasons"]) for s in series_list)
    emit("scan_done", {"series": len(series_list), "seasons": total_seasons})

    cache = load_cache()

    # Flatten into a list of (series_name, season_num, season_data) units of work.
    units = [
        (series["folder_name"], season_num, season_data)
        for series in series_list
        for season_num, season_data in sorted(series["seasons"].items())
    ]

    # --- Phase 1: AniList lookups, all in parallel ---
    # Workers only read from cache; they return results without writing.
    # Cache is updated sequentially below to avoid data races.
    counter_lock = threading.Lock()
    counters = {"anilist": 0, "nyaa": 0}

    def fetch_info(unit):
        fname, season_num, season_data = unit
        result, base_id = anilist_lookup(fname, season_num, cache)
        with counter_lock:
            counters["anilist"] += 1
            done = counters["anilist"]
        emit("anilist_progress", {"done": done, "total": len(units)})
        return fname, season_num, season_data, result, base_id

    with ThreadPoolExecutor(max_workers=ANILIST_THREAD_WORKERS) as pool:
        lookup_results = list(pool.map(fetch_info, units))

    # Merge AniList results into cache sequentially (no concurrent writes).
    for fname, season_num, season_data, result, base_id in lookup_results:
        season_data["info"] = result
        if result is not None and base_id is not None:
            if fname not in cache["series"]:
                cache["series"][fname] = {"anilist_id": base_id, "seasons": {}}
            if "seasons" not in cache["series"][fname]:
                cache["series"][fname]["seasons"] = {}
            cache["series"][fname]["anilist_id"] = base_id
            cache["series"][fname]["seasons"][str(season_num)] = result

    # --- Phase 2a: detect duplicate folders for the same show (same AniList ID + season) ---
    # Two folders can be the same anime under different titles (e.g. English vs romaji).
    # Merge their present-episode sets into one canonical folder so the others don't
    # produce false "Missing" lines or double downloads.
    groups: dict[tuple, list] = {}
    for unit in units:
        fname, season_num, season_data = unit
        info = season_data.get("info")
        if not info:
            continue
        groups.setdefault((info["anilist_id"], season_num), []).append(unit)

    for key, group in groups.items():
        if len(group) < 2:
            continue
        # Canonical = most episodes present (tiebreak: alphabetical folder name)
        canonical = max(group, key=lambda u: (len(u[2]["episodes"]), u[0]))
        canonical_name, _, canonical_data = canonical
        merged = set()
        for _, _, sd in group:
            merged |= sd["episodes"]
        canonical_data["episodes"] = merged
        for fname, season_num, season_data in group:
            if season_data is not canonical_data:
                season_data["duplicate_of"] = canonical_name

    # --- Phase 2: compute missing episodes (local, instant) ---
    for fname, season_num, season_data in units:
        if season_data.get("duplicate_of"):
            season_data["missing"] = []
            continue
        info = season_data.get("info")
        season_data["missing"] = find_missing_episodes(season_data["episodes"], info) if info else []

    # --- Phase 3: one Nyaa search per series that has gaps, all in parallel ---
    gap_count = sum(1 for _, _, sd in units if sd.get("missing"))

    def fetch_nyaa(unit):
        fname, season_num, season_data = unit
        if not season_data.get("missing"):
            return
        info = season_data["info"]
        # Ladder search: one request at the preferred resolution; lower rungs are
        # only fetched for episodes that had no trusted match above them.
        season_data["nyaa_pools"] = search_nyaa_ladder(
            fname, info.get("romaji_title"), season_num, season_data["style"],
            season_data["missing"],
        )
        # Batch/box fallback: only when the per-episode ladder found NOTHING for
        # any missing episode AND no episodes are on disk yet (a fresh/empty
        # finished show with no SubsPlease release, e.g. an older title). Never
        # runs for the common SubsPlease case -> no extra request there. No qbit
        # access here: the dedup decision stays in the sequential classify phase.
        pools = season_data["nyaa_pools"]
        if not season_data["episodes"] and not any(
                pick_episode_ladder(pools, ep) for ep in season_data["missing"]):
            season_data["batch_torrent"] = search_nyaa_batch_ladder(
                info.get("romaji_title") or fname, info.get("english_title"),
                info.get("format"),
            )
        with counter_lock:
            counters["nyaa"] += 1
            done = counters["nyaa"]
        emit("nyaa_progress", {"done": done, "total": gap_count})

    with ThreadPoolExecutor(max_workers=NYAA_THREAD_WORKERS) as pool:
        pool.map(fetch_nyaa, units)

    # --- Phase 4: classify each missing episode (sequential; first qbit.has()
    # call lazily loads the existing-torrent snapshot) ---
    seasons_out: list[dict] = []
    for fname, season_num, season_data in units:
        info = season_data.get("info")
        missing = season_data.get("missing", [])
        entry = {
            "folder_name": fname,
            "season_num": season_num,
            "path": season_data["path"],
            "present": season_data["episodes"],
            "style": season_data["style"],
            "duplicate_of": season_data.get("duplicate_of"),
            "info": info,
            "season_status": format_season_status(info) if info else None,
            "missing": missing,
            "in_qbit": [],
            "to_download": [],
            "no_source": [],
            "batch": None,          # whole-season box Torrent, if used
            "batch_in_qbit": False, # box already present in qBittorrent
        }
        if not entry["duplicate_of"] and info and missing:
            # Pools arrive already prefiltered (season/resolution/trusted) per rung.
            entry["in_qbit"], entry["to_download"], entry["no_source"] = \
                classify_episodes(season_data.get("nyaa_pools", []), missing, qbit)
        # Batch/box fallback: one trusted box covers the whole (empty) season.
        batch = season_data.get("batch_torrent")
        if batch:
            entry["no_source"] = []  # the box supplies the season
            if qbit.has(batch.title, batch.infohash):
                entry["batch_in_qbit"] = True
            else:
                entry["batch"] = batch
        seasons_out.append(entry)

    save_cache(cache)
    emit("classify_done")

    return {
        "series_count": len(series_list),
        "season_count": total_seasons,
        "seasons": seasons_out,
    }


def run_sync(qbit: QBittorrent, dry_run: bool = False, series_filter: str | None = None) -> set[str]:
    """Scan the library, find missing episodes, and add them to qBittorrent.
    Returns the set of infohashes that were added (empty on dry-run or no new episodes).
    Thin presenter over prepare_sync(): all compute happens there; this prints
    the results sequentially and performs the actual qbit.add calls."""
    def cli_progress(event, payload):
        # Only the two scan events print; *_progress events fire from worker
        # threads and printing there would interleave output.
        if event == "scan_start":
            print(f"Scanning library at {payload['library_path']}...")
        elif event == "scan_done":
            print(f"Found {payload['series']} series, {payload['seasons']} seasons.\n")

    plan = prepare_sync(qbit, series_filter=series_filter, progress=cli_progress)

    errors: list[str] = []
    total_missing = 0
    total_added = 0
    added_hashes: set[str] = set()

    for season in plan["seasons"]:
        fname = season["folder_name"]
        season_num = season["season_num"]
        present = season["present"]

        print(f"=== {fname} (Season {season_num}) ===")

        # Duplicate folder for a show already handled under another name.
        dup = season["duplicate_of"]
        if dup:
            print(f"  [duplicate of '{dup}' - same AniList ID, merged]\n")
            continue

        print(f"  Present: {format_ep_range(present) if present else '(none)'}")

        info = season["info"]
        if not info:
            errors.append(f"Could not look up '{fname}' S{season_num} on AniList.")
            print(f"  [skip] AniList lookup failed.\n")
            continue

        status_label = info.get("status", "?")
        total_eps = info.get("total_episodes")
        next_ep   = info.get("next_airing_episode")
        al_id     = info.get("anilist_id", "?")

        ep_label = str(total_eps) if total_eps else "?"
        aired_label = f"{next_ep - 1}" if next_ep else ep_label
        print(f"  AniList ID: {al_id} | Status: {status_label} | "
              f"Aired so far: {aired_label}/{ep_label}")
        print(f"  Season status: {season['season_status']}")

        if not season["missing"]:
            print(f"  Up to date.\n")
            continue

        save_path = str(season["path"])
        in_qbit, to_download, no_source = \
            season["in_qbit"], season["to_download"], season["no_source"]

        # Only genuinely-absent episodes count as "Missing".
        actionable = [ep for ep, _ in to_download] + no_source
        if actionable:
            print(f"  Missing: {', '.join(str(e) for e in actionable)}")
            total_missing += len(actionable)
        if in_qbit:
            print(f"  In qBittorrent (not yet in this folder): {format_ep_range(set(in_qbit))}")

        for ep_num in no_source:
            print(f"  Ep {ep_num:02d}: No trusted source on Nyaa (any resolution).")
            errors.append(f"No trusted source: {fname} S{season_num}E{ep_num:02d}")

        for ep_num, best in to_download:
            reso_note = "" if best.resolution == RESOLUTION else f" | {best.resolution or '?'} fallback"
            print(f"  Ep {ep_num:02d}: {best.title[:70]} | Seeds: {best.seeders} | trusted{reso_note}")
            url = build_magnet(best.infohash, best.title) if best.infohash else best.link
            ok = qbit.add(url, save_path, best.title, dry_run, infohash=best.infohash)
            if ok and not dry_run:
                total_added += 1
                if best.infohash:
                    added_hashes.add(best.infohash)

        # Batch/box fallback: add the single whole-season box, if one was chosen.
        batch = season.get("batch")
        if batch:
            url = build_magnet(batch.infohash, batch.title) if batch.infohash else batch.link
            ok = qbit.add(url, save_path, batch.title, dry_run, infohash=batch.infohash)
            verb = "would add" if dry_run else ("added" if ok else "FAILED")
            print(f"  [box] {verb}: {batch.title[:70]} ({batch.resolution})")
            if ok and not dry_run:
                total_added += 1
                if batch.infohash:
                    added_hashes.add(batch.infohash)
        elif season.get("batch_in_qbit"):
            print("  [box] already in qBittorrent")

        print()

    # Summary
    print("=" * 50)
    print(f"Series scanned:   {plan['series_count']}")
    print(f"Seasons checked:  {plan['season_count']}")
    print(f"Missing episodes: {total_missing}")
    if dry_run:
        print(f"Mode:             DRY RUN (nothing downloaded)")
    else:
        print(f"Torrents opened:  {total_added}")
    if errors:
        print(f"Warnings ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")

    return added_hashes


# ============================================================
# MONITOR
# ============================================================

def monitor_downloads(qbit: QBittorrent, added_hashes: set[str], poll_secs: int = 5):
    """Block until all torrents in added_hashes are done (or error/stalled).
    Prints a live one-line status that refreshes in place."""
    if not added_hashes:
        return
    client = qbit._client_or_none()
    if not client:
        print("  [monitor] qBittorrent not connected — cannot watch.")
        return

    # States where a torrent is no longer actively downloading
    done_states = {
        "uploading", "stalledUP", "pausedUP", "forcedUP", "checkingUP",
        "missingFiles", "error",
    }

    print(f"\nWatching {len(added_hashes)} torrent(s)... (Ctrl+C to stop)\n")
    try:
        while True:
            try:
                all_torrents = {t.hash.lower(): t for t in client.torrents_info() if t.hash}
            except Exception as e:
                print(f"\n  [monitor] Error fetching status: {e}")
                break

            active = [
                all_torrents[h] for h in added_hashes
                if h in all_torrents and all_torrents[h].state not in done_states
            ]

            if not active:
                print("  All downloads complete!                    ")
                break

            total_speed = sum(t.dlspeed for t in active) / 1024 / 1024  # MB/s
            names = ", ".join(
                re.sub(r'^\[SubsPlease\]\s*', '', t.name).split(" (")[0]
                for t in active[:3]
            )
            suffix = f" +{len(active)-3} more" if len(active) > 3 else ""
            print(f"  [{len(active)} left | {total_speed:.1f} MB/s] {names}{suffix}        ", end="\r", flush=True)
            time.sleep(poll_secs)
    except KeyboardInterrupt:
        print("\n  Stopped watching.")
    print()


# ============================================================
# DELETE ANIME
# ============================================================

def delete_anime_interactive(qbit: QBittorrent):
    """List library series, let the user pick one, remove its torrents from
    qBittorrent (without deleting files via qBit), then wipe the folder from disk."""
    root = Path(LIBRARY_PATH)
    series_dirs = sorted([
        d for d in root.iterdir()
        if d.is_dir()
        and d.name not in IGNORE_DIRS
        and not d.name.startswith(".")
    ])

    if not series_dirs:
        print("No series found in library.")
        return

    print("\nLibrary:")
    for i, d in enumerate(series_dirs, 1):
        print(f"  [{i:2d}] {d.name}")

    choice = _prompt("\nPick a number to delete (blank to cancel): ")
    if not choice.isdigit() or not (1 <= int(choice) <= len(series_dirs)):
        print("Cancelled.")
        return

    target = series_dirs[int(choice) - 1]
    print(f"\n  Will delete: {target}")
    confirm = _prompt("Remove from qBittorrent AND delete from disk? [y/N]: ")
    if confirm.lower() != "y":
        print("Cancelled.")
        return

    # Find matching torrents in qBittorrent by save_path prefix
    if qbit._client_or_none():
        hashes_to_remove = qbit.hashes_under(str(target))
        if hashes_to_remove:
            removed_count = qbit.delete(hashes_to_remove)
            print(f"  Removed {removed_count} torrent(s) from qBittorrent.")
        else:
            print("  No matching torrents found in qBittorrent.")

    # Delete the folder from disk
    try:
        shutil.rmtree(target)
        print(f"  Deleted folder: {target}")
    except Exception as e:
        print(f"  [error] Could not delete folder: {e}")


# ============================================================
# ADD-ANIME INTERACTIVE FLOW
# ============================================================

def _prompt(msg: str) -> str:
    """input() that treats Ctrl+C / EOF as an empty (cancel) response."""
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _anilist_search_candidates(term: str) -> list[Candidate]:
    data = anilist_request(ANILIST_SEARCH_QUERY, {"search": term})
    if not data:
        return []
    out = []
    for m in data["data"]["Page"]["media"]:
        t = m.get("title", {})
        out.append(Candidate(
            anilist_id=m["id"],
            romaji=t.get("romaji") or t.get("english") or f"id {m['id']}",
            english=t.get("english"),
            year=m.get("seasonYear"),
            format=m.get("format"),
            episodes=m.get("episodes"),
            status=m.get("status"),
            raw_media=m,
        ))
    return out


def add_anime_interactive(qbit: QBittorrent):
    """Search AniList, let the user pick a show + season, verify a trusted 1080p
    source exists on Nyaa, then create the folder and download via run_sync()."""
    term = _prompt("\nSearch for anime (blank to cancel): ")
    if not term:
        print("Cancelled.")
        return

    print("Searching AniList...")
    candidates = _anilist_search_candidates(term)
    if not candidates:
        print("No matches found.")
        return

    print("\nClosest matches:")
    for i, c in enumerate(candidates, 1):
        eng = f" ({c.english})" if c.english and c.english != c.romaji else ""
        bits = [b for b in [c.format, str(c.year) if c.year else None,
                            f"{c.episodes} eps" if c.episodes else None, c.status] if b]
        print(f"  [{i}] {c.romaji}{eng} - {' | '.join(bits)}")

    choice = _prompt("Pick a number (blank to cancel): ")
    if not choice.isdigit() or not (1 <= int(choice) <= len(candidates)):
        print("Cancelled.")
        return
    chosen = candidates[int(choice) - 1]

    # Enumerate seasons via the SEQUEL chain.
    print(f"\nLooking up seasons of '{chosen.romaji}'...")
    seasons = enumerate_seasons(chosen.raw_media)
    if not seasons:
        print("Could not determine seasons.")
        return

    print("\nSeasons:")
    for s in seasons:
        aired = f"{s.next_airing - 1} aired" if s.next_airing else (f"{s.episodes} eps" if s.episodes else "?")
        print(f"  [{s.season_num}] Season {s.season_num} - {s.title} - {s.status or '?'} - {aired}")

    sel = _prompt("Pick a season number (blank to cancel): ")
    if not sel.isdigit() or int(sel) not in {s.season_num for s in seasons}:
        print("Cancelled.")
        return
    season = next(s for s in seasons if s.season_num == int(sel))

    # Strict Nyaa availability check BEFORE creating anything.
    print(f"\nChecking Nyaa for a trusted source (1080p preferred)...")
    available = check_availability(chosen.romaji, season)
    box = None
    if not available:
        box = check_batch_availability(season)
    if not available and not box:
        print("  X  Not available from a trusted source on Nyaa at any resolution. Nothing created.")
        return
    if available:
        print(f"  OK - {len(available)} trusted episode(s) available: {format_availability(available)}")
    else:
        print(f"  OK - trusted complete box available: {box.title} ({box.resolution})")

    folder_name = safe_folder_name(chosen.romaji)
    folder = Path(LIBRARY_PATH) / folder_name / f"Season {season.season_num:02d}"
    confirm = _prompt(f"\nCreate '{folder}' and download now? [y/N]: ")
    if confirm.lower() != "y":
        print("Cancelled.")
        return

    folder.mkdir(parents=True, exist_ok=True)
    print(f"Created {folder}\n")

    # Reuse the fast sync pipeline scoped to just this new series. Filter by the
    # sanitized folder name — for titles like 'Re:Zero...' the raw romaji would
    # not match the on-disk folder.
    run_sync(qbit, dry_run=False, series_filter=folder_name)


# ============================================================
# MENU + ENTRY POINT
# ============================================================

def interactive_menu(qbit: QBittorrent):
    while True:
        print("\n" + "=" * 30)
        print("  Anime Downloader")
        print("=" * 30)
        print("  [1] Sync library (download new episodes)")
        print("  [2] Add a new anime")
        print("  [3] Delete an anime")
        print("  [Q] Quit")
        choice = _prompt("Choose: ").lower()

        if choice == "1":
            added = run_sync(qbit, dry_run=False)
            if added:
                watch = _prompt("Watch downloads until complete? [y/N]: ")
                if watch.lower() == "y":
                    monitor_downloads(qbit, added)
        elif choice == "2":
            add_anime_interactive(qbit)
        elif choice == "3":
            delete_anime_interactive(qbit)
        elif choice in ("q", ""):
            print("Bye!")
            return
        else:
            print("Invalid choice.")


def main():
    parser = argparse.ArgumentParser(description="Anime episode auto-downloader")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be downloaded without actually downloading")
    parser.add_argument("--series", metavar="NAME",
                        help="Only process series whose name contains NAME")
    parser.add_argument("--sync", action="store_true",
                        help="Run the library sync directly, skipping the menu")
    args = parser.parse_args()

    qbit = QBittorrent(QBIT_HOST, QBIT_PORT, QBIT_USERNAME, QBIT_PASSWORD)

    # Flags imply non-interactive sync (for the bat power user / scripting).
    if args.dry_run or args.series or args.sync:
        run_sync(qbit, dry_run=args.dry_run, series_filter=args.series)
    else:
        interactive_menu(qbit)


if __name__ == "__main__":
    main()
