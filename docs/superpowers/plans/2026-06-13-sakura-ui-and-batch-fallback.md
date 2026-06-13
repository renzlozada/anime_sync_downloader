# Sakura UI + Batch/Box Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the web UI with the Sakura editorial design wired to the live Flask API, and add an automatic trusted batch/box source fallback so boxed-only shows (e.g. Madoka) can be found and downloaded.

**Architecture:** Backend changes (Part A) live entirely in `anime_downloader.py` + `webapp.py`; the batch fallback hooks into `prepare_sync`'s existing parallel Nyaa phase so both library sync and the add-confirm flow get it. Frontend (Part B) ports `Anime Library Website/sakura/` into `.webui/`, replacing its mock `data.js`/`setTimeout` layer with the real JSON API. No new backend API endpoints.

**Tech Stack:** Python 3 (Flask, requests, rapidfuzz, qbittorrent-api), vanilla JS frontend, React/Babel via CDN for the tweaks panel only. Tests: lightweight assert scripts run with `.venv\Scripts\python.exe` (no pytest in the project).

**Source of truth for the port:** `Anime Library Website/sakura/{Anime Library - Sakura.html,app.js,sakura.css,tweaks-panel.jsx}` — read these while implementing Part B.

**Run backend tests with:** `.venv\Scripts\python.exe tests\test_batch_fallback.py` (prints `ALL PASS` / raises on failure).

---

## PART A — Batch/box source fallback (`anime_downloader.py`)

### Task 1: Title helpers — `looks_like_single_episode` + `_clean_torrent_title`

**Files:**
- Modify: `anime_downloader.py` — add constants near line 64–94 (config block) and the regexes near line 128 (regex block); add the two functions just above `prefilter_torrents` (line ~830).
- Test: `tests/test_batch_fallback.py` (create).

- [ ] **Step 1: Write the failing test**

Create `tests/test_batch_fallback.py`:

```python
"""Offline unit tests for the batch/box fallback. No network; uses canned Torrent
fixtures mirroring a real Madoka Nyaa search. Run:
    .venv\\Scripts\\python.exe tests\\test_batch_fallback.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import anime_downloader as core


def test_looks_like_single_episode():
    f = core.looks_like_single_episode
    assert f("[SubsPlease] Dandadan - 07 (1080p) [ABC123].mkv") is True
    assert f("Title - S01E07 - Name [HDTV-1080p].mkv") is True
    assert f("[MiniMTBB] Puella Magi Madoka Magica (BD 1080p)") is False  # the box
    assert f("[Group] Some Show 01-12 (BD 1080p) [Batch]") is False       # a range
    assert f("[Group] Some Show Complete Series (BD 1080p)") is False


def test_clean_torrent_title():
    c = core._clean_torrent_title
    assert c("[MiniMTBB] Puella Magi Madoka Magica (BD 1080p)") == "Puella Magi Madoka Magica"
    assert c("[LYS1TH3A] Solo Leveling Season 1 | Ore (BD 1080p HEVC)").startswith("Solo Leveling")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"ok  {name}")
    print("ALL PASS")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe tests\test_batch_fallback.py`
Expected: FAIL with `AttributeError: module 'anime_downloader' has no attribute 'looks_like_single_episode'`.

- [ ] **Step 3: Add constants (in the config block, after `MAX_PROBE_EPISODES` ~line 94)**

```python
# Batch/box fallback: when no per-episode trusted release exists for an empty
# season, fall back to a single trusted whole-season box from any group.
BATCH_FUZZY_THRESHOLD = 80  # min token_sort_ratio of a box title vs the AniList title
BATCH_EXCLUDE_KEYWORDS = ("movie", "gekijou", "gekijouban", "recap",
                          "side story", "specials")
```

- [ ] **Step 4: Add regexes (in the regex block, after `RE_RESOLUTION` ~line 131)**

```python
# Batch detection helpers.
RE_SINGLE_EP  = re.compile(r'(?:\s-\s\d{1,4}(?:v\d+)?(?=\s|\[|\()|[Ss]\d{1,2}[Ee]\d{1,3})')
RE_EP_RANGE   = re.compile(r'\b\d{1,4}\s*[-~]\s*\d{1,4}\b')
RE_BATCH_WORD = re.compile(r'\b(batch|complete|seasons?)\b', re.IGNORECASE)
```

- [ ] **Step 5: Add the two functions (just above `prefilter_torrents`, ~line 830)**

```python
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
    return re.sub(r'\s+', ' ', t).strip()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv\Scripts\python.exe tests\test_batch_fallback.py`
Expected: `ok  test_clean_torrent_title` / `ok  test_looks_like_single_episode` / `ALL PASS`.

---

### Task 2: `pick_batch_torrent`

**Files:**
- Modify: `anime_downloader.py` — add after `_clean_torrent_title`.
- Test: `tests/test_batch_fallback.py` — add a test.

- [ ] **Step 1: Add the failing test (append a function before the `__main__` block)**

```python
def _madoka_fixtures():
    """Canned results mirroring a real 'Madoka Magica 1080p' Nyaa search."""
    def T(title, trusted, seeders, res="1080p"):
        return core.Torrent(title=title, link="magnet:?x", seeders=seeders,
                            trusted=trusted, remake=False, resolution=res,
                            infohash="h" + str(seeders))
    return [
        T("[anime4life.] Puella Magi Madoka Magica LPCM BD_1080p Dual Audio", False, 22),
        T("Puella Magi Madoka Magica the Movie Part II Eternal (2012) [BD Remux 1080p]", False, 26),
        T("[ABi] Puella Magi Madoka Magica III - Rebellion (Dual Audio) [BluRay-1080p].mkv", False, 2),
        T("[MegukaMux] Magia Record Puella Magi Madoka Magica Side Story Season 1", False, 26),
        T("[LYS1TH3A] Puella Magi Madoka Magica the Movie Part III Rebellion (2013)", False, 82),
        T("[LYS1TH3A] Puella Magi Madoka Magica Season 1 (BD 1080p HEVC)", False, 101),
        T("[MiniMTBB] Puella Magi Madoka Magica the Movie: Rebellion (BD 1080p)", True, 33),
        T("[MiniMTBB] Puella Magi Madoka Magica (BD 1080p)", True, 49),  # the TV box
    ]


def test_pick_batch_torrent_madoka():
    box = core.pick_batch_torrent(
        _madoka_fixtures(),
        romaji="Mahou Shoujo Madoka Magica",
        english="Puella Magi Madoka Magica",
        season_format="TV", resolution="1080p")
    assert box is not None, "expected a box"
    assert box.title == "[MiniMTBB] Puella Magi Madoka Magica (BD 1080p)", box.title


def test_pick_batch_torrent_rejects_untrusted_only():
    only_untrusted = [t for t in _madoka_fixtures() if not t.trusted]
    assert core.pick_batch_torrent(only_untrusted, "Mahou Shoujo Madoka Magica",
                                   "Puella Magi Madoka Magica", "TV",
                                   resolution="1080p") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe tests\test_batch_fallback.py`
