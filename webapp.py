"""
webapp.py

Local web UI for the anime library. Serves a single-page app from .webui/ and
exposes the sync/add/delete flows of anime_downloader.py as a JSON API.

    .venv\\Scripts\\python.exe webapp.py        # or double-click "Anime Web.bat"

Design notes:
- One sync job at a time, run in a background thread; the frontend polls
  /api/sync/status. Job state lives server-side, so a page reload reattaches.
- A fresh QBittorrent instance per sync job (its dedup snapshot is meant to be
  once-per-run, matching the CLI); a long-lived instance for monitor/delete.
- Folder names always travel in JSON bodies, never URL path segments — they
  contain spaces, brackets, and unicode.
- debug=False always: the Flask reloader would fork a second process and
  duplicate the job state.
"""

import os
import shutil
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, request

import anime_downloader as core

BASE = Path(__file__).parent
# ANIME_WEB_PORT wins; PORT supports launchers that assign one (e.g. previews).
WEB_PORT = int(os.environ.get("ANIME_WEB_PORT")
               or os.environ.get("PORT")
               or "8765")

# States where a torrent is no longer actively downloading (same set the CLI
# monitor uses).
DONE_STATES = {
    "uploading", "stalledUP", "pausedUP", "forcedUP", "checkingUP",
    "missingFiles", "error",
}

app = Flask(__name__, static_folder=str(BASE / ".webui"), static_url_path="/ui")


# ============================================================
# QBITTORRENT (long-lived, self-healing)
# ============================================================

QB_LOCK = threading.Lock()
_qb: core.QBittorrent | None = None


def get_qb() -> core.QBittorrent:
    """Long-lived qBittorrent wrapper for monitor/delete endpoints. QBittorrent
    latches a failed connection permanently, so replace the instance after a
    failure — the UI then recovers as soon as qBittorrent is started."""
    global _qb
    with QB_LOCK:
        if _qb is None or _qb._connect_failed:
            _qb = core.QBittorrent(core.QBIT_HOST, core.QBIT_PORT,
                                   core.QBIT_USERNAME, core.QBIT_PASSWORD)
        return _qb


# ============================================================
# SYNC JOB MACHINERY
# ============================================================

SYNC_LOCK = threading.Lock()       # held for the whole duration of a sync job
JOB_STATE_LOCK = threading.Lock()  # guards reads/writes of SYNC_JOB fields

SYNC_JOB = {
    "id": 0,
    "state": "idle",   # idle | running | done | error
    "phase": "",
    "dry_run": False,
    "series": None,
    "events": deque(maxlen=300),
    "result": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
}


def _append_event(event: str, payload: dict):
    """progress callback for prepare_sync — called from worker threads, so it
    only touches state under the lock and never prints."""
    with JOB_STATE_LOCK:
        SYNC_JOB["events"].append({"event": event, **payload})
        if event == "scan_start":
            SYNC_JOB["phase"] = "Scanning library..."
        elif event == "scan_done":
            SYNC_JOB["phase"] = (f"Found {payload['series']} series, "
                                 f"{payload['seasons']} seasons")
        elif event == "anilist_progress":
            SYNC_JOB["phase"] = f"Looking up AniList ({payload['done']}/{payload['total']})"
        elif event == "nyaa_progress":
            SYNC_JOB["phase"] = f"Searching Nyaa ({payload['done']}/{payload['total']})"
        elif event == "classify_done":
            SYNC_JOB["phase"] = "Adding torrents..."


