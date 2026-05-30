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

/** Open one forecast hour's Zarr group. */
export function openForecastHour(baseUrl, cycleId, forecastHour) {
  const ff = String(forecastHour).padStart(2, "0");
  const store = new zarr.FetchStore(`${baseUrl}/cycles/${cycleId}/f${ff}`);
  return zarr.open(store, { kind: "group" });
}

/** Read a 2D variable in full as { data, shape }. (3D vars: pass a level index.) */
export async function readVariable(group, name, level = null) {
  const arr = await zarr.open(group.resolve(name), { kind: "array" });
  const selection = arr.shape.length === 3 ? [level ?? 0, null, null] : null;
  const chunk = await zarr.get(arr, selection);
  return { data: chunk.data, shape: chunk.shape };
}

/** Pressure levels (hPa) of the 3D fields, in storage order; [] if none. */
export async function readLevels(group) {
  try {
    const arr = await zarr.open(group.resolve("isobaricInhPa"), { kind: "array" });
    return Array.from((await zarr.get(arr)).data);
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
  const lat = await readVariable(group, "latitude");
  const lon = await readVariable(group, "longitude");
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
