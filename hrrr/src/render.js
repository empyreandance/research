// Reproject a native-grid field into a Web Mercator overlay for MapLibre.
//
// For each output (Mercator) pixel we find its lon/lat, map that to fractional
// HRRR grid indices via proj.makeGridMapper, sample the nearest cell, and color
// it with a caller-supplied function. Placing the result on a rectangular
// Mercator footprint preserves the Lambert grid's true (curved) boundaries.

import { invMercY, mercY } from "./proj.js";

export const OPS = {
  ">=": (v, t) => v >= t,
  ">": (v, t) => v > t,
  "<=": (v, t) => v <= t,
  "<": (v, t) => v < t,
  "==": (v, t) => v === t,
  "!=": (v, t) => v !== t,
};

/**
 * @param field {data, shape:[ny,nx]} native-grid array
 * @param colorFn (value) => [r,g,b,a] | null  (null = transparent)
 * @returns an object [dataUrl, coordinates] — coordinates are [TL, TR, BR, BL] lon/lat
 */
export function reproject({ data, shape }, colorFn, mapper, bbox, mask = null, outW = 1400) {
  const [ny, nx] = shape;
  const { west, east, north, south } = bbox;
  const yN = mercY(north);
  const yS = mercY(south);
  const lonRad = ((east - west) * Math.PI) / 180;
  const outH = Math.max(1, Math.round((outW * (yN - yS)) / lonRad));

  const canvas = document.createElement("canvas");
  canvas.width = outW;
  canvas.height = outH;
  const img = new ImageData(outW, outH);

  for (let py = 0; py < outH; py++) {
    const lat = invMercY(yN + (py / (outH - 1)) * (yS - yN));
    for (let px = 0; px < outW; px++) {
      const lon = west + (px / (outW - 1)) * (east - west);
      const { i, j } = mapper(lon, lat);
      const ii = Math.round(i);
      const jj = Math.round(j);
      const o = (py * outW + px) * 4;
      if (ii >= 0 && ii < nx && jj >= 0 && jj < ny && (!mask || mask[jj * nx + ii])) {
        const rgba = colorFn(data[jj * nx + ii]);
        if (rgba) {
          img.data[o] = rgba[0]; img.data[o + 1] = rgba[1];
          img.data[o + 2] = rgba[2]; img.data[o + 3] = rgba[3];
          continue;
        }
      }
      img.data[o + 3] = 0;
    }
  }
  canvas.getContext("2d").putImageData(img, 0, 0);
  return {
    dataUrl: canvas.toDataURL("image/png"),
    coordinates: [[west, north], [east, north], [east, south], [west, south]],
  };
}

// Sequential color ramp (YlOrRd-style) for t in [0,1].
const STOPS = [
  [255, 255, 178], [254, 204, 92], [253, 141, 60], [240, 59, 32], [189, 0, 38],
];
export function rampColor(t) {
  const x = Math.min(0.999, Math.max(0, t)) * (STOPS.length - 1);
  const i = Math.floor(x);
  const f = x - i;
  const a = STOPS[i];
  const b = STOPS[i + 1];
  return [0, 1, 2].map((k) => Math.round(a[k] + f * (b[k] - a[k])));
}

/** Build a colorFn for the count field: cells with count >= floor, graded 1..n. */
export function countColorFn(floor, n) {
  return (count) => {
    if (!(count >= floor) || count <= 0) return null;
    const t = n > 1 ? (count - 1) / (n - 1) : 1;
    const [r, g, b] = rampColor(t);
    return [r, g, b, 150 + Math.round(80 * t)];
  };
}
