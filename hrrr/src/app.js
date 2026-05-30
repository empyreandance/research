// HRRR Threshold Tool — count-map builder (spec 4 / 7 / 10.3).
//
// Define N ingredient conditions (parameter + operator + value). For every grid
// cell we count how many are met and render that count as graded colors. Click a
// cell to see each ingredient's value, threshold, pass/fail, and the limiting
// (failing) ingredients.

import { DATA_BASE_URL } from "./config.js";
import { buildGridMask, loadConus } from "./conus.js";
import { gridGeo, loadManifests, openForecastHour, readLevels, readVariable } from "./data.js";
import { OPS, countColorFn, rampColor, reproject } from "./render.js";
import {
  bundleAsBuiltin, bundleFromConditions, conditionsFromBundle, decodeFromHash,
  deleteUserPreset, downloadJson, encodeToHash, listUserPresets, loadBuiltins,
  parseImported, saveUserPreset,
} from "./presets.js";

// Two user-facing provenance labels: straight from HRRR, or calculated by us
// (both §4.2 derivations and §4.3 composites read as "calc" to the user).
const SOURCE_BADGE = { hrrr: "HRRR", derived: "calc", composite: "calc" };
const OP_LABELS = { ">=": "≥", ">": ">", "<=": "≤", "<": "<", "==": "=", "!=": "≠" };

// Display name for a condition, with the pressure level appended for 3D params.
const condName = (c) =>
  `${c.meta?.description || c.paramId}${c.levelVal != null ? ` @ ${c.levelVal} mb` : ""}`;

const els = Object.fromEntries(
  ["cycle-info", "fh", "fh-readout", "conditions", "cond-count", "add-cond",
   "floor", "floor-readout", "apply", "status", "map-legend", "inspect",
   "inspect-loc", "inspect-table", "inspect-close",
   "preset-select", "preset-load", "preset-save", "preset-share", "preset-delete",
   "preset-export", "preset-import", "preset-file", "authoring", "export-builtin",
   "hover", "hover-toggle", "export-image", "outlook-legend", "smooth", "smooth-readout",
   "extended-toggle", "extended-toggle-row",
  ].map((id) => [id, document.getElementById(id)]),
);

const state = {
  cycle: null, global: null, params: [], byId: {}, fhList: [0], forecastHour: 0,
  useExtended: localStorage.getItem("hrrr.useExtended") === "true",
  group: null, mapper: null, bbox: null, nx: 0, lat: null, lon: null,
  fieldCache: new Map(), groups: new Map(), last: null, builtins: [], conusMask: null, levels: [],
  smoothRadius: 0,
  lastClick: null, // {lng, lat} of the most recent click-to-inspect; null when closed
  lastHover: null, // {lng, lat} of the cursor's last position over the map; null when off-map
  outlookData: {},        // kind ("cat"/"torn"/...) and "cig-<kind>" -> currently-displayed GeoJSON
  outlookEnabled: {},     // kind -> true if the checkbox is on
  outlookActiveDay: {},   // kind -> SPC outlook day (1/2/3) currently shown
  outlookInWindow: {},    // kind -> true if the active day's VALID/EXPIRE covers the current FH's valid time
  outlookCache: new Map(),// URL -> in-flight or resolved Promise<GeoJSON>; dedupes fetches across day swaps
};

const map = new maplibregl.Map({
  container: "map", style: basemapStyle(), center: [-97, 38], zoom: 3.2,
  preserveDrawingBuffer: true, // required so the map canvas can be exported to PNG
});
// Arrow keys are reserved for FH scrubbing (setupArrowScrub). MapLibre's
// built-in keyboard pan would otherwise also fire after a map click.
map.keyboard.disable();

// Wire the resize handle and outlook toggles immediately (not inside the async
// init), so the sidebar is draggable AND the outlook checkboxes are responsive
// from the moment the page loads — before any data finishes loading.
setupPanelResize();
setupOutlooks();
setupArrowScrub();
setupSmooth();
setupBorders();

async function init() {
  try {
    const { global, cycle } = await loadManifests(DATA_BASE_URL, state.useExtended);
    state.global = global;
    state.cycle = cycle;
    // Include 3D pressure-level params; a per-condition level picker handles them.
    state.params = cycle.parameters.filter((p) => p.ui_visible);
    state.byId = Object.fromEntries(state.params.map((p) => [p.id, p]));
    state.fhList = cycle.forecast_hours;
    state.forecastHour = state.fhList[0];

    renderCycleInfo();
    syncExtendedToggle();
    setupForecastHourSlider();
    await openCurrentForecastHour(/*readGeo=*/ true);

    // CONUS mask (clip overlay + coverage % to the lower 48) — built once.
    try {
      const conusGeo = await loadConus();
      state.conusMask = buildGridMask(state.lon, state.lat);
      addConusOutline(conusGeo);
    } catch (e) {
      console.warn("CONUS mask unavailable:", e.message);
    }

    state.builtins = await loadBuiltins();
    const shared = decodeFromHash();
    if (shared?.thresholds?.length) {
      applyConditions(conditionsFromBundle(shared));
    }
    // Otherwise start with no ingredients — updateMap() shows "Add an ingredient".
    setupPresets();
    els["add-cond"].addEventListener("click", () => addConditionRow());
    els["apply"].addEventListener("click", updateMap);
    els["export-image"].addEventListener("click", exportImage);
    els["floor"].addEventListener("input", () => {
      els["floor-readout"].textContent = els["floor"].value;
      if (state.last) renderCount(); // re-color without recomputing
    });
    els["inspect-close"].addEventListener("click", () => {
      els["inspect"].hidden = true;
      state.lastClick = null;
      hideInspectMarker();
    });
    document.getElementById("ts-open").addEventListener("click", () => openTimeSeries(state.lastClick));
    els["extended-toggle"].addEventListener("change", async (e) => {
      state.useExtended = e.target.checked;
      localStorage.setItem("hrrr.useExtended", String(state.useExtended));
      await switchCycle();
    });
    map.on("click", onMapClick);
    map.on("moveend", () => { if (state.last) updateWindowStat(); });
    setupHover();
    updateMap();
  } catch (e) {
    els["cycle-info"].textContent = `Could not load data: ${e.message}`;
  }
}

// Drag #panel-resize to set the sidebar width; persisted in localStorage so it
// sticks between visits. The map canvas is resized to match (live during drag,
// throttled to one resize per frame).
function setupPanelResize() {
  const handle = document.getElementById("panel-resize");
  if (!handle) return;
  const root = document.documentElement;
  const KEY = "hrrr.panelWidth";
  const clamp = (w) => Math.min(680, Math.max(280, w));
  const saved = parseInt(localStorage.getItem(KEY) || "", 10);
  if (saved) root.style.setProperty("--panel-w", clamp(saved) + "px");

  let dragging = false, raf = 0;
  const xOf = (e) => (e.touches ? e.touches[0].clientX : e.clientX);
  const onMove = (e) => {
    if (!dragging) return;
    root.style.setProperty("--panel-w", clamp(xOf(e)) + "px");
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; map.resize(); });
    if (e.cancelable) e.preventDefault();
  };
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("dragging");
    document.body.style.userSelect = "";
    const w = parseInt(getComputedStyle(document.getElementById("panel")).width, 10);
    localStorage.setItem(KEY, String(w));
    map.resize();
  };
  const start = (e) => {
    dragging = true;
    handle.classList.add("dragging");
    document.body.style.userSelect = "none";
    if (e.cancelable) e.preventDefault();
  };
  handle.addEventListener("mousedown", start);
  handle.addEventListener("touchstart", start, { passive: false });
  window.addEventListener("mousemove", onMove);
  window.addEventListener("touchmove", onMove, { passive: false });
  window.addEventListener("mouseup", stop);
  window.addEventListener("touchend", stop);
}

function setupForecastHourSlider() {
  els["fh"].max = String(state.fhList.length - 1);
  els["fh"].value = "0";
  els["fh-readout"].textContent = `f${String(state.forecastHour).padStart(2, "0")}`;
  els["fh"].addEventListener("change", async () => {
    state.forecastHour = state.fhList[Number(els["fh"].value)];
    els["fh-readout"].textContent = `f${String(state.forecastHour).padStart(2, "0")}`;
    renderCycleInfo();
    await openCurrentForecastHour(false);
    await updateMap();
    // If the inspect panel and/or hover readout are showing a position, redraw
    // them against the new FH's fields so the values track the slider.
    if (state.lastClick) renderInspect(state.lastClick);
    if (state.lastHover) renderHover(state.lastHover);
    refreshOutlooksForFH();
  });
}

