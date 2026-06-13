/* ============================================================
   Anime Library — Sakura UI wired to the live Flask JSON API (webapp.py).
   Replaces the prototype's data.js mock + setTimeout fakes. No build step.
   ============================================================ */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const prefersReduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches || document.body.classList.contains("motion-off");

const state = { filter: "all", detail: null, add: {}, dlTimer: null, syncTimer: null, dryRun: false, qbit: false };
let LIBRARY = [];  // normalized series, populated from /api/library

/* ============================================================
   API client
   ============================================================ */
async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch { /* non-JSON error page */ }
  if (!resp.ok) throw new Error(data.error || `${resp.status} ${resp.statusText}`);
  return data;
}

/* ============================================================
   Gradient fallback art (used only when no real cover exists)
   ============================================================ */
function hueFromColor(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
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

/* ============================================================
   Data adapter: /api/library response -> Sakura view model
   ============================================================ */
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
  return {
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
      const t = {
        num,
        episodes: se.episodes || [],
        missing: se.missing || [],
        downloading: se.downloading || [],
        next_airing: (se.info && se.info.next_airing_episode) || null,
        season_status: se.season_status || null,
      };
      t.total = ceiling(t);
      return t;
    }),
  };
}

/* ============================================================
   View-model helpers
   ============================================================ */
function statusLabel(st) {
  return ({
    RELEASING: ["Airing", "airing"], FINISHED: ["Complete", "finished"],
    NOT_YET_RELEASED: ["Upcoming", "finished"], CANCELLED: ["Cancelled", "finished"],
    HIATUS: ["Hiatus", "finished"],
  })[st] || ["", "finished"];
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

/* ============================================================
   Poster (real cover, gradient fallback)
   ============================================================ */
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

/* ============================================================
   Ticker
   ============================================================ */
function renderTicker() {
  const airing = LIBRARY.filter((s) => s.status === "RELEASING").length;
  const dl = LIBRARY.reduce((n, s) => n + seriesView(s).downloading, 0);
  const miss = LIBRARY.reduce((n, s) => n + seriesView(s).missing, 0);
  const qb = state.qbit ? "qBittorrent connected" : "qBittorrent offline";
  const seg = `<span>★ Anime Library<i>•</i>${LIBRARY.length} series on disk<i>•</i><em>${airing} airing now</em><i>•</i><span class="pk">${dl} downloading</span><i>•</i>${miss} episodes missing<i>•</i>Quality rule: trusted releases, 1080p preferred<i>•</i>${qb}<i>•</i></span>`;
  $("#ticker").innerHTML = `<div class="ticker-track">${seg}${seg}</div>`;
}

/* ============================================================
   Stats (count-up)
   ============================================================ */
function countUp(el, to, dur = 1000) {
  if (prefersReduced() || document.hidden) { el.textContent = to; return; }
  const start = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - start) / dur);
    const e = 1 - Math.pow(1 - t, 3);
    el.textContent = Math.round(e * to);
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function renderStats() {
  const series = LIBRARY.length;
  const miss = LIBRARY.reduce((n, s) => n + seriesView(s).missing, 0);
  const dl = LIBRARY.reduce((n, s) => n + seriesView(s).downloading, 0);
  const ok = LIBRARY.filter((s) => { const v = seriesView(s); return v.missing === 0 && v.downloading === 0; }).length;
  $("#stats").innerHTML = `
    <div class="stat"><div class="n" data-c="${series}">${series}</div><div class="l">Series on disk</div></div>
    <div class="stat missing"><div class="n"><b data-c="${miss}">${miss}</b></div><div class="l">Episodes missing</div></div>
    <div class="stat downloading"><div class="n" data-c="${dl}">${dl}</div><div class="l">Downloading now</div></div>
    <div class="stat uptodate"><div class="n"><span class="tick">✓</span><span data-c="${ok}">${ok}</span></div><div class="l">Up to date</div></div>`;
  $$("#stats [data-c]").forEach((el) => countUp(el, +el.dataset.c));
}

/* ============================================================
   Featured spread
   ============================================================ */
function renderFeatured() {
  const s = LIBRARY.find((x) => x.status === "RELEASING") || LIBRARY[0];
  if (!s) { $("#featured").innerHTML = ""; return; }
  const vw = seriesView(s);
  const eps = `${vw.haveTotal} / ${vw.epTotal} eps`;
  const kick = s.status === "RELEASING"
    ? `<span class="live-dot"></span>Now airing · featured pick`
    : `Featured pick`;
  $("#featured").innerHTML = `
    <div class="fcover" data-folder="${esc(s.folder)}">${posterHtml(s, { stamp: false, progress: true })}</div>
    <div class="fbody">
      <div class="kick">${kick}</div>
      <h2>${esc(s.english)}</h2>
      ${s.desc ? `<p>${esc(s.desc)}</p>` : ""}
      <div class="ftags">
        <span class="tag pink">Season ${vw.last.num}</span>
        ${s.genres.map((g) => `<span class="tag">${esc(g)}</span>`).join("")}
        ${s.score ? `<span class="tag score">★ ${s.score}%</span>` : ""}
        <span class="tag">${eps}</span>
      </div>
      <div class="factions">
        <button class="btn pink" data-act="sync-one" data-folder="${esc(s.folder)}">▸ Sync this series</button>
        <button class="btn" data-act="open-detail" data-folder="${esc(s.folder)}">Read more</button>
      </div>
    </div>`;
  $(".fcover").addEventListener("click", () => openDetail(s.folder));
}