def _run_sync_job(dry_run: bool, series_filter: str | None):
    """Job body: prepare_sync (all compute) then the add phase. Runs in a
    background thread; SYNC_LOCK is held by the caller and released here."""
    try:
        # Fresh instance per run: dedup snapshot semantics match the CLI.
        qbit = core.QBittorrent(core.QBIT_HOST, core.QBIT_PORT,
                                core.QBIT_USERNAME, core.QBIT_PASSWORD)
        plan = core.prepare_sync(qbit, series_filter=series_filter,
                                 progress=_append_event)

        seasons_out = []
        warnings = []
        total_missing = 0
        total_added = 0
        added_hashes = []

        for season in plan["seasons"]:
            fname = season["folder_name"]
            snum = season["season_num"]
            info = season["info"]

            if not season["duplicate_of"] and info is None:
                warnings.append(f"Could not look up '{fname}' S{snum} on AniList.")
            for ep in season["no_source"]:
                warnings.append(f"No trusted source: {fname} S{snum}E{ep:02d}")
            total_missing += len(season["no_source"]) + len(season["to_download"])

            added = []
            for ep_num, t in season["to_download"]:
                url = (core.build_magnet(t.infohash, t.title)
                       if t.infohash else t.link)
                ok = qbit.add(url, str(season["path"]), t.title, dry_run,
                              infohash=t.infohash)
                added.append({"episode": ep_num, "title": t.title,
                              "seeders": t.seeders, "ok": ok,
                              "resolution": t.resolution})
                if ok and not dry_run:
                    total_added += 1
                    if t.infohash:
                        added_hashes.append(t.infohash)
                _append_event("torrent_added", {
                    "folder": fname, "episode": ep_num,
                    "title": t.title, "dry_run": dry_run, "ok": ok,
                    "resolution": t.resolution,
                })

            batch = season.get("batch")
            if batch:
                url = (core.build_magnet(batch.infohash, batch.title)
                       if batch.infohash else batch.link)
                ok = qbit.add(url, str(season["path"]), batch.title, dry_run,
                              infohash=batch.infohash)
                added.append({"episode": None, "title": batch.title,
                              "seeders": batch.seeders, "ok": ok,
                              "resolution": batch.resolution, "batch": True})
                if ok and not dry_run:
                    total_added += 1
                    if batch.infohash:
                        added_hashes.append(batch.infohash)
                _append_event("torrent_added", {
                    "folder": fname, "episode": None, "title": batch.title,
                    "dry_run": dry_run, "ok": ok,
                    "resolution": batch.resolution, "batch": True,
                })

            seasons_out.append({
                "folder_name": fname,
                "season_num": snum,
                "duplicate_of": season["duplicate_of"],
                "season_status": season["season_status"],
                "lookup_failed": info is None and not season["duplicate_of"],
                "present": sorted(season["present"]),
                "missing": season["missing"],
                "in_qbit": season["in_qbit"],
                "no_source": season["no_source"],
                "added": added,
                "cover_image": ((info or {}).get("cover_image_xl")
                                or (info or {}).get("cover_image")),
                "batch": ({"title": season["batch"].title,
                           "resolution": season["batch"].resolution}
                          if season.get("batch") else None),
                "batch_in_qbit": season.get("batch_in_qbit", False),
            })

        with JOB_STATE_LOCK:
            SYNC_JOB["result"] = {
                "series_count": plan["series_count"],
                "season_count": plan["season_count"],
                "total_missing": total_missing,
                "total_added": total_added,
                "dry_run": dry_run,
                "warnings": warnings,
                "added_hashes": added_hashes,
                "seasons": seasons_out,
            }
            SYNC_JOB["state"] = "done"
            SYNC_JOB["phase"] = "Done"
            SYNC_JOB["finished_at"] = time.time()
    except Exception as e:  # surface anything unexpected to the UI
        with JOB_STATE_LOCK:
            SYNC_JOB["state"] = "error"
            SYNC_JOB["error"] = f"{type(e).__name__}: {e}"
            SYNC_JOB["finished_at"] = time.time()
    finally:
        SYNC_LOCK.release()


def _try_start_job(dry_run: bool, series: str | None) -> int | None:
    """Start a sync job unless one is running. Returns the job id, or None if busy."""
    if not SYNC_LOCK.acquire(blocking=False):
        return None
    with JOB_STATE_LOCK:
        SYNC_JOB["id"] += 1
        job_id = SYNC_JOB["id"]
        SYNC_JOB["state"] = "running"
        SYNC_JOB["phase"] = "Starting..."
        SYNC_JOB["dry_run"] = dry_run
        SYNC_JOB["series"] = series
        SYNC_JOB["events"].clear()
        SYNC_JOB["result"] = None
        SYNC_JOB["error"] = None
        SYNC_JOB["started_at"] = time.time()
        SYNC_JOB["finished_at"] = None
    threading.Thread(target=_run_sync_job, args=(dry_run, series),
                     daemon=True).start()
    return job_id


# ============================================================
# ROUTES — PAGE + LIBRARY
# ============================================================

