// HRRR grid <-> geographic projection (Lambert Conformal Conic, spherical).
//
// HRRR CONUS grid parameters (stable): standard parallels 38.5/38.5, central
// meridian -97.5, sphere radius 6371229 m, 3 km spacing. We don't hardcode the
// grid origin — it's calibrated at runtime from the data's SW corner lat/lon, so
// this stays correct if the grid ever shifts (and for RRFS later, swap the consts).

const D2R = Math.PI / 180;
const R = 6371229; // m (HRRR/WRF sphere)
const PHI1 = 38.5 * D2R; // standard parallel (tangent: phi1 == phi2)
const LAM0 = -97.5 * D2R; // central meridian
const PHI0 = 38.5 * D2R; // reference latitude
const DX = 3000; // m
const DY = 3000;

const n = Math.sin(PHI1);
const F = (Math.cos(PHI1) * Math.pow(Math.tan(Math.PI / 4 + PHI1 / 2), n)) / n;
const rho0 = (R * F) / Math.pow(Math.tan(Math.PI / 4 + PHI0 / 2), n);

/** Geographic (deg) -> Lambert projection coordinates (m). */
function project(lonDeg, latDeg) {
  const lat = latDeg * D2R;
  const rho = (R * F) / Math.pow(Math.tan(Math.PI / 4 + lat / 2), n);
  const theta = n * (lonDeg * D2R - LAM0);
  return { x: rho * Math.sin(theta), y: rho0 - rho * Math.cos(theta) };
}

/**
 * Build a mapper from (lon, lat) to fractional grid indices {i, j}, calibrated
 * so the SW grid cell (j=0, i=0) at (swLon, swLat) maps to (0, 0).
 */
export function makeGridMapper(swLon, swLat) {
  const o = project(swLon, swLat);
  return (lonDeg, latDeg) => {
    const p = project(lonDeg, latDeg);
    return { i: (p.x - o.x) / DX, j: (p.y - o.y) / DY };
  };
}

// Web Mercator latitude helpers (MapLibre interpolates images linearly in Y here).
export const mercY = (latDeg) => Math.log(Math.tan(Math.PI / 4 + (latDeg * D2R) / 2));
export const invMercY = (y) => (2 * Math.atan(Math.exp(y)) - Math.PI / 2) / D2R;