/* ============================================================
   Filters + grid
   ============================================================ */
function renderFilters() {
  const counts = {
    all: LIBRARY.length,
    airing: LIBRARY.filter((s) => s.status === "RELEASING").length,
    missing: LIBRARY.filter((s) => seriesView(s).missing > 0).length,
    downloading: LIBRARY.filter((s) => seriesView(s).downloading > 0).length,
    complete: LIBRARY.filter((s) => { const v = seriesView(s); return !v.missing && !v.downloading; }).length,
  };
  const defs = [["all", "All"], ["airing", "Airing"], ["missing", "Missing"], ["downloading", "Downloading"], ["complete", "Complete"]];
  $("#filters").innerHTML = defs.map(([k, lbl]) =>
    `<button class="chip ${state.filter === k ? "on" : ""}" data-f="${k}">${lbl}<span class="c">${counts[k]}</span></button>`).join("");
  $$("#filters .chip").forEach((el) => el.addEventListener("click", () => { state.filter = el.dataset.f; renderFilters(); renderGrid(); }));
}
function renderGrid() {
  const list = LIBRARY.filter((s) => matchFilter(s, state.filter));
  $("#gridCount").textContent = `${list.length} series`;
  const grid = $("#grid");
  grid.innerHTML = list.map((s, i) => {
    const vw = seriesView(s);
    return `<div class="card reveal" data-folder="${esc(s.folder)}" style="animation-delay:${Math.min(i * 45, 620)}ms">
      ${posterHtml(s, { progress: true })}
      <div class="cap">
        <div class="t">${esc(s.english)}</div>
        <div class="s dotline"><i class="${vw.dot}"></i>${esc(vw.pill)} · ${esc(vw.sub)}</div>
      </div>
    </div>`;
  }).join("");
  $$("#grid .card").forEach((el) => el.addEventListener("click", () => openDetail(el.dataset.folder)));
}

function renderEmpty() {
  $("#stats").innerHTML = "";
  $("#featured").innerHTML = "";
  $("#filters").innerHTML = "";
  $("#gridCount").textContent = "";
  $("#grid").innerHTML = `<div class="empty-state">No series in your library yet.<br>Use <b>+ Add anime</b> to download your first show.</div>`;
  renderTicker();
}
function refreshAll() {
  if (!LIBRARY.length) { renderEmpty(); return; }
  renderTicker(); renderStats(); renderFeatured(); renderFilters(); renderGrid();
}

/* ============================================================
   Library load + qBittorrent pill
   ============================================================ */
function setQbit(connected) {
  state.qbit = !!connected;
  const pill = $("#qbit");
  if (pill) pill.classList.toggle("on", state.qbit);
}
async function loadLibrary() {
  let resp;
  try { resp = await api("/api/library"); }
  catch (e) { toast("Could not load library: " + e.message); return; }
  setQbit(resp.qbit_connected);
  LIBRARY = (resp.series || []).map(normalizeSeries);
  refreshAll();
  if (state.detail) openDetail(state.detail, true); // refresh an open spread in place
}

/* ============================================================
   Detail spread
   ============================================================ */