Expected: FAIL with `AttributeError: ... 'pick_batch_torrent'`.

- [ ] **Step 3: Implement `pick_batch_torrent` (after `_clean_torrent_title`)**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe tests\test_batch_fallback.py`
Expected: `ALL PASS` (4 tests).

- [ ] **Step 5: Commit** (skip if the project is not its own git repo — see plan note "Git")

---

### Task 3: Broad search + ladder + availability — `build_nyaa_batch_query`, `search_nyaa_batch_ladder`, `check_batch_availability`

**Files:**
- Modify: `anime_downloader.py` — add after `pick_batch_torrent`; place `check_batch_availability` right after `check_availability` (~line 932).

- [ ] **Step 1: Implement the broad search + ladder (after `pick_batch_torrent`)**

```python
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
```

- [ ] **Step 2: Implement `check_batch_availability` (right after `check_availability`)**

```python
def check_batch_availability(romaji: str, english: str | None,
                             season: SeasonOption) -> Torrent | None:
    """Add-flow preview: is there a trusted whole-season box? Returns the box
    Torrent or None. Meaningful only when per-episode check_availability is empty."""
    return search_nyaa_batch_ladder(romaji, english, season.format)
```

- [ ] **Step 3: Live smoke check (network; manual, not part of the assert script)**

Run:
```bash
.venv/Scripts/python.exe -c "import anime_downloader as core; \
box = core.search_nyaa_batch_ladder('Mahou Shoujo Madoka Magica','Puella Magi Madoka Magica','TV'); \
print(box.title if box else 'NONE', '|', box.resolution if box else '', '|', box.trusted if box else '')"
```
Expected: a trusted 1080p box, e.g. `[MiniMTBB] Puella Magi Madoka Magica (BD 1080p) | 1080p | True`.

- [ ] **Step 4: Commit** (per Git note)

---

### Task 4: Wire batch fallback into `prepare_sync`

**Files:**
- Modify: `anime_downloader.py` — `fetch_nyaa` worker (~line 1258–1272) and the classify loop (~line 1280–1301).

- [ ] **Step 1: Extend `fetch_nyaa` to probe a box only when per-episode is empty AND season is empty**

Replace the body of `fetch_nyaa` (currently lines ~1258–1272) with:

```python
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
```

- [ ] **Step 2: Add `batch` fields to the season plan entry + classify decision**

In the classify loop (~line 1283), add two keys to the `entry` dict (after `"no_source": []`):

```python
            "no_source": [],
            "batch": None,          # whole-season box Torrent, if used
            "batch_in_qbit": False, # box already present in qBittorrent
```

Then replace the classify block (currently ~line 1297–1300) with:

```python
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
```

- [ ] **Step 3: Verify nothing broke (import + targeted dry-run on an existing series)**

Run: `.venv\Scripts\python.exe -c "import anime_downloader"` → no error.
Run: `.venv\Scripts\python.exe anime_downloader.py --dry-run --series "Kill Ao"`
Expected: runs as before; a SubsPlease show shows per-episode lines and **no** batch behavior (the batch path must not trigger when per-episode sources exist).

- [ ] **Step 4: Commit** (per Git note)

---

### Task 5: CLI presenters — `run_sync` adds the box; add-flow shows box availability

**Files:**
- Modify: `anime_downloader.py` — `run_sync` per-season block (~line 1333+, read it first) and `add_anime_interactive` (~line 1596–1601).

- [ ] **Step 1: In `run_sync`, add the box after the per-episode add loop**

Read the per-season loop in `run_sync` (starts ~line 1333). After the existing loop that performs `qbit.add(...)` for each `(ep_num, torrent)` in `season["to_download"]` and before the season's summary print, insert:

```python
        # Batch/box fallback: add the single whole-season box, if one was chosen.
        batch = season.get("batch")
        if batch:
            url = (build_magnet(batch.infohash, batch.title)
                   if batch.infohash else batch.link)
            ok = qbit.add(url, str(season["path"]), batch.title, dry_run,
                          infohash=batch.infohash)
            verb = "would add" if dry_run else ("added" if ok else "FAILED")
            print(f"  [box] {verb}: {batch.title} ({batch.resolution})")
            if ok and not dry_run:
                total_added += 1
                if batch.infohash:
                    added_hashes.add(batch.infohash)
        elif season.get("batch_in_qbit"):
            print("  [box] already in qBittorrent")
```

(Match the exact variable names used in `run_sync` for the add loop — confirm `total_added` / `added_hashes` exist there; they do per the function's signature/return.)

- [ ] **Step 2: In `add_anime_interactive`, fall back to a box when per-episode is empty**

Replace lines ~1597–1600 (the `available = check_availability(...)` / `if not available:` block) with:

```python
    available = check_availability(chosen.romaji, season)
    box = None
    if not available:
        box = check_batch_availability(chosen.romaji, chosen.english, season)
    if not available and not box:
        print("  X  Not available from a trusted source on Nyaa at any resolution. Nothing created.")
        return
    if available:
        print(f"  OK - {len(available)} trusted episode(s) available: {format_availability(available)}")
    else:
        print(f"  OK - trusted complete box available: {box.title} ({box.resolution})")
```

- [ ] **Step 3: Verify the CLI add availability path (network, dry of the search only)**

Run:
```bash
.venv/Scripts/python.exe -c "import anime_downloader as core; \
from anime_downloader import SeasonOption; \
s=SeasonOption(season_num=1, anilist_id=9756, title='Mahou Shoujo Madoka Magica', episodes=12, status='FINISHED', next_airing=None, format='TV'); \
print('per-ep:', core.check_availability('Mahou Shoujo Madoka Magica', s)); \
print('box:', (lambda b: b.title if b else None)(core.check_batch_availability('Mahou Shoujo Madoka Magica','Puella Magi Madoka Magica', s)))"
```
Expected: `per-ep: {}` then `box: [MiniMTBB] Puella Magi Madoka Magica (BD 1080p)`.

- [ ] **Step 4: Commit** (per Git note)

---

### Task 6: webapp.py — job runner adds the box; `/api/add/check` reports batch availability

**Files:**
- Modify: `webapp.py` — `_run_sync_job` per-season loop (~line 124–167) and `api_add_check` (~line 368–400).

- [ ] **Step 1: In `_run_sync_job`, add the box after the per-episode loop**

After the `for ep_num, t in season["to_download"]:` loop and before `seasons_out.append({...})` (around line 153), insert:

```python
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
```

Then add a `batch` summary to the `seasons_out.append({...})` dict (after `"cover_image": ...`):

```python
                "batch": ({"title": season["batch"].title,
                           "resolution": season["batch"].resolution}
                          if season.get("batch") else None),
                "batch_in_qbit": season.get("batch_in_qbit", False),