@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/library")
def api_library():
    """Card-grid data: filesystem scan + cached AniList info only — never blocks
    on the network, so the page renders instantly even with a cold cache."""
    series_list = core.scan_library(core.LIBRARY_PATH)
    cache = core.load_cache()
    qb = get_qb()
    connected = qb._client_or_none() is not None
    active_torrents = []
    if connected:
        active_torrents = [
            t for t in qb.torrents_under(str(core.LIBRARY_PATH))
            if t["state"] not in DONE_STATES and t["progress"] < 1.0
        ]

    out = []
    for series in series_list:
        fname = series["folder_name"]
        series_cache = cache["series"].get(fname, {}).get("seasons", {})
        seasons = {}
        for snum, sdata in sorted(series["seasons"].items()):
            info = series_cache.get(str(snum))
            season_path = str(sdata["path"]).lower()
            downloading = sorted({
                ep for t in active_torrents
                if t["save_path"] and t["save_path"].lower().startswith(season_path)
                for ep in [core.parse_episode_number(t["name"])]
                if ep is not None
            })
            seasons[snum] = {
                "path": str(sdata["path"]),
                "episodes": sorted(sdata["episodes"]),
                "style": sdata["style"],
                "missing": (core.find_missing_episodes(sdata["episodes"], info)
                            if info else []),
                "downloading": downloading,
                "season_status": core.format_season_status(info) if info else None,
                "info": info,
            }
        out.append({"folder_name": fname, "seasons": seasons})

    return jsonify({
        "series": out,
        "qbit_connected": connected,
        "library_path": core.LIBRARY_PATH,
    })


# ============================================================
# ROUTES — SYNC
# ============================================================

@app.post("/api/sync")
def api_sync():
    body = request.get_json(silent=True) or {}
    job_id = _try_start_job(bool(body.get("dry_run")), body.get("series") or None)
    if job_id is None:
        return jsonify({"error": "A sync is already running."}), 409
    return jsonify({"job_id": job_id}), 202


@app.get("/api/sync/status")
def api_sync_status():
    with JOB_STATE_LOCK:
        return jsonify({
            "job_id": SYNC_JOB["id"],
            "state": SYNC_JOB["state"],
            "phase": SYNC_JOB["phase"],
            "dry_run": SYNC_JOB["dry_run"],
            "series": SYNC_JOB["series"],
            "events": list(SYNC_JOB["events"]),
            "result": SYNC_JOB["result"],
            "error": SYNC_JOB["error"],
        })


# ============================================================
# ROUTES — DOWNLOAD MONITOR
# ============================================================

@app.get("/api/downloads")
def api_downloads():
    qb = get_qb()
    connected = qb._client_or_none() is not None
    torrents = qb.torrents_under(str(core.LIBRARY_PATH)) if connected else []
    for t in torrents:
        t["done"] = t["state"] in DONE_STATES or t["progress"] >= 1.0
    return jsonify({"connected": connected, "torrents": torrents})


# ============================================================
# ROUTES — ADD ANIME
# ============================================================

@app.post("/api/anilist/search")
def api_anilist_search():
    body = request.get_json(silent=True) or {}
    term = (body.get("term") or "").strip()
    if not term:
        return jsonify({"candidates": []})
    candidates = core._anilist_search_candidates(term)
    out = []
    for c in candidates:
        cover = c.raw_media.get("coverImage") or {}
        out.append({
            "anilist_id": c.anilist_id,
            "romaji": c.romaji,
            "english": c.english,
            "year": c.year,
            "format": c.format,
            "episodes": c.episodes,
            "status": c.status,
            "cover_image": cover.get("large"),
            "cover_image_xl": cover.get("extraLarge"),
            "cover_color": cover.get("color"),
            "genres": c.raw_media.get("genres") or [],
            "average_score": c.raw_media.get("averageScore"),
            "description": core._clean_description(c.raw_media.get("description")),
        })
    return jsonify({"candidates": out})