// Show/hide the "Extended forecast (48 hr)" checkbox based on whether the
// global manifest actually carries an extended pointer. Until the updated
// ingest worker has run at least one extended cycle, the checkbox stays
// hidden (no point exposing a toggle that can't switch anywhere).
function syncExtendedToggle() {
  const row = els["extended-toggle-row"];
  const cb = els["extended-toggle"];
  if (!row || !cb) return;
  const available = !!state.global?.current_extended_cycle_manifest_key;
  row.hidden = !available;
  cb.checked = state.useExtended && available;
}

// Swap the active cycle to whichever pointer state.useExtended now selects.
// Tears down everything tied to the OLD cycle (field/group caches, last
// computed map, inspect/hover positions) so nothing leaks across the swap,
// then re-inits cycle metadata + FH slider + map. Active outlooks re-pick
// the right day for the new FH valid time via refreshOutlooksForFH.
async function switchCycle() {
  els["status"].textContent = "Switching cycle…";
  state.fieldCache.clear();
  state.groups.clear();
  state.last = null;
  state.lastClick = null;
  state.lastHover = null;
  els["inspect"].hidden = true;
  els["hover"].hidden = true;
  hideInspectMarker();

  const { global, cycle } = await loadManifests(DATA_BASE_URL, state.useExtended);
  state.global = global;
  state.cycle = cycle;
  state.params = cycle.parameters.filter((p) => p.ui_visible);
  state.byId = Object.fromEntries(state.params.map((p) => [p.id, p]));
  state.fhList = cycle.forecast_hours;
  state.forecastHour = state.fhList[0];

  els["fh"].max = String(state.fhList.length - 1);
  els["fh"].value = "0";
  els["fh-readout"].textContent = `f${String(state.forecastHour).padStart(2, "0")}`;
  renderCycleInfo();
  syncExtendedToggle();

  await openCurrentForecastHour(false);
  await updateMap();
  refreshOutlooksForFH();
}

// Sidebar header line: cycle id + parameter count, plus a sub-line with the
// currently selected FH and its UTC valid time (re-rendered on every FH change).
function renderCycleInfo() {
  const c = state.cycle;
  if (!c) return;
  const fh = `f${String(state.forecastHour).padStart(2, "0")}`;
  els["cycle-info"].innerHTML =
    `Cycle ${c.cycle_id} · ${state.params.length} parameters` +
    `<br>${fh} valid ${validTimeUTC()}`;
}

async function openCurrentForecastHour(readGeo) {
  // No cache clear: groups + fields persist across FH switches, so revisited
  // and pre-loaded forecast hours are instant.
  state.group = await getGroup(state.forecastHour);
  if (readGeo) {
    const geo = await gridGeo(state.group);
    state.mapper = geo.mapper;
    state.bbox = geo.bbox;
    state.nx = geo.nx;
    state.lat = geo.lat;
    state.lon = geo.lon;
    state.levels = await readLevels(state.group);
  }
}

// --- presets ---------------------------------------------------------------

// ─── SPC convective + WPC ERO outlook overlays ────────────────────────────
// Fetched straight from SPC (its server is CORS-open), and the GeoJSON carries
// its own official fill/stroke colors + labels — so we style the layers and
// build the legend directly from the data. "<2%" base areas have empty fill and
// are filtered out.
//
// Multi-day awareness: each outlook GeoJSON has VALID/EXPIRE properties (UTC
// YYYYMMDDHHMM) marking the window it covers. When the user scrubs forecast
// hours, the active outlook auto-switches to the day whose window contains the
// current FH's valid time. SPC publishes most products for Day 1+2, cat through
// Day 3; WPC ERO is Day 1 only here because the GitHub Actions mirror (see
// .github/workflows/wpc-ero-mirror.yml) only fetches Day 1.
const SPC_BASE = "https://www.spc.noaa.gov/products/outlook/";
const OUTLOOK_NAMES = { cat: "Categorical", torn: "Tornado", wind: "Wind", hail: "Hail", ero: "Excessive rainfall" };
const OUTLOOK_DAYS = { cat: [1, 2, 3], torn: [1, 2], wind: [1, 2], hail: [1, 2], ero: [1, 2, 3] };
const ERO_COLORS = {
  Marginal: { fill: "#66A366", stroke: "#2E7D32" },
  Slight:   { fill: "#E8E84A", stroke: "#B0B000" },
  Moderate: { fill: "#E0782E", stroke: "#A8480F" },
  High:     { fill: "#CC44CC", stroke: "#800080" },
};
function outlookUrl(kind, day) {
  if (kind === "ero") return `${DATA_BASE_URL}/outlooks/wpc_ero_day${day}.geojson`;
  return `${SPC_BASE}day${day}otlk_${kind}.lyr.geojson`;
}
const cigUrl = (kind, day) => `${SPC_BASE}day${day}otlk_cig${kind}.lyr.geojson`;
function colorizeERO(gj) {
  for (const f of gj.features || []) {
    const p = f.properties || (f.properties = {});
    const c = ERO_COLORS[String(p.OUTLOOK || "").split(/[ (]/)[0]];
    if (c) { p.fill = c.fill; p.stroke = c.stroke; p.LABEL2 = p.OUTLOOK; }
    else { p.fill = ""; p.stroke = ""; }
  }
}

// Build (once) one of SPC's three Conditional Intensity Group hatch patterns:
//   CIG1 = sparse dashed diagonals
//   CIG2 = solid diagonals
//   CIG3 = cross-hatch (both diagonals)
// Always black on transparent — drawn on top of the probability fill, exactly
// mirroring SPC's March-2026 CIG rendering (which replaced the old SIGN label).
function ensureCigPattern(level) {
  const name = `cig${level}`;
  if (map.hasImage(name)) return name;
  const s = 10;
  const cv = document.createElement("canvas");
  cv.width = cv.height = s;
  const ctx = cv.getContext("2d");
  ctx.strokeStyle = "#000";
  ctx.lineWidth = 1.3;
  ctx.lineCap = "butt";
  const drawDiag = (slope, dash) => {
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    if (slope > 0) {
      for (let i = -1; i <= 1; i++) { ctx.moveTo(0, i * s); ctx.lineTo(s, (i + 1) * s); }
    } else {
      for (let i = 0; i <= 2; i++) { ctx.moveTo(0, i * s); ctx.lineTo(s, (i - 1) * s); }
    }
    ctx.stroke();
  };
  // SPC: CIG1 = forward-slash dashed, CIG2 = backslash solid, CIG3 = solid cross.
  if (level === "1") drawDiag(-1, [3, 3]);
  else if (level === "2") drawDiag(+1, null);
  else if (level === "3") { drawDiag(+1, null); drawDiag(-1, null); }
  const img = ctx.getImageData(0, 0, s, s);
  map.addImage(name, { width: s, height: s, data: new Uint8Array(img.data.buffer) });
  return name;
}

function setupOutlooks() {
  document.querySelectorAll(".otlk").forEach((cb) =>
    cb.addEventListener("change", () => toggleOutlook(cb.dataset.otlk, cb.checked, cb)));
}

// Arrow keys scrub forecast hours from anywhere on the page. The only skip is
// when focus is on the FH slider itself — its native arrow behavior already
// scrubs, so handling here too would double-fire. Other inputs/selects lose
// their native arrow behavior; that's deliberate per user preference.
function setupArrowScrub() {
  document.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const fh = els["fh"];
    if (!fh) return;
    if (document.activeElement === fh) return; // native arrow handles it
    const cur = Number(fh.value);
    const max = Number(fh.max);
    const next = e.key === "ArrowLeft" ? Math.max(0, cur - 1) : Math.min(max, cur + 1);
    if (next !== cur) {
      fh.value = String(next);
      fh.dispatchEvent(new Event("change"));
      e.preventDefault();
    }
  });
}

// Parse SPC's YYYYMMDDHHMM date strings to a JS UTC ms timestamp.
function spcDateMs(s) {
  if (typeof s !== "string" || s.length < 12) return NaN;
  return Date.UTC(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8),
                  +s.slice(8, 10), +s.slice(10, 12));
}

// All features in a single SPC outlook share VALID/EXPIRE (the day's window);
// take the first feature with parseable dates.
function outlookWindow(gj) {
  for (const f of (gj.features || [])) {
    const v = spcDateMs(f.properties?.VALID);
    const e = spcDateMs(f.properties?.EXPIRE);
    if (Number.isFinite(v) && Number.isFinite(e)) return { valid: v, expire: e };
  }
  return { valid: NaN, expire: NaN };
}

// Current FH's UTC valid time as ms. Mirrors validTimeUTC() but returns ms.
function validTimeMs() {
  const id = state.cycle.cycle_id;
  const init = Date.UTC(+id.slice(0, 4), +id.slice(4, 6) - 1, +id.slice(6, 8), +id.slice(8, 10));
  return init + state.forecastHour * 3600 * 1000;
}

