# Design: Sakura web UI + batch/box source fallback

Date: 2026-06-13

## Summary

Two related pieces of work on the personal anime-library tool:

1. **Replace the web UI** with the "Sakura" editorial design (currently a mock
   prototype under `Anime Library Website/sakura/`) by porting it into `.webui/`
   and wiring it to the live Flask JSON API in `webapp.py`. No backend API
   changes are needed for this part — the existing contract already covers it.
2. **Add a batch/box source fallback** to `anime_downloader.py` so older/finished
   shows that have no per-episode SubsPlease release (e.g. *Madoka Magica*) can
   still be found and downloaded as a single trusted whole-season box torrent.

Cross-cutting requirement: **efficiency** — the common case must not pay for the
new fallback, and the new UI must not poll or re-render wastefully.

Out of scope / already done: the **resolution ladder** (1080p → 720p → 480p) is
already implemented (`search_nyaa_ladder` / `pick_episode_ladder`) and verified
working. No work there.

---

## Part 1 — Sakura web UI

### Decisions (confirmed with user)
- **Cover art:** real AniList covers/banners, with the Sakura gradient as a
  fallback when no image exists.
- **Tweaks panel:** keep it (React island via unpkg CDN + in-browser Babel).
- **Source folder:** delete `Anime Library Website/` after porting.

### File layout (in `.webui/`, overwriting the current UI)

| File | Source | Change |
|---|---|---|
| `index.html` | Sakura HTML | Asset refs → `/ui/...`; drop `<script src="data.js">`; tweaks `<script>` points at `/ui/tweaks-panel.jsx` |
| `style.css` | Sakura `sakura.css` (renamed) | Verbatim + a few rules for real `<img>` covers/banners and the empty state |
| `app.js` | **Rewritten** | Sakura's rendering/animation, fed by the real API instead of the `data.js` mock + `setTimeout` fakes |
| `tweaks-panel.jsx` | Sakura | Verbatim |

`data.js` is not ported. `webapp.py` already serves `.webui/` statically at
`/ui` and `index.html` at `/`, so **no Python change is required** — it serves
`/ui/tweaks-panel.jsx` like any other static file.

### Data adapter (the heart of the rewrite)

`webapp.py`'s `GET /api/library` returns:

```
{ series: [ { folder_name,
              seasons: { "<n>": { path, episodes[], style, missing[],
                                  downloading[], season_status, info } } } ],
  qbit_connected, library_path }
```

`info` (when present) carries: `romaji_title`, `english_title`, `status`,
`average_score`, `genres[]`, `description`, `cover_image`, `cover_image_xl`,
`cover_color`, `banner_image`, `total_episodes`, `next_airing_episode`.

A `normalizeSeries(apiSeries)` adapter maps each series onto the flat shape the
Sakura renderers already expect, choosing cover info from the first season that
has it and status from the latest season (mirrors the current `.webui`
`seriesView`):

| Sakura field | From |
|---|---|
| `folder` | `folder_name` |
| `romaji` / `english` | `info.romaji_title` / `info.english_title` |
| `status` | latest season `info.status` |
| `score` | `info.average_score` |
| `genres` / `desc` | `info.genres` / `info.description` |
| `cover` / `banner` | `info.cover_image_xl||cover_image` / `info.banner_image` (**new**) |
| `coverColor` | `info.cover_color` (drives gradient fallback) |
| `seasons[].num` | season key |
| `seasons[].total` | ceiling: `max(total_episodes, next_airing-1, max(present/missing/downloading))` (mirrors current `seasonCeiling`) |
| `seasons[].episodes/missing/downloading` | direct |
| `seasons[].next_airing` | `info.next_airing_episode` |
| `featured` | first `RELEASING` series, else first |

### Rendering changes
- `posterHtml()` / `artStyle()`: render a real `<img loading="lazy">` when
  `cover` exists; otherwise the existing gradient, derived from `coverColor`
  (fallback hue if absent). Detail-spread banner uses real `banner` when present.
- `statusLabel()` extended to handle `CANCELLED` / `HIATUS` (the API can return
  them) in addition to `RELEASING` / `FINISHED` / `NOT_YET_RELEASED`.
- Empty-library state: a short message when `series` is empty.

### Wiring the real flows (replacing the mock `setTimeout` layer)
- **Library:** `GET /api/library` on load → normalize → `refreshAll()`; refresh
  an open detail spread after sync/delete.
- **Sync:** `POST /api/sync {dry_run, series:<folder_name>}`, then poll
  `GET /api/sync/status` every 1 s. Drive `#syncPhase / #syncProgress /
  #syncLog / #syncSummary` from real `events` + `result` (port `syncFraction`
  and `renderSyncSummary` from the current `.webui/app.js`). **Fixes a latent
  bug:** Sakura passed the *English title* as the series target; the API filters
  by **folder name**. Reattach to an in-flight job on reload
  (`resumeSyncIfRunning`).
