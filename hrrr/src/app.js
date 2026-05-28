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
   "hover", "hover-toggle", "export-image", "outlook-legend",
  ].map((id) => [id, document.getElementById(id)]),
);

const state = {
  cycle: null, params: [], byId: {}, fhList: [0], forecastHour: 0,
  group: null, mapper: null, bbox: null, nx: 0, lat: null, lon: null,
  fieldCache: new Map(), last: null, builtins: [], conusMask: null, levels: [],
  outlookData: {},
};

const map = new maplibregl.Map({
  container: "map", style: basemapStyle(), center: [-97, 38], zoom: 3.2,
  preserveDrawingBuffer: true, // required so the map canvas can be exported to PNG
});

// Wire the resize handle immediately (not inside the async init), so the
// sidebar is draggable the moment the page loads, before data finishes loading.
setupPanelResize();

async function init() {
  try {
    const { cycle } = await loadManifests(DATA_BASE_URL);
    state.cycle = cycle;
    // Include 3D pressure-level params; a per-condition level picker handles them.
    state.params = cycle.parameters.filter((p) => p.ui_visible);
    state.byId = Object.fromEntries(state.params.map((p) => [p.id, p]));
    state.fhList = cycle.forecast_hours;
    state.forecastHour = state.fhList[0];

    els["cycle-info"].textContent =
      `Cycle ${cycle.cycle_id} · ${state.params.length} parameters available`;
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
    els["inspect-close"].addEventListener("click", () => (els["inspect"].hidden = true));
    map.on("click", onMapClick);
    map.on("moveend", () => { if (state.last) updateWindowStat(); });
    setupHover();
    setupOutlooks();
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
    await openCurrentForecastHour(false);
    updateMap();
  });
}