// Cached fetch keyed by URL: every (kind, day) URL ends up in the same Map so
// repeat scrubs across the same day boundary don't re-download.
function fetchOutlookData(url, kind) {
  if (!state.outlookCache.has(url)) {
    state.outlookCache.set(url, (async () => {
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const gj = await r.json();
      if (kind === "ero") colorizeERO(gj);
      return gj;
    })());
  }
  return state.outlookCache.get(url);
}

// Pick the day whose VALID/EXPIRE window contains validMs. Falls back to the
// first day that loads at all (kept with inWindow=false so the legend can flag
// the stale display rather than silently showing the wrong window).
async function pickOutlookForFH(kind, validMs) {
  let fallback = null;
  for (const day of OUTLOOK_DAYS[kind] || [1]) {
    let gj;
    try { gj = await fetchOutlookData(outlookUrl(kind, day), kind); }
    catch { continue; } // skip days that 404 (e.g., issuance gap)
    const w = outlookWindow(gj);
    if (Number.isFinite(w.valid) && validMs >= w.valid && validMs < w.expire) {
      return { day, gj, inWindow: true };
    }
    if (!fallback) fallback = { day, gj, inWindow: false };
  }
  return fallback;
}

async function toggleOutlook(kind, on, cb) {
  if (!on) {
    state.outlookEnabled[kind] = false;
    removeOutlookLayers(kind);
    delete state.outlookData[kind];
    delete state.outlookData[`cig-${kind}`];
    delete state.outlookActiveDay[kind];
    delete state.outlookInWindow[kind];
    renderOutlookLegend();
    return;
  }
  state.outlookEnabled[kind] = true;
  await applyOutlook(kind, cb);
}

// Add/update the outlook for `kind` against the current FH. Called on toggle
// (with cb so failure can untick the checkbox) and on every FH change. If a
// source already exists, it's a setData() update — no flicker, no layer reset.
async function applyOutlook(kind, cb = null) {
  try {
    const picked = await pickOutlookForFH(kind, validTimeMs());
    if (!picked) throw new Error("no outlook data available");
    const src = `otlk-${kind}`, fillId = `${src}-fill`, lineId = `${src}-line`;
    // Outlooks render UNDER the HRRR count overlay (and under the conus outline
    // before that layer exists), so the ingredient map sits on top.
    const before = map.getLayer("count") ? "count"
                 : map.getLayer("conus-outline") ? "conus-outline"
                 : undefined;
    if (map.getSource(src)) {
      map.getSource(src).setData(picked.gj);
    } else {
      map.addSource(src, { type: "geojson", data: picked.gj });
      // Probability/categorical: SPC's authentic colored fills, dialed to low
      // opacity so the ingredient count-map still reads through.
      map.addLayer({
        id: fillId, type: "fill", source: src,
        // Exclude CIG features — they live in the separate CIG layer and render
        // as hatching only, on top of whatever color is already underneath.
        filter: ["all",
          ["!=", ["get", "fill"], ""],
          ["!", ["in", ["get", "LABEL"], ["literal", ["CIG1", "CIG2", "CIG3"]]]],
        ],
        paint: { "fill-color": ["get", "fill"], "fill-opacity": 0.3 },
      }, before);
      map.addLayer({
        id: lineId, type: "line", source: src,
        filter: ["all",
          ["!=", ["get", "stroke"], ""],
          ["!", ["in", ["get", "LABEL"], ["literal", ["CIG1", "CIG2", "CIG3"]]]],
        ],
        paint: { "line-color": ["get", "stroke"], "line-width": 1.5 },
      }, before);
    }
    state.outlookData[kind] = picked.gj;
    state.outlookActiveDay[kind] = picked.day;
    state.outlookInWindow[kind] = picked.inWindow;

    // CIG layer (March-2026: CIG1 dashed / CIG2 solid / CIG3 cross-hatch, all
    // in black) — follows the same day as the prob outlook for torn/wind/hail.
    if (kind === "torn" || kind === "wind" || kind === "hail") {
      await applyCigOutlook(kind, picked.day, before);
    }
    renderOutlookLegend();
  } catch (e) {
    if (cb) cb.checked = false;
    state.outlookEnabled[kind] = false;
    els["status"].textContent = `Couldn't load ${OUTLOOK_NAMES[kind] || kind} outlook (${e.message}).`;
  }
}

async function applyCigOutlook(kind, day, before) {
  const cigSrc = `otlk-cig-${kind}`, cigFillId = `${cigSrc}-fill`, cigLineId = `${cigSrc}-line`;
  let cgj;
  try { cgj = await fetchOutlookData(cigUrl(kind, day), "cig"); }
  catch { return; } // CIG may legitimately be missing for some day/kind; silent skip
  for (const f of cgj.features || []) {
    const p = f.properties || {};
    const lvl = String(p.LABEL || "").replace(/^CIG/, ""); // "CIG2" -> "2"
    if (lvl === "1" || lvl === "2" || lvl === "3") {
      ensureCigPattern(lvl);
      p._cigpat = `cig${lvl}`;
    }
  }
  if (map.getSource(cigSrc)) {
    map.getSource(cigSrc).setData(cgj);
  } else {
    map.addSource(cigSrc, { type: "geojson", data: cgj });
    map.addLayer({
      id: cigFillId, type: "fill", source: cigSrc,
      filter: ["has", "_cigpat"],
      paint: { "fill-pattern": ["get", "_cigpat"] },
    }, before);
    map.addLayer({
      id: cigLineId, type: "line", source: cigSrc,
      filter: ["has", "_cigpat"],
      paint: { "line-color": "#000", "line-width": 1.2 },
    }, before);
  }
  state.outlookData[`cig-${kind}`] = cgj;
}

function removeOutlookLayers(kind) {
  for (const src of [`otlk-${kind}`, `otlk-cig-${kind}`]) {
    for (const lid of [`${src}-fill`, `${src}-line`]) {
      if (map.getLayer(lid)) map.removeLayer(lid);
    }
    if (map.getSource(src)) map.removeSource(src);
  }
}

// Re-apply every enabled outlook after a forecast-hour change. Sources already
// exist, so each call collapses to a setData() — no flicker, no layer reset.
async function refreshOutlooksForFH() {
  const kinds = Object.keys(state.outlookEnabled).filter((k) => state.outlookEnabled[k]);
  await Promise.all(kinds.map((k) => applyOutlook(k)));
}

function renderOutlookLegend() {
  const el = els["outlook-legend"];
  if (!el) return;
  // Dedup by LABEL2/LABEL (not fill), so CIG1/2/3 — which share a placeholder
  // fill — appear as distinct rows. CIG entries get a hatch-styled swatch.
  const hatchBg = "repeating-linear-gradient(45deg,#000 0 1.5px,transparent 1.5px 5px)";
  let html = "";
  for (const kind of Object.keys(state.outlookData)) {
    const isCig = kind.startsWith("cig-");
    const seen = new Set(), rows = [];
    for (const f of state.outlookData[kind].features || []) {
      const p = f.properties || {};
      // In a probability kind, hide CIG features — the dedicated cig-kind layer
      // and its sub-legend cover them; here they'd just duplicate as a gray row.
      if (!isCig && /^CIG[123]$/.test(p.LABEL || "")) continue;
      const key = p.LABEL2 || p.LABEL || p.fill;
      if (!key || seen.has(key)) continue;
      if (!p.fill && !p._cigpat) continue; // skip empty placeholders
      seen.add(key);
      const bg = p._cigpat ? hatchBg : (p.fill || "#888");
      const border = p.stroke || "#888";
      rows.push(`<div class="legend-row"><span class="sw" style="background:${bg};border-color:${border}"></span>${p.LABEL2 || p.LABEL || ""}</div>`);
    }
    if (rows.length) {
      const baseKind = isCig ? kind.slice(4) : kind;
      const source = baseKind === "ero" ? "WPC" : "SPC";
      const cigTag = isCig ? " CIG" : "";
      const day = state.outlookActiveDay[baseKind] ?? 1;
      const warn = state.outlookInWindow[baseKind] === false
        ? ` <span class="otlk-warn">(outside this FH's window)</span>` : "";
      html += `<div class="otlk-sub">${source} ${OUTLOOK_NAMES[baseKind] || baseKind}${cigTag} · Day ${day}${warn}</div>${rows.join("")}`;
    }
  }
  el.innerHTML = html;
  el.hidden = !html;
}

