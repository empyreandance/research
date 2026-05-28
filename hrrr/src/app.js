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
  ].map((id) => [id, document.getElementById(id)]),
);

const state = {
  cycle: null, params: [], byId: {}, fhList: [0], forecastHour: 0,
  group: null, mapper: null, bbox: null, nx: 0, lat: null, lon: null,
  fieldCache: new Map(), groups: new Map(), last: null, builtins: [], conusMask: null, levels: [],
  smoothRadius: 0,
  outlookData: {},
};

const map = new maplibregl.Map({
  container: "map", style: basemapStyle(), center: [-97, 38], zoom: 3.2,
  preserveDrawingBuffer: true, // required so the map canvas can be exported to PNG
});

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
    const { cycle } = await loadManifests(DATA_BASE_URL);
    state.cycle = cycle;
    // Include 3D pressure-level params; a per-condition level picker handles them.
    state.params = cycle.parameters.filter((p) => p.ui_visible);
    state.byId = Object.fromEntries(state.params.map((p) => [p.id, p]));
    state.fhList = cycle.forecast_hours;
    state.forecastHour = state.fhList[0];

    renderCycleInfo();
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
    updateMap();
  });
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

async function toggleOutlook(kind, on, cb) {
  const src = `otlk-${kind}`, fillId = `${src}-fill`, lineId = `${src}-line`;
  const cigSrc = `otlk-cig-${kind}`, cigFillId = `${cigSrc}-fill`, cigLineId = `${cigSrc}-line`;
  if (!on) {
    [fillId, lineId, cigFillId, cigLineId].forEach((l) => { if (map.getLayer(l)) map.removeLayer(l); });
    if (map.getSource(src)) map.removeSource(src);
    if (map.getSource(cigSrc)) map.removeSource(cigSrc);
    delete state.outlookData[kind];
    delete state.outlookData[`cig-${kind}`];
    renderOutlookLegend();
    return;
  }
  try {
    const resp = await fetch(outlookUrl(kind));
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const gj = await resp.json();
    if (kind === "ero") colorizeERO(gj);
    if (map.getSource(src)) map.removeSource(src);
    map.addSource(src, { type: "geojson", data: gj });
    // Outlooks render UNDER the HRRR count overlay (and under the conus outline
    // before that layer exists), so the ingredient map sits on top.
    const before = map.getLayer("count") ? "count"
                 : map.getLayer("conus-outline") ? "conus-outline"
                 : undefined;
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
    state.outlookData[kind] = gj;

    // For torn/wind/hail, overlay SPC's paired Conditional Intensity Group layer
    // (March-2026: CIG1 dashed / CIG2 solid / CIG3 cross-hatch, all in black).
    if (kind === "torn" || kind === "wind" || kind === "hail") {
      try {
        const r = await fetch(`${SPC_BASE}cig${kind}.lyr.geojson`);
        if (r.ok) {
          const cgj = await r.json();
          for (const f of cgj.features || []) {
            const p = f.properties || {};
            const lvl = String(p.LABEL || "").replace(/^CIG/, ""); // "CIG2" -> "2"
            if (lvl === "1" || lvl === "2" || lvl === "3") {
              ensureCigPattern(lvl);
              p._cigpat = `cig${lvl}`;
            }
          }
          if (map.getSource(cigSrc)) map.removeSource(cigSrc);
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
          state.outlookData[`cig-${kind}`] = cgj;
        }
      } catch (_) { /* CIG layer is best-effort; ignore if missing */ }
    }

    renderOutlookLegend();
  } catch (e) {
    if (cb) cb.checked = false;
    els["status"].textContent = `Couldn't load SPC ${OUTLOOK_NAMES[kind] || kind} outlook (${e.message}).`;
  }
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
      html += `<div class="otlk-sub">${source} ${OUTLOOK_NAMES[baseKind] || baseKind}${cigTag} · Day 1</div>${rows.join("")}`;
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