- **Add flow:** real `POST /api/anilist/search` → `/api/add/seasons` →
  `/api/add/check` → `/api/add/confirm`, rendered into Sakura's existing 4-step
  modal markup, with real candidate covers. `/api/add/check` now also reports
  **batch availability** (see Part 2) so the Confirm button enables for boxed
  shows.
- **Delete:** `POST /api/delete {folder_name, confirm:true}`; the confirm box
  shows the real on-disk path (derived from the season `path`, stripping
  `\Season NN`).
- **Downloads strip:** poll `GET /api/downloads` (3 s while active, 15 s idle);
  real `progress` (0–1) and `dlspeed` (bytes/s, via `fmtSpeed`); also sets the
  qBittorrent status pill from `connected`.
- **qBit pill / dry-run toggle:** pill from `qbit_connected`; dry-run checkbox
  persists to `localStorage` and is passed to sync.

### Efficiency (UI)
- One `/api/library` fetch per load/refresh; normalize once into module state.
- Downloads polling backs off to 15 s when nothing is active; sync polling stops
  on terminal state.
- `<img loading="lazy">` for all covers; build grid HTML once per refresh.
- Keep Sakura's existing reduced-motion / hidden-tab guards.

---

## Part 2 — Batch/box source fallback (backend)

### Problem (verified empirically)
The Nyaa pipeline only ever queries `[SubsPlease] <title> (<res>)`. SubsPlease
is a post-2020 simulcast group, so a 2011 show like *Madoka Magica* returns **0
results** and is reported "no trusted source". A broad search confirms a trusted
box exists: `[MiniMTBB] Puella Magi Madoka Magica (BD 1080p)` (trusted, ~49
seeders), alongside non-trusted batches, the three movies, and the *Magia
Record* spin-off (which the fix must avoid selecting).

### Decisions (confirmed with user)
- **Automatic** batch/box fallback.
- Applies to **add flow + sync** (shared pipeline).
- **Trusted-only is preserved** — a trusted box exists, so the strict quality
  rule is kept. Shows with only non-trusted boxes remain "unavailable".

### Season-awareness (added after the real target turned out to be *Medaka Box*)
Multi-season older shows split their boxes across seasons and resolutions —
*Medaka Box* (S1) has only a **720p** trusted box (`[CBM] … 1-12 Complete`) while
*Medaka Box Abnormal* (S2) has a **1080p** trusted box (`[FFF] …`). So the box
match must use **each season's own romaji + English titles**, never the base
show title, or a sequel would grab Season 1's box. `SeasonOption` gains an
`english` field (populated by `enumerate_seasons` from `title.english`); the sync
path already uses the per-season `info` romaji/English. `_clean_torrent_title`
also strips episode-range tokens (`1-12`) so `[CBM] Medaka Box 1-12 Complete`
matches `Medaka Box`. Verified live: S1 → 720p `[CBM]` box, S2 → 1080p `[FFF]`
box.

### New functions (in `anime_downloader.py`)

`search_nyaa_batch(romaji, english, season_num, resolution) -> list[Torrent]`
- Untagged (no group) Nyaa query anchored to the AniList title, e.g.
  `f"{english or romaji} {resolution}"`, with a romaji fallback when distinct.
  Same `c=1_2` category, same `SESSION`.

`pick_batch_torrent(results, romaji, english, season_format, resolution) -> Torrent | None`
- Filters: `trusted` AND `resolution == <rung>` AND **looks like a whole-season
  batch** (reject titles carrying a single-episode marker — a small
  `looks_like_single_episode(title)` helper, e.g. a ` - NN ` / `S##E##` token
  not part of a range) AND not a movie/recap/side-story (generic keyword blocklist:
  `movie`, `gekijou`, `gekijouban`, `recap`, `side story`, `specials`) when the
  season format is TV/ONA/OVA.
- Title match: `rapidfuzz.fuzz.token_sort_ratio(core_title, romaji|english)` —
  `token_sort_ratio` (not `token_set_ratio`) so extra tokens from movies /
  spin-offs are penalized. `core_title` = torrent title stripped of `[group]`
  tags, `(BD …)`, resolution, year. Require ≥ `BATCH_FUZZY_THRESHOLD` (new
  const, default 80).
- Pick: `max(title_score, then seeders)`.

`search_nyaa_batch_ladder(romaji, english, season_num, season_format) -> Torrent | None`
- Walk `RESOLUTION_LADDER`: try the highest rung first; only fetch the next rung
  if no trusted box matched above. One request per rung, short-circuits — mirrors
  `search_nyaa_ladder`.