function setupPresets() {
  refreshPresetSelect();
  els["preset-load"].addEventListener("click", loadSelectedPreset);
  els["preset-save"].addEventListener("click", () => {
    const name = prompt("Name this preset:");
    if (!name) return;
    saveUserPreset(bundleFromConditions(name, readConditions()));
    // Auto-download the whole library as a backup (spec 6.4 layer 2).
    downloadJson({ schema_version: "1.0", presets: listUserPresets() }, "hrrr-presets.json");
    refreshPresetSelect();
    els["status"].textContent = `Saved “${name}” (backup downloaded to your Downloads).`;
  });
  els["preset-share"].addEventListener("click", async () => {
    const preset = bundleFromConditions("shared", readConditions());
    const url = location.origin + location.pathname + encodeToHash(preset);
    try {
      await navigator.clipboard.writeText(url);
      els["status"].textContent = "Share link copied to clipboard.";
    } catch {
      location.hash = encodeToHash(preset);
      els["status"].textContent = "Share link is in the address bar — copy it.";
    }
  });
  els["preset-export"].addEventListener("click", () => {
    downloadJson({ schema_version: "1.0", presets: listUserPresets() }, "hrrr-presets.json");
    els["status"].textContent = "Exported your presets to hrrr-presets.json.";
  });
  els["preset-import"].addEventListener("click", () => els["preset-file"].click());
  els["preset-file"].addEventListener("change", importPresetFile);
  els["preset-delete"].addEventListener("click", () => {
    const sel = els["preset-select"].value;
    if (sel.startsWith("u:") && confirm("Delete this saved preset?")) {
      deleteUserPreset(sel.slice(2));
      refreshPresetSelect();
    }
  });

  // Authoring mode (spec 6.3): enabled by visiting /forge once.
  if (localStorage.getItem("authoringMode") === "1") {
    els["authoring"].hidden = false;
    els["export-builtin"].addEventListener("click", () => {
      const name = prompt("Built-in preset name:");
      if (!name) return;
      const preset = bundleAsBuiltin(name, readConditions());
      downloadJson(preset, `${preset.id}.json`);
      els["status"].textContent = `Exported ${preset.id}.json — add "${preset.id}" to presets/index.json.`;
    });
  }
}

function refreshPresetSelect() {
  const sel = els["preset-select"];
  sel.innerHTML = '<option value="">— choose a preset —</option>';
  const group = (label, presets, prefix) => {
    if (!presets.length) return;
    const og = document.createElement("optgroup");
    og.label = label;
    for (const p of presets) {
      const o = document.createElement("option");
      o.value = prefix + p.id;
      o.textContent = p.name;
      og.appendChild(o);
    }
    sel.appendChild(og);
  };
  group("Built-in", state.builtins, "b:");
  group("My presets", listUserPresets(), "u:");
}

function findPreset(value) {
  if (value.startsWith("b:")) return state.builtins.find((p) => p.id === value.slice(2));
  if (value.startsWith("u:")) return listUserPresets().find((p) => p.id === value.slice(2));
  return null;
}

// Apply a preset's conditions, skipping any parameter this cycle doesn't have
// (defensive: a cycle/catalog may not include every parameter a preset references).
function applyConditions(conds) {
  els["conditions"].innerHTML = "";
  const usable = conds.filter((c) => state.byId[c.paramId]);
  usable.forEach((c) => addConditionRow(c));
  if (!usable.length) addConditionRow();
  refreshCondMeta();
  const skipped = conds.length - usable.length;
  if (skipped) els["status"].textContent = `Loaded preset (${skipped} ingredient(s) not in this cycle were skipped).`;
}

function loadSelectedPreset() {
  const p = findPreset(els["preset-select"].value);
  if (!p) return;
  applyConditions(conditionsFromBundle(p));
  updateMap();
}

async function importPresetFile(e) {
  const file = e.target.files?.[0];
  if (!file) return;
  try {
    const presets = parseImported(await file.text());
    presets.forEach((p) => saveUserPreset({ ...p, origin: "user" }));
    refreshPresetSelect();
    els["status"].textContent = `Imported ${presets.length} preset(s).`;
  } catch (err) {
    els["status"].textContent = `Import failed: ${err.message}`;
  }
  e.target.value = ""; // allow re-importing the same file
}

// --- condition rows --------------------------------------------------------

// Category display order in the parameter dropdown (any extra categories follow).
const CATEGORY_ORDER = [
  "Instability (CAPE/CIN)", "Kinematic (wind/shear)", "Convective / radar",
  "Moisture", "Temperature", "Levels & heights", "Cloud cover", "Precipitation",
  "Radiation", "Smoke & aerosols", "Soil", "Surface", "Composite indices", "Other",
];