async function openCurrentForecastHour(readGeo) {
  state.fieldCache.clear();
  state.group = await openForecastHour(DATA_BASE_URL, state.cycle.cycle_id, state.forecastHour);
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

// ─── SPC Day 1 convective outlook overlays ────────────────────────────────
// Fetched straight from SPC (its server is CORS-open), and the GeoJSON carries
// its own official fill/stroke colors + labels — so we style the layers and
// build the legend directly from the data. "<2%" base areas have empty fill and
// are filtered out.
const SPC_BASE = "https://www.spc.noaa.gov/products/outlook/day1otlk_";
// WPC ERO has no CORS + carries no colors, so the worker mirrors it to R2 and we
// apply WPC's palette client-side (keyed by the OUTLOOK level word).
const ERO_URL = `${DATA_BASE_URL}/outlooks/wpc_ero_day1.geojson`;
const OUTLOOK_NAMES = { cat: "Categorical", torn: "Tornado", wind: "Wind", hail: "Hail", ero: "Excessive rainfall" };
const ERO_COLORS = {
  Marginal: { fill: "#66A366", stroke: "#2E7D32" },
  Slight:   { fill: "#E8E84A", stroke: "#B0B000" },
  Moderate: { fill: "#E0782E", stroke: "#A8480F" },
  High:     { fill: "#CC44CC", stroke: "#800080" },
};
const outlookUrl = (kind) => (kind === "ero" ? ERO_URL : `${SPC_BASE}${kind}.lyr.geojson`);
function colorizeERO(gj) {
  for (const f of gj.features || []) {
    const p = f.properties || (f.properties = {});
    const c = ERO_COLORS[String(p.OUTLOOK || "").split(/[ (]/)[0]];
    if (c) { p.fill = c.fill; p.stroke = c.stroke; p.LABEL2 = p.OUTLOOK; }
    else { p.fill = ""; p.stroke = ""; }
  }
}

// Build (once) a small diagonal-hatch tile in the given color, for fill-pattern.
function ensureHatch(color) {
  const name = `hatch-${color}`;
  if (map.hasImage(name)) return name;
  const s = 8;
  const cv = document.createElement("canvas");
  cv.width = cv.height = s;
  const ctx = cv.getContext("2d");
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  ctx.moveTo(0, s); ctx.lineTo(s, 0);
  ctx.moveTo(-s, s); ctx.lineTo(s, -s);
  ctx.moveTo(0, 2 * s); ctx.lineTo(2 * s, 0);
  ctx.stroke();
  const img = ctx.getImageData(0, 0, s, s);
  map.addImage(name, { width: s, height: s, data: new Uint8Array(img.data.buffer) });
  return name;
}

function setupOutlooks() {
  document.querySelectorAll(".otlk").forEach((cb) =>
    cb.addEventListener("change", () => toggleOutlook(cb.dataset.otlk, cb.checked, cb)));
}

async function toggleOutlook(kind, on, cb) {
  const src = `otlk-${kind}`, fillId = `${src}-fill`, lineId = `${src}-line`;
  if (!on) {
    [fillId, lineId].forEach((l) => { if (map.getLayer(l)) map.removeLayer(l); });
    if (map.getSource(src)) map.removeSource(src);
    delete state.outlookData[kind];
    renderOutlookLegend();
    return;
  }
  try {
    const resp = await fetch(outlookUrl(kind));
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const gj = await resp.json();
    if (kind === "ero") colorizeERO(gj);
    if (map.getSource(src)) map.removeSource(src);
    // Per-color diagonal hatch (mirrors SPC's hatching) so the ingredient map
    // underneath stays readable; "<2%" base areas have empty fill and are skipped.
    for (const f of gj.features || []) {
      const fc = f.properties && f.properties.fill;
      if (fc) { ensureHatch(fc); f.properties._pat = `hatch-${fc}`; }
    }
    map.addSource(src, { type: "geojson", data: gj });
    map.addLayer({
      id: fillId, type: "fill", source: src,
      filter: ["!=", ["get", "fill"], ""],
      paint: { "fill-pattern": ["get", "_pat"] },
    });
    map.addLayer({
      id: lineId, type: "line", source: src,
      filter: ["!=", ["get", "stroke"], ""],
      paint: { "line-color": ["get", "stroke"], "line-width": 1.5 },
    });
    state.outlookData[kind] = gj;
    renderOutlookLegend();
  } catch (e) {
    if (cb) cb.checked = false;
    els["status"].textContent = `Couldn't load SPC ${OUTLOOK_NAMES[kind] || kind} outlook (${e.message}).`;
  }
}

function renderOutlookLegend() {
  const el = els["outlook-legend"];
  if (!el) return;
  let html = "";
  for (const kind of Object.keys(state.outlookData)) {
    const seen = new Set(), rows = [];
    for (const f of state.outlookData[kind].features || []) {
      const p = f.properties || {};
      if (!p.fill || seen.has(p.fill)) continue;
      seen.add(p.fill);
      rows.push(`<div class="legend-row"><span class="sw" style="background:${p.fill};border-color:${p.stroke || "#888"}"></span>${p.LABEL2 || p.LABEL || ""}</div>`);
    }
    if (rows.length) html += `<div class="otlk-sub">SPC ${OUTLOOK_NAMES[kind] || kind} · Day 1</div>${rows.join("")}`;
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

async function getField(paramId, levelIdx = null) {
  const key = `${paramId}@${levelIdx ?? ""}`;
  if (!state.fieldCache.has(key)) {
    state.fieldCache.set(key, await readVariable(state.group, paramId, levelIdx));
  }
  return state.fieldCache.get(key);
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
  } catch (e) {
    els["status"].textContent = `Error: ${e.message}`;
  }
}

function renderCount() {
  const { count, shape, n } = state.last;
  const floor = Math.min(Number(els["floor"].value), n);
  const { dataUrl, coordinates } = reproject(
    { data: count, shape }, countColorFn(floor, n), state.mapper, state.bbox, state.conusMask);
  showOverlay(dataUrl, coordinates);
  updateWindowStat();
  renderLegend(floor, n);
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
  let lastEvt = null;
  map.on("mousemove", (e) => {
    lastEvt = e;
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; onMapHover(lastEvt); });
  });
  map.on("mouseout", () => (els["hover"].hidden = true));
  els["hover-toggle"].addEventListener("change", () => {
    if (!els["hover-toggle"].checked) els["hover"].hidden = true;
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

function onMapHover(e) {
  if (!state.last || !els["hover-toggle"].checked) { els["hover"].hidden = true; return; }
  const k = cellIndexAt(e.lngLat.lng, e.lngLat.lat);
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
    `<div class="hover-head">${e.lngLat.lat.toFixed(2)}, ${e.lngLat.lng.toFixed(2)} · ${met}/${conds.length} met</div>${rows}`;
  els["hover"].hidden = false;
}

// --- click to inspect ------------------------------------------------------

function onMapClick(e) {
  if (!state.last) return;
  const k = cellIndexAt(e.lngLat.lng, e.lngLat.lat);
  if (k < 0) {
    els["inspect"].hidden = true;
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
    `${e.lngLat.lat.toFixed(2)}, ${e.lngLat.lng.toFixed(2)} · f${String(state.forecastHour).padStart(2, "0")} · ${met} of ${conds.length} met`;
  els["inspect-table"].innerHTML =
    "<tr><th>Ingredient</th><th>Value</th><th>Threshold</th><th></th></tr>" + rows +
    (limiting.length ? `<tr><td colspan="4" class="limiting">Limiting: ${limiting.join(", ")}</td></tr>` : "");
  els["inspect"].hidden = false;
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