```

- [ ] **Step 2: In `api_add_check`, probe a box when per-episode is empty**

Replace the body from `avail = core.check_availability(...)` to the `return jsonify({...})` (lines ~389–400) with:

```python
    avail = core.check_availability(romaji, season)  # {episode: resolution}
    by_resolution: dict[str, list[int]] = {}
    for ep, reso in avail.items():
        by_resolution.setdefault(reso, []).append(ep)
    for eps in by_resolution.values():
        eps.sort()

    box = None
    if not avail:
        english = (body.get("english") or "").strip() or None
        box = core.check_batch_availability(romaji, english, season)

    return jsonify({
        "ok": bool(avail) or box is not None,
        "mode": "episodes" if avail else ("batch" if box else "none"),
        "available": sorted(avail),
        "by_resolution": by_resolution,
        "available_label": core.format_availability(avail) if avail else None,
        "batch_title": box.title if box else None,
        "batch_resolution": box.resolution if box else None,
    })
```

- [ ] **Step 3: Verify the endpoints import cleanly**

Run: `.venv\Scripts\python.exe -c "import webapp"` → no error.

- [ ] **Step 4: Commit** (per Git note)

---

## PART B — Sakura web UI port (`.webui/`)

> Read the four Sakura source files before starting. Functions described as
> "copy verbatim" are taken unchanged from `Anime Library Website/sakura/app.js`
> unless an adaptation is listed.

### Task 7: `.webui/style.css`

**Files:**
- Create/overwrite: `.webui/style.css` (from `Anime Library Website/sakura/sakura.css`).

- [ ] **Step 1: Copy the Sakura stylesheet to `.webui/style.css`**

Run:
```bash
cp "Anime Library Website/sakura/sakura.css" ".webui/style.css"
```

- [ ] **Step 2: Append rules for real cover images, banners, and the empty state**

Append to the end of `.webui/style.css`:

```css
/* ---- Real cover art (added during port) ---- */
.poster .art-img { position: absolute; inset: 0; width: 100%; height: 100%;
  object-fit: cover; display: block; }
.spread-banner .bg-img { position: absolute; inset: 0; width: 100%; height: 100%;
  object-fit: cover; display: block; filter: saturate(1.05); }
.result-cover img, .spread-cover .art-img, .fcover .art-img { object-fit: cover; }

/* ---- Empty library state ---- */
.empty-state { grid-column: 1 / -1; text-align: center; padding: 64px 16px;
  color: var(--ink-3); font-size: 16px; line-height: 1.7; }
.empty-state b { color: var(--ink); }
```

- [ ] **Step 3: Commit** (per Git note)

---

### Task 8: `.webui/tweaks-panel.jsx`

**Files:**
- Create/overwrite: `.webui/tweaks-panel.jsx` (verbatim copy).

- [ ] **Step 1: Copy verbatim**

Run:
```bash
cp "Anime Library Website/sakura/tweaks-panel.jsx" ".webui/tweaks-panel.jsx"
```

- [ ] **Step 2: Commit** (per Git note)

---

### Task 9: `.webui/index.html`

**Files:**
- Create/overwrite: `.webui/index.html` (Sakura HTML with adapted asset paths).

- [ ] **Step 1: Write `.webui/index.html`**

Start from `Anime Library Website/sakura/Anime Library - Sakura.html` and apply exactly these changes:
1. `<link rel="stylesheet" href="sakura.css">` → `<link rel="stylesheet" href="/ui/style.css">`
2. Delete the line `<script src="data.js"></script>`
3. `<script src="app.js"></script>` → `<script src="/ui/app.js"></script>`
4. `<script type="text/babel" src="tweaks-panel.jsx"></script>` → `<script type="text/babel" src="/ui/tweaks-panel.jsx"></script>`

Everything else (header, ticker, masthead, featured, sync banner, grid, spread, add overlay, downloads strip, toast, `#tweaksRoot`, the React/unpkg `<script>` tags, and the inline `TweaksApp` babel block) stays identical to the source.

- [ ] **Step 2: Verify served paths exist**

Run: `ls .webui/` → must list `index.html app.js style.css tweaks-panel.jsx`.

- [ ] **Step 3: Commit** (per Git note)

---

### Task 10: `.webui/app.js` — part 1: helpers, data adapter, posters, library/grid/featured/stats/ticker/filters

**Files:**
- Create/overwrite: `.webui/app.js` (rewrite). This task writes the top of the file; Tasks 11–13 append the remaining sections. Implement all of Tasks 10–13 before testing.

- [ ] **Step 1: Write the file header, helpers, API client, and the data adapter**