function buildParamSelect(selectedId) {
  const sel = document.createElement("select");
  sel.className = "param";
  const byCat = {};
  for (const p of state.params) (byCat[p.category || "Other"] ??= []).push(p);
  const cats = [...CATEGORY_ORDER.filter((c) => byCat[c]),
    ...Object.keys(byCat).filter((c) => !CATEGORY_ORDER.includes(c))];
  for (const cat of cats) {
    const og = document.createElement("optgroup");
    og.label = cat;
    const params = byCat[cat].sort((a, b) =>
      (a.description || a.id).localeCompare(b.description || b.id));
    for (const p of params) {
      const o = document.createElement("option");
      o.value = p.id;
      // Clean name (no [HRRR]/[calc] prefix); provenance shown by the box outline.
      o.textContent = p.description || p.id;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
  if (selectedId) sel.value = selectedId;
  paintParamSource(sel);
  return sel;
}

// Outline the parameter box blue (HRRR) or purple (calculated) to show provenance.
function paintParamSource(sel) {
  const isHrrr = state.byId[sel.value]?.source === "hrrr";
  sel.classList.toggle("src-hrrr", isHrrr);
  sel.classList.toggle("src-calc", !isHrrr);
}

const defaultParamId = () =>
  (state.params.find((p) => !p.is_3d) ?? state.params[0])?.id;

function addConditionRow(preset) {
  const row = document.createElement("div");
  row.className = "cond";
  const sel = buildParamSelect(preset?.paramId ?? defaultParamId());
  const op = document.createElement("select");
  op.className = "op";
  for (const k of [">=", ">", "<=", "<", "==", "!="]) {
    const o = document.createElement("option");
    o.value = k; o.textContent = OP_LABELS[k];
    op.appendChild(o);
  }
  op.value = preset?.op ?? ">=";
  const val = document.createElement("input");
  val.type = "number"; val.step = "any"; val.value = preset?.value ?? 0;
  const rm = document.createElement("button");
  rm.className = "link"; rm.textContent = "✕"; rm.title = "Remove";
  // Full-width sub-row: pressure-level picker, shown only for 3D parameters.
  const level = document.createElement("select");
  level.className = "level";
  for (const idx of levelDisplayOrder()) {
    const o = document.createElement("option");
    o.value = String(idx); o.textContent = `${state.levels[idx]} mb`;
    level.appendChild(o);
  }
  rm.addEventListener("click", () => { row.remove(); refreshCondMeta(); });
  sel.addEventListener("change", () => { paintParamSource(sel); syncLevelRow(sel, level); refreshCondMeta(); });

  row.append(sel, op, val, rm, level);
  els["conditions"].appendChild(row);
  syncLevelRow(sel, level, preset?.level);
  refreshCondMeta();
}

// Level option indices, ordered high pressure (low altitude) first for display.
function levelDisplayOrder() {
  return state.levels.map((_, i) => i).sort((a, b) => state.levels[b] - state.levels[a]);
}

// Show/hide the level picker based on whether the selected param is 3D, and
// default it to 500 mb (or the preset's level).
function syncLevelRow(sel, level, presetLevel) {
  const is3d = state.byId[sel.value]?.is_3d && state.levels.length;
  level.hidden = !is3d;
  if (is3d && presetLevel != null) {
    const idx = state.levels.indexOf(presetLevel);
    if (idx >= 0) level.value = String(idx);
  } else if (is3d && !level.value) {
    const i500 = state.levels.indexOf(500);
    level.value = String(i500 >= 0 ? i500 : levelDisplayOrder()[0]);
  }
}

function readConditions() {
  return [...els["conditions"].querySelectorAll(".cond")].map((row) => {
    const selects = row.querySelectorAll("select"); // [param, op, level]
    const meta = state.byId[selects[0].value];
    const levelSel = row.querySelector("select.level");
    const levelIdx = meta?.is_3d && !levelSel.hidden ? Number(levelSel.value) : null;
    return {
      paramId: selects[0].value,
      op: selects[1].value,
      value: parseFloat(row.querySelector("input").value),
      levelIdx,
      levelVal: levelIdx != null ? state.levels[levelIdx] : null,
      meta,
    };
  });
}

function refreshCondMeta() {
  const n = els["conditions"].querySelectorAll(".cond").length;
  els["cond-count"].textContent = n ? `(${n})` : "";
  els["floor"].max = String(Math.max(1, n));
  if (Number(els["floor"].value) > n) els["floor"].value = String(n);
  els["floor-readout"].textContent = els["floor"].value;
  // show units next to each row's value
  for (const row of els["conditions"].querySelectorAll(".cond")) {
    const p = state.byId[row.querySelector("select").value];
    row.querySelector("input").title = p ? `${p.units} · ${p.source}` : "";
  }
}

// --- count + render --------------------------------------------------------

// Cache the zarr group per forecast hour so we open each FH at most once.
function getGroup(fh) {
  if (!state.groups.has(fh)) {
    state.groups.set(fh, openForecastHour(DATA_BASE_URL, state.cycle.cycle_id, fh));
  }
  return state.groups.get(fh);
}

// Cache reads per (paramId, fh, levelIdx). Storing the in-flight promise dedupes
// concurrent calls (e.g. an updateMap fetch racing with a preload).
async function getField(paramId, levelIdx = null, fh = state.forecastHour) {
  const key = `${paramId}@${levelIdx ?? ""}@${fh}`;
  if (!state.fieldCache.has(key)) {
    state.fieldCache.set(key, (async () => {
      const group = await getGroup(fh);
      return readVariable(group, paramId, levelIdx);
    })());
  }
  return state.fieldCache.get(key);
}

// Background-load *every* forecast hour in the cycle for the active ingredients,
// so any scrubbing destination is already in memory. Fire-and-forget; cached
// promises dedupe duplicates, and the browser caps concurrency on its own.
function preloadAll() {
  const conds = readConditions().filter((c) => c.paramId);
  for (const fh of state.fhList) {
    if (fh === state.forecastHour) continue;
    getGroup(fh).catch(() => {});
    for (const c of conds) getField(c.paramId, c.levelIdx, fh).catch(() => {});
  }
}

async function updateMap() {
  const conds = readConditions();
  if (!conds.length) {
    els["status"].textContent = "Add an ingredient to begin.";
    return;
  }
  els["status"].textContent = "Reading data…";
  try {
    const fields = await Promise.all(conds.map((c) => getField(c.paramId, c.levelIdx)));
    const { data: d0, shape } = fields[0];
    const count = new Uint8Array(d0.length);
    for (let c = 0; c < conds.length; c++) {
      const test = OPS[conds[c].op];
      const t = conds[c].value;
      const data = fields[c].data;
      for (let k = 0; k < data.length; k++) {
        const v = data[k];
        if (Number.isFinite(v) && test(v, t)) count[k]++;
      }
    }
    state.last = { conds, fields, count, shape, n: conds.length };
    renderCount();
    preloadAll();
  } catch (e) {
    els["status"].textContent = `Error: ${e.message}`;
  }
}

function renderCount() {
  const { count, shape, n } = state.last;
  const [ny, nx] = shape;
  const floor = Math.min(Number(els["floor"].value), n);
  const r = Number(state.smoothRadius || 0);
  // Smooth only affects what's painted on the map; the raw count drives the
  // % window stat and the click-inspect drill-down (which read state.last).
  const painted = r > 0 ? maxFilterSeparable(count, ny, nx, r) : count;
  const { dataUrl, coordinates } = reproject(
    { data: painted, shape }, countColorFn(floor, n), state.mapper, state.bbox, state.conusMask);
  showOverlay(dataUrl, coordinates);
  updateWindowStat();
  renderLegend(floor, n);
}

// Separable horizontal+vertical max filter over a Uint8 grid. O(n*m*(rx+ry))
// — fast enough at HRRR resolution for any interactive radius (1-3 cells).
function maxFilterSeparable(arr, ny, nx, r) {
  if (r <= 0) return arr;
  const tmp = new Uint8Array(arr.length);
  const out = new Uint8Array(arr.length);
  for (let i = 0; i < ny; i++) {
    const row = i * nx;
    for (let j = 0; j < nx; j++) {
      const j0 = Math.max(0, j - r), j1 = Math.min(nx - 1, j + r);
      let m = 0;
      for (let jj = j0; jj <= j1; jj++) { const v = arr[row + jj]; if (v > m) m = v; }
      tmp[row + j] = m;
    }
  }
  for (let i = 0; i < ny; i++) {
    const i0 = Math.max(0, i - r), i1 = Math.min(ny - 1, i + r);
    for (let j = 0; j < nx; j++) {
      let m = 0;
      for (let ii = i0; ii <= i1; ii++) { const v = tmp[ii * nx + j]; if (v > m) m = v; }
      out[i * nx + j] = m;
    }
  }
  return out;
}

// Lazy-loaded reference border layers (county + NWS CWA). Files live in
// hrrr/data/ so they're served same-origin — no CORS, no external dependency.
const BORDER_LAYERS = {
  county: { url: "data/counties.geojson", color: "#777", width: 0.5, opacity: 0.55 },
  cwa:    { url: "data/cwa.geojson",      color: "#1b365d", width: 1.4, opacity: 0.75 },
};

function setupBorders() {
  document.querySelectorAll(".brd").forEach((cb) =>
    cb.addEventListener("change", () => toggleBorder(cb.dataset.brd, cb.checked, cb)));
}

async function toggleBorder(kind, on, cb) {
  const cfg = BORDER_LAYERS[kind];
  if (!cfg) return;
  const src = `brd-${kind}`, lineId = `${src}-line`;
  if (!on) {
    if (map.getLayer(lineId)) map.removeLayer(lineId);
    if (map.getSource(src)) map.removeSource(src);
    return;
  }
  try {
    if (!map.getSource(src)) {
      const r = await fetch(cfg.url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      map.addSource(src, { type: "geojson", data: await r.json() });
    }
    // Render just below the conus state outline (which stays on top) and above
    // the HRRR count overlay.
    const before = map.getLayer("conus-outline") ? "conus-outline" : undefined;
    map.addLayer({
      id: lineId, type: "line", source: src,
      paint: { "line-color": cfg.color, "line-width": cfg.width, "line-opacity": cfg.opacity },
    }, before);
  } catch (e) {
    if (cb) cb.checked = false;
    els["status"].textContent = `Couldn't load ${kind} borders: ${e.message}`;
  }
}

function setupSmooth() {
  if (!els["smooth"]) return;
  els["smooth"].addEventListener("input", () => {
    const r = Number(els["smooth"].value);
    state.smoothRadius = r;
    els["smooth-readout"].textContent = r === 0 ? "off" : `${r} cell${r > 1 ? "s" : ""}`;
    if (state.last) renderCount();
  });
}

// Percentage of the *visible map area* (not all of CONUS) meeting the floor —
// recomputed as the user pans/zooms.
function updateWindowStat() {
  const { count, n } = state.last;
  const floor = Math.min(Number(els["floor"].value), n);
  const b = map.getBounds();
  const w = b.getWest(), e = b.getEast(), s = b.getSouth(), nth = b.getNorth();
  const { lat, lon, conusMask } = state;
  let inWin = 0;
  let met = 0;
  for (let k = 0; k < count.length; k++) {
    if (conusMask && !conusMask[k]) continue; // CONUS only
    const la = lat[k];
    const lo = lon[k];
    if (la >= s && la <= nth && lo >= w && lo <= e) {
      inWin++;
      if (count[k] >= floor) met++;
    }
  }
  const where = conusMask ? "visible CONUS" : "visible area";
  els["status"].textContent = inWin
    ? `${((met / inWin) * 100).toFixed(1)}% of the ${where} meets ≥ ${floor} of ${n} ingredients`
    : "No data in view — pan/zoom over the map.";
}

function addConusOutline(geojson) {
  if (map.getSource("conus")) return;
  map.addSource("conus", { type: "geojson", data: geojson });
  map.addLayer({
    id: "conus-outline", type: "line", source: "conus",
    paint: { "line-color": "#444", "line-width": 0.7, "line-opacity": 0.5 },
  });
}

// Build the legend model (active ingredients + count color scale) used by both
// the on-map legend box and the PNG export.
function legendModel(floor, n) {
  const ingredients = state.last.conds.map((c) => ({
    text: `${condName(c)} ${OP_LABELS[c.op]} ${c.value} ${c.meta?.units || ""}`.trim(),
    source: c.meta?.source === "hrrr" ? "hrrr" : "calc",
  }));
  const scale = [];
  for (let c = n; c >= 1; c--) {
    scale.push({ label: `${c} of ${n}`, rgb: rampColor(n > 1 ? (c - 1) / (n - 1) : 1), dim: c < floor });
  }
  return { ingredients, scale };
}

// Valid time = cycle init (from YYYYMMDDHH) + forecast hour, formatted UTC.
function validTimeUTC() {
  const id = state.cycle.cycle_id;
  const init = Date.UTC(+id.slice(0, 4), +id.slice(4, 6) - 1, +id.slice(6, 8), +id.slice(8, 10));
  const iso = new Date(init + state.forecastHour * 3600 * 1000).toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

function renderLegend(floor, n) {
  const m = legendModel(floor, n);
  state.legend = m;
  const fh = `f${String(state.forecastHour).padStart(2, "0")}`;
  let html = `<div class="ml-title">Ingredients · ${state.cycle.cycle_id} ${fh}</div>`;
  html += `<div class="ml-valid">Valid: ${validTimeUTC()}</div>`;
  html += m.ingredients.map((it) =>
    `<div class="ml-ing"><span class="prov prov-${it.source}"></span>${it.text} ` +
    `<span class="prov-tag">${it.source === "hrrr" ? "HRRR" : "calc"}</span></div>`).join("");
  html += '<div class="ml-sub">Cells colored by ingredients met</div>';
  html += m.scale.map((s) =>
    `<div class="legend-row${s.dim ? " legend-dim" : ""}">` +
    `<span class="sw" style="background:rgb(${s.rgb.join(",")})"></span>${s.label}</div>`).join("");
  els["map-legend"].innerHTML = html;
  els["map-legend"].hidden = false;
}

// Export the current map (basemap + overlay + outline) plus the legend as a PNG.
function exportImage() {
  if (!state.legend) { els["status"].textContent = "Update the map first."; return; }
  const src = map.getCanvas();
  const out = document.createElement("canvas");
  out.width = src.width;
  out.height = src.height;
  const ctx = out.getContext("2d");
  ctx.drawImage(src, 0, 0);
  drawLegendOnCanvas(ctx, src.width, src.height);
  const a = document.createElement("a");
  a.download = `hrrr-${state.cycle.cycle_id}-f${String(state.forecastHour).padStart(2, "0")}.png`;
  a.href = out.toDataURL("image/png");
  a.click();
  els["status"].textContent = "Exported map image.";
}

function drawLegendOnCanvas(ctx, W, H) {
  const { ingredients, scale } = state.legend;
  const s = (W / map.getCanvas().clientWidth) || 1; // device-pixel scale
  const pad = 8 * s, line = 18 * s, font = 12 * s, sw = 12 * s;
  const rows = ingredients.length + scale.length + 3; // title + valid + scale header
  const boxW = 320 * s, boxH = pad * 2 + rows * line;
  const x = 10 * s, y = H - boxH - 10 * s;
  ctx.fillStyle = "rgba(255,255,255,0.92)";
  ctx.strokeStyle = "rgba(0,0,0,0.25)";
  ctx.fillRect(x, y, boxW, boxH);
  ctx.strokeRect(x, y, boxW, boxH);
  let cy = y + pad + font;
  const fh = `f${String(state.forecastHour).padStart(2, "0")}`;
  ctx.fillStyle = "#000";
  ctx.font = `bold ${font}px system-ui, sans-serif`;
  ctx.fillText(`Ingredients · ${state.cycle.cycle_id} ${fh}`, x + pad, cy);
  cy += line;
  ctx.font = `${font}px system-ui, sans-serif`;
  ctx.fillStyle = "#555";
  ctx.fillText(`Valid: ${validTimeUTC()}`, x + pad, cy);
  cy += line;
  ctx.fillStyle = "#000";
  for (const it of ingredients) {
    ctx.fillStyle = it.source === "hrrr" ? "#1565c0" : "#6a1b9a";
    ctx.fillRect(x + pad, cy - font * 0.8, sw, sw);
    ctx.fillStyle = "#000";
    ctx.fillText(`${it.text}  [${it.source === "hrrr" ? "HRRR" : "calc"}]`, x + pad + sw + 5 * s, cy);
    cy += line;
  }
  ctx.font = `bold ${font}px system-ui, sans-serif`;
  ctx.fillText("Ingredients met", x + pad, cy);
  cy += line;
  ctx.font = `${font}px system-ui, sans-serif`;
  for (const sc of scale) {
    ctx.globalAlpha = sc.dim ? 0.4 : 1;
    ctx.fillStyle = `rgb(${sc.rgb.join(",")})`;
    ctx.fillRect(x + pad, cy - font * 0.8, sw, sw);
    ctx.fillStyle = "#000";
    ctx.fillText(sc.label, x + pad + sw + 5 * s, cy);
    cy += line;
    ctx.globalAlpha = 1;
  }
}

function showOverlay(dataUrl, coordinates) {
  if (map.getLayer("count")) map.removeLayer("count");
  if (map.getSource("count")) map.removeSource("count");
  map.addSource("count", { type: "image", url: dataUrl, coordinates });
  // Draw beneath the CONUS outline so borders stay visible on top.
  const before = map.getLayer("conus-outline") ? "conus-outline" : undefined;
  map.addLayer(
    { type: "raster", id: "count", source: "count", paint: { "raster-opacity": 0.75 } }, before);
}

// --- cursor hover readout --------------------------------------------------

function setupHover() {
  let queued = false;
  map.on("mousemove", (e) => {
    state.lastHover = { lng: e.lngLat.lng, lat: e.lngLat.lat };
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; renderHover(state.lastHover); });
  });
  map.on("mouseout", () => {
    state.lastHover = null;
    els["hover"].hidden = true;
  });
  els["hover-toggle"].addEventListener("change", () => {
    if (!els["hover-toggle"].checked) els["hover"].hidden = true;
    else if (state.lastHover) renderHover(state.lastHover);
  });
}

function cellIndexAt(lng, lat) {
  const { i, j } = state.mapper(lng, lat);
  const ii = Math.round(i);
  const jj = Math.round(j);
  const [ny, nx] = state.last.shape;
  if (ii < 0 || ii >= nx || jj < 0 || jj >= ny) return -1;
  const k = jj * nx + ii;
  if (state.conusMask && !state.conusMask[k]) return -1; // outside CONUS
  return k;
}

// Render the hover readout for a stored lng/lat. Called on mousemove (via rAF)
// and on FH change so the readout tracks the slider while the cursor is parked.
function renderHover(ll) {
  if (!state.last || !els["hover-toggle"].checked) { els["hover"].hidden = true; return; }
  const k = cellIndexAt(ll.lng, ll.lat);
  if (k < 0) { els["hover"].hidden = true; return; }
  const { conds, fields } = state.last;
  let met = 0;
  let rows = "";
  conds.forEach((c, idx) => {
    const v = fields[idx].data[k];
    const pass = Number.isFinite(v) && OPS[c.op](v, c.value);
    if (pass) met++;
    const vstr = Number.isFinite(v) ? v.toFixed(1) : "—";
    rows += `<div class="${pass ? "pass" : "fail"}">${condName(c)}: ` +
      `<b>${vstr}</b> ${c.meta?.units || ""}</div>`;
  });
  els["hover"].innerHTML =
    `<div class="hover-head">${ll.lat.toFixed(2)}, ${ll.lng.toFixed(2)} · ${met}/${conds.length} met</div>${rows}`;
  els["hover"].hidden = false;
}

// --- click to inspect ------------------------------------------------------

function onMapClick(e) {
  state.lastClick = { lng: e.lngLat.lng, lat: e.lngLat.lat };
  renderInspect(state.lastClick);
}

// Render the inspect panel for a stored lng/lat. Called both on click and on
// FH change, so the panel stays in sync with the currently-displayed forecast.
function renderInspect(ll) {
  if (!state.last) return;
  const k = cellIndexAt(ll.lng, ll.lat);
  if (k < 0) {
    els["inspect"].hidden = true;
    hideInspectMarker();
    return;
  }
  const { conds, fields } = state.last;
  let met = 0;
  let rows = "";
  conds.forEach((c, idx) => {
    const v = fields[idx].data[k];
    const pass = Number.isFinite(v) && OPS[c.op](v, c.value);
    if (pass) met++;
    const vstr = Number.isFinite(v) ? v.toFixed(1) : "—";
    rows += `<tr class="${pass ? "pass" : "fail"}">` +
      `<td>${condName(c)}</td>` +
      `<td>${vstr}</td>` +
      `<td>${OP_LABELS[c.op]} ${c.value} ${c.meta?.units || ""}</td>` +
      `<td>${pass ? "✓" : "✗"}</td></tr>`;
  });
  const limiting = conds.filter((c, idx) => {
    const v = fields[idx].data[k];
    return !(Number.isFinite(v) && OPS[c.op](v, c.value));
  }).map((c) => condName(c));

  els["inspect-loc"].textContent =
    `${ll.lat.toFixed(2)}, ${ll.lng.toFixed(2)} · f${String(state.forecastHour).padStart(2, "0")} · ${met} of ${conds.length} met`;
  els["inspect-table"].innerHTML =
    "<tr><th>Ingredient</th><th>Value</th><th>Threshold</th><th></th></tr>" + rows +
    (limiting.length ? `<tr><td colspan="4" class="limiting">Limiting: ${limiting.join(", ")}</td></tr>` : "");
  els["inspect"].hidden = false;
  showInspectMarker(ll);
}

// Marker for the clicked inspect point — a small filled circle with a white
// halo so it reads on both light basemap and any colored count overlay. Source
// + layer are created lazily on first use; subsequent renders just setData.
function showInspectMarker(ll) {
  const data = { type: "Feature", geometry: { type: "Point", coordinates: [ll.lng, ll.lat] } };
  if (map.getSource("inspect-marker")) {
    map.getSource("inspect-marker").setData(data);
    return;
  }
  map.addSource("inspect-marker", { type: "geojson", data });
  map.addLayer({
    id: "inspect-marker", type: "circle", source: "inspect-marker",
    paint: {
      "circle-radius": 6,
      "circle-color": "#1565c0",          // matches the HRRR provenance blue
      "circle-stroke-color": "#fff",
      "circle-stroke-width": 2,
    },
  });
}

function hideInspectMarker() {
  if (map.getLayer("inspect-marker")) map.removeLayer("inspect-marker");
  if (map.getSource("inspect-marker")) map.removeSource("inspect-marker");
}

// --- time-series modal -----------------------------------------------------
// Opens a modal with one chart per ingredient at the inspected cell across
// every forecast hour in the cycle, plus a top "all ingredients met" consensus
// strip so the user can see the window where every threshold lines up. Data
// comes from state.fieldCache (preloadAll fills it after first paint), so this
// is fast — each (cond, fh) lookup is an O(1) cell index into a cached array.

async function openTimeSeries(ll) {
  if (!ll || !state.last) return;
  const cellK = cellIndexAt(ll.lng, ll.lat);
  if (cellK < 0) return;
  const conds = state.last.conds;
  if (!conds.length) return;

  const modal = ensureTSModal();
  setTSCoords(ll);
  const ctxLine = state.cycle
    ? `Cycle ${state.cycle.cycle_id} · ${state.fhList.length} forecast hours`
    : "";
  modal.querySelector("#ts-title").textContent =
    `Time-series · ${ll.lat.toFixed(2)}, ${ll.lng.toFixed(2)}`;
  modal.querySelector(".ts-body").innerHTML =
    `<p class="muted ts-context">${ctxLine}</p><p class="muted">Loading values across all forecast hours…</p>`;
  modal.hidden = false;

  // Sample each (cond, fh) at the inspect cell. getField is cached, so this
  // is fast once preloadAll has run; any uncached FH is fetched on demand.
  const fhs = state.fhList;
  const series = await Promise.all(conds.map(async (c) => {
    const out = new Array(fhs.length).fill(NaN);
    await Promise.all(fhs.map(async (fh, i) => {
      try {
        const field = await getField(c.paramId, c.levelIdx, fh);
        out[i] = field.data[cellK];
      } catch { /* leave NaN */ }
    }));
    return out;
  }));

  const consensus = fhs.map((_, fi) =>
    conds.every((c, ci) => Number.isFinite(series[ci][fi]) && OPS[c.op](series[ci][fi], c.value)));

  renderTimeSeriesBody(modal, { ll, conds, fhs, series, consensus });
}

function ensureTSModal() {
  let modal = document.getElementById("ts-modal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "ts-modal";
  modal.hidden = true;
  modal.innerHTML = `
    <div class="ts-backdrop"></div>
    <div class="ts-panel">
      <div class="ts-header">
        <h2 id="ts-title">Time-series</h2>
        <div class="ts-actions">
          <button id="ts-export" class="link" title="Download as PNG">📷 Export</button>
          <button id="ts-close" class="link" title="Close (Esc)">✕</button>
        </div>
      </div>
      <div class="ts-body"></div>
    </div>`;
  document.body.appendChild(modal);
  const close = () => { modal.hidden = true; };
  modal.querySelector(".ts-backdrop").addEventListener("click", close);
  modal.querySelector("#ts-close").addEventListener("click", close);
  modal.querySelector("#ts-export").addEventListener("click", exportTimeSeries);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hidden) close();
  });
  return modal;
}