### Integration into `prepare_sync` (sync + add-confirm both use this)
- **Trigger condition (efficiency guard):** in the parallel `fetch_nyaa` worker,
  after building the per-episode pools, if **no** missing episode has a
  per-episode pick **and** the season has **zero present episodes**, run
  `search_nyaa_batch_ladder(...)` and stash the result on
  `season_data["batch_torrent"]`. Otherwise it is never called → **zero extra
  requests in the common case**. (Network in the worker; no `qbit` access there,
  preserving the "qbit snapshot loads lazily in the sequential phase" invariant.)
- **Classify phase (sequential):** the season plan entry gains a
  `"batch": Torrent | None` field. If a `batch_torrent` exists and
  `not qbit.has(batch.title, batch.infohash)`, set `entry["batch"]` and clear
  `no_source` (the box covers the season); if qBit already has it, treat as
  `in_qbit`. Per-episode behavior is unchanged whenever per-episode sources exist.

### Presenters add the box
- `run_sync()` (CLI) and `webapp.py`'s `_run_sync_job()` both add `entry["batch"]`
  when present (one `qbit.add(magnet, save_path=season_folder, ...)`), and report
  it as e.g. `+ added <folder> — complete box (BD 1080p)`. The web result
  `seasons_out` entry includes a `batch` summary (title + resolution) so the UI
  log/summary can render it.

### Availability preview (add flow only)
- `check_availability(romaji, season)` keeps returning the per-episode
  `{episode: resolution}` dict. A sibling `check_batch_availability(romaji,
  english, season) -> Torrent | None` runs the batch ladder.
- `/api/add/check` (and the CLI add availability display) call per-episode first;
  if empty, call the batch probe. Response gains:
  `mode: "episodes" | "batch" | "none"`, plus `batch_title` / `batch_resolution`
  when boxed. `ok` is true if either per-episode or batch is available, so the
  Confirm button enables for *Madoka*. The user sees the exact box title before
  confirming (a safety net against a mis-matched batch).
- `/api/add/confirm` is **unchanged**: it creates the folder and starts a
  targeted `run_sync`, which now resolves the box through `prepare_sync`.

### New constants
- `BATCH_FUZZY_THRESHOLD = 80` — minimum title match for a batch.
- `BATCH_EXCLUDE_KEYWORDS = ("movie", "gekijou", "gekijouban", "recap", "side story", "specials")`.

### Efficiency (backend)
- Batch search only fires when per-episode is empty **and** the season is empty
  (new/finished-with-nothing) — never for the SubsPlease common case.
- Batch ladder short-circuits at the first rung that yields a trusted box.
- Runs inside the existing parallel `fetch_nyaa` pool; no console output from
  workers (preserves the output-altitude rule); `qbit` dedup stays sequential.
- Known minor cost: a box whose files don't parse to episode numbers keeps the
  season "empty", so each subsequent sync re-runs one batch RSS request for it
  (the add is still deduped by infohash). Acceptable; documented.

---

## Cleanup & docs
- Delete the `Anime Library Website/` folder after the port (removes the stray
  un-prefixed folder the scanner would otherwise see).
- Update `CLAUDE.md`:
  - **Web App** section: Sakura editorial design; new `.webui/tweaks-panel.jsx`;
    the React/Babel unpkg CDN dependency; real covers + gradient fallback.
  - **Architecture / quality rule:** document the batch/box fallback, its trigger
    condition, trusted-only guarantee, and the new constants.

## Testing / verification
- **Backend:** a small probe script (using the module's own functions) asserting
  `pick_batch_torrent` selects `[MiniMTBB] Puella Magi Madoka Magica (BD 1080p)`
  and rejects the movies / *Magia Record* / non-trusted batches for a Madoka
  search; and that `check_batch_availability` reports a box for the Madoka
  `SeasonOption`. Confirm the SubsPlease common case still issues exactly one
  request and never calls the batch path (e.g. for a current SubsPlease show).
- **Frontend:** start `webapp.py`, load `/`, verify: grid with real covers,
  detail spread, a dry-run sync (live progress), the add flow reaching the
  availability step for both a SubsPlease show and Madoka (box reported), and the
  downloads strip. `--dry-run` keeps it safe (nothing added to qBittorrent).

## Risks
- Batch title-matching is heuristic; mitigations: trusted-only, `token_sort_ratio`
  ≥ 80 against both titles, movie/spin-off keyword blocklist, and (in the add
  flow) the user confirms after seeing the exact box title.
- CDN dependency for the tweaks panel means it needs internet (the tool already
  needs internet for AniList/Nyaa, so this is acceptable).