@app.post("/api/add/seasons")
def api_add_seasons():
    """Walk the SEQUEL chain for a chosen show. Stateless: re-fetches the media
    by id rather than holding raw_media between requests."""
    body = request.get_json(silent=True) or {}
    try:
        anilist_id = int(body.get("anilist_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "anilist_id required"}), 400
    data = core.anilist_request(core.ANILIST_ID_QUERY, {"id": anilist_id})
    if not data:
        return jsonify({"error": "AniList lookup failed."}), 502
    seasons = core.enumerate_seasons(data["data"]["Media"])
    return jsonify({"seasons": [{
        "season_num": s.season_num,
        "anilist_id": s.anilist_id,
        "title": s.title,
        "english": s.english,
        "episodes": s.episodes,
        "status": s.status,
        "next_airing": s.next_airing,
        "format": s.format,
    } for s in seasons]})


@app.post("/api/add/check")
def api_add_check():
    """Strict trusted-1080p availability check — same policy as the CLI, via
    the shared check_availability()."""
    body = request.get_json(silent=True) or {}
    romaji = (body.get("romaji") or "").strip()
    try:
        season_num = int(body.get("season_num"))
    except (TypeError, ValueError):
        return jsonify({"error": "season_num required"}), 400
    if not romaji:
        return jsonify({"error": "romaji required"}), 400
    season = core.SeasonOption(
        season_num=season_num,
        anilist_id=int(body.get("anilist_id") or 0),
        title=body.get("title") or romaji,
        episodes=body.get("episodes"),
        status=body.get("status"),
        next_airing=body.get("next_airing"),
        format=body.get("format"),
        english=(body.get("english") or "").strip() or None,
    )
    avail = core.check_availability(romaji, season)  # {episode: resolution}
    by_resolution: dict[str, list[int]] = {}
    for ep, reso in avail.items():
        by_resolution.setdefault(reso, []).append(ep)
    for eps in by_resolution.values():
        eps.sort()

    box = None
    if not avail:
        box = core.check_batch_availability(season)

    return jsonify({
        "ok": bool(avail) or box is not None,
        "mode": "episodes" if avail else ("batch" if box else "none"),
        "available": sorted(avail),
        "by_resolution": by_resolution,
        "available_label": core.format_availability(avail) if avail else None,
        "batch_title": box.title if box else None,
        "batch_resolution": box.resolution if box else None,
    })


@app.post("/api/add/confirm")
def api_add_confirm():
    """Create the series folder and start a targeted sync job for it."""
    body = request.get_json(silent=True) or {}
    romaji = (body.get("romaji") or "").strip()
    try:
        season_num = int(body.get("season_num"))
    except (TypeError, ValueError):
        return jsonify({"error": "season_num required"}), 400
    folder_name = core.safe_folder_name(romaji)
    if not folder_name:
        return jsonify({"error": "Invalid title."}), 400

    root = Path(core.LIBRARY_PATH).resolve()
    target = (root / folder_name).resolve()
    if target.parent != root:
        return jsonify({"error": "Invalid folder name."}), 400

    job_id = _try_start_job(False, folder_name)
    if job_id is None:
        return jsonify({"error": "A sync is already running."}), 409

    season_dir = target / f"Season {season_num:02d}"
    season_dir.mkdir(parents=True, exist_ok=True)
    return jsonify({"job_id": job_id, "folder": str(season_dir)}), 202


# ============================================================
# ROUTES — DELETE ANIME
# ============================================================

@app.post("/api/delete")
def api_delete():
    """Remove a series: torrents out of qBittorrent (files untouched by qBit),
    then rmtree the folder. Mutually exclusive with sync jobs — deleting a
    folder while a sync scans it would be bad."""
    body = request.get_json(silent=True) or {}
    folder_name = (body.get("folder_name") or "").strip()
    if body.get("confirm") is not True:
        return jsonify({"error": "Confirmation required."}), 400
    if not folder_name or folder_name.startswith(".") \
            or folder_name in core.IGNORE_DIRS:
        return jsonify({"error": "Invalid folder name."}), 400

    root = Path(core.LIBRARY_PATH).resolve()
    target = (root / folder_name).resolve()
    if target.parent != root or not target.is_dir():
        return jsonify({"error": "Not a series folder in the library."}), 400

    if not SYNC_LOCK.acquire(blocking=False):
        return jsonify({"error": "A sync is running; try again when it finishes."}), 409
    try:
        qb = get_qb()
        removed = qb.delete(qb.hashes_under(str(target)))
        shutil.rmtree(target)
        return jsonify({"torrents_removed": removed, "deleted": True})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    finally:
        SYNC_LOCK.release()


# ============================================================
# ENTRY POINT
# ============================================================

def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    url = f"http://127.0.0.1:{WEB_PORT}/"
    if _port_in_use(WEB_PORT):
        # Double-clicked the launcher while a server is already up (or the port
        # is taken by something else): don't crash with a bind error — just
        # point the browser at whatever is serving.
        print(f"Already running at {url} — opening browser instead of starting twice.")
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        return
    print(f"Anime library web UI -> {url}")
    print("Ctrl+C to stop (closing this window also stops the server).")
    if "--no-browser" not in sys.argv:
        # Fire after the server has had a moment to bind, so the page never 404s.
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host="127.0.0.1", port=WEB_PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