// Stash the click coordinates on the modal for the export filename — set
// right before the modal is populated so they match the displayed data.
function setTSCoords(ll) {
  const modal = document.getElementById("ts-modal");
  if (!modal) return;
  modal.dataset.lat = ll.lat.toFixed(2);
  modal.dataset.lng = ll.lng.toFixed(2);
}

const escapeXML = (s) => String(s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

// Roll up every chart in the modal into one big SVG, rasterize via canvas,
// and download as PNG. Each chart's inner SVG content is re-embedded into a
// translated+scaled <g> on the composite so layout stays consistent.
async function exportTimeSeries() {
  const modal = document.getElementById("ts-modal");
  if (!modal || modal.hidden) return;
  const body = modal.querySelector(".ts-body");
  const baseW = 820;
  const pad = 18;
  let y = pad;
  const parts = [];

  const title = modal.querySelector("#ts-title").textContent;
  parts.push(`<text x="${pad}" y="${y + 14}" font-size="15" font-weight="700" fill="#222">${escapeXML(title)}</text>`);
  y += 22;
  const ctx = modal.querySelector(".ts-context");
  if (ctx) {
    parts.push(`<text x="${pad}" y="${y + 12}" font-size="11" fill="#666">${escapeXML(ctx.textContent)}</text>`);
    y += 18;
  }
  y += 6;

  body.querySelectorAll(".ts-section").forEach((sect) => {
    const lbl = sect.querySelector(".ts-label");
    if (lbl) {
      const txt = lbl.textContent.trim().replace(/\s+/g, " ");
      parts.push(`<text x="${pad}" y="${y + 12}" font-size="12" font-weight="600" fill="#333">${escapeXML(txt)}</text>`);
      y += 18;
    }
    const svg = sect.querySelector("svg");
    if (svg) {
      const vb = (svg.getAttribute("viewBox") || "0 0 760 110").split(/\s+/).map(Number);
      const [, , vw, vh] = vb;
      const scale = (baseW - pad * 2) / vw;
      parts.push(`<g transform="translate(${pad} ${y}) scale(${scale})">${svg.innerHTML}</g>`);
      y += vh * scale + 8;
    }
  });
  y += pad;

  // Inline font CSS so rasterizing into canvas keeps the system-ui look the
  // user sees on screen; otherwise the SVG default ("serif") would render.
  const fontCss = `<style>text{font-family:system-ui,-apple-system,Segoe UI,sans-serif;}</style>`;
  const composite =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${baseW}" height="${y}" viewBox="0 0 ${baseW} ${y}">` +
    `${fontCss}<rect width="${baseW}" height="${y}" fill="#fff"/>${parts.join("")}</svg>`;

  // Render to canvas at 2x for crispness, then download.
  const dpr = 2;
  const url = URL.createObjectURL(new Blob([composite], { type: "image/svg+xml;charset=utf-8" }));
  try {
    const img = new Image();
    img.crossOrigin = "anonymous";
    await new Promise((res, rej) => { img.onload = res; img.onerror = () => rej(new Error("SVG decode failed")); img.src = url; });
    const canvas = document.createElement("canvas");
    canvas.width = baseW * dpr;
    canvas.height = y * dpr;
    const cx = canvas.getContext("2d");
    cx.scale(dpr, dpr);
    cx.drawImage(img, 0, 0);
    canvas.toBlob((blob) => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      const ll = (modal.dataset.lat && modal.dataset.lng) ? `_${modal.dataset.lat}_${modal.dataset.lng}` : "";
      const cyc = state.cycle?.cycle_id || "hrrr";
      const fh = `f${String(state.forecastHour).padStart(2, "0")}`;
      a.download = `hrrr-ts-${cyc}-${fh}${ll}.png`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    }, "image/png");
  } finally {
    URL.revokeObjectURL(url);
  }
}