```javascript
/* Anime Library — Sakura UI wired to the live Flask JSON API (webapp.py).
   Replaces the prototype's data.js mock + setTimeout fakes. No build step. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const prefersReduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches || document.body.classList.contains("motion-off");

const state = { filter: "all", detail: null, add: {}, dlTimer: null, syncTimer: null, dryRun: false, qbit: false, libraryPath: "" };
let LIBRARY = [];  // normalized series (populated from /api/library)

/* ---------- API ---------- */
async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch { /* non-JSON */ }
  if (!resp.ok) throw new Error(data.error || `${resp.status} ${resp.statusText}`);
  return data;
}

/* ---------- gradient fallback art ---------- */
function hueFromColor(hex) {
  if (!hex) return 250;
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) return 250;
  const r = parseInt(m[1], 16) / 255, g = parseInt(m[2], 16) / 255, b = parseInt(m[3], 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  let h = 0;
  if (d) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
  }
  return Math.round(((h * 60) + 360) % 360);
}
const artStyle = (hue) => `background:linear-gradient(158deg, hsl(${hue} 68% 47%), hsl(${(hue + 26) % 360} 56% 18%) 72%, #0b0710)`;

/* ---------- data adapter: /api/library -> Sakura view model ---------- */
function ceiling(se) {
  return Math.max(se.total || 0,
    se.next_airing ? se.next_airing - 1 : 0,
    se.episodes.length ? Math.max(...se.episodes) : 0,
    se.missing.length ? Math.max(...se.missing) : 0,
    se.downloading.length ? Math.max(...se.downloading) : 0, 0);
}
function normalizeSeries(s) {
  const seasons = Object.entries(s.seasons)
    .map(([n, se]) => [parseInt(n, 10), se])
    .sort((a, b) => a[0] - b[0]);
  const infos = seasons.map(([, se]) => se.info).filter(Boolean);
  const coverInfo = infos.find((i) => i.cover_image_xl || i.cover_image) || infos[0] || null;
  const latest = infos.length ? infos[infos.length - 1] : null;
  const ci = coverInfo || {};
  const norm = {
    folder: s.folder_name,
    romaji: ci.romaji_title || s.folder_name,
    english: ci.english_title || ci.romaji_title || s.folder_name,
    status: (latest && latest.status) || "",
    score: ci.average_score || 0,
    genres: ci.genres || [],
    desc: ci.description || "",
    cover: ci.cover_image_xl || ci.cover_image || null,
    banner: ci.banner_image || null,
    hue: hueFromColor(ci.cover_color),
    hasInfo: infos.length > 0,
    path: (seasons[0] && seasons[0][1].path) || "",
    seasons: seasons.map(([num, se]) => {
      const t = { num, episodes: se.episodes || [], missing: se.missing || [],
        downloading: se.downloading || [], next_airing: (se.info && se.info.next_airing_episode) || null,
        season_status: se.season_status || null };
      t.total = ceiling(t);
      return t;
    }),
  };
  return norm;
}

/* ---------- view-model helpers ---------- */
function statusLabel(st) {
  return ({ RELEASING: ["Airing", "airing"], FINISHED: ["Complete", "finished"],
    NOT_YET_RELEASED: ["Upcoming", "finished"], CANCELLED: ["Cancelled", "finished"],
    HIATUS: ["Hiatus", "finished"] })[st] || ["", "finished"];
}
function seriesView(s) {
  const missing = s.seasons.reduce((n, se) => n + se.missing.length, 0);
  const downloading = s.seasons.reduce((n, se) => n + se.downloading.length, 0);
  const last = s.seasons[s.seasons.length - 1];
  const haveTotal = s.seasons.reduce((n, se) => n + se.episodes.length, 0);
  const epTotal = s.seasons.reduce((n, se) => n + (se.total || 0), 0) || 1;
  let cls = "ok", pill = s.hasInfo ? "Up to date" : "no data", dot = "dot-ok";
  if (downloading) { cls = "dl"; pill = `${downloading} downloading`; dot = "dot-dl"; }
  else if (missing) { cls = "miss"; pill = `${missing} missing`; dot = "dot-miss"; }
  const sub = s.seasons.length > 1 ? `${s.seasons.length} seasons` : `Season ${s.seasons[0].num}`;
  return { missing, downloading, last, cls, pill, dot, sub, haveTotal, epTotal, prog: Math.round((haveTotal / epTotal) * 100) };
}
function matchFilter(s, v) {
  const vw = seriesView(s);
  if (v === "all") return true;
  if (v === "airing") return s.status === "RELEASING";
  if (v === "missing") return vw.missing > 0;
  if (v === "downloading") return vw.downloading > 0;
  if (v === "complete") return vw.missing === 0 && vw.downloading === 0;
  return true;
}

/* ---------- poster (real cover, gradient fallback) ---------- */
function posterHtml(s, opts = {}) {
  const vw = seriesView(s);
  const [, stCls] = statusLabel(s.status);
  let stamp = "";
  if (opts.stamp !== false) {
    if (vw.downloading) stamp = `<span class="stamp dl">Live</span>`;
    else if (vw.missing) stamp = `<span class="stamp miss">${vw.missing} miss</span>`;
    else if (statusLabel(s.status)[0]) stamp = `<span class="stamp ${stCls}">${statusLabel(s.status)[0]}</span>`;
  }
  const prog = (opts.progress && vw.prog < 100) ? `<div class="progress"><i style="width:${vw.prog}%"></i></div>` : "";
  const title = opts.titleOnArt ? `<div class="pt">${esc(s.english)}</div>` : "";
  const art = s.cover
    ? `<div class="art"><img class="art-img" src="${esc(s.cover)}" alt="" loading="lazy"></div>`
    : `<div class="art" style="${artStyle(s.hue)}"></div>`;
  return `<div class="poster">${art}${stamp}<div class="scrim"></div>${title}${prog}</div>`;
}
```

- [ ] **Step 2: Append the presentation renderers (adapted from Sakura)**

Append the following functions. **Copy verbatim from `sakura/app.js`** (their logic is data-source-agnostic): `renderTicker`, `countUp`, `renderStats`, `renderFilters`, `renderGrid`, `seasonCeiling`, `toast`, `refreshAll`. Apply these **adaptations** while copying:

- In `renderFeatured` (copy from Sakura) change the cover markup to use `posterHtml` (already done — it calls `posterHtml(s, {...})`), and ensure the "Now airing" kick only shows when `s.status === "RELEASING"` (otherwise emit `featured pick`):
  replace the `kick` line with
  `<div class="kick">${s.status === "RELEASING" ? '<span class="live-dot"></span>Now airing · featured pick' : 'Featured pick'}</div>`
- `renderGrid`: copy verbatim — it already uses `posterHtml` and `seriesView`.
- `refreshAll`: copy verbatim (`renderTicker(); renderStats(); renderFeatured(); renderFilters(); renderGrid();`), then add a guard at the top: `if (!LIBRARY.length) { renderEmpty(); return; }` — and add:

```javascript
function renderEmpty() {
  $("#stats").innerHTML = "";
  $("#featured").innerHTML = "";
  $("#filters").innerHTML = "";
  $("#gridCount").textContent = "";
  $("#grid").innerHTML = `<div class="empty-state">No series in your library yet.<br>Use <b>+ Add anime</b> to download your first show.</div>`;
  renderTicker();
}
```

- In `renderTicker` (copy from Sakura), change the trailing `qBittorrent connected` segment to reflect real state:
  replace `qBittorrent connected` with `${state.qbit ? "qBittorrent connected" : "qBittorrent offline"}`.

- [ ] **Step 3: Add `loadLibrary` + qBit pill**

```javascript
function setQbit(connected) {
  state.qbit = connected;
  const pill = $("#qbit");
  if (pill) pill.classList.toggle("on", !!connected);
}
async function loadLibrary() {
  let resp;
  try { resp = await api("/api/library"); }
  catch (e) { toast("Could not load library: " + e.message); return; }
  state.libraryPath = resp.library_path || "";
  setQbit(resp.qbit_connected);
  LIBRARY = (resp.series || []).map(normalizeSeries);
  refreshAll();
  if (state.detail) openDetail(state.detail, true); // refresh open spread
}
```

(`openDetail`'s second arg `keepOpen` is defined in Task 11.)

- [ ] **Step 4: Continue to Task 11 (do not test yet).**

---

### Task 11: `.webui/app.js` — part 2: detail spread + delete (real API)

**Files:**
- Modify: `.webui/app.js` (append).

- [ ] **Step 1: Append the detail spread (adapted from Sakura `openDetail`/`closeDetail`)**

Copy `openDetail`, `closeDetail` from `sakura/app.js` with these adaptations:
- Signature: `function openDetail(folder, keepOpen) { ... }`. When `keepOpen` is true, skip the slide-in animation/scroll reset (just re-render `#spreadInner`).
- Banner: replace the `spread-banner` markup with a real banner when present:

```javascript
  const bannerHtml = s.banner
    ? `<div class="spread-banner"><div class="bg-img" style="background-image:url('${esc(s.banner)}')"></div></div>`
    : `<div class="spread-banner"><div class="bg" style="${artStyle(s.hue)}"></div></div>`;
```
- Cover in `spread-cover`: already uses `posterHtml(s, { stamp: false })` — keep.
- Season status: append the real per-season status under each season head. In the `seasonsHtml` map, change the season-head to:

```javascript
    return `<div class="season-block">
      <div class="season-head"><h3>Season ${se.num}</h3>
        <span class="ss"><b>${got}</b> / ${se.total} downloaded${se.season_status ? " · " + esc(se.season_status) : ""}</span></div>
      <div class="ep-grid">${chips}</div>
    </div>`;
```
- The delete confirm block: replace the hardcoded `/anime/<folder>/` path with the real path derived from `s.path` (strip the trailing `Season NN`):

```javascript
  const seriesPath = (s.path || "").replace(/[\\/]+Season\s*\d+\s*$/i, "");
  // ...in the markup:
  //   <code>${esc(seriesPath)}</code>
```
- Keep the `#delBtn` / `#delCancel` / `#delReal` wiring; `#delReal` calls `deleteSeries(s.folder)`.

- [ ] **Step 2: Append the real delete**

```javascript
async function deleteSeries(folder) {
  const btn = $("#delReal");
  if (btn) { btn.disabled = true; btn.textContent = "Deleting…"; }
  try {
    const r = await api("/api/delete", { folder_name: folder, confirm: true });
    toast(`Deleted “${folder}” (${r.torrents_removed} torrent(s) removed).`, true);
    closeDetail();
    loadLibrary();
  } catch (e) {
    toast("Delete failed: " + e.message);
    if (btn) { btn.disabled = false; btn.textContent = "Yes, delete everything"; }
  }
}
```

- [ ] **Step 3: Continue to Task 12 (do not test yet).**

---

### Task 12: `.webui/app.js` — part 3: sync (real) + downloads (real) + init/resume

**Files:**
- Modify: `.webui/app.js` (append).

- [ ] **Step 1: Append real sync (replaces Sakura's `SYNC_PHASES`/`startSync`/`finishSync`)**

```javascript
function syncFraction(events) {
  let f = 0.02;
  for (const ev of events) {
    if (ev.event === "scan_done") f = Math.max(f, 0.05);
    else if (ev.event === "anilist_progress") f = Math.max(f, 0.05 + 0.40 * (ev.done / ev.total));
    else if (ev.event === "nyaa_progress" && ev.total) f = Math.max(f, 0.45 + 0.25 * (ev.done / ev.total));
    else if (ev.event === "classify_done") f = Math.max(f, 0.72);
    else if (ev.event === "torrent_added") f = Math.max(f, 0.8);
  }
  return f;
}
async function startSync(dryRun, only) {
  // `only` is a FOLDER NAME (or null for the whole library).
  try { await api("/api/sync", { dry_run: dryRun, series: only || null }); }
  catch (e) { toast(e.message); return; }
  showSyncRunning(dryRun, only);
  pollSync();
}
function showSyncRunning(dryRun, only) {
  const banner = $("#syncBanner");
  banner.classList.remove("hidden");
  void banner.offsetWidth; banner.classList.add("in");
  $("#syncMode").style.display = dryRun ? "" : "none";
  $("#syncGear").style.display = "";
  $("#syncLog").classList.add("show");
  $("#syncLog").innerHTML = "";
  $("#syncSummary").classList.remove("show");
  $("#syncSummary").innerHTML = "";
  $("#syncProgress").style.width = "2%";
  $("#syncPhase").textContent = only ? `Starting… — ${only}` : "Starting…";
  $("#syncBtn").disabled = true;
}
function pollSync() {
  clearInterval(state.syncTimer);
  state.syncTimer = setInterval(async () => {
    let st;
    try { st = await api("/api/sync/status"); } catch { return; }
    $("#syncPhase").textContent = st.series ? `${st.phase} — ${st.series}` : st.phase;
    $("#syncMode").style.display = st.dry_run ? "" : "none";

    const added = st.events.filter((e) => e.event === "torrent_added");
    if (added.length) {
      $("#syncLog").innerHTML = added.map((e) => {
        const label = e.batch
          ? `complete box${e.resolution ? ` (${esc(e.resolution)})` : ""}`
          : `Ep ${String(e.episode).padStart(2, "0")}${e.resolution && e.resolution !== "1080p" ? ` (${esc(e.resolution)})` : ""}`;
        return `<div class="ln"><b>${e.dry_run ? "would add" : "+ added"}</b> ${esc(e.folder)} — ${label}</div>`;
      }).join("");
      $("#syncLog").scrollTop = $("#syncLog").scrollHeight;
    }

    if (st.state === "running") {
      $("#syncProgress").style.width = (syncFraction(st.events) * 100).toFixed(1) + "%";
      return;
    }
    clearInterval(state.syncTimer); state.syncTimer = null;
    $("#syncGear").style.display = "none";
    $("#syncBtn").disabled = false;
    $("#syncProgress").style.width = "100%";
    if (st.state === "error") {
      $("#syncPhase").textContent = "Sync failed";
      $("#syncSummary").classList.add("show");
      $("#syncSummary").innerHTML = `<span class="num warn">${esc(st.error)}</span>`;
      return;
    }
    renderSyncSummary(st.result);
    loadLibrary();
    pollDownloads(true);
  }, 1000);
}
function fmtRange(eps) {
  if (!eps || !eps.length) return "(none)";
  const a = [...eps].sort((x, y) => x - y), parts = [];
  let s = a[0], e = a[0];
  for (const ep of a.slice(1)) { if (ep === e + 1) { e = ep; continue; } parts.push(s === e ? `${s}` : `${s}-${e}`); s = e = ep; }
  parts.push(s === e ? `${s}` : `${s}-${e}`);
  return parts.join(", ");
}
function renderSyncSummary(r) {
  if (!r) return;
  $("#syncPhase").textContent = r.dry_run ? "Dry run complete" : "Sync complete";
  const verb = r.dry_run ? "would download" : "added";
  const totalAdds = r.seasons.reduce((n, s) => n + s.added.length, 0);
  const noSource = r.seasons.reduce((n, s) => n + s.no_source.length, 0);
  const inQbit = r.seasons.reduce((n, s) => n + s.in_qbit.length, 0);
  let html = `Checked <b>${r.series_count}</b> series / <b>${r.season_count}</b> seasons — `
    + `<span class="num ${totalAdds ? "ok" : ""}">${totalAdds} item(s) ${verb}</span>`;
  if (inQbit) html += `, <span class="num warn">${inQbit} already in qBittorrent</span>`;
  if (noSource) html += `, <span class="num warn">${noSource} with no trusted source</span>`;
  if (!totalAdds && !noSource && !inQbit) html += ` — everything is up to date ✓`;
  const addLines = r.seasons.filter((s) => s.added.length).map((s) => {
    const eps = s.added.filter((a) => a.episode != null).map((a) => a.episode);
    const box = s.added.some((a) => a.batch) ? " + complete box" : "";
    const epLabel = eps.length ? `Ep ${esc(fmtRange(eps))}` : "";
    return `<li>${esc(s.folder_name)} S${String(s.season_num).padStart(2, "0")} — ${epLabel}${box}</li>`;
  }).join("");
  if (addLines) html += `<ul>${addLines}</ul>`;
  if (r.warnings && r.warnings.length) html += `<ul>${r.warnings.map((w) => `<li class="num warn">${esc(w)}</li>`).join("")}</ul>`;
  $("#syncSummary").classList.add("show");
  $("#syncSummary").innerHTML = html;
}
async function resumeSyncIfRunning() {
  try {
    const st = await api("/api/sync/status");
    if (st.state === "running") { showSyncRunning(st.dry_run, st.series); pollSync(); }
  } catch { /* server starting */ }
}
```

- [ ] **Step 2: Append real downloads (replaces Sakura's `gatherDownloads`/`startDownloads`/`renderDownloads`)**

```javascript
function fmtSpeed(bps) {
  if (bps >= 1048576) return (bps / 1048576).toFixed(1) + " MB/s";
  if (bps >= 1024) return (bps / 1024).toFixed(0) + " kB/s";
  return (bps || 0) + " B/s";
}
const cleanName = (n) => n.replace(/^\[SubsPlease\]\s*/i, "").replace(/\.mkv$/i, "");
function pollDownloads(immediate) {
  clearTimeout(state.dlTimer);
  const tick = async () => {
    let d;
    try { d = await api("/api/downloads"); }
    catch { state.dlTimer = setTimeout(tick, 15000); return; }
    setQbit(d.connected);
    const active = (d.torrents || []).filter((t) => !t.done);
    renderDownloads(active);
    state.dlTimer = setTimeout(tick, active.length ? 3000 : 15000);
  };
  state.dlTimer = setTimeout(tick, immediate ? 100 : 0);
}
function renderDownloads(active) {
  const strip = $("#dlStrip");
  if (!active.length) { strip.classList.remove("show"); return; }
  strip.classList.add("show");
  const speed = active.reduce((n, t) => n + (t.dlspeed || 0), 0);
  $("#dlCount").textContent = `${active.length} downloading`;
  $("#dlSpeed").textContent = fmtSpeed(speed);
  $("#dlItems").innerHTML = active.map((t) => `
    <div class="dl-item"><div class="r"><span class="nm">${esc(cleanName(t.name))}</span>
      <span class="st">${Math.round((t.progress || 0) * 100)}% · ${fmtSpeed(t.dlspeed || 0)}</span></div>
      <div class="dl-bar"><i style="width:${((t.progress || 0) * 100).toFixed(1)}%"></i></div></div>`).join("");
}
```

- [ ] **Step 3: Append the global `[data-act]` click handler + `init` (adapted from Sakura)**

Copy Sakura's document-level `[data-act]` click handler, but change the `sync-one` branch to pass the **folder**:

```javascript
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-act]");
  if (!t) return;
  const act = t.dataset.act, folder = t.dataset.folder;
  if (act === "sync-one") { closeDetail(); startSync(state.dryRun, folder); }
  else if (act === "open-detail") openDetail(folder);
  else if (act === "close-detail") closeDetail();
});

function init() {
  loadLibrary();
  resumeSyncIfRunning();
  pollDownloads();

  const enableAnim = () => { if (!prefersReduced() && !document.hidden) requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.add("anim"))); };
  enableAnim();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) enableAnim(); });

  $("#syncBtn").addEventListener("click", () => startSync(state.dryRun, null));
  $("#addBtn").addEventListener("click", openAdd);
  $("#brand").addEventListener("click", () => { closeDetail(); window.scrollTo({ top: 0, behavior: prefersReduced() ? "auto" : "smooth" }); });

  const dry = $("#dryRun");
  dry.checked = localStorage.getItem("sakuraDry") === "1";
  state.dryRun = dry.checked;
  dry.addEventListener("change", () => { state.dryRun = dry.checked; localStorage.setItem("sakuraDry", dry.checked ? "1" : "0"); });

  $("#spreadBack").addEventListener("click", closeDetail);
  $("#addClose").addEventListener("click", closeAdd);
  $("#addOverlay").addEventListener("click", (e) => { if (e.target === e.currentTarget) closeAdd(); });
  $("#dlHead").addEventListener("click", () => {
    const s = $("#dlStrip"); s.classList.toggle("collapsed");
    $("#dlCaret").textContent = s.classList.contains("collapsed") ? "▴" : "▾";
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeDetail(); closeAdd(); } });
}
init();
```

- [ ] **Step 4: Continue to Task 13 (the add flow is referenced by `init` via `openAdd`).**

---

### Task 13: `.webui/app.js` — part 4: add flow (real API + batch availability)

**Files:**
- Modify: `.webui/app.js` (append the add-flow section **before** the `init()` call — or anywhere at top level, since functions hoist; keep it above `init();` for readability).

- [ ] **Step 1: Append the real add flow**

```javascript
/* ---------- Add-anime flow (real AniList + Nyaa) ---------- */
function openAdd() { state.add = {}; $("#addOverlay").classList.remove("hidden"); renderAddSearch(); }
function closeAdd() { $("#addOverlay").classList.add("hidden"); state.add = {}; }
function steps(active) {
  const labels = ["Search", "Season", "Availability", "Download"];
  return `<div class="steps">${labels.map((l, i) => `<div class="step ${i === active ? "active" : i < active ? "done" : ""}">${i + 1}. ${l}</div>`).join("")}</div>`;
}
function renderAddSearch() {
  $("#addContent").innerHTML = `
    <h2>Add a new anime</h2>${steps(0)}
    <div class="search-row">
      <input id="addSearch" type="text" placeholder="Search AniList… (e.g. Madoka)" autocomplete="off">
      <button class="btn ink" id="addSearchBtn">Search</button>
    </div>
    <div class="result-list" id="addResults"></div>`;
  const input = $("#addSearch");
  input.focus();
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  $("#addSearchBtn").addEventListener("click", doSearch);
}
async function doSearch() {
  const term = $("#addSearch").value.trim();
  if (!term) return;
  const box = $("#addResults");
  box.innerHTML = `<div class="loading"><div class="spinner"></div>Searching AniList…</div>`;
  let resp;
  try { resp = await api("/api/anilist/search", { term }); }
  catch (e) { box.innerHTML = ""; toast(e.message); return; }
  state.add.candidates = resp.candidates || [];
  if (!state.add.candidates.length) { box.innerHTML = `<div class="loading">No matches found.</div>`; return; }
  box.innerHTML = state.add.candidates.map((c, i) => {
    const meta = [c.format, c.year, c.episodes ? `${c.episodes} eps` : null, c.status].filter(Boolean).join(" · ");
    const cover = c.cover_image
      ? `<img class="art-img" src="${esc(c.cover_image)}" alt="" loading="lazy">`
      : `<div class="art" style="${artStyle(hueFromColor(c.cover_color))};position:absolute;inset:0"></div>`;
    return `<div class="result-row" data-i="${i}">
      <div class="result-cover">${cover}</div>
      <div class="result-info">
        <div class="rt">${esc(c.romaji)}</div>
        ${c.english && c.english !== c.romaji ? `<div class="ra">${esc(c.english)}</div>` : ""}
        <div class="rm">${esc(meta)}${c.average_score ? ` · ★ ${c.average_score}%` : ""}</div>
        <div class="rg">${(c.genres || []).slice(0, 4).map((g) => `<span class="tag" style="font-size:10px;padding:2px 7px">${esc(g)}</span>`).join("")}</div>
      </div>
    </div>`;
  }).join("");
  $$("#addResults .result-row").forEach((el) => el.addEventListener("click", () => pickCandidate(+el.dataset.i)));
}
async function pickCandidate(i) {
  const c = state.add.candidates[i];
  state.add.chosen = c;
  $("#addContent").innerHTML = `<h2>${esc(c.romaji)}</h2>${steps(1)}<div class="loading"><div class="spinner"></div>Walking the sequel chain on AniList…</div>`;
  let resp;
  try { resp = await api("/api/add/seasons", { anilist_id: c.anilist_id }); }
  catch (e) { toast(e.message); renderAddSearch(); return; }
  state.add.seasons = resp.seasons || [];
  renderSeasonPicker();
}
function renderSeasonPicker() {
  const { chosen, seasons } = state.add;
  $("#addContent").innerHTML = `
    <h2>${esc(chosen.romaji)}</h2>${steps(1)}
    <div class="season-pick">${seasons.map((s) => {
      const aired = s.next_airing ? `${s.next_airing - 1} aired` : (s.episodes ? `${s.episodes} eps` : "? eps");
      return `<div class="season-opt" data-n="${s.season_num}">
        <div class="sn">S${s.season_num}</div>
        <div class="si"><div class="st">${esc(s.title)}</div>
          <div class="sm">${esc([s.status, aired, s.format].filter(Boolean).join(" · "))}</div></div>
      </div>`;
    }).join("")}</div>
    <div id="availArea"></div>
    <div class="modal-actions">
      <button class="btn ghost" id="addBack">← Back</button><div class="spacer"></div>
      <button class="btn pink" id="addConfirm" disabled>Create folder &amp; download</button>
    </div>
    <div class="modal-note">Quality rule: only <b>trusted</b> Nyaa releases, <b>1080p preferred</b>. Lower resolutions and a complete box are used only when nothing better exists.</div>`;
  $("#addBack").addEventListener("click", renderAddSearch);
  $("#addConfirm").addEventListener("click", confirmAdd);
  $$("#addContent .season-opt").forEach((el) => el.addEventListener("click", () => pickSeason(+el.dataset.n, el)));
}
async function pickSeason(n, el) {
  $$("#addContent .season-opt").forEach((x) => x.classList.toggle("sel", x === el));
  const season = state.add.seasons.find((s) => s.season_num === n);
  state.add.season = season;
  $("#addConfirm").disabled = true;
  const area = $("#availArea");
  area.innerHTML = `<div class="avail checking"><div class="spinner"></div>Checking Nyaa for trusted episodes of Season ${n} (1080p preferred)…</div>`;
  let resp;
  try {
    resp = await api("/api/add/check", {
      romaji: state.add.chosen.romaji,
      english: season.english || state.add.chosen.english,  // SEASON's english (batch matching)
      season_num: season.season_num, anilist_id: season.anilist_id,
      title: season.title, episodes: season.episodes, status: season.status,
      next_airing: season.next_airing, format: season.format,
    });
  } catch (e) { area.innerHTML = ""; toast(e.message); return; }
  if (state.add.season !== season) return; // user switched seasons
  if (resp.ok && resp.mode === "episodes") {
    area.innerHTML = `<div class="avail yes"><div class="at">✓ Available — ${resp.available.length} trusted episode(s)</div>
      <div class="as">Found on Nyaa: ${esc(resp.available_label)} (SubsPlease preferred).</div></div>`;
    $("#addConfirm").disabled = false;
  } else if (resp.ok && resp.mode === "batch") {
    area.innerHTML = `<div class="avail yes"><div class="at">✓ Available as a complete box${resp.batch_resolution ? ` (${esc(resp.batch_resolution)})` : ""}</div>
      <div class="as">Trusted box on Nyaa: ${esc(resp.batch_title)}</div></div>`;
    $("#addConfirm").disabled = false;
  } else {
    area.innerHTML = `<div class="avail no"><div class="at">✕ No trusted source at any resolution</div>
      <div class="as">Nothing will be created. Try another season or check back later.</div></div>`;
  }
}
async function confirmAdd() {
  const { chosen, season } = state.add;
  const btn = $("#addConfirm"); btn.disabled = true; btn.textContent = "Starting…";
  try {
    await api("/api/add/confirm", { romaji: chosen.romaji, season_num: season.season_num });
    toast(`Created “${chosen.english}” — downloading now.`, true);
    closeAdd();
    showSyncRunning(false, null);
    pollSync();
  } catch (e) { toast(e.message); btn.disabled = false; btn.textContent = "Create folder & download"; }
}

/* ---------- Toast ---------- */
let toastTimer;
function toast(msg, ok = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("ok", ok);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3600);
}
```

> Note: `toast` is also used by Tasks 10–12; define it once (here). If Sakura's verbatim `toast` was already copied in Task 10 Step 2, do **not** duplicate it — keep this single definition and drop it from the verbatim copy list.

- [ ] **Step 2: Sanity-check the file parses**

Open the page later (Task 16). For now, do a static check: `node --check .webui/app.js` if Node is available; otherwise rely on the browser console in Task 16. (Node is optional; skip if not installed.)

- [ ] **Step 3: Commit** (per Git note)

---

## PART C — Cleanup, docs, verification

### Task 14: Remove the source folder

**Files:**
- Delete: `Anime Library Website/`

- [ ] **Step 1: Confirm the port copied everything, then delete**

Run:
```bash
ls .webui/   # must show index.html app.js style.css tweaks-panel.jsx
rm -rf "Anime Library Website"
ls -d */ | grep -i "anime library" || echo "removed"
```
Expected: `removed`.

- [ ] **Step 2: Commit** (per Git note)

---

### Task 15: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md` — the **Web App** section and the **quality rule** / **Architecture** notes.

- [ ] **Step 1: Update the Web App section**

Replace the first paragraph of the **Web App** section so it reads (keep surrounding bullets, updating the frontend file list):

> `webapp.py` is a Flask server exposing the same flows as a single-page web UI (the **"Sakura" editorial/risograph design** — cream paper, ink, spot colors — with real AniList cover art and gradient fallbacks, a headline ticker, a featured spread, per-season episode chips, sync with live progress, add/delete flows, and a download monitor). ...

Update the frontend bullet to list **four** files and the CDN dependency:

> - The frontend lives in **`.webui/`** (`index.html`, `app.js`, `style.css`, `tweaks-panel.jsx`) — dot-prefixed deliberately so the scanner/delete-picker skip it. No Node/build step; Flask serves it statically at `/ui`. The **Tweaks panel** (`tweaks-panel.jsx`) is a small React island loaded from the unpkg CDN with in-browser Babel — purely cosmetic (accent color / motion toggles via CSS vars + body classes); it requires internet but the tool already needs it for AniList/Nyaa.

- [ ] **Step 2: Document the batch/box fallback**

Under the **Quality rule** paragraph, append:

> **Batch/box fallback:** when a season has no episodes on disk *and* no trusted per-episode release exists at any resolution (e.g. older shows with no SubsPlease release like *Madoka Magica*), the tool falls back to a single trusted **whole-season box** from any group — anchored to the AniList title (`token_sort_ratio ≥ BATCH_FUZZY_THRESHOLD`), excluding movies/spin-offs/single-episode files, walking the same resolution ladder. It fires only when the per-episode search came up empty, so the common SubsPlease case costs no extra requests. Trusted-only is preserved. Implemented by `search_nyaa_batch_ladder` / `pick_batch_torrent` / `check_batch_availability`; wired into `prepare_sync` (so sync and add-confirm both use it) and surfaced by `/api/add/check` (`mode: "batch"`).

Add to the config-constants table: `BATCH_FUZZY_THRESHOLD` and `BATCH_EXCLUDE_KEYWORDS`.

- [ ] **Step 3: Commit** (per Git note)

---

### Task 16: End-to-end manual verification

**Files:** none (runtime verification).

- [ ] **Step 1: Backend test suite green**

Run: `.venv\Scripts\python.exe tests\test_batch_fallback.py` → `ALL PASS`.

- [ ] **Step 2: Start the web app**

Run: `.venv\Scripts\python.exe webapp.py --no-browser` (in a background terminal), then open `http://127.0.0.1:8765/`.

- [ ] **Step 3: Verify the UI**

Confirm in the browser (and devtools console — **no JS errors**):
- Library grid renders with **real cover images**; missing-cover series show a gradient.
- Stats count-up, ticker, filters (All/Airing/Missing/Downloading/Complete) all work.
- Click a card → detail spread with banner, season episode chips, season status.
- Toggle **Dry run**, click **Sync library** → live progress banner; summary on completion; **nothing added** to qBittorrent.
- **+ Add anime** → search `Madoka` → pick *Mahou Shoujo Madoka Magica* → pick Season 1 → availability shows **"Available as a complete box (1080p)"** with the MiniMTBB title.
- **+ Add anime** → search a current SubsPlease show → availability shows **per-episode** ("Available — N trusted episode(s)").
- Tweaks panel: change accent color / toggle motion → applies live.

- [ ] **Step 4: Stop the server.** Done.

---

## Git note

The project directory is **not its own git repo** — the enclosing repo root is the
user's home directory (`C:/Users/Renz Lozada`) and none of these files are tracked.
**Do not commit into the home-directory repo.** Treat every "Commit" step as a
no-op unless the user first runs `git init` for this project. If they do, commit
per step with conventional-commit messages.

## Self-review notes (addressed)
- **Spec coverage:** Part 1 (UI port, real covers + fallback, tweaks kept, source
  deleted, latent folder-name sync bug fixed) → Tasks 7–14. Part 2 (batch fallback
  everywhere, add-check preview, trusted-only, efficiency guard, constants) →
  Tasks 1–6. Cleanup/docs → Tasks 14–15. Verification → Task 16. No gaps.
- **Types/signatures consistent:** `pick_batch_torrent`, `search_nyaa_batch_ladder`,
  `check_batch_availability` signatures match across tasks and callers; plan entry
  keys `batch`/`batch_in_qbit` consistent between `prepare_sync`, `run_sync`,
  `_run_sync_job`. Frontend `startSync(dryRun, folder)` consistently passes the
  folder name; `toast` defined exactly once.
- **No placeholders:** all steps carry concrete code/commands.