function seasonCeiling(se) {
  return Math.max(se.total || 0,
    se.episodes.length ? Math.max(...se.episodes) : 0,
    se.missing.length ? Math.max(...se.missing) : 0,
    se.downloading.length ? Math.max(...se.downloading) : 0, 0);
}
function openDetail(folder, keepOpen) {
  const s = LIBRARY.find((x) => x.folder === folder);
  if (!s) { if (!keepOpen) closeDetail(); return; }
  state.detail = folder;
  const vw = seriesView(s);
  const [stLabel] = statusLabel(s.status);

  const seasonsHtml = s.seasons.map((se) => {
    const ceil = seasonCeiling(se);
    const have = new Set(se.episodes), miss = new Set(se.missing), dl = new Set(se.downloading);
    let chips = "";
    for (let e = 1; e <= ceil; e++) {
      let c = "future";
      if (dl.has(e)) c = "dl"; else if (have.has(e)) c = "have"; else if (miss.has(e)) c = "miss";
      chips += `<div class="ep ${c}">${e}</div>`;
    }
    const statusBit = se.season_status ? ` · ${esc(se.season_status)}`
      : (se.next_airing ? ` · next ep ${se.next_airing} not aired` : "");
    const got = se.episodes.length;
    return `<div class="season-block">
      <div class="season-head"><h3>Season ${se.num}</h3>
        <span class="ss"><b>${got}</b> / ${se.total} downloaded${statusBit}</span></div>
      <div class="ep-grid">${chips || '<span class="ss">No episode data yet — run a sync.</span>'}</div>
    </div>`;
  }).join("");

  const bannerHtml = s.banner
    ? `<div class="spread-banner"><div class="bg-img" style="background-image:url('${esc(s.banner)}')"></div></div>`
    : `<div class="spread-banner"><div class="bg" style="${artStyle(s.hue)}"></div></div>`;
  const seriesPath = (s.path || "").replace(/[\\/]+Season\s*\d+\s*$/i, "");

  $("#spreadInner").innerHTML = `
    ${bannerHtml}
    <div class="spread-in">
      <div class="spread-head">
        <div class="spread-cover">${posterHtml(s, { stamp: false })}</div>
        <div class="spread-titles">
          <h1>${esc(s.english)}</h1>
          ${s.romaji !== s.english ? `<div class="alt">${esc(s.romaji)}</div>` : ""}
          <div class="spread-meta">
            ${stLabel ? `<span class="tag ${s.status === "RELEASING" ? "pink" : "fill"}">${esc(stLabel)}</span>` : ""}
            ${s.score ? `<span class="tag score">★ ${s.score}%</span>` : ""}
            ${s.genres.map((g) => `<span class="tag">${esc(g)}</span>`).join("")}
          </div>
        </div>
      </div>
      ${s.desc ? `<div class="spread-desc">${esc(s.desc)}</div>` : ""}
      ${seasonsHtml}
      <div class="legend">
        <span><i class="have"></i>Downloaded</span>
        <span><i class="dl"></i>Downloading</span>
        <span><i class="miss"></i>Missing</span>
        <span><i class="future"></i>Not aired</span>
      </div>
      <div class="spread-actions">
        <button class="btn pink" data-act="sync-one" data-folder="${esc(s.folder)}">▸ Sync this series</button>
        <button class="btn ghost" data-act="close-detail">Back to library</button>
        <div class="spacer"></div>
        <button class="btn" id="delBtn">Delete…</button>
      </div>
      <div class="delete-confirm hidden" id="delConfirm">
        <b>Delete “${esc(s.folder)}”?</b> Its torrents are removed from qBittorrent and the folder is erased from disk:
        <code>${esc(seriesPath)}</code>
        <div class="row">
          <button class="btn pink" id="delReal">Yes, delete everything</button>
          <button class="btn ghost" id="delCancel">Cancel</button>
        </div>
      </div>
    </div>`;

  if (!keepOpen) {
    const spread = $("#spread");
    spread.classList.remove("hidden");
    void spread.offsetWidth; // force reflow so the slide-in transition fires
    spread.classList.add("open");
    spread.scrollTop = 0;
    document.body.style.overflow = "hidden";
  }

  $("#delBtn").addEventListener("click", () => $("#delConfirm").classList.toggle("hidden"));
  $("#delCancel").addEventListener("click", () => $("#delConfirm").classList.add("hidden"));
  $("#delReal").addEventListener("click", () => deleteSeries(s.folder));
}
function closeDetail() {
  const spread = $("#spread");
  spread.classList.remove("open");
  document.body.style.overflow = "";
  state.detail = null;
  setTimeout(() => spread.classList.add("hidden"), 420);
}
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

/* ============================================================
   Sync (real: POST /api/sync + poll /api/sync/status)
   ============================================================ */
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

/* ============================================================
   Downloads strip (real: poll /api/downloads)
   ============================================================ */
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

/* ============================================================
   Add-anime flow (real AniList + Nyaa, with batch availability)
   ============================================================ */
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
      <input id="addSearch" type="text" placeholder="Search AniList… (e.g. Medaka Box)" autocomplete="off">
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
      ? `<img class="art-img" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover" src="${esc(c.cover_image)}" alt="" loading="lazy">`
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
    <div class="modal-note">Quality rule: only <b>trusted</b> Nyaa releases, <b>1080p preferred</b>. Lower resolutions and a complete box are used only when nothing better exists. The folder is created only if a source is found.</div>`;
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
      english: season.english || state.add.chosen.english,  // SEASON's English (batch matching)
      season_num: season.season_num, anilist_id: season.anilist_id,
      title: season.title, episodes: season.episodes, status: season.status,
      next_airing: season.next_airing, format: season.format,
    });
  } catch (e) { area.innerHTML = ""; toast(e.message); return; }
  if (state.add.season !== season) return; // user switched seasons meanwhile
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

/* ============================================================
   Toast
   ============================================================ */
let toastTimer;
function toast(msg, ok = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("ok", ok);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3600);
}

/* ============================================================
   Global action handler + init
   ============================================================ */
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
