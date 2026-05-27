// CONUS (lower-48) masking: clip the overlay and the coverage % to the US, so
// data over ocean / Canada / Mexico (which HRRR's grid covers) isn't shown or
// counted. Uses ray-casting point-in-polygon with bbox acceleration.

let polys = null; // [{bbox:[w,s,e,n], rings}]
let overall = null; // [w,s,e,n] over all of CONUS, for a fast first reject

/** Load the bundled CONUS GeoJSON; returns it (for drawing the outline). */
export async function loadConus(url = "conus.geojson") {
  const fc = await (await fetch(url)).json();
  polys = [];
  let w0 = Infinity, s0 = Infinity, e0 = -Infinity, n0 = -Infinity;
  for (const f of fc.features) {
    const multi = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
    for (const rings of multi) {
      let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
      for (const ring of rings) {
        for (const [lon, lat] of ring) {
          if (lon < w) w = lon; if (lon > e) e = lon;
          if (lat < s) s = lat; if (lat > n) n = lat;
        }
      }
      polys.push({ bbox: [w, s, e, n], rings });
      if (w < w0) w0 = w; if (e > e0) e0 = e; if (s < s0) s0 = s; if (n > n0) n0 = n;
    }
  }
  overall = [w0, s0, e0, n0];
  return fc;
}

function pointInRing(lon, lat, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
    if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

export function inCONUS(lon, lat) {
  if (!polys) return true;
  if (lon < overall[0] || lon > overall[2] || lat < overall[1] || lat > overall[3]) return false;
  for (const p of polys) {
    const [w, s, e, n] = p.bbox;
    if (lon < w || lon > e || lat < s || lat > n) continue;
    let inside = false; // even-odd across outer ring + holes
    for (const ring of p.rings) if (pointInRing(lon, lat, ring)) inside = !inside;
    if (inside) return true;
  }
  return false;
}

/** Uint8 CONUS mask over the native grid (1 = in CONUS). Computed once per grid. */
export function buildGridMask(lon, lat) {
  const mask = new Uint8Array(lon.length);
  for (let k = 0; k < lon.length; k++) mask[k] = inCONUS(lon[k], lat[k]) ? 1 : 0;
  return mask;
}
