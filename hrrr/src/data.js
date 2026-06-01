// Data access: read the manifest and Zarr arrays the ingest worker wrote.
//
// Uses zarrita (Zarr v3 reader) from a CDN — no build step. Verified to read the
// ingest's Blosc-zstd Zarr v3 output. The same code path works against a local
// folder in dev and the R2 bucket in production.

import * as zarr from "https://esm.sh/zarrita@0.7.3";
import { makeGridMapper } from "./proj.js";

/** Fetch the global manifest, then the cycle manifest it points at.
 *
 * The global manifest carries two pointers: `current_cycle` (every hour) and
 * `current_extended_cycle` (only updated on 00/06/12/18 Z runs, preserved
 * unchanged through the standard cycles in between). With `useExtended=true`,
 * follow the extended pointer instead — used by the sidebar's "Extended
 * forecast (48 hr)" toggle. Falls back to the standard pointer if the
 * extended one isn't populated yet (ingest worker still on old code, or no
 * extended cycle has run since deploy).
 */
export async function loadManifests(baseUrl, useExtended = false) {
  const global = await (await fetch(`${baseUrl}/manifest.json`)).json();
  const key = useExtended && global.current_extended_cycle_manifest_key
    ? global.current_extended_cycle_manifest_key
    : global.cycle_manifest_key;
  const cycle = await (await fetch(`${baseUrl}/${key}`)).json();
  return { global, cycle };
}

// Schema 2.0: one sharded store per cycle (cycles/<id>/data.zarr) with a
// forecast_hour dimension, instead of a group per forecast hour. We open the
// store once and slice the forecast_hour axis. The "group" handle the rest of
// the app passes around is now { root, fhIndex } into that single store.
const _storeCache = new Map(); // cycleId -> Promise<{ root, fhs }>

async function openStore(baseUrl, cycleId) {
  if (!_storeCache.has(cycleId)) {
    const p = (async () => {
      const store = new zarr.FetchStore(`${baseUrl}/cycles/${cycleId}/data.zarr`);
      const root = await zarr.open(store, { kind: "group" });
      const fhArr = await zarr.open(root.resolve("forecast_hour"), { kind: "array" });
      const fhs = Array.from((await zarr.get(fhArr)).data);
      return { root, fhs };
    })();
    p.catch(() => _storeCache.delete(cycleId)); // don't cache a failed open
    _storeCache.set(cycleId, p);
  }
  return _storeCache.get(cycleId);
}

/** A handle for one forecast hour: the shared store root + this hour's index. */
export async function openForecastHour(baseUrl, cycleId, forecastHour) {
  const { root, fhs } = await openStore(baseUrl, cycleId);
  const fhIndex = fhs.indexOf(forecastHour);
  if (fhIndex < 0) throw new Error(`forecast hour ${forecastHour} not in cycle ${cycleId}`);
  return { root, fhIndex };
}

/** Read a whole array (for coords that don't depend on forecast hour). */
async function readWhole(root, name) {
  const arr = await zarr.open(root.resolve(name), { kind: "array" });
  const chunk = await zarr.get(arr);
  return { data: chunk.data, shape: chunk.shape };
}

/** Undo scaled-int16 packing → Float32. Fields are stored as int16 with CF
 *  `scale_factor`/`add_offset` (and `_FillValue`=-32768 for NaN) to halve bytes;
 *  zarrita reads the raw ints, so we apply value = raw*scale + offset here. Vars
 *  written as plain float (constant/all-NaN fields) pass through untouched. */
function unpack(chunk, attrs) {
  const s = attrs?.scale_factor, o = attrs?.add_offset, fv = attrs?._FillValue;
  if (s == null && o == null && fv == null) return { data: chunk.data, shape: chunk.shape };
  const src = chunk.data, out = new Float32Array(src.length);
  const sf = s ?? 1, of = o ?? 0;
  for (let i = 0; i < src.length; i++) out[i] = src[i] === fv ? NaN : src[i] * sf + of;
  return { data: out, shape: chunk.shape };
}

/** Read a data variable at this handle's forecast hour as { data, shape }.
 *  Vars are dimensioned [forecast_hour, (isobaricInhPa,) y, x]; 3D vars take a
 *  level index. Returns a [y, x] slice either way. */
export async function readVariable(group, name, level = null) {
  const arr = await zarr.open(group.root.resolve(name), { kind: "array" });
  const selection = arr.shape.length === 4
    ? [group.fhIndex, level ?? 0, null, null]  // [fh, level, y, x]
    : [group.fhIndex, null, null];             // [fh, y, x]
  const chunk = await zarr.get(arr, selection);
  return unpack(chunk, arr.attrs);
}

/** Pressure levels (hPa) of the 3D fields, in storage order; [] if none. */
export async function readLevels(group) {
  try {
    return Array.from((await readWhole(group.root, "isobaricInhPa")).data);
  } catch {
    return [];
  }
}

/**
 * Read the grid's 2D latitude/longitude once and return a projection mapper
 * (lon/lat -> fractional grid i,j, calibrated on the SW corner) plus the
 * geographic bounding box. Used to reproject overlays into Web Mercator.
 */
export async function gridGeo(group) {
  const lat = await readWhole(group.root, "latitude");
  const lon = await readWhole(group.root, "longitude");
  const nx = lat.shape[1];
  const wrap = (x) => (x > 180 ? x - 360 : x); // 0..360 -> -180..180
  const mapper = makeGridMapper(wrap(lon.data[0]), lat.data[0]); // SW cell = (j=0,i=0)

  const lonW = new Float32Array(lon.data.length); // wrapped longitudes, kept for viewport stats
  let west = Infinity, east = -Infinity, north = -Infinity, south = Infinity;
  for (let k = 0; k < lat.data.length; k++) {
    const la = lat.data[k];
    const lo = wrap(lon.data[k]);
    lonW[k] = lo;
    if (la < south) south = la;
    if (la > north) north = la;
    if (lo < west) west = lo;
    if (lo > east) east = lo;
  }
  return { mapper, bbox: { west, east, north, south }, nx, lat: lat.data, lon: lonW };
}