function renderTimeSeriesBody(modal, { conds, fhs, series, consensus }) {
  const ctxLine = state.cycle
    ? `Cycle ${state.cycle.cycle_id} · ${fhs.length} forecast hours`
    : "";
  let html = `<p class="muted ts-context">${ctxLine}</p>`;
  html += `<div class="ts-section">
    <div class="ts-label"><b>All ingredients met</b>
      <span class="ts-thr">(${consensus.filter(Boolean).length} of ${fhs.length} forecast hours)</span></div>
    ${consensusStripSVG(fhs, consensus, state.forecastHour)}
  </div>`;
  conds.forEach((cond, i) => {
    html += `<div class="ts-section">
      <div class="ts-label"><b>${condName(cond)}</b>
        <span class="ts-thr">${OP_LABELS[cond.op]} ${cond.value}${cond.meta?.units ? " " + cond.meta.units : ""}</span></div>
      ${chartSVG(cond, fhs, series[i], state.forecastHour)}
    </div>`;
  });
  modal.querySelector(".ts-body").innerHTML = html;
}

// Compact number formatter for axis labels — fewer decimals for big values.
function niceNum(v) {
  if (!Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  return a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2);
}

// Per-ingredient line chart: value vs FH, with the threshold drawn as a dashed
// line, the passing half-plane shaded green, points/segments colored
// pass-green / fail-red, and the current FH highlighted with a vertical line.
function chartSVG(cond, fhs, values, currentFH) {
  const W = 760, H = 110;
  const pad = { top: 8, right: 14, bottom: 22, left: 52 };
  const w = W - pad.left - pad.right;
  const h = H - pad.top - pad.bottom;
  const finite = values.filter(Number.isFinite);
  if (!finite.length) {
    return `<svg viewBox="0 0 ${W} ${H}" class="ts-chart"><text x="${W/2}" y="${H/2}" text-anchor="middle" fill="#888" font-size="11">no data at this cell</text></svg>`;
  }
  let vmin = Math.min(...finite, cond.value);
  let vmax = Math.max(...finite, cond.value);
  if (vmin === vmax) { vmin -= 1; vmax += 1; }
  const range = vmax - vmin;
  const yMin = vmin - 0.06 * range, yMax = vmax + 0.06 * range;
  const yScale = (v) => pad.top + h - (v - yMin) / (yMax - yMin) * h;
  const xScale = (i) => fhs.length === 1
    ? pad.left + w / 2
    : pad.left + (i / (fhs.length - 1)) * w;

  const thrY = yScale(cond.value);
  // Shade the passing half-plane: above the threshold for ≥/>, below for ≤/<.
  let band = "";
  if (cond.op === ">=" || cond.op === ">") {
    band = `<rect x="${pad.left}" y="${pad.top}" width="${w}" height="${Math.max(0, thrY - pad.top)}" fill="#e8f5e9"/>`;
  } else if (cond.op === "<=" || cond.op === "<") {
    band = `<rect x="${pad.left}" y="${thrY}" width="${w}" height="${Math.max(0, pad.top + h - thrY)}" fill="#e8f5e9"/>`;
  }
  const thrLine = `<line x1="${pad.left}" y1="${thrY}" x2="${pad.left + w}" y2="${thrY}" stroke="#666" stroke-width="1" stroke-dasharray="4 3"/>`;

  let segs = "", pts = "";
  for (let i = 0; i < fhs.length; i++) {
    const v = values[i];
    if (!Number.isFinite(v)) continue;
    const pass = OPS[cond.op](v, cond.value);
    pts += `<circle cx="${xScale(i)}" cy="${yScale(v)}" r="2.3" fill="${pass ? "#1b5e20" : "#b71c1c"}"/>`;
    if (i > 0 && Number.isFinite(values[i - 1])) {
      const passPrev = OPS[cond.op](values[i - 1], cond.value);
      const segColor = (pass && passPrev) ? "#1b5e20"
                     : (!pass && !passPrev) ? "#b71c1c" : "#888";
      segs += `<line x1="${xScale(i - 1)}" y1="${yScale(values[i - 1])}" x2="${xScale(i)}" y2="${yScale(v)}" stroke="${segColor}" stroke-width="1.5"/>`;
    }
  }

  // X labels: sparse — about 10 ticks across the range.
  const step = Math.max(1, Math.ceil(fhs.length / 10));
  let xLabels = "";
  for (let i = 0; i < fhs.length; i += step) {
    xLabels += `<text x="${xScale(i)}" y="${H - 6}" text-anchor="middle" font-size="10" fill="#555">f${String(fhs[i]).padStart(2, "0")}</text>`;
  }
  // Threshold label is always drawn; min/max labels are suppressed when they
  // would visually overlap it (e.g. when the data hugs the threshold and the
  // 6 % y-padding leaves min/max within a few values of the threshold itself).
  const maxLabelY = pad.top + 9, minLabelY = pad.top + h - 1, thrLabelY = thrY + 3;
  const showMax = Math.abs(thrLabelY - maxLabelY) > 11;
  const showMin = Math.abs(minLabelY - thrLabelY) > 11;
  const yLabels =
    (showMax ? `<text x="${pad.left - 4}" y="${maxLabelY}" text-anchor="end" font-size="10" fill="#555">${niceNum(yMax)}</text>` : "") +
    (showMin ? `<text x="${pad.left - 4}" y="${minLabelY}" text-anchor="end" font-size="10" fill="#555">${niceNum(yMin)}</text>` : "") +
    `<text x="${pad.left - 4}" y="${thrLabelY}" text-anchor="end" font-size="10" fill="#666">${niceNum(cond.value)}</text>`;

  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="ts-chart">
    ${band}${thrLine}${segs}${pts}${xLabels}${yLabels}
  </svg>`;
}

// Top "all ingredients met" strip — one filled box per FH, green if all
// thresholds pass at that cell at that hour, gray otherwise. Each run of
// consecutive green hours gets bracket tick marks at its bounds and a UTC
// time label above so the danger windows pop visually.
//
// Box i represents the HOUR STARTING AT FH i — it spans
// [xScale(i), xScale(i+1)] colored by consensus[i]. So a passing f14 paints
// a green block from the f14 chart point to the f15 chart point. n-1 boxes
// total for n forecast hours; the very last FH (consensus[n-1]) has no
// following hour to extend into and is not drawn as a box (its value still
// contributes to the 'X of N forecast hours' tally above the strip).
function consensusStripSVG(fhs, consensus, currentFH) {
  const W = 760, H = 48;
  const pad = { left: 52, right: 14 };
  const w = W - pad.left - pad.right;
  const stripY = 24, stripH = 18;
  const n = fhs.length;
  // Same xScale as the per-ingredient charts so box edges land on points.
  const xScale = (i) => n === 1 ? pad.left + w / 2 : pad.left + (i / (n - 1)) * w;

  // Cycle init time (UTC ms); each FH valid time is init + fh*3600s.
  const id = state.cycle?.cycle_id || "";
  const initMs = id.length >= 10
    ? Date.UTC(+id.slice(0, 4), +id.slice(4, 6) - 1, +id.slice(6, 8), +id.slice(8, 10))
    : 0;
  const WKDS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  let boxes = "";
  for (let i = 0; i < n - 1; i++) {
    const x = xScale(i), wid = xScale(i + 1) - x;
    boxes += `<rect x="${x}" y="${stripY}" width="${wid}" height="${stripH}" fill="${consensus[i] ? "#43a047" : "#e0e0e0"}"/>`;
  }

  // Walk box indices 0..n-2 and group runs of true boxes. Run a..b spans
  // [xScale(a), xScale(b+1)] visually and [fhs[a], fhs[b+1]] in valid time.
  let labels = "";
  let runStart = -1;
  for (let i = 0; i <= n - 1; i++) {
    const v = i < n - 1 ? consensus[i] : false;
    if (v && runStart < 0) runStart = i;
    if (!v && runStart >= 0) {
      const runEnd = i - 1;
      const xStart = xScale(runStart);
      const xEnd   = xScale(runEnd + 1);
      const xMid   = (xStart + xEnd) / 2;
      const startD = new Date(initMs + fhs[runStart]   * 3600 * 1000);
      const endD   = new Date(initMs + fhs[runEnd + 1] * 3600 * 1000);
      // Plain "22Z" when the run stays inside one UTC day; otherwise prefix
      // each end with a weekday abbreviation so "22Z–05Z" isn't ambiguous.
      const crossesDay =
        startD.getUTCFullYear() !== endD.getUTCFullYear() ||
        startD.getUTCMonth()    !== endD.getUTCMonth()    ||
        startD.getUTCDate()     !== endD.getUTCDate();
      const fmt = (d) => {
        const hh = String(d.getUTCHours()).padStart(2, "0") + "Z";
        return crossesDay ? `${WKDS[d.getUTCDay()]} ${hh}` : hh;
      };
      const text = `${fmt(startD)}–${fmt(endD)}`;
      labels +=
        `<line x1="${xStart}" y1="${stripY - 5}" x2="${xStart}" y2="${stripY - 1}" stroke="#1b5e20" stroke-width="1.5"/>` +
        `<line x1="${xEnd}" y1="${stripY - 5}" x2="${xEnd}" y2="${stripY - 1}" stroke="#1b5e20" stroke-width="1.5"/>` +
        `<line x1="${xStart}" y1="${stripY - 5}" x2="${xEnd}" y2="${stripY - 5}" stroke="#1b5e20" stroke-width="1.5"/>` +
        `<text x="${xMid}" y="${stripY - 8}" text-anchor="middle" font-size="10" font-weight="700" fill="#1b5e20">${text}</text>`;
      runStart = -1;
    }
  }

  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="ts-chart">${boxes}${labels}</svg>`;
}

// CARTO Positron: a clean, muted, free basemap (no account/token) — the light
// gray makes the colored count overlay stand out. Swap "light_all" for
// "dark_all" or another provider here if you ever want a different look.
function basemapStyle() {
  const subs = ["a", "b", "c", "d"];
  return {
    version: 8,
    sources: {
      carto: {
        type: "raster",
        tiles: subs.map((s) => `https://${s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png`),
        tileSize: 256,
        attribution: "© OpenStreetMap contributors © CARTO",
      },
    },
    layers: [{ id: "carto", type: "raster", source: "carto" }],
  };
}

map.on("load", init);
