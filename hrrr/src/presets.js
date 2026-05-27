// Preset bundles: save/load a set of ingredient conditions.
//
// Durability without accounts (spec 6.4): browser localStorage + a JSON
// auto-download on save + URL-hash sharing. Preset shape follows spec 6.1
// (a subset for v1; hard/soft thresholds and the built-in library come later).

const LS_KEY = "hrrr_user_presets";
const SCHEMA_VERSION = "1.0";

/** Build a preset object from the current UI conditions. */
export function bundleFromConditions(name, conditions) {
  return {
    schema_version: SCHEMA_VERSION,
    id: slugify(name) || `user_${Date.now()}`,
    name: name || "Untitled",
    origin: "user",
    created: new Date().toISOString(),
    thresholds: conditions.map((c) => ({
      parameter_id: c.paramId,
      operator: c.op,
      value: c.value,
      units: c.meta?.units ?? "",
      level: c.levelVal ?? null, // pressure level (hPa) for 3D params, else null
    })),
  };
}

/** A built-in preset object (origin "builtin"), for the authoring export. */
export function bundleAsBuiltin(name, conditions) {
  return { ...bundleFromConditions(name, conditions), origin: "builtin", category: "uncategorized" };
}

/** Convert a preset's thresholds back to UI condition presets. */
export function conditionsFromBundle(preset) {
  return (preset.thresholds ?? []).map((t) => ({
    paramId: t.parameter_id,
    op: t.operator,
    value: t.value,
    level: t.level ?? null,
  }));
}

// --- localStorage user presets ---------------------------------------------

export function listUserPresets() {
  try {
    return JSON.parse(localStorage.getItem(LS_KEY) ?? "[]");
  } catch {
    return [];
  }
}

export function saveUserPreset(preset) {
  const all = listUserPresets().filter((p) => p.id !== preset.id);
  all.push(preset);
  localStorage.setItem(LS_KEY, JSON.stringify(all));
  return all;
}

export function deleteUserPreset(id) {
  const all = listUserPresets().filter((p) => p.id !== id);
  localStorage.setItem(LS_KEY, JSON.stringify(all));
  return all;
}

// --- built-in preset library (spec 6.2) ------------------------------------

/**
 * Load the built-in presets shipped with the site: read presets/index.json,
 * then each presets/<id>.json. Returns [] if there's no library yet.
 */
export async function loadBuiltins(baseUrl = "presets") {
  try {
    const index = await (await fetch(`${baseUrl}/index.json`)).json();
    const ids = index.presets ?? [];
    const presets = await Promise.all(
      ids.map((id) => fetch(`${baseUrl}/${id}.json`).then((r) => r.json()).catch(() => null)),
    );
    return presets.filter(Boolean).map((p) => ({ ...p, origin: "builtin" }));
  } catch {
    return [];
  }
}

// --- import a preset / library file (user upload) --------------------------

/** Parse an uploaded file's JSON into an array of presets (handles both shapes). */
export function parseImported(text) {
  const obj = JSON.parse(text);
  const list = Array.isArray(obj) ? obj : obj.presets ? obj.presets : [obj];
  return list.filter((p) => p && Array.isArray(p.thresholds));
}

// --- URL-hash sharing (spec 6.4) -------------------------------------------

export function encodeToHash(preset) {
  return "#p=" + b64urlEncode(JSON.stringify(preset));
}

export function decodeFromHash(hash = location.hash) {
  const m = /[#&]p=([^&]+)/.exec(hash);
  if (!m) return null;
  try {
    return JSON.parse(b64urlDecode(m[1]));
  } catch {
    return null;
  }
}

// --- auto-download backup (spec 6.4 layer 2) -------------------------------

export function downloadJson(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

// --- helpers ---------------------------------------------------------------

function slugify(s) {
  return (s ?? "").toLowerCase().trim().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
}

function b64urlEncode(str) {
  return btoa(unescape(encodeURIComponent(str))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(str) {
  const s = str.replace(/-/g, "+").replace(/_/g, "/");
  return decodeURIComponent(escape(atob(s)));
}
