/* LakeBand map page. Reads the run folder of design/realtime-design.md section 5.
   Plain canvas, no libraries. Every number shown comes from the run's files. */
'use strict';

// ------------------------------------------------------------------ constants
const LEADS = [15, 30, 45, 60, 90, 120, 180];
const COS43 = Math.cos(43 * Math.PI / 180);
const PALETTE = ['#d90429', '#6d28d9', '#0369a1', '#be185d', '#15803d', '#b45309'];   // the reviewed panels
const DEFAULT_SCALE = [                                                               // the reviewed panels
  [5, 7.5, '#bae6fd'], [7.5, 10, '#7dd3fc'], [10, 12.5, '#38bdf8'], [12.5, 15, '#0284c7'],
  [15, 17.5, '#4ade80'], [17.5, 20, '#16a34a'], [20, 22.5, '#15803d'], [22.5, 25, '#fde047'],
  [25, 27.5, '#eab308'], [27.5, 30, '#fb923c'], [30, 32.5, '#ea580c'], [32.5, 35, '#dc2626'],
  [35, 40, '#9333ea'], [40, null, '#581c87']];
const REASONS = {
  radar_stale: 'Newest radar is more than 15 minutes old; no forecast was issued for this lake.',
  radar_partial: 'A radar of this lake’s set is missing; coverage is smaller and more values are unknown.',
  detector_warmup: 'Fewer than 2 scans since start or since a gap; new splits cannot be confirmed yet.',
  history_short: 'Under 60 minutes of history for the band; its recent-motion inputs are missing.',
  model_stale: 'Newest allowed weather-model run is more than 6 hours old.',
  model_missing: 'The model’s weather-model, radar-wind, airport or lake inputs were not available; the forecast shown is persistence.',
  vad_missing: 'No usable radar wind profile in the last 30 minutes.',
  features_out_of_range: 'An input is outside the range seen in training.',
  fallback_persistence: 'The model could not run for this issuance; the forecast shown is persistence (the band stays where it is).',
  hrrr_late: 'The weather-model maps this issuance needs (the run allowed 90 minutes after it starts, and the run before) were not built yet; the forecast shown is persistence.',
  composite_late: 'The newest national radar composite this issuance may use was not in yet; the forecast shown is persistence.',
  lake_map_late: 'The lake temperature and ice map this issuance may use was not in yet; the forecast shown is persistence.',
  model_profiles_late: 'The weather-model profiles the environment inputs need were not built yet; the forecast shown is persistence.',
  lake_values_late: 'The previous day’s lake temperature was not posted yet; the forecast shown is persistence.',
  vad_history_short: 'Under 65 minutes of radar wind profiles since the start; the forecast shown is persistence.',
  lake_values_older: 'The previous day’s lake temperature was not posted yet, so the one before it was used.',
};
const FLAGS = {
  probably_not_lake_effect: 'probably not lake effect',
  outside_radar_inferred_lake_corridor: 'outside the lake’s downwind corridor',
  misaligned_with_lake_echo: 'not aligned with the lake echo',
  inland_quality_hold: 'inland band held on weak evidence',
  no_independent_flow_reference: 'no independent wind reference',
};
const params = new URLSearchParams(location.search);
// Where the page reads from: ONE line in config.js (window.LAKEBAND_SOURCE), overridable by ?source= for testing.
//   'run:<base>'      a pipeline run folder (status.json, latest.json, <lake>/radar|bands|forecast/...), as before
//   'bundles:<base>'  the publishing layout (src/pilot/rt/publish.py): latest.json pointer + one bundle per lake and scan
const SOURCE = (() => {
  const raw = params.get('source') || window.LAKEBAND_SOURCE || '';
  const m = /^(run|bundles):(.*)$/.exec(raw);
  if (m) return { kind: m[1], base: m[2] };
  return { kind: 'run', base: params.get('run') || (location.pathname.includes('/page/') ? '../' : 'run/') };
})();
const BUNDLES = SOURCE.kind === 'bundles';
const BASE = SOURCE.base;
// Cases mode (past storms; the CASES block near the end of this file): on only with ?case=<id> (or ?case= for the
// first storm). Its files: <base>cases/index.json and cases/<id>/... (src/pilot/cases/pack.py). Live and bundle
// modes never enter the Cases block.
const CASES = params.has('case');
const CASES_BASE = params.get('cases_base') || window.LAKEBAND_CASES || (BUNDLES ? BASE + 'cases/' : 'cases/');
const LOCAL_TZ = params.get('tz') || 'America/New_York';   // local time where the bands are

// ------------------------------------------------------------------ state
const S = {
  status: null, latest: null, lakes: [], index: {}, steps: [], step: -1, follow: true,
  lead: 0, playing: null, view: null, point: null, band: null, farOnly: null,
  showFarOnly: params.get('faronly') === '1',            // objects seen only in the far zone: hidden by default
  showPersist: params.get('persist') === '1',            // persistence beside a model forecast: off by default
  showTrain: params.get('train') !== '0',                 // the training area's boundary: on by default
  mosaicIx: null, mosaicBounds: null,                     // the display mosaic (MRMS over the whole map; mosaic.py)
  fixed: {}, cache: new Map(), basemap: null, sites: {}, scene: null,
};

// ------------------------------------------------------------------ small helpers
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
// file stamps: YYYYMMDDTHHMMZ (scans, :00/:30 forecasts) or YYYYMMDDTHHMMSSZ (forecasts issued at a scan, bundles)
const parseStamp = s => Date.UTC(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8), +s.slice(9, 11), +s.slice(11, 13),
  /^\d{8}T\d{6}Z/.test(s) ? +s.slice(13, 15) : 0);
const STAMP_RE = /^\d{8}T\d{4}(\d{2})?Z$/;
const tIso = s => (s ? Date.parse(s) : NaN);
function fmtUTC(ms, sec) {
  if (!isFinite(ms)) return '—';
  const d = new Date(ms).toISOString();
  return d.slice(11, sec ? 19 : 16) + ' UTC';
}
function fmtLocal(ms) {
  if (!isFinite(ms)) return '';
  return new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', timeZoneName: 'short', timeZone: LOCAL_TZ })
    .format(new Date(ms));
}
function fmtDate(ms) {
  return new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
    .format(new Date(ms));
}
const both = (ms, sec) => isFinite(ms) ? `<span class="utc">${fmtUTC(ms, sec)}</span> · ${fmtLocal(ms)}` : '—';
function ago(min) {
  if (!isFinite(min)) return '';
  const m = Math.round(min);
  if (Math.abs(m) < 120) return `${m} min`;
  if (Math.abs(m) < 2880) return `${(m / 60).toFixed(1)} h`;
  return `${Math.round(m / 1440)} days`;
}
const r1 = v => (v == null || !isFinite(v) ? null : Math.round(v * 10) / 10);
function hashColour(id) {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}
// bands joined through the far zone (one display group) share the colour of their first (westernmost) member
const colourOf = p => hashColour((p.display_group_members && p.display_group_members[0]) || p.band_id);
const groupOf = p => p.display_group || p.band_id;
const inGroup = p => (p.display_group_members || []).length > 1;
function lighten(hex, f) {               // mix with white: far-zone parts are drawn visibly lighter
  const v = [1, 3, 5].map(q => parseInt(hex.slice(q, q + 2), 16)).map(c => Math.round(c + (255 - c) * f));
  return '#' + v.map(c => c.toString(16).padStart(2, '0')).join('');
}
const FAR_ONLY_COLOUR = '#475569';
const LIMIT2 = '#111827', LIMIT3 = '#713f12';            // 2 km (solid) and 3 km (dashed) beam limits
function parseBandId(id) {
  const [lake, tier, track] = String(id).split('|');
  return { lake, tier: tier === 'T2' ? 2 : 1, track: track || id };
}
// the uncertainty region of a forecast: the documented 80% corridor, else the pipeline's envelope
const envOf = p => p.corridor_80 || p.envelope || null;
function envName(sc) {
  for (const lake of S.lakes) {
    const fc = sc && sc.lakes[lake] && sc.lakes[lake].forecast;
    if (fc) for (const f of fc.features) {
      const p = f.properties;
      if (p.corridor_80) return { short: '80% range', map: '80% range, sideways position only', long: '80% range for sideways position; widths and ends in it are the median forecast' };
      if (p.envelope) {
        const r = p.envelope_radius_km != null ? `±${r1(p.envelope_radius_km)} km` : '';
        return { short: 'envelope', map: `uncertainty envelope ${r}`.trim(), long: `uncertainty envelope ${r}${p.envelope_kind ? ' (' + p.envelope_kind + ')' : ''}` };
      }
    }
  }
  return { short: 'range', map: 'uncertainty range', long: 'uncertainty range' };
}
const lakeName = l => ({ erie: 'Lake Erie', ontario: 'Lake Ontario' }[l] || l);
function dupTracks(sc) {
  // track ids are numbered per lake (and possibly per tier): find ids used by more than one lake or tier
  const seen = new Map();
  for (const lake of S.lakes) {
    const L = sc && sc.lakes[lake]; if (!L) continue;
    for (const fc of [L.bands, L.forecast]) if (fc) for (const f of fc.features) {
      const b = parseBandId(f.properties.band_id);
      if (!seen.has(b.track)) seen.set(b.track, new Set());
      seen.get(b.track).add(`${b.lake}|${b.tier}`);
    }
  }
  const out = new Map();
  for (const [t, set] of seen) if (set.size > 1) {
    const keys = [...set];
    out.set(t, { lakes: new Set(keys.map(k => k.split('|')[0])).size > 1, tiers: new Set(keys.map(k => k.split('|')[1])).size > 1 });
  }
  return out;
}
function labelOf(bandId, forBadge) {
  const b = parseBandId(bandId), d = S.dups && S.dups.get(b.track);
  let t = b.track;
  if (d && d.lakes) t += ' ' + lakeName(b.lake).replace('Lake ', '');
  if (b.tier === 2 && (forBadge || (d && d.tiers))) t += ' inland';
  return t;
}
// one label for a display group: its members' identifiers, west to east ("B011/B025")
function groupLabel(p, forBadge) {
  if (!inGroup(p)) return labelOf(p.band_id, forBadge);
  const ids = p.display_group_members.map(id => labelOf(id, false));
  return ids.join('/') + (p.tier === 2 && forBadge ? ' inland' : '');
}
function compass(az) {
  const n = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  return n[Math.round((((az % 360) + 360) % 360) / 22.5) % 16];
}

function latestLakes(latest) {
  const src = latest.lakes && typeof latest.lakes === 'object' && !Array.isArray(latest.lakes) ? latest.lakes : latest;
  const out = {};
  for (const [k, v] of Object.entries(src)) if (v && typeof v === 'object' && !Array.isArray(v) && (v.bands || v.radar || v.radar_image || v.forecast)) out[k] = v;
  return out;
}
async function getJSON(url, fresh) {
  const r = await fetch(url, { cache: fresh ? 'no-store' : 'no-cache' });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
// a bundle: gzip-compressed JSON; decompressed here unless the host already did (Content-Encoding: gzip)
async function getBundle(url) {
  const r = await fetch(url, { cache: 'no-cache' });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  const buf = new Uint8Array(await r.arrayBuffer());
  let text;
  if (buf[0] === 0x1f && buf[1] === 0x8b) {
    const ds = new Blob([buf]).stream().pipeThrough(new DecompressionStream('gzip'));
    text = await new Response(ds).text();
  } else text = new TextDecoder().decode(buf);
  return JSON.parse(text);
}
function cached(key, make) {
  if (S.cache.has(key)) { const v = S.cache.get(key); S.cache.delete(key); S.cache.set(key, v); return v; }
  const p = make().catch(e => { S.cache.delete(key); throw e; });
  S.cache.set(key, p);
  while (S.cache.size > 80) S.cache.delete(S.cache.keys().next().value);
  return p;
}
function loadImage(url) {
  return new Promise((res, rej) => {
    const im = new Image();
    // the mosaic comes from the data domain: ask with CORS so its pixels stay readable (getImageData below)
    if (!String(url).startsWith('data:')) im.crossOrigin = 'anonymous';
    im.onload = () => {
      const c = document.createElement('canvas');
      c.width = im.naturalWidth; c.height = im.naturalHeight;
      const x = c.getContext('2d', { willReadFrequently: true });
      x.drawImage(im, 0, 0);
      res({ img: im, pix: x.getImageData(0, 0, c.width, c.height) });
    };
    im.onerror = () => rej(new Error(url));
    im.src = url;
  });
}

// ------------------------------------------------------------------ run index
async function loadIndex() {
  if (CASES) return casesIndex();
  if (BUNDLES) {                               // the pointer lists the bundles kept (about a day) per lake
    const out = {};
    for (const [lake, b] of Object.entries((S.latest && S.latest.bundles) || {}))
      out[lake] = { bands: [...b.names], radar: [...b.names], forecast: [...b.names], folder: b.folder, suffix: b.suffix };
    return out;
  }
  try {
    const j = await getJSON('api/index', true);
    return j.lakes;
  } catch (e) {
    // plain static server (python -m http.server): read the directory listings instead
    const lakes = {};
    for (const lake of Object.keys(latestLakes(S.latest || {}))) {
      lakes[lake] = {};
      for (const [kind, ext] of [['radar', '.png'], ['bands', '.geojson'], ['forecast', '.geojson'], ['forecast_persistence', '.geojson']]) {
        // (plain static server: the folder listings)
        try {
          const html = await (await fetch(`${BASE}${lake}/${kind}/`, { cache: 'no-store' })).text();
          lakes[lake][kind] = [...html.matchAll(/href="([0-9]{8}T[0-9]{4}(?:[0-9]{2})?Z)\.(png|geojson)"/g)]
            .filter(m => '.' + m[2] === ext).map(m => m[1]).sort();
        } catch (_) { lakes[lake][kind] = []; }
      }
    }
    return lakes;
  }
}
const latestIn = (arr, t) => {
  let best = null;
  for (const s of arr || []) if (parseStamp(s) <= t) best = s; else break;
  return best;
};

function rebuildSteps() {
  const set = new Set();
  for (const lake of S.lakes) {
    for (const s of (S.index[lake] || {}).bands || []) set.add(parseStamp(s));
    for (const s of (S.index[lake] || {}).forecast || []) set.add(parseStamp(s));
  }
  S.steps = [...set].sort((a, b) => a - b);
  const sc = $('scrub');
  sc.max = Math.max(0, S.steps.length - 1);
  if (S.follow || S.step < 0) S.step = S.steps.length - 1;
  S.step = Math.min(S.step, S.steps.length - 1);
  sc.value = S.step;
}

// ------------------------------------------------------------------ scene loading
async function lakeFixed(lake) {
  if (CASES) { if (!S.fixed[lake]) S.fixed[lake] = Promise.resolve(casesFixed(lake)); return S.fixed[lake]; }
  if (!S.fixed[lake]) {
    S.fixed[lake] = (async () => {
      if (BUNDLES) {
        const st = ((S.latest && S.latest.static) || {})[lake] || {};
        const [bounds, coverage, coverageFar] = await Promise.all(['bounds.json', 'coverage.geojson', 'coverage_far.geojson']
          .map(k => st[k] ? getJSON(BASE + st[k]).catch(() => null) : Promise.resolve(null)));
        return { bounds, coverage, coverageFar };
      }
      const v = (S.latestLakes || {})[lake] || {};
      const [bounds, coverage, coverageFar] = await Promise.all([
        getJSON(`${BASE}${lake}/radar/bounds.json`).catch(() => v.radar_bounds ? getJSON(BASE + v.radar_bounds) : null).catch(() => null),
        getJSON(`${BASE}${lake}/coverage.geojson`).catch(() => v.coverage ? getJSON(BASE + v.coverage) : null).catch(() => null),
        // two-zone runs only: the 3 km limit and the far zone (older runs have no such file)
        v.coverage_far ? getJSON(BASE + v.coverage_far).catch(() => null) : Promise.resolve(null)]);
      return { bounds, coverage, coverageFar };
    })();
  }
  return S.fixed[lake];
}

async function loadScene() {
  if (S.step < 0 || !S.steps.length) return null;
  const t = S.steps[S.step];
  const out = { t, lakes: {} };
  await Promise.all(S.lakes.map(async lake => {
    const ix = S.index[lake] || {};
    const fixed = await lakeFixed(lake);
    if (CASES) { out.lakes[lake] = await casesLake(lake, t, fixed); return; }
    if (BUNDLES) {                             // one file per scan: radar, far radar, bands, forecast, persistence
      const name = latestIn(ix.bands, t);
      const b = name ? await cached(`B/${lake}/${name}`, () => getBundle(`${BASE}${ix.folder}${name}${ix.suffix}`)).catch(() => null) : null;
      const [radar, radarFar] = b ? await Promise.all([b.radar_png ? cached(`rB/${lake}/${name}`, () => loadImage(b.radar_png)).catch(() => null) : null,
        b.radar_far_png ? cached(`rfB/${lake}/${name}`, () => loadImage(b.radar_far_png)).catch(() => null) : null]) : [null, null];
      out.lakes[lake] = { ...fixed, bands: b && b.bands, radar, radarFar, forecast: b && b.forecast, persistence: b && b.persistence,
        bundle: b && { name, issuance_time: b.issuance_time, issue_kind: b.issue_kind, forecast_from_scan: b.forecast_from_scan,
                       radar_scan_end: b.radar_scan_end },
        stamps: { bs: name, rs: name, fs: name, ps: name } };
      return;
    }
    const bs = latestIn(ix.bands, t);
    const rs = ix.radar && ix.radar.includes(bs) ? bs : latestIn(ix.radar, t);
    const fs = latestIn(ix.forecast, t);
    // persistence written beside a model forecast of the same issuance (none when persistence is the forecast)
    const ps = fs && (ix.forecast_persistence || []).includes(fs) ? fs : null;
    const [bands, radar, forecast, persistence] = await Promise.all([
      bs ? cached(`b/${lake}/${bs}`, () => getJSON(`${BASE}${lake}/bands/${bs}.geojson`)).catch(() => null) : null,
      rs ? cached(`r/${lake}/${rs}`, () => loadImage(`${BASE}${lake}/radar/${rs}.png`)).catch(() => null) : null,
      fs ? cached(`f/${lake}/${fs}`, () => getJSON(`${BASE}${lake}/forecast/${fs}.geojson`)).catch(() => null) : null,
      ps ? cached(`p/${lake}/${ps}`, () => getJSON(`${BASE}${lake}/forecast_persistence/${ps}.geojson`)).catch(() => null) : null]);
    // far-zone echo (presence only) of the same scan as the bands, when the run has the two-zone overlay
    const rf = bands && bands.radar_far_image;
    const radarFar = rf ? await cached(`rf/${lake}/${rf}`, () => loadImage(BASE + rf)).catch(() => null) : null;
    out.lakes[lake] = { ...fixed, bands, radar, radarFar, forecast, persistence, stamps: { bs, rs, fs, ps } };
  }));
  out.mosaic = await mosaicAt(t);
  return out;
}

// the display mosaic (MRMS over the whole map; mosaic.py): the one nearest the middle of the scan (scan start + 4
// minutes), at most 15 minutes away. MRMS runs nearer real time than the newest scan, which is why "nearest".
async function mosaicAt(t) {
  const ix = S.mosaicIx;
  if (CASES || !ix || !S.mosaicBounds || !isFinite(t)) return null;
  let name = null, best = 15 * 60000;
  for (const s of ix.names || []) {
    const d = Math.abs(parseStamp(s) - (t + 4 * 60000));
    if (d <= best) { best = d; name = s; }
  }
  if (!name) return null;
  const m = await cached(`M/${name}`, () => loadImage(`${BASE}${ix.folder}${name}${ix.suffix || '.png'}`)).catch(() => null);
  return m ? { ...m, name } : null;
}

// ------------------------------------------------------------------ projection and view
const cv = $('map');
const ctx = cv.getContext('2d');
let W = 0, H = 0, DPR = 1;
function resize() {
  const head = $('top').getBoundingClientRect().height;
  document.documentElement.style.setProperty('--head', `${Math.ceil(head)}px`);
  const r = cv.getBoundingClientRect();
  DPR = window.devicePixelRatio || 1;
  W = r.width; H = r.height;
  cv.width = Math.round(W * DPR); cv.height = Math.round(H * DPR);
  draw();
}
const px = (lon, lat) => [(lon - S.view.lon) * S.view.ppd * COS43 + W / 2, (S.view.lat - lat) * S.view.ppd + H / 2];
const unpx = (x, y) => [S.view.lon + (x - W / 2) / (S.view.ppd * COS43), S.view.lat - (y - H / 2) / S.view.ppd];
function extentAll() {
  let w = 180, e = -180, s = 90, n = -90;
  for (const lake of S.lakes) {
    const b = S.scene && S.scene.lakes[lake] && S.scene.lakes[lake].bounds;
    if (!b) continue;
    const [bw, be, bs, bn] = boundsBox(b);
    w = Math.min(w, bw); e = Math.max(e, be); s = Math.min(s, bs); n = Math.max(n, bn);
  }
  return w < e ? [w, e, s, n] : [-82.3, -75.0, 41.0, 44.5];
}
function bandsExtent() {
  let w = 180, e = -180, s = 90, n = -90;
  if (S.scene) for (const f of currentBands(S.scene)) for (const l of linesOf(f.geometry)) for (const [x, y] of l) {
    w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y);
  }
  if (!(w < e)) return null;
  const cx = (w + e) / 2, cy = (s + n) / 2, hw = Math.max((e - w) / 2 + 0.3, 1.0), hh = Math.max((n - s) / 2 + 0.25, 0.7);
  return [cx - hw, cx + hw, cy - hh, cy + hh];
}
function fitView(toBands) {
  const [w, e, s, n] = (toBands && bandsExtent()) || extentAll();
  const ppd = Math.min((W - 30) / ((e - w) * COS43), (H - 30) / (n - s));
  S.view = { lon: (w + e) / 2, lat: (s + n) / 2, ppd };
}
function zoomAt(f, x, y) {
  const [lon, lat] = unpx(x, y);
  S.view.ppd = Math.max(40, Math.min(6000, S.view.ppd * f));
  const [lon2, lat2] = unpx(x, y);
  S.view.lon += lon - lon2; S.view.lat += lat - lat2;
  draw();
}
function boundsBox(b) {
  if (b.west != null) return [b.west, b.east, b.south, b.north];
  if (b.corners_lonlat) { const sw = b.corners_lonlat.south_west, ne = b.corners_lonlat.north_east; return [sw[0], ne[0], sw[1], ne[1]]; }
  const cs = Array.isArray(b.corners) ? b.corners : Object.values(b.corners || {});
  const xs = cs.map(c => c[0]), ys = cs.map(c => c[1]);
  return [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
}
// image rows: evenly spaced in latitude (default) or in Web Mercator y (said so in bounds.json)
const mercY = lat => Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
const isMerc = b => /mercator|3857/i.test(`${b.rows || ''} ${b.projection || ''} ${b.grid || ''}`) && !/row 0 is north; regular/i.test(b.grid || '');
function rowOfLat(b, lat, h) {          // fractional image row (0 = north edge) of a latitude
  const [, , s, n] = boundsBox(b);
  return isMerc(b) ? (mercY(n) - mercY(lat)) / (mercY(n) - mercY(s)) * h : (n - lat) / (n - s) * h;
}
function latOfRow(b, r, h) {
  const [, , s, n] = boundsBox(b);
  if (!isMerc(b)) return n - r / h * (n - s);
  const y = mercY(n) - r / h * (mercY(n) - mercY(s));
  return (2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180 / Math.PI;
}

// ------------------------------------------------------------------ geometry helpers
function polysOf(g) {
  if (!g) return [];
  if (g.type === 'Polygon') return [g.coordinates];
  if (g.type === 'MultiPolygon') return g.coordinates;
  return [];
}
function linesOf(g) {
  if (!g) return [];
  if (g.type === 'LineString') return [g.coordinates];
  if (g.type === 'MultiLineString') return g.coordinates;
  if (g.type === 'Polygon') return g.coordinates;
  if (g.type === 'MultiPolygon') return g.coordinates.flat();
  return [];
}
function pathPolys(g) {
  for (const poly of polysOf(g)) for (const ring of poly) {
    ring.forEach(([lon, lat], i) => { const [x, y] = px(lon, lat); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.closePath();
  }
}
function pathLines(lines) {
  for (const line of lines) line.forEach(([lon, lat], i) => { const [x, y] = px(lon, lat); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
}
function inRing(ring, lon, lat) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > lat) !== (yj > lat) && lon < (xj - xi) * (lat - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
function inGeom(g, lon, lat) {
  for (const poly of polysOf(g)) {
    if (!poly.length || !inRing(poly[0], lon, lat)) continue;
    if (!poly.slice(1).some(h => inRing(h, lon, lat))) return true;
  }
  return false;
}
const kmBetween = (a, b) => Math.hypot((a[0] - b[0]) * 111.32 * Math.cos((a[1] + b[1]) * Math.PI / 360), (a[1] - b[1]) * 110.57);
function lineKm(lines) { let s = 0; for (const l of lines) for (let i = 1; i < l.length; i++) s += kmBetween(l[i - 1], l[i]); return s; }
function longestLine(lines) { let best = null, bl = -1; for (const l of lines) { const k = lineKm([l]); if (k > bl) { bl = k; best = l; } } return best; }
function pointAlong(line, frac) {
  const total = lineKm([line]); let acc = 0;
  for (let i = 1; i < line.length; i++) {
    const d = kmBetween(line[i - 1], line[i]);
    if (acc + d >= frac * total) { const f = d ? (frac * total - acc) / d : 0; return [line[i - 1][0] + f * (line[i][0] - line[i - 1][0]), line[i - 1][1] + f * (line[i][1] - line[i - 1][1])]; }
    acc += d;
  }
  return line[line.length - 1];
}
function distToLinesPx(lines, x, y) {
  let best = Infinity;
  for (const l of lines) for (let i = 1; i < l.length; i++) {
    const [ax, ay] = px(...l[i - 1]), [bx, by] = px(...l[i]);
    const dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
    const t = L ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / L)) : 0;
    best = Math.min(best, Math.hypot(x - ax - t * dx, y - ay - t * dy));
  }
  return best;
}

// ------------------------------------------------------------------ drawing
const hatchCache = {};
function hatch(colour) {
  if (hatchCache[colour]) return hatchCache[colour];
  const c = document.createElement('canvas'); const n = Math.round(12 * DPR);
  c.width = c.height = n;
  const x = c.getContext('2d');
  x.fillStyle = colour; x.globalAlpha = 0.13; x.fillRect(0, 0, n, n);
  x.globalAlpha = 0.75; x.strokeStyle = colour; x.lineWidth = 2 * DPR;
  x.beginPath(); x.moveTo(-1, n + 1); x.lineTo(n + 1, -1); x.moveTo(-1, 1); x.lineTo(1, -1); x.moveTo(n - 1, n + 1); x.lineTo(n + 1, n - 1); x.stroke();
  return (hatchCache[colour] = ctx.createPattern(c, 'repeat'));
}
function strokeHalo(width, colour, dash, halo = 3) {
  ctx.setLineDash([]); ctx.lineJoin = 'round'; ctx.lineCap = 'round';
  ctx.strokeStyle = '#ffffff'; ctx.lineWidth = width + 2 * halo; ctx.stroke();
  ctx.setLineDash(dash || []); ctx.strokeStyle = colour; ctx.lineWidth = width; ctx.stroke();
  ctx.setLineDash([]);
}
function textHalo(text, x, y, font, colour, align = 'center') {
  ctx.font = font; ctx.textAlign = align; ctx.textBaseline = 'middle';
  ctx.lineWidth = 5; ctx.strokeStyle = '#ffffff'; ctx.lineJoin = 'round'; ctx.strokeText(text, x, y);
  ctx.fillStyle = colour; ctx.fillText(text, x, y);
}

let placed = [];
const overlaps = r => placed.some(p => r.x < p.x + p.w && r.x + r.w > p.x && r.y < p.y + p.h && r.y + r.h > p.y);
function segHitsRect(ax, ay, bx, by, r) {
  for (let k = 0; k <= 12; k++) {
    const x = ax + (bx - ax) * k / 12, y = ay + (by - ay) * k / 12;
    if (x > r.x && x < r.x + r.w && y > r.y && y < r.y + r.h) return true;
  }
  return false;
}
function badge(text, anchor, colour, style, avoid) {
  // style: 'now' filled, 'inland' white with coloured border, 'fc' white with dashed coloured border.
  // Only the position is chosen here; flushBadges draws every leader first and every box after.
  ctx.font = BADGE_FONT;
  const w = ctx.measureText(text).width + 18, h = 30;
  const [ax, ay] = anchor;
  const cands = [];
  for (const d of [44, 66, 92, 124, 160]) for (const [ox, oy] of [[0, -d], [0, d], [d, -d * 0.6], [-d, -d * 0.6], [d, d * 0.6], [-d, d * 0.6], [d * 1.4, 0], [-d * 1.4, 0]]) cands.push([ax + ox - w / 2, ay + oy - h / 2]);
  let pos = null;
  // pass 1 keeps clear of band lines, pass 2 only of other labels; a forecast label that fits nowhere is left out
  for (const strict of [true, false]) {
    for (const [x, y] of cands) {
      const r = { x, y, w, h };
      if (x < 4 || y < 4 || x + w > W - 4 || y + h > H - S.bottomReserve) continue;
      if (x + w > W - 64 && y < 180) continue;          // the zoom buttons' column (top right)
      if (overlaps(r) || (strict && avoid && avoid(r))) continue;
      const ex = Math.max(r.x, Math.min(ax, r.x + w)), ey = Math.max(r.y, Math.min(ay, r.y + h));
      if (placed.some(p => segHitsRect(ax, ay, ex, ey, p))) continue;
      pos = r; break;
    }
    if (pos) break;
  }
  if (!pos && style === 'fc') return null;
  if (!pos) {                         // no free spot: a compact label on the band itself
    ctx.font = '700 17px -apple-system, Helvetica, Arial, sans-serif';
    const cw = ctx.measureText(text).width + 6;
    pos = { x: ax - cw / 2, y: ay - 26, w: cw, h: 22 };
    placed.push(pos);
    S.badges.push({ pos, anchor, colour, style: 'compact', text });
    return pos;
  }
  placed.push(pos);
  S.badges.push({ pos, anchor, colour, style, text });
  return pos;
}
function flushBadges() {
  S.lastLabels = S.badges.map(b => b.text);
  for (const b of S.badges) {
    const { pos, colour, style } = b, [ax, ay] = b.anchor;
    if (style === 'compact') continue;
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(Math.max(pos.x, Math.min(ax, pos.x + pos.w)), Math.max(pos.y, Math.min(ay, pos.y + pos.h)));
    strokeHalo(2, colour, style === 'fc' ? [5, 4] : [], 2);
    ctx.beginPath(); ctx.arc(ax, ay, 4, 0, 2 * Math.PI); ctx.fillStyle = colour; ctx.fill();
  }
  ctx.font = BADGE_FONT;
  for (const b of S.badges) {
    const { pos, colour, style, text } = b;
    if (style === 'compact') { textHalo(text, pos.x + pos.w / 2, pos.y + pos.h / 2, '700 17px -apple-system, Helvetica, Arial, sans-serif', '#0f172a'); ctx.font = BADGE_FONT; continue; }
    ctx.beginPath(); ctx.roundRect(pos.x, pos.y, pos.w, pos.h, 4);
    ctx.fillStyle = style === 'now' ? colour : '#ffffff'; ctx.fill();
    ctx.setLineDash(style === 'fc' ? [6, 4] : []);
    ctx.lineWidth = style === 'now' ? 2 : 3; ctx.strokeStyle = style === 'now' ? '#ffffff' : colour; ctx.stroke();
    ctx.setLineDash([]);
    if (style === 'now') { ctx.beginPath(); ctx.roundRect(pos.x - 1.5, pos.y - 1.5, pos.w + 3, pos.h + 3, 5); ctx.lineWidth = 1.5; ctx.strokeStyle = '#0f172a'; ctx.stroke(); }
    ctx.fillStyle = style === 'now' ? '#ffffff' : '#0f172a';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(text, pos.x + pos.w / 2, pos.y + pos.h / 2 + 1);
  }
  S.badges = [];
}
const BADGE_FONT = '700 18px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

function draw() {
  if (!S.view || !W) return;
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#f8fafc'; ctx.fillRect(0, 0, W, H);
  placed = [];
  S.hits = [];
  S.linePts = [];
  S.badges = [];
  // room kept free of labels for the colour-bar box (and, on narrow screens, the distance bar above it)
  S.bottomReserve = W <= 640 ? 150 : S.scene && hasFar(S.scene) ? 112 : 84;
  const B = S.basemap;
  if (B) {
    ctx.beginPath(); for (const l of B.lakes) pathPolys(l.geometry);
    ctx.fillStyle = '#f1f6fc'; ctx.fill('evenodd');      // light, so 5-10 dBZ snow (pale blues) shows over the water
  }
  const sc = S.scene;
  if (sc && sc.mosaic && S.mosaicBounds) {
    // total coverage: the MRMS mosaic over the whole map, then, inside the training area, the radar LakeBand read
    ctx.imageSmoothingEnabled = false;
    drawRadarImage(sc.mosaic.img, S.mosaicBounds);
    for (const lake of S.lakes) {
      const L = sc.lakes[lake];
      if (L && L.bounds && L.radar) drawRadarImage(L.radar.img, L.bounds);
    }
    ctx.imageSmoothingEnabled = true;
  } else if (sc) {
  // no mosaic (past storms for now): outside radar coverage, grey over the land and water, under the radar. With the
  // two-zone overlay the grey starts at the 3 km limit and the far zone (beam 2-3 km above ground) gets a lighter grey.
    const off = document.createElement('canvas'); off.width = cv.width; off.height = cv.height;
    const o = off.getContext('2d'); o.setTransform(DPR, 0, 0, DPR, 0, 0);
    o.fillStyle = 'rgba(148,163,184,0.55)'; o.fillRect(0, 0, W, H);
    o.globalCompositeOperation = 'destination-out';
    const cut = g => {
      o.beginPath();
      for (const poly of polysOf(g)) for (const ring of poly) {
        ring.forEach(([lon, lat], i) => { const [x, y] = px(lon, lat); i ? o.lineTo(x, y) : o.moveTo(x, y); }); o.closePath();
      }
      o.fill('evenodd');
    };
    for (const lake of S.lakes) {
      const L = sc.lakes[lake]; if (!L) continue;
      if (L.coverage) for (const f of L.coverage.features) cut(f.geometry);
      for (const f of farFeatures(L, 'near+far')) cut(f.geometry);
    }
    ctx.drawImage(off, 0, 0, W, H);
    for (const lake of S.lakes) {
      const L = sc.lakes[lake]; if (!L) continue;
      for (const f of farFeatures(L, 'far')) { ctx.beginPath(); pathPolys(f.geometry); ctx.fillStyle = 'rgba(148,163,184,0.30)'; ctx.fill('evenodd'); }
    }
    // radar: reflectivity in the near zone; far-zone echo as presence only (one grey, never on the dBZ scale)
    ctx.imageSmoothingEnabled = false;
    for (const lake of S.lakes) {
      const L = sc.lakes[lake];
      if (!L || !L.bounds) continue;
      if (L.radar) drawRadarImage(L.radar.img, L.bounds);
      if (L.radarFar) { ctx.globalAlpha = 0.6; drawRadarImage(L.radarFar.img, L.bounds); ctx.globalAlpha = 1; }
    }
    ctx.imageSmoothingEnabled = true;
  }
  if (B) {
    const z = S.view.ppd;
    if (z > 90) {
      ctx.beginPath(); for (const c of B.counties) pathLines(linesOf(c.geometry));
      ctx.strokeStyle = 'rgba(71,85,105,0.55)'; ctx.lineWidth = 1; ctx.stroke();
    }
    ctx.beginPath(); for (const b of B.borders.filter(b => b.level === 'admin1')) pathLines(linesOf(b.geometry));
    ctx.strokeStyle = '#334155'; ctx.lineWidth = 1.8; ctx.setLineDash([2, 4]); ctx.stroke(); ctx.setLineDash([]);
    ctx.beginPath(); for (const b of B.borders.filter(b => b.level === 'admin0')) pathLines(linesOf(b.geometry));
    ctx.strokeStyle = '#334155'; ctx.lineWidth = 2; ctx.setLineDash([10, 4, 2, 4]); ctx.stroke(); ctx.setLineDash([]);
    ctx.beginPath(); for (const l of B.lakes) pathPolys(l.geometry);
    ctx.strokeStyle = '#0f172a'; ctx.lineWidth = 1.8; ctx.stroke();
  }
  if (sc) {
    if (S.showTrain) {
      // the training area: where LakeBand finds and forecasts bands (each lake's box, inside the 2 km beam limit),
      // dashed as the coverage edge of the reviewed panels; the colour is not in the band palette
      ctx.beginPath(); pathLines(trainingEdges(sc));
      ctx.strokeStyle = TRAIN_COLOUR; ctx.lineWidth = 2.5; ctx.setLineDash([9, 6]); ctx.stroke(); ctx.setLineDash([]);
    }
    drawSites(sc);
    drawBands(sc);
  }
  drawPoint();
  drawLabelsLast(sc);
  drawScale();
}

function drawRadarImage(img, bounds) {
  const [w, e, s, n] = boundsBox(bounds);
  const ih = img.naturalHeight, iw = img.naturalWidth;
  if (!isMerc(bounds)) {
    const [x0, y0] = px(w, n), [x1, y1] = px(e, s);
    ctx.drawImage(img, x0, y0, x1 - x0, y1 - y0);
    return;
  }
  const x0 = px(w, n)[0], x1 = px(e, n)[0];
  const dev = y => Math.round(y * DPR) / DPR;          // row edges on whole device pixels: rows meet, never overlap
  for (let r = 0; r < ih; r++) {
    const ya = dev(px(w, latOfRow(bounds, r, ih))[1]), yb = dev(px(w, latOfRow(bounds, r + 1, ih))[1]);
    if (yb < 0 || ya > H || yb <= ya) continue;
    ctx.drawImage(img, 0, r, iw, 1, x0, ya, x1 - x0, yb - ya);
  }
}

// ------------------------------------------------------------------ two zones (runs with the overlay only)
const farFeatures = (L, zone) => (L && L.coverageFar ? L.coverageFar.features.filter(f => f.properties.zone === zone && f.geometry) : []);
const hasFar = sc => !!sc && S.lakes.some(l => farFeatures(sc.lakes[l], 'far').length);
function farOnlyObjects(sc) {
  const out = [];
  for (const lake of S.lakes) { const b = sc.lakes[lake] && sc.lakes[lake].bands; if (b && b.far_only_features) out.push(...b.far_only_features); }
  return out;
}
function inFarZone(sc, lon, lat) {
  return S.lakes.some(l => farFeatures(sc.lakes[l], 'far').some(f => inGeom(f.geometry, lon, lat)));
}
function farEchoAt(sc, lon, lat) {         // presence only: the far image has one colour where there is echo
  for (const lake of S.lakes) {
    const L = sc.lakes[lake]; if (!L || !L.radarFar || !L.bounds) continue;
    const [w, e, s, n] = boundsBox(L.bounds);
    if (lon < w || lon > e || lat < s || lat > n) continue;
    const { pix } = L.radarFar;
    const i = Math.min(pix.width - 1, Math.floor((lon - w) / (e - w) * pix.width));
    const j = Math.min(pix.height - 1, Math.floor(rowOfLat(L.bounds, lat, pix.height)));
    return pix.data[4 * (j * pix.width + i) + 3] > 0;
  }
  return null;
}
const coverageEdges = sc => limitEdges(sc, 'near');
// the training area's whole outline: the 2 km beam limit AND the lake boxes' own edges, merged across the lakes
const trainingEdges = sc => limitEdges(sc, 'near', true);
const TRAIN_COLOUR = '#c2410c';

function limitEdges(sc, which, withBox = false) {
  // which = 'near': the edge of coverage.geojson (beam 2 km); 'far': the edge of the 3 km area (coverage_far.geojson)
  const featsOf = L => which === 'near' ? (L && L.coverage ? L.coverage.features : []) : farFeatures(L, 'near+far');
  const key = which + (withBox ? '+box' : '') + ':' + S.lakes.map(l => (featsOf(sc.lakes[l]).length ? l : '')).join('|');
  S.edgeCache = S.edgeCache || {};
  if (S.edgeCache[key]) return S.edgeCache[key];
  const lines = [];
  for (const lake of S.lakes) {
    const L = sc.lakes[lake]; if (!featsOf(L).length) continue;
    let box = null;
    if (L.bounds) {
      const [w, e, s, n] = boundsBox(L.bounds);
      const tl = 0.5 * (e - w) / (L.bounds.width_px || 300), tb = 0.5 * (n - s) / (L.bounds.height_px || 200);
      box = { w, e, s, n, tl, tb };
    }
    const onBorder = (a, b) => box && ((Math.abs(a[0] - box.w) < box.tl && Math.abs(b[0] - box.w) < box.tl) ||
      (Math.abs(a[0] - box.e) < box.tl && Math.abs(b[0] - box.e) < box.tl) ||
      (Math.abs(a[1] - box.s) < box.tb && Math.abs(b[1] - box.s) < box.tb) ||
      (Math.abs(a[1] - box.n) < box.tb && Math.abs(b[1] - box.n) < box.tb));
    const others = S.lakes.filter(o => o !== lake).map(o => featsOf(sc.lakes[o])).filter(fs => fs.length);
    for (const f of featsOf(L)) for (const ring of linesOf(f.geometry)) {
      let cur = [];
      for (let i = 1; i < ring.length; i++) {
        const a = ring[i - 1], b = ring[i], mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
        const keep = (withBox || !onBorder(a, b)) && !others.some(fs => fs.some(g => inGeom(g.geometry, mid[0], mid[1])));
        if (keep) { if (!cur.length) cur.push(a); cur.push(b); }
        else if (cur.length) { lines.push(cur); cur = []; }
      }
      if (cur.length) lines.push(cur);
    }
  }
  S.edgeCache[key] = lines;
  return lines;
}

function drawSites(sc) {
  const used = new Set();
  for (const lake of S.lakes) {
    const L = sc.lakes[lake] || {};
    for (const r of (L.bounds || {}).radars || []) used.add(r);
    for (const r of (L.bands || {}).radars_in_composite || []) used.add(r);
  }
  for (const [id, s] of Object.entries(S.sites)) {
    const [x, y] = px(s.lon, s.lat);
    if (x < -20 || y < -20 || x > W + 20 || y > H + 20) continue;
    const on = used.has(id);
    ctx.beginPath(); ctx.moveTo(x, y - 10); ctx.lineTo(x + 9, y + 7); ctx.lineTo(x - 9, y + 7); ctx.closePath();
    ctx.fillStyle = on ? '#facc15' : '#cbd5e1'; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = '#0f172a'; ctx.stroke();
    S.deferred.push(() => {
      const under = x + 26 > W - 66 && y - 34 < 186;         // would sit under the zoom buttons: put it left of the site
      const lx = under ? x - 40 : x, ly = under ? y - 2 : y - 22;
      textHalo(id, lx, ly, '700 18px -apple-system, Helvetica, Arial, sans-serif', on ? '#0f172a' : '#475569');
      placed.push({ x: lx - 26, y: ly - 12, w: 52, h: 24 });
    });
  }
}

function currentBands(sc) {
  const out = [];
  for (const lake of S.lakes) { const b = sc.lakes[lake] && sc.lakes[lake].bands; if (b) for (const f of b.features) out.push(f); }
  return out;
}
function forecastsAt(sc, lead, kind = 'forecast') {
  const out = [];
  for (const lake of S.lakes) { const fc = sc.lakes[lake] && sc.lakes[lake][kind]; if (fc) for (const f of fc.features) if (f.properties.lead_min === lead) out.push(f); }
  return out;
}
// the forecaster of the forecasts on screen: from the forecast files' own metadata
function forecasterOf(sc) {
  const out = { model: false, used: null, role: null, label: null, short: null, sample: null, fallback: null, meta: null,
                inputs: null, comparison: false, noModel: null };
  if (!sc) return out;
  for (const lake of S.lakes) {
    const fc = sc.lakes[lake] && sc.lakes[lake].forecast; if (!fc) continue;
    const m = fc.forecaster || {};
    out.meta = out.meta || m;
    if (!out.sample && fc.in_sample) out.sample = fc.in_sample;
    if (m.no_model_reason) out.noModel = m.no_model_reason;
    if (fc.fallback_reason) out.fallback = out.fallback || fc.fallback_reason;
    if (m.family && m.family !== 'persistence') {
      out.label = out.label || `${m.family}${m.config ? ' ' + m.config : ''}`;
      out.short = out.short || m.artifact_short_id || (m.artifact_sha256 || '').slice(0, 8);
      out.role = out.role || m.role || null;
      out.sample = out.sample || m.in_sample || null;
    }
    if (fc.forecaster_used && !/^persistence/.test(fc.forecaster_used)) { out.model = true; out.used = fc.forecaster_used; }
    else if (!out.used) out.used = fc.forecaster_used || null;
    if (fc.model_inputs && !out.inputs) out.inputs = fc.model_inputs;
    if (sc.lakes[lake].persistence) out.comparison = true;
  }
  return out;
}
const ROLE_TITLE = { frozen: 'Frozen model.', candidate: 'Candidate model, not yet frozen.', test: 'TEST MODEL, not the frozen one.' };
const GRADED_TITLE = 'Graded model.';
const ROLE_KIND = { frozen: 'info', candidate: 'warn', test: 'bad' };
const ROLE_TEXT = {
  graded: 'The graded model (version 2, round 3), byte for byte the artifact the sealed winter was scored with.',
  frozen: 'The frozen model named in the freeze file.',
  candidate: 'The refit on all training events; the freeze file does not name it yet, so this is not the final model.',
  test: 'A test artifact, not the pilot’s model: its forecasts mean nothing.',
};
const PERSIST_COLOUR = '#334155';

function sampleLines(lines) {
  for (const l of lines) for (let i = 1; i < l.length; i++) {
    const [ax, ay] = px(...l[i - 1]), [bx, by] = px(...l[i]);
    const n = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / 8));
    for (let k = 0; k <= n; k++) S.linePts.push([ax + (bx - ax) * k / n, ay + (by - ay) * k / n]);
  }
}
const coversLine = r => S.linePts.some(([a, b]) => a > r.x - 3 && a < r.x + r.w + 3 && b > r.y - 3 && b < r.y + r.h + 3);

function selectedGroup(sc) {
  if (!S.band || !sc) return null;
  const f = currentBands(sc).find(b => b.properties.band_id === S.band);
  return f ? groupOf(f.properties) : null;
}
// a forecast label is drawn only when the forecast has moved off the band now: under persistence the
// band's own label already names it (fewer labels; the key explains the dashed forecast outline)
function forecastMoved(f, bands) {
  const now = bands.find(b => b.properties.band_id === f.properties.band_id);
  const main = longestLine(linesOf(f.properties.centerline));
  if (!now || !main) return true;
  return distToLinesPx(linesOf(now.properties.centerline), ...px(...pointAlong(main, 0.5))) > 24;
}

function drawBands(sc) {
  const bands = currentBands(sc);
  const farOnly = S.showFarOnly ? farOnlyObjects(sc) : [];
  const fcs = S.lead ? forecastsAt(sc, S.lead) : [];
  const selG = selectedGroup(sc);
  for (const f of bands) { sampleLines(linesOf(f.properties.centerline)); sampleLines(linesOf(f.properties.centerline_far)); }
  for (const f of fcs) sampleLines(linesOf(f.properties.centerline));
  // 0. objects seen only in the far zone (only when switched on): grey, dotted outline, dashed centerline
  for (const f of farOnly) {
    const p = f.properties, sel = S.farOnly === p.band_id;
    ctx.beginPath(); pathPolys(f.geometry); strokeHalo(sel ? 3 : 2, '#94a3b8', [3, 5], 1.5);
    ctx.beginPath(); pathLines(linesOf(p.centerline)); strokeHalo(sel ? 5 : 3.5, FAR_ONLY_COLOUR, [8, 7], 2);
  }
  // 1. forecast 80% ranges (regions), 2. forecast outlines and centerlines, 3. bands now on top
  for (const f of fcs) {
    const p = f.properties, col = colourOf(p);
    ctx.globalAlpha = p.probability && p.probability.exists != null && p.probability.exists < 0.5 ? 0.45 : 1;
    if (envOf(p)) { ctx.beginPath(); pathPolys(envOf(p)); ctx.fillStyle = hatch(col); ctx.fill('evenodd'); }
    ctx.globalAlpha = 1;
  }
  for (const f of fcs) {
    const p = f.properties, col = colourOf(p);
    const faint = p.probability && p.probability.exists != null && p.probability.exists < 0.5;
    ctx.globalAlpha = faint ? 0.5 : 1;
    ctx.beginPath(); pathPolys(f.geometry); strokeHalo(2.5, col, [10, 6], 2);
    const cl = linesOf(p.centerline);
    ctx.beginPath(); pathLines(cl); strokeHalo(4, col, [14, 9], 3);
    ctx.globalAlpha = 1;
  }
  for (const f of bands) {
    const p = f.properties, col = colourOf(p), sel = S.band === p.band_id || (selG && groupOf(p) === selG);
    // far-zone parts first, under the near parts: dashed and visibly lighter (lower confidence)
    if (p.outline_far) { ctx.beginPath(); pathPolys(p.outline_far); strokeHalo(sel ? 3 : 2, '#64748b', [6, 5], 1.5); }
    if (p.centerline_far) { ctx.beginPath(); pathLines(linesOf(p.centerline_far)); strokeHalo(sel ? 5 : 4, lighten(col, 0.5), [11, 7], 2.5); }
    ctx.beginPath(); pathPolys(f.geometry);
    strokeHalo(sel ? 4 : 2.5, '#111827', p.tier === 2 ? [7, 5] : [], 2);
    const cl = linesOf(p.centerline);
    ctx.beginPath(); pathLines(cl); strokeHalo(sel ? 6 : 4.5, col, [], 3);
    // a dot every 15 km along the centerline, as in the panels (near zone only)
    if (S.view.ppd > 120) for (const l of cl) {
      const total = lineKm([l]);
      for (let d = 15; d < total; d += 15) {
        const [x, y] = px(...pointAlong(l, d / total));
        ctx.beginPath(); ctx.arc(x, y, 3.5, 0, 2 * Math.PI); ctx.fillStyle = col; ctx.fill(); ctx.lineWidth = 1.5; ctx.strokeStyle = '#fff'; ctx.stroke();
      }
    }
  }
  if (CASES && S.lead) casesDrawActual(sc);      // past storms: where each band actually was at the valid time
  // 4. persistence beside the model (switch on): where each band is now, grey dotted, on top of the band now
  if (S.showPersist && S.lead) for (const f of forecastsAt(sc, S.lead, 'persistence')) {
    const p = f.properties;
    // outline: fine grey dots, no halo; centerline: large round dots with a white rim (model forecasts are dashed)
    ctx.beginPath(); pathPolys(f.geometry);
    ctx.lineCap = 'round'; ctx.setLineDash([0.1, 6]); ctx.strokeStyle = PERSIST_COLOUR; ctx.lineWidth = 3; ctx.stroke();
    ctx.beginPath(); pathLines(linesOf(p.centerline));
    ctx.setLineDash([0.1, 13]); ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 10; ctx.stroke();
    ctx.strokeStyle = PERSIST_COLOUR; ctx.lineWidth = 6.5; ctx.stroke();
    ctx.setLineDash([]);
  }
  // labels: deferred so they sit above everything else; one label per display group
  S.deferred.push(() => {
    const done = new Set();
    // groups first, longest first: the bands most worth naming get the free spots
    const order = [...bands].sort((a, b) => (inGroup(b.properties) - inGroup(a.properties)) ||
      (lineKm(linesOf(b.properties.centerline)) - lineKm(linesOf(a.properties.centerline))));
    for (const f of order) {
      const p = f.properties, g = groupOf(p);
      if (done.has(g)) continue;
      done.add(g);
      const members = inGroup(p) ? bands.filter(b => groupOf(b.properties) === g) : [f];
      const main = longestLine(members.flatMap(b => linesOf(b.properties.centerline)));
      if (!main) continue;
      const anchor = px(...pointAlong(main, 0.5));
      if (anchor[0] < 0 || anchor[1] < 0 || anchor[0] > W || anchor[1] > H) continue;
      let text = groupLabel(p, true);
      if (members.some(b => (b.properties.flags || []).includes('probably_not_lake_effect'))) text += ' ?';
      const r = badge(text, anchor, colourOf(p), p.tier === 2 ? 'inland' : 'now', coversLine);
      S.hits.push({ kind: 'band', id: p.band_id, rect: r });
    }
    for (const f of fcs) {
      if (!forecastMoved(f, bands)) continue;
      const p = f.properties, col = colourOf(p);
      const main = longestLine(linesOf(p.centerline));
      if (!main) continue;
      const anchor = px(...pointAlong(main, 0.72));
      if (anchor[0] < 0 || anchor[1] < 0 || anchor[0] > W || anchor[1] > H) continue;
      let text = `${labelOf(p.band_id)} +${p.lead_min}`;
      const ex = p.probability && p.probability.exists;
      if (ex != null && !/base rate/i.test(p.probability_source || '')) text += ` · ${Math.round(ex * 100)}%`;
      const r = badge(text, anchor, col, 'fc', coversLine);
      if (r) S.hits.push({ kind: 'band', id: p.band_id, rect: r });
    }
    flushBadges();
    if (fcs.length) labelCorridor(fcs);
  });
}

function labelCorridor(fcs) {
  // one on-map label for the hatched region, on the widest visible range
  let best = null, area = 0;
  for (const f of fcs) for (const poly of polysOf(envOf(f.properties))) {
    const ring = poly[0]; if (!ring) continue;
    const xs = ring.map(c => px(...c)), minx = Math.min(...xs.map(a => a[0])), maxx = Math.max(...xs.map(a => a[0]));
    const miny = Math.min(...xs.map(a => a[1])), maxy = Math.max(...xs.map(a => a[1]));
    const a = (maxx - minx) * (maxy - miny);
    if (a > area && minx > 0 && maxx < W) { area = a; best = { x: (minx + maxx) / 2, y: maxy + 18, col: hashColour(f.properties.band_id) }; }
  }
  if (!best || best.y > H - S.bottomReserve + 4) return;
  ctx.font = '700 17px -apple-system, Helvetica, Arial, sans-serif';
  const text = `hatched: ${envName(S.scene).map}`;
  const w = ctx.measureText(text).width + 8;
  const r = { x: best.x - w / 2, y: best.y - 12, w, h: 24 };
  if (overlaps(r)) return;
  placed.push(r);
  textHalo(text, best.x, best.y, '700 17px -apple-system, Helvetica, Arial, sans-serif', '#0f172a');
}

function drawPoint() {
  if (!S.point) return;
  const [x, y] = px(S.point.lon, S.point.lat);
  ctx.beginPath(); ctx.arc(x, y, 11, 0, 2 * Math.PI); ctx.lineWidth = 6; ctx.strokeStyle = '#ffffff'; ctx.stroke();
  ctx.lineWidth = 3; ctx.strokeStyle = '#0f172a'; ctx.stroke();
  ctx.beginPath(); ctx.moveTo(x - 20, y); ctx.lineTo(x - 6, y); ctx.moveTo(x + 6, y); ctx.lineTo(x + 20, y);
  ctx.moveTo(x, y - 20); ctx.lineTo(x, y - 6); ctx.moveTo(x, y + 6); ctx.lineTo(x, y + 20);
  ctx.lineWidth = 3; ctx.stroke();
  placed.push({ x: x - 22, y: y - 22, w: 44, h: 44 });
}

function drawLabelsLast(sc) {
  for (const f of S.deferred) f();
  S.deferred = [];
  // county names when zoomed in far enough for them to fit
  const B = S.basemap;
  if (B && S.view.ppd > 260) {
    for (const c of B.counties) {
      const [x, y] = px(...c.label);
      if (x < 40 || y < 20 || x > W - 70 || y > H - S.bottomReserve + 4) continue;
      if (c.area_deg2 * S.view.ppd * S.view.ppd * COS43 < 9000) continue;
      ctx.font = '600 17px -apple-system, Helvetica, Arial, sans-serif';
      const w = ctx.measureText(c.name).width;
      const r = { x: x - w / 2 - 3, y: y - 12, w: w + 6, h: 24 };
      if (overlaps(r) || coversLine(r)) continue;          // never over a band's line or another label
      placed.push(r);
      textHalo(c.name, x, y, '600 17px -apple-system, Helvetica, Arial, sans-serif', '#475569');
    }
  }
  if (sc) labelCoverageEdge(sc);
  drawColourBar(sc);
}

function labelCoverageEdge(sc) {
  if (S.showTrain) labelLine(trainingEdges(sc), 'training area', 0, '#9a3412');
}
function labelLine(rings, text, start, colour) {
  // one label on the line, where it overlaps nothing; start: where along the line to begin looking (0-1)
  ctx.font = '700 17px -apple-system, Helvetica, Arial, sans-serif';
  const w = ctx.measureText(text).width + 10;
  const pts = rings.flat();
  const k0 = Math.floor(start * pts.length);
  for (let n = 0; n < pts.length; n += 5) {
    const [x, y] = px(...pts[(k0 + n) % pts.length]);
    if (x < w || y < 60 || x > W - w - 70 || y > H - 110) continue;
    const r = { x: x - w / 2, y: y - 30, w, h: 24 };
    if (overlaps(r) || coversLine(r)) continue;
    placed.push(r);
    textHalo(text, x, y - 18, '700 17px -apple-system, Helvetica, Arial, sans-serif', colour);
    return;
  }
}

function scaleSteps(sc) {
  for (const lake of S.lakes) {
    const b = sc && sc.lakes[lake] && sc.lakes[lake].bounds;
    const cs = b && b.colour_scale;
    const st = cs && (Array.isArray(cs) ? cs : cs.steps);
    if (st && st.length) return st.map(x => [x.from_dbz != null ? x.from_dbz : x.dbz_from, x.to_dbz != null ? x.to_dbz : x.dbz_to,
      x.colour || x.color || (x.rgba ? '#' + x.rgba.slice(0, 3).map(v => v.toString(16).padStart(2, '0')).join('') : '#000000')]);
  }
  return DEFAULT_SCALE;
}
function drawColourBar(sc) {
  // the snow scale of the detection panels the owner reviewed: 2.5 dBZ steps from 5 to 35, 35-40, 40 and above
  const mosaic = !!(sc && sc.mosaic), steps = scaleSteps(sc), far = hasFar(sc) && !mosaic, wide = W > 640;
  const title = mosaic
    ? (wide ? `Snow reflectivity, dBZ (lowest beam) · NOAA MRMS ${fmtUTC(parseStamp(sc.mosaic.name))}` : 'Snow dBZ · NOAA MRMS')
    : (wide ? 'Snow reflectivity, dBZ (lowest sweep) · NOAA NEXRAD' : 'Snow dBZ · NOAA NEXRAD');
  ctx.font = '700 17px -apple-system, Helvetica, Arial, sans-serif';
  const tw = ctx.measureText(title).width;
  const cw = Math.max(18, Math.min(30, (W - 200) / steps.length)), h = 16;
  const farText = wide ? 'beyond the solid line: lower confidence, strength not shown' : 'beyond solid line: lower confidence';
  ctx.font = '600 17px -apple-system, Helvetica, Arial, sans-serif';
  const fw = far ? ctx.measureText(farText).width + 30 : 0;
  ctx.font = '700 17px -apple-system, Helvetica, Arial, sans-serif';
  const besides = far && wide && 18 + cw * steps.length + 22 + fw + 12 < W - 190;   // far key beside the bar, clear of the scale bar
  const boxH = far && !besides ? 98 : 74;
  const x0 = 18, y0 = H - 46 - (boxH - 74), bw = Math.max(cw * steps.length + 24 + (besides ? fw + 10 : 0), tw + 16, far && !besides ? fw + 16 : 0);
  ctx.fillStyle = 'rgba(255,255,255,0.94)'; ctx.fillRect(x0 - 12, y0 - 30, bw, boxH);
  ctx.strokeStyle = '#0f172a'; ctx.lineWidth = 1; ctx.strokeRect(x0 - 12, y0 - 30, bw, boxH);
  placed.push({ x: x0 - 12, y: y0 - 30, w: bw, h: boxH });
  S.barBox = { x: x0 - 12, y: y0 - 30, w: bw, h: boxH };
  ctx.fillStyle = '#0f172a'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText(title, x0 - 4, y0 - 15);
  steps.forEach(([a, b, col], i) => { ctx.fillStyle = col; ctx.fillRect(x0 + i * cw, y0, cw, h); });
  ctx.strokeStyle = '#0f172a'; ctx.strokeRect(x0, y0, cw * steps.length, h);
  ctx.font = '600 17px -apple-system, Helvetica, Arial, sans-serif'; ctx.fillStyle = '#0f172a'; ctx.textBaseline = 'top';
  const every = cw * 2 >= 26 ? 5 : 10;                  // label every 5 dBZ when there is room, else every 10
  const lastX = x0 + (steps.length - 1) * cw;           // where the open-ended top step ("40+") is labelled
  steps.forEach(([a, b], i) => {
    if (b == null) { ctx.textAlign = 'left'; ctx.fillText(`${a}+`, lastX - 6, y0 + h + 2); return; }
    if (a % every === 0 && lastX - (x0 + i * cw) >= 24) { ctx.textAlign = 'center'; ctx.fillText(String(a), x0 + i * cw, y0 + h + 2); }
  });
  if (far) {
    const fx = besides ? x0 + cw * steps.length + 22 : x0, fy = besides ? y0 : y0 + h + 26;
    ctx.fillStyle = 'rgba(148,163,184,0.30)'; ctx.fillRect(fx, fy, 18, h);
    ctx.fillStyle = 'rgba(71,85,105,0.47)'; ctx.fillRect(fx, fy, 18, h);
    ctx.strokeStyle = '#0f172a'; ctx.strokeRect(fx, fy, 18, h);
    ctx.fillStyle = '#0f172a'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.font = '600 17px -apple-system, Helvetica, Arial, sans-serif'; ctx.fillText(farText, fx + 26, fy + h / 2 + 1);
  }
}

function drawScale() {
  // a 50 km (or 20 / 100 km) bar, bottom right
  const kmPerPx = 110.57 / S.view.ppd;
  const km = [10, 20, 50, 100, 200].find(k => k / kmPerPx >= 90) || 200;
  const len = km / kmPerPx, x1 = W - 16, x0 = x1 - len;
  const bb = S.barBox, y = bb && bb.x + bb.w > x0 - 16 ? bb.y - 14 : H - 22;
  ctx.fillStyle = 'rgba(255,255,255,0.92)'; ctx.fillRect(x0 - 8, y - 30, len + 16, 40);
  ctx.beginPath(); ctx.moveTo(x0, y - 8); ctx.lineTo(x0, y); ctx.lineTo(x1, y); ctx.lineTo(x1, y - 8);
  ctx.strokeStyle = '#0f172a'; ctx.lineWidth = 3; ctx.stroke();
  textHalo(`${km} km`, (x0 + x1) / 2, y - 16, '700 17px -apple-system, Helvetica, Arial, sans-serif', '#0f172a');
}
S.deferred = [];

// ------------------------------------------------------------------ reading values
function dbzAt(sc, lon, lat) {
  // reflectivity bin of the radar image at a point, from the image's own colour scale
  const steps = scaleSteps(sc);
  for (const lake of S.lakes) {
    const L = sc.lakes[lake]; if (!L || !L.radar || !L.bounds) continue;
    const [w, e, s, n] = boundsBox(L.bounds);
    if (lon < w || lon > e || lat < s || lat > n) continue;
    const { pix } = L.radar;
    const i = Math.min(pix.width - 1, Math.floor((lon - w) / (e - w) * pix.width));
    const j = Math.min(pix.height - 1, Math.floor(rowOfLat(L.bounds, lat, pix.height)));
    const k = 4 * (j * pix.width + i);
    const rgb = [pix.data[k], pix.data[k + 1], pix.data[k + 2]], a = pix.data[k + 3];
    const nd = L.bounds.no_data_in_coverage_rgba;
    if (nd && a > 0 && Math.abs(a - nd[3]) < 8 && rgb.every((v, q) => Math.abs(v - nd[q]) < 8)) return { bin: null, nodata: true };
    if (a < 128) return { bin: null };
    const hexv = h => [1, 3, 5].map(q => parseInt(h.slice(q, q + 2), 16));
    const st = steps.find(x => hexv(x[2]).every((v, q) => Math.abs(v - rgb[q]) < 6));
    return { bin: st || null };
  }
  return { bin: null, outside: true };
}
function inCoverage(sc, lon, lat) {
  for (const lake of S.lakes) { const c = sc.lakes[lake] && sc.lakes[lake].coverage; if (c && c.features.some(f => inGeom(f.geometry, lon, lat))) return true; }
  return false;
}

// ------------------------------------------------------------------ header and banners
// rule (b) of the real-time pipeline: a lead the model's weather maps cannot reach at this issuance (+180 min at
// minutes 01-29) is carried from the newest issuance that reached it, with that issuance's own issue and valid times
const carriedFrom = p => p && (p.lead180_carried_from || Object.keys(p).filter(k => /^lead\d+_carried_from$/.test(k))
  .map(k => p[k]).find(Boolean)) || null;
function leadNote(fcs) {
  const c = fcs.map(f => f.properties).find(carriedFrom);
  if (!c) return '';
  return ` · carried from the forecast issued ${fmtUTC(tIso(carriedFrom(c)), true)}, valid ${fmtUTC(tIso(c.valid_time))}`;
}
// a lead not issued at all at this scan (rule (b) at start-up): the forecast file says why
function leadOmitted(sc, lead) {
  for (const lake of S.lakes) { const fc = sc.lakes[lake] && sc.lakes[lake].forecast; if (fc && fc[`lead${lead}_omitted`]) return fc[`lead${lead}_omitted`]; }
  return null;
}
function forecastMeta(sc) {
  // issuance, model inputs, and degraded reasons of the forecasts on screen
  let iss = NaN, scan = NaN, model = null, init = null, avail = null, kind = null;
  const reasons = new Map(), models = new Set();
  for (const lake of S.lakes) {
    const fc = sc.lakes[lake] && sc.lakes[lake].forecast; if (!fc) continue;
    if (fc.issuance_time && (!isFinite(iss) || tIso(fc.issuance_time) >= iss)) { kind = fc.issue_kind || 'half_hour'; scan = tIso(fc.radar_scan_time); }
    if (fc.issuance_time && !fc.features.length) iss = isFinite(iss) ? Math.max(iss, tIso(fc.issuance_time)) : tIso(fc.issuance_time);
    for (const f of fc.features) {
      const p = f.properties;
      if (carriedFrom(p)) continue;                    // a lead carried from an earlier issuance (its own times)
      iss = isFinite(iss) ? Math.max(iss, tIso(p.issuance_time)) : tIso(p.issuance_time);
      scan = tIso(p.radar_scan_time);
      const ia = p.inputs_available || {};
      if (ia.model_init) init = init == null ? tIso(ia.model_init) : Math.max(init, tIso(ia.model_init));
      if (ia.model_available) avail = tIso(ia.model_available);
      if (p.model) models.add(`${p.model.family}${p.model.config ? ' ' + p.model.config : ''}${p.model.artifact_short_id ? ' · artifact ' + p.model.artifact_short_id : ''}`);
      if (!S.lead || p.lead_min === S.lead) for (const r of p.degraded_reasons || []) {
        if (!reasons.has(r)) reasons.set(r, new Set());
        reasons.get(r).add(labelOf(p.band_id));
      }
    }
  }
  model = [...models].join(', ');
  return { iss, scan, init, avail, model, reasons, kind };
}

const SHORT_REASONS = {
  radar_stale: 'radar stale', radar_partial: 'a radar missing', detector_warmup: 'detector warming up',
  history_short: 'under 60 min of history', model_stale: 'model run over 6 h old', model_missing: 'no model run, fallback model',
  vad_missing: 'no radar wind profile', features_out_of_range: 'input outside training range', fallback_persistence: 'persistence fallback',
  hrrr_late: 'weather maps late', composite_late: 'radar composite late', lake_map_late: 'lake map late',
  model_profiles_late: 'model profiles late', lake_values_late: 'lake temperature late',
  vad_history_short: 'radar wind history short', lake_values_older: 'lake temperature a day older',
};
// banner: [kind, title, long html, short html]; the short text is shown on phones
const bannerHtml = list => list.map(b => `<p class="banner ${b[0]}"><strong>${b[1]}</strong><span class="long">${b[2]}</span><span class="short">${b[3]}</span></p>`).join('');
// a header time: main line always; detail lines on wide screens only; extra only on phones
const timeHtml = (main, detail, extra) => `${main}${extra ? `<span class="short"> · ${extra}</span>` : ''}${detail ? `<span class="long"><br>${detail}</span>` : ''}`;

function renderHeader() {
  const sc = S.scene, st = S.status || {};
  const live = st.mode === 'live';
  const modeEl = $('mode');
  const shownT = S.steps[S.step];
  modeEl.innerHTML = esc(live ? 'LIVE' : `REPLAY${st.event ? ' ' + st.event : ''}`) +
    (isFinite(shownT) ? `<span class="long"> · ${esc(fmtDate(shownT))} UTC</span>` : '');
  modeEl.className = 'mode' + (live ? ' live' : '');
  if (CASES) casesModeText(modeEl, shownT);
  const banners = [];
  if (st.fixture) banners.push(['info', 'Fixture.', esc(st.fixture.note || 'Built from stored products, not pipeline output.'), 'Not pipeline output; see the report.']);
  if (!sc) { $('banners').innerHTML = bannerHtml(banners); return; }
  // the oldest scan of the lakes shown sets the age; each lake's own time is listed
  let scanStart = NaN, scanEnd = NaN;
  const perLake = [];
  for (const lake of S.lakes) {
    const L = sc.lakes[lake]; if (!L) continue;
    const f = L.bands && L.bands.features[0];
    let t0 = NaN, t1 = NaN;
    if (f) { t0 = tIso(f.properties.radar_scan_time); t1 = tIso(f.properties.radar_scan_end); }
    else if (L.stamps.bs || L.stamps.rs) t0 = parseStamp(L.stamps.bs || L.stamps.rs);
    // no band on this scan (clear air): a bundle still carries its scan's end, so the age counts from the end as above
    if (!isFinite(t1) && L.bundle && L.bundle.radar_scan_end) t1 = tIso(L.bundle.radar_scan_end);
    if (!isFinite(t0)) continue;
    perLake.push([lake, t0]);
    if (!isFinite(scanStart) || t0 < scanStart) { scanStart = t0; scanEnd = isFinite(t1) ? t1 : t0; }
  }
  const nowMs = live ? Date.now() : (S.step === S.steps.length - 1 && st.now ? tIso(st.now) : S.steps[S.step]);
  // a running replay can write a scan before status.json catches up: never show a negative age
  const age = Math.max(0, (nowMs - (isFinite(scanEnd) ? scanEnd : scanStart)) / 60000);
  const atLatest = S.step === S.steps.length - 1 && !CASES;
  const ageText = !isFinite(age) ? '' : live ? `${ago(age)} old` : atLatest ? `${ago(age)} before replay clock` : `scan ended ${fmtUTC(scanEnd, true)}`;
  const ageShort = !isFinite(age) ? '' : (live || atLatest) ? `${ago(age)} old` : `ended ${fmtUTC(scanEnd).replace(' UTC', 'Z')}`;
  const lakeTimes = perLake.length > 1 && new Set(perLake.map(x => fmtUTC(x[1]))).size > 1
    ? perLake.map(([l, t]) => `${lakeName(l).replace('Lake ', '')} ${fmtUTC(t).replace(' UTC', '')}`).join(', ') + ' UTC<br>' : '';
  $('t-radar').innerHTML = timeHtml(both(scanStart), `${lakeTimes}${esc(ageText)}`, ageShort);
  const fm = forecastMeta(sc);
  const kindText = fm.kind === 'scan' ? 'when that scan arrived' : fm.kind === 'half_hour' ? ':00/:30 issuance (scored)' : '';
  $('t-issued').innerHTML = isFinite(fm.iss)
    ? timeHtml(fm.kind === 'scan' ? `<span class="utc">${fmtUTC(fm.iss, true)}</span> · ${fmtLocal(fm.iss)}` : both(fm.iss),
               `from the ${fmtUTC(fm.scan)} scan${kindText ? ', ' + kindText : ''}`, `from ${fmtUTC(fm.scan).replace(' UTC', 'Z')} scan`)
    : 'None yet in this run';
  const every = (st.issues && st.issues.every_scan) || st.source === 'bundles' || fm.kind === 'scan';
  $('updates').hidden = !every || CASES;
  $('t-model').innerHTML = fm.init != null && isFinite(fm.init) ? timeHtml(`${both(fm.init)} run`, esc(fm.model), esc(fm.model))
    : (isFinite(fm.iss) ? timeHtml('No weather-model run used', esc(fm.model || ''), esc(fm.model || '')) : '—');
  if (CASES) $('t-model').innerHTML = casesModelTime();
  // a carried lead (rule (b)) keeps its own issue and valid times: the header shows those, not this issuance's
  const carried = S.lead ? forecastsAt(S.scene, S.lead).map(f => f.properties).find(carriedFrom) : null;
  $('t-valid').innerHTML = carried
    ? timeHtml(both(tIso(carried.valid_time)), `+${S.lead} min forecast, carried from the forecast issued ${fmtUTC(tIso(carriedFrom(carried)), true)}`, `+${S.lead} min, carried`)
    : S.lead && isFinite(fm.iss) ? timeHtml(both(fm.iss + S.lead * 60000), `+${S.lead} min forecast`, `+${S.lead} min`)
    : timeHtml(both(scanStart), 'radar now (no lead selected)', 'now');
  // stale data and degraded forecasts
  if (live && isFinite(age) && age > 15) banners.push(['bad', 'Stale radar.', `The newest radar scan is ${ago(age)} old.`, `Radar ${ago(age)} old.`]);
  const heartbeat = st.heartbeat || st.heartbeat_wall;
  // published (bundles): the heartbeat is the pointer, which changes only when a scan arrives, 5 to 10 minutes apart
  // in clear air; a running pipeline's own status.json beats every few seconds
  const hbLimit = st.source === 'bundles' ? 15 * 60000 : 3 * 60000;
  if (live && heartbeat && (Date.now() - tIso(heartbeat)) > hbLimit) {
    const hb = ago((Date.now() - tIso(heartbeat)) / 60000);
    banners.push(['bad', 'Pipeline not responding.', `Last heartbeat ${hb} ago.`, `Last heartbeat ${hb} ago.`]);
  }
  if (Array.isArray(st.errors) && st.errors.length) {
    const first = typeof st.errors[0] === 'string' ? st.errors[0] : JSON.stringify(st.errors[0]);
    banners.push(['bad', 'Pipeline errors.', `${st.errors.length} reported; first: ${esc(first.slice(0, 200))}`, `${st.errors.length} reported.`]);
  }
  const fx = forecasterOf(sc);
  if (CASES) banners.push(casesBanner());
  else if (fx.model) {
    // which forecaster, its artifact, the weather-model run it read, and whether it is the frozen model
    const run = fx.inputs && fx.inputs.model_init ? `${fmtUTC(tIso(fx.inputs.model_init))} model run` : 'no weather-model run';
    const src = fx.inputs && fx.inputs.inputs ? ` (${esc(fx.inputs.inputs.kind)}${fx.inputs.inputs.event ? ' of ' + esc(fx.inputs.inputs.event) : ''})` : '';
    const title = (fx.meta && fx.meta.graded) ? GRADED_TITLE : (ROLE_TITLE[fx.role] || 'Model forecast.');
    banners.push([ROLE_KIND[fx.role] || 'info', title,
      `Forecaster: ${esc(fx.label || fx.used)} · artifact ${esc(fx.short || '?')} · input: ${esc(run)}${src}. ` +
      `${(fx.meta && fx.meta.graded ? ROLE_TEXT.graded : ROLE_TEXT[fx.role]) || ''}${fx.comparison ? ' Persistence can be switched on beside it for comparison.' : ''}`,
      `${esc(fx.label || fx.used)} · ${esc(fx.short || '?')} · ${esc(run)}`]);
  } else if (fx.used === 'persistence (fallback)') {
    banners.push(['warn', 'Persistence fallback.',
      `The ${esc(fx.label || 'model')} forecast could not be made for this issuance${fx.fallback ? ': ' + esc(String(fx.fallback).slice(0, 220)) : ''}. Each band is shown staying where it is.`,
      `Model not run; persistence shown.`]);
  } else if (/persistence/i.test(fm.model || '')) banners.push(['info', 'Persistence forecast.',
    `Each band is forecast to stay where it is, with the same width, ends and intensity. ${esc(fx.noModel ? 'No trained model is in use: ' + fx.noModel + '.' : 'No trained model is in use.')}`,
    'Bands forecast to stay where they are.']);
  if (fx.sample && fx.sample.in_sample) banners.push(['warn', 'In-sample replay.',
    `The model was trained on this event (${esc((fx.sample.training_events_overlapping || []).join(', '))}). These forecasts show the pipeline working; they are not evidence of skill.`,
    'Model trained on this event: not skill.']);
  const glob = new Map();
  const addR = (r, who) => { if (!glob.has(r)) glob.set(r, new Set()); if (who) glob.get(r).add(who); };
  if (S.step === S.steps.length - 1) {
    for (const r of st.degraded_reasons || []) addR(r);
    for (const [lake, ls] of Object.entries(st.lakes || {})) for (const r of ls.degraded_reasons || []) addR(r, lakeName(lake));
  }
  for (const [r, ids] of fm.reasons) for (const id of ids) addR(r, id);
  if (glob.size) {
    const items = [...glob].map(([r, who]) => `${esc(REASONS[r] || r)}${who.size ? ` (${esc([...who].join(', '))})` : ''}`);
    const shorts = [...glob].map(([r, who]) => `${esc(SHORT_REASONS[r] || r.replace(/_/g, ' '))}${who.size ? ` (${who.size === 1 ? esc([...who][0]) : who.size + ' bands'})` : ''}`);
    banners.push(['warn', 'Degraded.', items.join('<br>'), shorts.join('; ') + '.']);
  }
  $('banners').innerHTML = bannerHtml(banners);
}

// ------------------------------------------------------------------ controls
function buildLeadButtons() {
  const box = $('leads');
  box.innerHTML = '';
  for (const lead of [0, ...LEADS]) {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = lead ? String(lead) : 'Now';
    b.setAttribute('aria-label', lead ? `${lead} minutes` : 'Now, no forecast');
    b.addEventListener('click', () => { stopPlay(); setLead(lead); });
    b.dataset.lead = lead;
    box.appendChild(b);
  }
}
function setLead(lead) {
  S.lead = lead;
  for (const b of $('leads').children) b.setAttribute('aria-pressed', String(+b.dataset.lead === lead));
  renderAll();
}
function stopPlay() { if (S.playing) { clearInterval(S.playing); S.playing = null; $('play').textContent = 'Play'; $('play').setAttribute('aria-pressed', 'false'); } }
function togglePlay() {
  if (S.playing) return stopPlay();
  $('play').textContent = 'Stop'; $('play').setAttribute('aria-pressed', 'true');
  const seq = [0, ...LEADS];
  let i = Math.max(0, seq.indexOf(S.lead));
  const tick = () => { i = (i + 1) % seq.length; setLead(seq[i]); };
  if (S.lead === 0) tick();
  S.playing = setInterval(tick, 1600);
}
async function setStep(i, follow) {
  if (!S.steps.length) return;
  S.step = Math.max(0, Math.min(S.steps.length - 1, i));
  S.follow = follow != null ? follow : S.step === S.steps.length - 1;
  $('scrub').value = S.step;
  const token = (S.loadToken = (S.loadToken || 0) + 1);
  const sc = await loadScene();
  if (token !== S.loadToken) return;
  S.scene = sc;
  S.dups = dupTracks(sc);
  if (!S.view) { fitView(true); applyUrlView(); }
  renderAll();
}
// every band at the selected lead: chance it still exists and how far the forecast moves it from where it is now
function leadTable(sc, fx) {
  if (!sc || !S.lead) return '';
  const fcs = forecastsAt(sc, S.lead);
  if (!fcs.length) { const why = leadOmitted(sc, S.lead); return why ? `<p class="hint">+${S.lead} min: not issued at this scan (${esc(why)}).</p>` : ''; }
  const bands = currentBands(sc);
  const mean = a => { const v = (a || []).filter(x => x != null); return v.length ? v.reduce((t, x) => t + x, 0) / v.length : null; };
  const rows = fcs.map(f => {
    const p = f.properties, now = bands.find(b => b.properties.band_id === p.band_id);
    const az = now && now.properties.frame ? now.properties.frame.axis_azimuth_deg : (p.frame ? p.frame.axis_azimuth_deg : null);
    const m = p.summary && p.summary.translation_km != null ? p.summary.translation_km : mean((p.station_shift_km || {}).p50);
    const dir = v => v == null ? 'unknown' : Math.abs(v) < 0.5 ? 'stays' : `${Math.abs(v).toFixed(0)} km ${az == null ? (v > 0 ? 'left' : 'right') : compass(v > 0 ? az - 90 : az + 90)}`;
    const ex = p.probability && p.probability.exists;
    const model = !/base rate/i.test(p.probability_source || '');
    return { id: p.band_id, tier: p.tier, html: `<tr><td><button type="button" class="linkish" data-band="${esc(p.band_id)}">${esc(labelOf(p.band_id, true))}</button></td>` +
      `<td>${ex != null ? Math.round(ex * 100) + '%' : '—'}${model ? '' : '<sup>*</sup>'}</td><td>${esc(dir(m))}</td>` +
      `${fx.comparison ? '<td>stays</td>' : ''}</tr>`, ex: ex == null ? -1 : ex, model };
  }).sort((a, b) => (a.tier - b.tier) || (b.ex - a.ex));
  const anyBase = rows.some(r => !r.model);
  return `<table class="leads compact"><caption>At +${S.lead} min${esc(leadNote(forecastsAt(sc, S.lead)))}</caption><thead><tr><th>Band</th><th>Still exists</th>` +
    `<th>${fx.model ? esc(fx.label) : 'Forecast'}: centerline</th>${fx.comparison ? '<th>Persistence</th>' : ''}</tr></thead><tbody>` +
    rows.map(r => r.html).join('') + '</tbody></table>' +
    `<p class="hint">Still exists: ${fx.model && !anyBase ? `the ${esc(fx.label)} model’s probability that the band is still there at +${S.lead} min` : '* training base rate, not a forecast for this band'}. Centerline: mean sideways move of the most likely forecast from where the band is now.${fx.sample && fx.sample.in_sample ? ' In-sample replay: not skill.' : ''}</p>`;
}
// the running scorecard of the :00/:30 series (the official, scored forecasts), from status.json or the pointer
function renderScorecard() {
  if (CASES) return casesScorecard();
  const el = $('scorecard'), sc = (S.status || {}).scorecard;
  const leads = sc && sc.leads ? LEADS.filter(L => sc.leads[String(L)] && (sc.leads[String(L)].model || sc.leads[String(L)].persistence)) : [];
  if (!leads.length) { el.hidden = true; el.innerHTML = ''; return; }
  const fx = forecasterOf(S.scene);
  const name = fx.label || (S.status && S.status.forecaster && S.status.forecaster.label) || 'model';
  const cell = v => v && v.median_km != null ? `${v.median_km.toFixed(1)} km <span class="opt">(${v.n})</span>` : '—';
  const rows = leads.map(L => {
    const r = sc.leads[String(L)];
    const ch = r.change_pct != null ? `${r.change_pct > 0 ? '+' : ''}${r.change_pct.toFixed(0)}%` : '—';
    return `<tr${L === S.lead ? ' class="cur"' : ''}><td>+${L}</td><td>${cell(r.model)}</td><td>${cell(r.persistence)}</td><td>${ch}</td></tr>`;
  }).join('');
  el.hidden = false;
  el.innerHTML = `<div class="blockhead"><h2>Scorecard</h2></div>
    <table class="leads compact"><thead><tr><th>Lead</th><th>${esc(name)}</th><th>Persistence</th><th>Change</th></tr></thead><tbody>${rows}</tbody></table>
    <p class="hint">Median centerline error of the :00/:30 forecasts verified so far (primary bands, both lakes; a missed band counts as 50 km; in brackets the number of verified band pairings). Lower is better; change is the model against persistence. Forecasts issued at other scans are not scored.${fx.sample && fx.sample.in_sample ? ' In-sample replay: not evidence of skill.' : ''}</p>`;
}

function renderAll() {
  const t = S.steps[S.step];
  const fx = forecasterOf(S.scene);
  const who = fx.model ? `${esc(fx.label)} forecast` : 'forecast';
  $('leadnote').innerHTML = S.lead
    ? `Minutes after issuance. <span class="key solid"></span>band now <span class="key dashed"></span>${who}, most likely <span class="key hatch"></span>${esc(envName(S.scene).long)}.` +
      (fx.comparison && S.showPersist ? ` <span class="key dotted"></span>persistence (lies on the band now).` : '') +
      (CASES ? casesLeadKey() : '')
    : 'Showing radar and bands now. Choose a lead to see each band’s forecast.';
  $('persistwrap').hidden = !fx.comparison;
  $('leadtable').innerHTML = CASES ? casesLeadTable(S.scene, fx) : leadTable(S.scene, fx);
  $('steplabel').textContent = isFinite(t) ? `${fmtDate(t)} ${fmtUTC(t)} · ${S.step + 1} of ${S.steps.length}${S.follow ? ' · latest' : ''}` : '';
  $('prev').disabled = S.step <= 0; $('next').disabled = S.step >= S.steps.length - 1; $('latest').disabled = S.step >= S.steps.length - 1;
  $('zonekey').hidden = !S.scene;
  $('mosaicnote').hidden = !(S.scene && S.scene.mosaic);
  renderHeader();
  renderScorecard();
  draw();
  renderDetails();
  requestAnimationFrame(() => { const h = $('top').getBoundingClientRect().height; document.documentElement.style.setProperty('--head', `${Math.ceil(h)}px`); });
}

// ------------------------------------------------------------------ details: point query
function pointReport() {
  const sc = S.scene, { lon, lat } = S.point;
  const EN = envName(sc);
  const rows = [], hits = [];
  const now = currentBands(sc).filter(f => inGeom(f.geometry, lon, lat)).map(f => f.properties);
  let iss = NaN;
  for (const lead of LEADS) {
    const fcs = forecastsAt(sc, lead);
    if (fcs.length) iss = tIso(fcs[0].properties.issuance_time);
    const inOutline = [], inRange = [];
    for (const f of fcs) {
      const p = f.properties, id = labelOf(p.band_id);
      const ex = p.probability && p.probability.exists;
      const tag = ex != null && ex < 0.5 && !/base rate/i.test(p.probability_source || '') ? `${id} (${Math.round(ex * 100)}% exists)` : id;
      if (inGeom(f.geometry, lon, lat)) inOutline.push(tag);
      else if (inGeom(envOf(p), lon, lat)) inRange.push(tag);
    }
    rows.push({ lead, valid: iss + lead * 60000, inOutline, inRange, any: fcs.length > 0 });
    hits.push(inOutline.length ? 2 : inRange.length ? 1 : 0);
  }
  const dz = dbzAt(sc, lon, lat), cov = inCoverage(sc, lon, lat);
  const farZone = !cov && hasFar(sc) && inFarZone(sc, lon, lat);
  const farNow = currentBands(sc).filter(f => f.properties.outline_far && inGeom(f.properties.outline_far, lon, lat)).map(f => f.properties);
  let nowText;
  if (farZone) nowText = farEchoAt(sc, lon, lat) ? 'echo beyond the solid line (beam 2–3 km above ground; its strength is not shown)'
    : 'no echo seen beyond the solid line (the beam may pass over shallow snow here)';
  else if (dz.outside && !cov) nowText = 'outside the radar grid';
  else if (dz.nodata) nowText = 'no radar data here in this scan';
  else if (dz.bin) nowText = `${dz.bin[0]}${dz.bin[1] != null ? '–' + dz.bin[1] : '+'} dBZ`;
  else nowText = cov ? 'no echo above 5 dBZ' : 'no echo above 5 dBZ (outside radar coverage: low snow may be missed)';
  // one answer per band, as a range between leads, never a single minute
  const anyFc = rows.some(r => r.any);
  const per = new Map();               // track -> [state per lead]: 2 outline, 1 range only, 0 outside
  rows.forEach((r, i) => {
    for (const t of r.inOutline) { const k = t.split(' (')[0]; if (!per.has(k)) per.set(k, LEADS.map(() => 0)); per.get(k)[i] = 2; }
    for (const t of r.inRange) { const k = t.split(' (')[0]; if (!per.has(k)) per.set(k, LEADS.map(() => 0)); per.get(k)[i] = Math.max(per.get(k)[i], 1); }
  });
  const L = i => `+${LEADS[i]}`;
  const tz = fmtLocal(rows[0].valid).split(' ').pop();
  const loc = ms => fmtLocal(ms).replace(' ' + tz, '');
  const nowIds = new Set(now.map(p => labelOf(p.band_id)));
  const answers = [];
  for (const [id, st] of per) {
    const f2 = st.indexOf(2), f1 = st.findIndex(v => v >= 1), last2 = st.lastIndexOf(2);
    const leadsOf = v => LEADS.filter((_, i) => st[i] === v).map(l => '+' + l).join(', ');
    let a;
    if (nowIds.has(id) && st[0] === 2) {
      let k = 0; while (k + 1 < st.length && st[k + 1] === 2) k++;
      a = `${id}: over this point now; most likely still here through ${L(k)} min` + (k + 1 < st.length ? `, leaving between ${L(k)} and ${L(k + 1)} min.` : '.');
    } else if (f2 >= 0) {
      a = f2 === 0 ? `${id}: most likely here by +15 min (${fmtUTC(rows[0].valid)}, ${loc(rows[0].valid)} ${tz}).`
        : `${id}: most likely arrives between ${L(f2 - 1)} and ${L(f2)} min (${fmtUTC(rows[f2 - 1].valid).replace(' UTC', '')}–${fmtUTC(rows[f2].valid)}, ${loc(rows[f2 - 1].valid)}–${loc(rows[f2].valid)} ${tz}).`;
      if (f1 >= 0 && f1 < f2) a += ` Possible from ${L(f1)} min (${EN.short}).`;
      if (last2 > f2 || st.slice(f2).some(v => v !== 2)) a += ` Inside its most likely outline at ${leadsOf(2)} min.`;
    } else {
      a = `${id}: never inside its most likely outline; inside its ${EN.short} at ${leadsOf(1)} min.`;
    }
    answers.push([f2 >= 0 ? f2 : 10 + f1, a]);
  }
  answers.sort((a, b) => a[0] - b[0]);
  let answer;
  if (!anyFc) answer = 'No forecast has been issued yet at this time.';
  else if (!answers.length) answer = `No band’s forecast outline or ${EN.short} covers this point through +180 min.`;
  else answer = answers.map(a => a[1]).join('<br>');
  const nowBand = now.length ? `inside ${now.map(p => labelOf(p.band_id) + (p.tier === 2 ? ' (inland)' : '')).join(', ')}`
    : farNow.length ? `inside the dashed part of ${farNow.map(p => groupLabel(p)).join(', ')} beyond the solid line (lower confidence)` : 'not inside a band';
  return `
    <div class="closebar"><h3>At ${Math.abs(lat).toFixed(3)}°${lat >= 0 ? 'N' : 'S'} ${Math.abs(lon).toFixed(3)}°${lon < 0 ? 'W' : 'E'}</h3>
      <button type="button" id="clear">Clear</button></div>
    <p>Radar now: ${esc(nowText)}; ${esc(nowBand)}.</p>
    <p class="answer">${answer.split('<br>').map(esc).join('<br>')}</p>
    <table class="leads"><thead><tr><th>Lead</th><th>Valid</th><th class="opt">${esc(tz)}</th><th>This point</th></tr></thead><tbody>
    ${rows.map(r => `<tr${r.lead === S.lead ? ' class="cur"' : ''}><td>+${r.lead}</td><td class="t">${r.any ? fmtUTC(r.valid) : '—'}</td><td class="opt t">${r.any ? esc(loc(r.valid)) : ''}</td>
      <td>${!r.any ? 'no forecast' : r.inOutline.length ? `<span class="inside">Inside ${esc(r.inOutline.join(', '))}</span>` : r.inRange.length ? `In ${EN.short} of ${esc(r.inRange.join(', '))}` : 'Outside'}</td></tr>`).join('')}
    </tbody></table>
    <p class="hint">Forecasts exist only at these leads; times between them are not forecast. Hatched region: ${esc(EN.long)}.</p>`;
}

// ------------------------------------------------------------------ details: band
function bandFacts(p, id) {
  const st = (p.stations || []).filter(s => s && s.peak_dbz != null);
  const means = (p.stations || []).filter(s => s && s.mean_dbz != null).map(s => s.mean_dbz);
  const whole = p.peak_dbz != null || p.mean_dbz != null;
  const peak = p.peak_dbz != null ? p.peak_dbz : st.length ? Math.max(...st.map(s => s.peak_dbz)) : null;
  const mean = p.mean_dbz != null ? p.mean_dbz : means.length ? 10 * Math.log10(means.reduce((a, v) => a + 10 ** (v / 10), 0) / means.length) : null;
  const beams = (p.stations || []).filter(s => s && s.beam_km != null).map(s => s.beam_km);
  const visible = p.visible_length_km != null ? p.visible_length_km : lineKm(linesOf(p.centerline));
  const len = p.length_km != null ? `${r1(p.length_km)} km`
    : `unknown: an end is cut by the coverage edge (at least ${Math.round(visible)} km of centerline visible)`;
  const az = p.frame && p.frame.axis_azimuth_deg;
  const orient = az != null ? `${Math.round(az)}° / ${Math.round((az + 180) % 360)}° (${compass(az)}–${compass(az + 180)})` : 'unknown';
  const lin = [];
  if (p.split_from) lin.push(`split from ${p.split_from}`);
  if (p.merged_from && p.merged_from.length) lin.push(`absorbed ${p.merged_from.join(', ')}`);
  const flags = (p.flags || []).map(f => FLAGS[f] || f.replace(/_/g, ' '));
  const twoZone = p.intensity_zone === 'near_only';
  const nz = twoZone ? ' (inside the solid line)' : '';
  const far = p.zone === 'near+far' && p.far_beam_agl_km
    ? `yes, dashed: beam ${r1(p.far_beam_agl_km[0])}–${r1(p.far_beam_agl_km[1])} km above ground; lower confidence; its strength is not used`
    : 'none';
  return `<dl class="facts">
      <dt>Lake</dt><dd>${esc(lakeName(p.lake || id.lake))}</dd>
      <dt>Tier</dt><dd>${p.tier === 2 ? '2, inland addition (outline dashed)' : '1, primary band (outline solid)'}</dd>
      <dt>Length</dt><dd>${esc(len)}${twoZone ? ' inside the solid line' : ''}</dd>
      <dt>Orientation</dt><dd>${esc(orient)}</dd>
      <dt>Peak${esc(nz)}</dt><dd>${peak != null ? `${r1(peak)} dBZ` : 'not available'}</dd>
      <dt>Mean${esc(nz)}</dt><dd>${mean != null ? `${r1(mean)} dBZ` : 'not available'}</dd>
      <dt>Beam height</dt><dd>${beams.length ? `${r1(Math.min(...beams))}–${r1(Math.max(...beams))} km above ground` : 'unknown'}</dd>
      ${twoZone ? `<dt>Beyond the line</dt><dd>${esc(far)}</dd>` : ''}
      <dt>Age</dt><dd>${p.age_min != null ? `${ago(p.age_min)} since first detected` : 'unknown'}</dd>
      <dt>Lineage</dt><dd>${esc(lin.length ? lin.join('; ') : 'no split or merge recorded')}</dd>
      ${flags.length ? `<dt>Flags</dt><dd>${esc(flags.join('; '))}</dd>` : ''}
      ${p.coverage_gaps ? `<dt>Gaps</dt><dd>${p.coverage_gaps} coverage gap(s) inside the band</dd>` : ''}
    </dl>
    <p class="hint">${twoZone ? 'Peak and mean are over the band’s part inside the solid line only; beyond it the radar beam is above much of the snow, so those values are never used.'
      : whole ? 'Peak and mean are over the whole band outline.' : 'Peak and mean are over the band’s 7 measuring stations.'} Reflectivity is not a snowfall rate; it weakens with range as the beam rises above shallow snow.</p>`;
}

function forecastTable(bandId, p) {
  const sc = S.scene;
  const fcs = [];
  for (const lake of S.lakes) { const fc = sc.lakes[lake] && sc.lakes[lake].forecast; if (fc) for (const f of fc.features) if (f.properties.band_id === bandId) fcs.push(f.properties); }
  fcs.sort((a, b) => a.lead_min - b.lead_min);
  if (!fcs.length) return '<p class="hint">No forecast for this band in the forecast shown. Forecasts are issued at :00 and :30 for the bands of the newest scan; band identifiers can change between scans.</p>';
  const az = p && p.frame ? p.frame.axis_azimuth_deg : null;
  const side = v => {
    if (v == null) return 'unknown';
    if (Math.abs(v) < 0.5) return '0 km';
    const dir = az == null ? (v > 0 ? 'left' : 'right') : compass(v > 0 ? az - 90 : az + 90);
    return `${Math.abs(v).toFixed(0)} km ${dir}`;
  };
  const mean = a => { const v = (a || []).filter(x => x != null); return v.length ? v.reduce((s, x) => s + x, 0) / v.length : null; };
  const end = v => v == null ? 'unknown' : Math.abs(v) < 0.5 ? 'no change' : (v > 0 ? `+${v.toFixed(0)} km` : `${v.toFixed(0)} km`);
  return `<table class="leads"><thead><tr><th>Lead</th><th>Centerline, most likely (${esc(envName(sc).short)})</th><th>Lake end</th><th>Inland end</th><th class="opt">Exists</th></tr></thead><tbody>
    ${fcs.map(f => {
      const ss = f.station_shift_km || {};
      const m = f.summary && f.summary.translation_km != null ? f.summary.translation_km : mean(ss.p50);
      const lo = mean(ss.p10), hi = mean(ss.p90);
      const ex = f.probability && f.probability.exists;
      return `<tr${f.lead_min === S.lead ? ' class="cur"' : ''}><td>+${f.lead_min}</td><td>${esc(side(m))}${lo != null && hi != null ? ` <span class="opt">(${esc(side(lo))} to ${esc(side(hi))})</span>` : ''}</td>
        <td>${esc(end(f.end_advance_km && f.end_advance_km.lake))}</td><td>${esc(end(f.end_advance_km && f.end_advance_km.inland))}</td>
        <td class="opt">${ex != null ? Math.round(ex * 100) + '%' : '—'}</td></tr>`;
    }).join('')}</tbody></table>
    <p class="hint">Centerline: mean sideways shift over the 7 stations, by compass direction. Ends: + means the end extends, − means it retreats.${fcs[0].probability_source ? ` Exists: ${esc(fcs[0].probability_source)}.` : ''} Issued ${esc(fmtUTC(tIso(fcs[0].issuance_time)))}; model ${esc(fcs[0].model ? fcs[0].model.family + ' ' + (fcs[0].model.config || '') + (fcs[0].model.artifact_short_id ? ' (artifact ' + fcs[0].model.artifact_short_id + ')' : '') : 'unknown')}.${
      forecasterOf(sc).comparison ? ' Persistence, for comparison: 0 km and no change at every lead (the band stays where it is).' : ''}</p>`;
}

function bandReport() {
  const sc = S.scene;
  const now = currentBands(sc).find(f => f.properties.band_id === S.band);
  const id = parseBandId(S.band);
  const p = now ? now.properties : null;
  if (p && inGroup(p)) {
    // one band across the far zone, tracked in pieces: one heading, each piece with its own facts and forecast
    const members = p.display_group_members.map(m => currentBands(sc).find(f => f.properties.band_id === m)).filter(Boolean);
    const col = colourOf(p);
    return `<div class="closebar"><h3><span class="swatch" style="background:${col}"></span>Band ${esc(groupLabel(p))}</h3><button type="button" id="clear">Close</button></div>
      <p>One band, joined through the zone beyond the solid line${p.display_group_join_held ? ' (join held from recent scans while the echo between the pieces is weak)' : ''}.
      It is tracked as ${members.length} pieces, west to east; each piece keeps its own identifier and its own forecast.</p>
      ${members.map(f => `<h3 class="piece">Piece ${esc(labelOf(f.properties.band_id))}</h3>${bandFacts(f.properties, parseBandId(f.properties.band_id))}
        <h3 class="piece sub">How ${esc(labelOf(f.properties.band_id))} is forecast to move</h3>${forecastTable(f.properties.band_id, f.properties)}`).join('')}`;
  }
  const col = p ? colourOf(p) : hashColour(S.band);
  const facts = p ? bandFacts(p, id) : `<p>This band is not in the radar scan shown; it was in the scan the forecast was made from.</p>`;
  return `<div class="closebar"><h3><span class="swatch" style="background:${col}"></span>Band ${esc(labelOf(S.band))}</h3><button type="button" id="clear">Close</button></div>${facts}<h3>How it is forecast to move</h3>${forecastTable(S.band, p)}${CASES ? casesBandVerify(S.band) : ''}`;
}

function farOnlyReport() {
  const f = farOnlyObjects(S.scene).find(x => x.properties.band_id === S.farOnly);
  if (!f) return '<p class="hint">That object is not in the scan shown.</p><button type="button" id="clear">Close</button>';
  const p = f.properties, b = p.far_beam_agl_km || [];
  const az = p.orientation_deg;
  return `<div class="closebar"><h3><span class="swatch" style="background:${FAR_ONLY_COLOUR}"></span>Seen only beyond the solid line</h3><button type="button" id="clear">Close</button></div>
    <dl class="facts">
      <dt>Lake</dt><dd>${esc(lakeName(p.lake))}</dd>
      <dt>Length</dt><dd>${p.length_km != null ? `${r1(p.length_km)} km` : 'unknown'}</dd>
      ${az != null ? `<dt>Orientation</dt><dd>${Math.round(az)}° / ${Math.round((az + 180) % 360)}° (${compass(az)}–${compass(az + 180)})</dd>` : ''}
      <dt>Beam height</dt><dd>${b[0] != null ? `${r1(b[0])}–${r1(b[1])} km above ground` : 'unknown'}</dd>
      <dt>Strength</dt><dd>not available: the beam is above much of the snow here</dd>
      <dt>Identity</dt><dd>none: not tracked and not forecast</dd>
    </dl>
    <p class="hint">${esc(p.review_note || '')} Hidden unless “Show objects seen only beyond the solid line” is on.</p>`;
}

function renderDetails() {
  const el = $('details');
  if (!S.scene) { el.innerHTML = '<p class="hint">Waiting for the run’s files.</p>'; return; }
  if (S.band) el.innerHTML = bandReport();
  else if (S.farOnly && S.showFarOnly) el.innerHTML = farOnlyReport();
  else if (S.point) el.innerHTML = pointReport();
  else el.innerHTML = '<p class="hint">Tap the map at a place to see, for each lead, whether a band’s forecast covers it. Tap a band’s label or centerline for its details.</p>';
  const c = $('clear'); if (c) c.addEventListener('click', () => { S.band = null; S.point = null; S.farOnly = null; renderAll(); });
}

// ------------------------------------------------------------------ pointer input: pan, pinch, click
const ptrs = new Map();
let gesture = null;
cv.addEventListener('pointerdown', e => {
  cv.setPointerCapture(e.pointerId);
  ptrs.set(e.pointerId, [e.offsetX, e.offsetY]);
  gesture = { moved: false, start: [e.offsetX, e.offsetY], view: { ...S.view }, dist: null };
  if (ptrs.size === 2) { const [a, b] = [...ptrs.values()]; gesture.dist = Math.hypot(a[0] - b[0], a[1] - b[1]); gesture.mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]; gesture.moved = true; }
});
cv.addEventListener('pointermove', e => {
  if (!ptrs.has(e.pointerId) || !gesture) return;
  const prev = ptrs.get(e.pointerId);
  ptrs.set(e.pointerId, [e.offsetX, e.offsetY]);
  if (ptrs.size === 2 && gesture.dist) {
    const [a, b] = [...ptrs.values()];
    const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
    zoomAt(d / gesture.dist, gesture.mid[0], gesture.mid[1]);
    gesture.dist = d;
    return;
  }
  if (Math.hypot(e.offsetX - gesture.start[0], e.offsetY - gesture.start[1]) > 6) gesture.moved = true;
  if (gesture.moved) {
    S.view.lon -= (e.offsetX - prev[0]) / (S.view.ppd * COS43);
    S.view.lat += (e.offsetY - prev[1]) / S.view.ppd;
    draw();
  }
});
function endPtr(e) {
  const g = gesture;
  ptrs.delete(e.pointerId);
  if (ptrs.size === 0) gesture = null;
  if (e.type === 'pointerup' && g && !g.moved && ptrs.size === 0) clickAt(e.offsetX, e.offsetY);
}
cv.addEventListener('pointerup', endPtr);
cv.addEventListener('pointercancel', endPtr);
cv.addEventListener('wheel', e => { e.preventDefault(); zoomAt(Math.exp(-e.deltaY * 0.0015), e.offsetX, e.offsetY); }, { passive: false });
cv.addEventListener('dblclick', e => zoomAt(1.6, e.offsetX, e.offsetY));

function clickAt(x, y) {
  if (!S.scene) return;
  for (const h of S.hits || []) {
    const r = h.rect;
    if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) { S.band = h.id; S.point = null; S.farOnly = null; renderAll(); return; }
  }
  let best = null, bd = 9;            // close to a centerline selects the band; anywhere else is a point query
  for (const f of currentBands(S.scene)) {
    const d = Math.min(distToLinesPx(linesOf(f.properties.centerline), x, y), distToLinesPx(linesOf(f.properties.centerline_far), x, y));
    if (d < bd) { bd = d; best = f.properties.band_id; }
  }
  if (best) { S.band = best; S.point = null; S.farOnly = null; renderAll(); return; }
  if (S.showFarOnly) for (const f of farOnlyObjects(S.scene)) {
    const d = distToLinesPx(linesOf(f.properties.centerline), x, y);
    if (d < bd) { bd = d; best = f.properties.band_id; }
  }
  if (best) { S.farOnly = best; S.band = null; S.point = null; renderAll(); return; }
  const [lon, lat] = unpx(x, y);
  S.point = { lon, lat }; S.band = null; S.farOnly = null;
  renderAll();
  if (window.innerWidth <= 900) $('details').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ================================================================== CASES MODE (past storms) — begin
// A stored storm played with the same scrubber and drawing as live: cases/index.json, cases/<id>/case.json.gz and
// cases/<id>/frames-NN.jsonl.gz (contract lakeband.cases/1, src/pilot/cases/pack.py). Each frame is one lake and one
// radar scan: radar picture, bands now, the forecast at every lead, persistence, and for each lead where the band
// actually was at the valid time (the band LakeBand's verification matched to it). The rest of the page reaches this
// block only behind `if (CASES)`; outside Cases mode only casesUi() runs (it adds the Past storms button).
const C = { index: null, id: null, man: null, chunks: new Map(), frames: new Map(), where: {}, showActual: params.get('actual') === '1' };   // verification: off by default

// geometry of the packs (src/pilot/cases/encode.py): outlines in whole cell-edge numbers of the lake grid, as
// [first x, first y, step x, step y, ...] per ring; lines in 1e-4 degree, the same way
function casesRings(polys, g) {
  if (!polys) return null;
  return { type: 'MultiPolygon', coordinates: polys.map(poly => poly.map(flat => {
    let x = flat[0], y = flat[1];
    const ring = [[g.lon0 + g.dlon * (x - 0.5), g.lat0 + g.dlat * (y - 0.5)]];
    for (let k = 2; k < flat.length; k += 2) { x += flat[k]; y += flat[k + 1]; ring.push([g.lon0 + g.dlon * (x - 0.5), g.lat0 + g.dlat * (y - 0.5)]); }
    ring.push(ring[0]);
    return ring;
  })) };
}
function casesLines(parts) {
  if (!parts) return null;
  return { type: 'MultiLineString', coordinates: parts.map(flat => {
    let x = flat[0], y = flat[1];
    const line = [[x / 1e4, y / 1e4]];
    for (let k = 2; k < flat.length; k += 2) { x += flat[k]; y += flat[k + 1]; line.push([x / 1e4, y / 1e4]); }
    return line;
  }) };
}
async function casesText(url) {             // gzip text, decompressed here unless the host already did
  const r = await fetch(url, { cache: 'no-cache' });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  const buf = new Uint8Array(await r.arrayBuffer());
  if (buf[0] === 0x1f && buf[1] === 0x8b) return new Response(new Blob([buf]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
  return new TextDecoder().decode(buf);
}
// a frames file: line 1 its header, then one frame per line; only the frames shown are parsed (at most 3 files kept)
function casesChunk(k) {
  const key = `${C.id}/${k}`;
  if (C.chunks.has(key)) { const v = C.chunks.get(key); C.chunks.delete(key); C.chunks.set(key, v); return v; }
  const p = casesText(`${CASES_BASE}${C.id}/${C.man.chunks[k].name}`).then(text => {
    const lines = new Map();
    let a = text.indexOf('\n') + 1;
    while (a > 0 && a < text.length) {
      let b = text.indexOf('\n', a); if (b < 0) b = text.length;
      const m = /^\{"lake":"([a-z]+)","n":"(\d{8}T\d{6}Z)"/.exec(text.slice(a, a + 60));
      if (m) lines.set(`${m[1]}/${m[2]}`, [a, b]);
      a = b + 1;
    }
    return { text, lines };
  }).catch(e => { C.chunks.delete(key); throw e; });
  C.chunks.set(key, p);
  while (C.chunks.size > 3) C.chunks.delete(C.chunks.keys().next().value);
  return p;
}
function casesFrame(lake, name) {
  const key = `${C.id}/${lake}/${name}`;
  if (C.frames.has(key)) { const v = C.frames.get(key); C.frames.delete(key); C.frames.set(key, v); return v; }
  const k = C.where[lake] && C.where[lake].get(name);
  if (k == null) return Promise.resolve(null);
  const p = casesChunk(k).then(ch => {
    const ab = ch.lines.get(`${lake}/${name}`);
    return ab ? casesDecode(JSON.parse(ch.text.slice(ab[0], ab[1])), lake) : null;
  }).catch(e => { C.frames.delete(key); throw e; });
  C.frames.set(key, p);
  while (C.frames.size > 160) C.frames.delete(C.frames.keys().next().value);
  return p;
}
const casesIso = ms => new Date(ms).toISOString().replace('.000Z', 'Z');
// one packed frame -> the live bundle's shapes (band and forecast collections), so the page draws it unchanged
function casesDecode(fr, lake) {
  const g = C.man.lake[lake].grid, m = C.man.model;
  const bands = fr.b.map(b => ({ type: 'Feature', geometry: casesRings(b.o, g), properties: {
    band_id: `${lake}|T${b.tier}|${b.id}`, lake, tier: b.tier, track_id: b.id, radar_scan_time: fr.t0, radar_scan_end: fr.t1,
    centerline: casesLines(b.c), peak_dbz: b.pk ?? null, mean_dbz: b.mn ?? null, flags: b.fl || [], split_from: b.sf || null,
    merged_from: b.mf || [], coverage_gaps: b.gaps || 0, length_km: b.len ?? null, visible_length_km: b.vis ?? null,
    frame: b.az != null ? { axis_azimuth_deg: b.az } : null, age_min: b.age ?? null,
    stations: b.beam ? [{ beam_km: b.beam[0] }, { beam_km: b.beam[1] }] : [] } }));
  const feats = [], pfeats = [];
  const modelTag = { family: m.family, config: `${m.config}, cross-validation version ${m.fold + 1} of ${m.folds}` };
  for (const f of fr.f) {
    const id = `${lake}|T${f.tier}|${f.id}`;
    const pc = casesLines(f.pc), po = casesRings(f.po, g);
    for (const L of f.L) {
      const common = { band_id: id, lake, tier: f.tier, track_id: f.id, lead_min: L.lead, radar_scan_time: fr.t0,
        radar_scan_end: fr.t1, issuance_time: fr.iss, valid_time: casesIso(tIso(fr.iss) + L.lead * 60000) };
      feats.push({ type: 'Feature', geometry: casesRings(L.o, g), properties: { ...common, centerline: casesLines(L.c),
        corridor_80: casesRings(L.r, g), corridor_80_kind: 'model percentiles',
        station_shift_km: { p10: [L.p10 ?? null], p50: [L.p50 ?? null], p90: [L.p90 ?? null] },
        summary: { translation_km: L.tr ?? null }, end_advance_km: { lake: L.dA ?? null, inland: L.dB ?? null },
        probability: { exists: L.pe ?? null }, probability_source: 'model', model: modelTag, forecaster: 'model',
        shown_as: 'main', inputs_available: {}, degraded: false, degraded_reasons: [],
        verify: { status: L.st || null, censor: L.cz || null, target: L.tg || null, model_km: L.em ?? null, persistence_km: L.ep ?? null } } });
      pfeats.push({ type: 'Feature', geometry: po, properties: { ...common, centerline: pc, model: { family: 'persistence' },
        forecaster: 'persistence', shown_as: 'comparison', probability_source: 'training base rate (not a forecast)', degraded_reasons: [] } });
    }
  }
  const omitted = Object.fromEntries(Object.entries(fr.omit || {}).map(([L, why]) => [`lead${L}_omitted`, why]));
  const coll = (features, extra) => ({ type: 'FeatureCollection', lake, issuance_time: fr.iss, issue_kind: 'scan', scored: false,
    radar_scan_time: fr.t0, radar_scan_end: fr.t1, ...omitted, ...extra, features });
  return { name: fr.n, iss: fr.iss, png: fr.png,
    bands: { type: 'FeatureCollection', lake, radars_in_composite: fr.radars, features: bands },
    forecast: fr.iss ? coll(feats, { forecaster: { family: m.family, config: m.config, role: 'cross_validation', label: m.label },
      forecaster_used: `${m.family} ${m.config}`, shown_as: 'main' }) : null,
    persistence: fr.iss && pfeats.length ? coll(pfeats, { forecaster_used: 'persistence', shown_as: 'comparison' }) : null };
}
// where each band actually was: the verification's matched band, drawn from the frame of the scan near the valid time
async function casesAttachActual(lake, fr) {
  if (fr.actualDone) return;
  const names = new Set();
  for (const f of fr.forecast.features) { const tg = f.properties.verify.target; if (tg) names.add(tg.n); }
  const got = new Map(await Promise.all([...names].map(async n => [n, await casesFrame(lake, n).catch(() => null)])));
  for (const f of fr.forecast.features) {
    const v = f.properties.verify, tg = v.target;
    if (!tg) continue;
    const later = got.get(tg.n);
    const b = later && later.bands.features.find(x => x.properties.tier === 1 && x.properties.track_id === tg.id);
    if (b) v.actual = { geometry: b.geometry, centerline: b.properties.centerline, scan_time: b.properties.radar_scan_time, scan_end: tg.end, track_id: tg.id };
  }
  fr.actualDone = true;
}
function casesIndex() {
  const out = {};
  for (const lake of S.lakes) {
    const fr = (C.man.lake[lake] || {}).frames || [];
    out[lake] = { bands: fr.map(x => x[0]), radar: fr.map(x => x[0]), forecast: fr.filter(x => x[2]).map(x => x[0]) };
  }
  return out;
}
function casesFixed(lake) {
  const L = C.man.lake[lake];
  return { bounds: L.bounds, coverageFar: null,
    coverage: { type: 'FeatureCollection', features: [{ type: 'Feature', geometry: casesRings(L.coverage, L.grid), properties: { lake } }] } };
}
async function casesLake(lake, t, fixed) {
  const name = latestIn((S.index[lake] || {}).bands, t);
  const fr = name ? await casesFrame(lake, name).catch(() => null) : null;
  const radar = fr ? await cached(`cR/${C.id}/${lake}/${name}`, () => loadImage('data:image/png;base64,' + fr.png)).catch(() => null) : null;
  if (fr && fr.forecast) await casesAttachActual(lake, fr).catch(() => null);
  return { ...fixed, bands: fr && fr.bands, radar, radarFar: null, forecast: fr && fr.forecast, persistence: fr && fr.persistence,
    bundle: fr && { name, issuance_time: fr.iss, issue_kind: 'scan', forecast_from_scan: fr.bands.features.length ? fr.bands.features[0].properties.radar_scan_time : null },
    stamps: { bs: name, rs: name, fs: name, ps: name } };
}
function casesDrawActual(sc) {
  if (!C.showActual) return;
  const fcs = forecastsAt(sc, S.lead);
  for (const f of fcs) {
    const a = f.properties.verify && f.properties.verify.actual;
    if (!a) continue;
    // outline: thin black dash-dot; centerline: a black line with a white core (neither is used for anything else)
    ctx.beginPath(); pathPolys(a.geometry);
    ctx.setLineDash([10, 4, 2, 4]); ctx.lineWidth = 2; ctx.strokeStyle = '#0f172a'; ctx.stroke(); ctx.setLineDash([]);
    ctx.beginPath(); pathLines(linesOf(a.centerline));
    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 12; ctx.stroke();
    ctx.strokeStyle = '#0f172a'; ctx.lineWidth = 8; ctx.stroke();
    ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 3; ctx.stroke();
    sampleLines(linesOf(a.centerline));
  }
  const sel = S.band && fcs.find(f => f.properties.band_id === S.band && f.properties.verify && f.properties.verify.actual);
  if (sel) S.deferred.push(() => {                  // a label for the selected band's actual position only
    const main = longestLine(linesOf(sel.properties.verify.actual.centerline));
    if (main) badge(`${labelOf(sel.properties.band_id)} actual +${S.lead}`, px(...pointAlong(main, 0.3)), '#0f172a', 'fc', coversLine);
    flushBadges();
  });
}
const casesKm = v => v == null ? '—' : `${v.toFixed(1)} km`;
function casesWhy(v) {
  if (v.model_km != null) return null;
  if (v.censor === 'outside_event_window') return 'after the storm’s end';
  if (v.censor) return 'radar could not tell';
  if (v.status === 'absent') return 'band gone';
  return 'not scored';
}
function casesLeadTable(sc, fx) {
  if (!sc || !S.lead) return '';
  const fcs = forecastsAt(sc, S.lead);
  if (!fcs.length) { const why = leadOmitted(sc, S.lead); return `<p class="hint">${why ? `+${S.lead} min: not shown from this scan; ${esc(why)}.` : 'No band was forecast at this scan.'}</p>`; }
  const bands = currentBands(sc);
  const rows = fcs.map(f => {
    const p = f.properties, v = p.verify || {}, now = bands.find(b => b.properties.band_id === p.band_id);
    const az = now && now.properties.frame ? now.properties.frame.axis_azimuth_deg : null;
    const m = p.summary ? p.summary.translation_km : null;
    const dir = m == null ? 'unknown' : Math.abs(m) < 0.5 ? 'stays' : `${Math.abs(m).toFixed(0)} km ${az == null ? (m > 0 ? 'left' : 'right') : compass(m > 0 ? az - 90 : az + 90)}`;
    const ex = p.probability && p.probability.exists;
    const why = casesWhy(v);
    const better = v.model_km != null && v.persistence_km != null ? (v.model_km < v.persistence_km ? ' class="better"' : '') : '';
    return { ex: ex == null ? -1 : ex, html: `<tr><td><button type="button" class="linkish" data-band="${esc(p.band_id)}">${esc(labelOf(p.band_id, true))}</button></td>` +
      `<td>${ex != null ? Math.round(ex * 100) + '%' : '—'}</td><td class="opt">${esc(dir)}</td>` +
      (!C.showActual ? '' : why ? `<td colspan="2" class="why">${esc(why)}</td>` : `<td${better}>${esc(casesKm(v.model_km))}</td><td>${esc(casesKm(v.persistence_km))}</td>`) + '</tr>' };
  }).sort((a, b) => b.ex - a.ex);
  const valids = [...new Set(fcs.map(f => fmtUTC(tIso(f.properties.valid_time))))].sort();   // each lake's own issuance
  const valid = valids.length > 1 ? `${valids[0].replace(' UTC', '')}–${valids[valids.length - 1]}` : valids[0];
  return `<table class="leads compact casestable"><caption>At +${S.lead} min · valid ${esc(valid)}</caption><thead><tr><th>Band</th><th>Still exists</th>` +
    `<th class="opt">Model: centerline</th>${C.showActual ? '<th>Off by: model</th><th>Off by: persistence</th>' : ''}</tr></thead><tbody>${rows.map(r => r.html).join('')}</tbody></table>` +
    casesOmittedNote(sc, fcs) +
    (!C.showActual ? '' : `<p class="hint">Off by: distance between the forecast centerline and the centerline of the band LakeBand’s verification matched to it in the radar scan nearest the valid time (drawn as a black line with a white core). Bold: the model was closer than persistence (the band left where it was). Band gone: no band was found there again.</p>`);
}
// the band report (Cases mode): for every lead, how far the model and persistence were from where the band actually was
function casesBandVerify(bandId) {
  if (!C.showActual) return '';                       // verification is off by default (an operational view)
  const sc = S.scene, fcs = [];
  for (const lake of S.lakes) { const fc = sc && sc.lakes[lake] && sc.lakes[lake].forecast; if (fc) for (const f of fc.features) if (f.properties.band_id === bandId) fcs.push(f.properties); }
  if (!fcs.length) return '';
  fcs.sort((a, b) => a.lead_min - b.lead_min);
  const rows = fcs.map(p => {
    const v = p.verify || {}, why = casesWhy(v);
    const at = v.target ? `${fmtUTC(tIso(v.target.end))}${v.target.id !== p.track_id ? ' as ' + v.target.id : ''}` : '—';
    return `<tr${p.lead_min === S.lead ? ' class="cur"' : ''}><td>+${p.lead_min}</td>` +
      (why ? `<td colspan="2" class="why">${esc(why)}</td>` : `<td${v.model_km < v.persistence_km ? ' class="better"' : ''}>${esc(casesKm(v.model_km))}</td><td>${esc(casesKm(v.persistence_km))}</td>`) +
      `<td class="opt">${esc(at)}</td></tr>`;
  }).join('');
  return `<h3>Where it actually went</h3><table class="leads casestable"><thead><tr><th>Lead</th><th>Off by: model</th><th>Off by: persistence</th><th class="opt">Found in the scan ending</th></tr></thead><tbody>${rows}</tbody></table>
    <p class="hint">Centerline distance from the forecast to the band LakeBand’s verification matched to it in the radar scan nearest the valid time (“as B0xx” when that band carries another identifier, after a split or merge). Bold: the model was closer than persistence.</p>`;
}
// a lake whose scan does not show this lead (+180 at minutes 01-29, as live), while the other lake's does
function casesOmittedNote(sc, fcs) {
  const lakes = S.lakes.filter(l => { const fc = sc.lakes[l] && sc.lakes[l].forecast; return fc && fc[`lead${S.lead}_omitted`]; });
  if (!lakes.length || !fcs.length) return '';
  return `<p class="hint">+${S.lead} min is not shown for ${esc(lakes.map(lakeName).join(' and '))} from its newest scan: ${esc(leadOmitted(sc, S.lead))}.</p>`;
}
function casesLeadKey() {
  return C.showActual ? ` <span class="key actual"></span>where it actually was (verification).` : '';
}
function casesScorecard() {
  const el = $('scorecard'), m = C.man;
  if (!m) { el.hidden = true; el.innerHTML = ''; return; }
  const s = m.summary.leads;
  const pct = v => v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(0)}%`;
  const row = L => { const r = s[String(L)] || {};
    return `<tr${L === S.lead ? ' class="cur"' : ''}><td>+${L / 60} h</td><td>${esc(casesKm(r.model_mean_km))}</td><td>${esc(casesKm(r.persistence_mean_km))}</td><td>${esc(pct(r.change_pct))}</td><td class="opt">${r.n_forecasts ?? '—'} / ${r.n_bands ?? '—'}</td></tr>`; };
  const h1 = s['60'] || {};
  el.hidden = false;
  el.innerHTML = `<div class="blockhead"><h2>This storm</h2></div>
    <p class="casestitle"><strong>${esc(m.title)}</strong><br><span class="hint">${esc(casesDates(m))} · ${esc(m.lakes.map(lakeName).join(' and '))}</span></p>
    <p class="note">${esc(m.description)}</p>
    <table class="leads compact"><thead><tr><th>Lead</th><th>Model</th><th>Persistence</th><th>Change</th><th class="opt">Forecasts / bands</th></tr></thead><tbody>${row(60)}${row(120)}</tbody></table>
    <p class="hint">Mean position error of the band centerline over the whole storm: every forecast issued at a radar scan whose band was found again near the valid time, primary bands, both lakes. Persistence leaves the band where it was. Scored: ${esc(casesCounts(s))}. At +1 h the model was closer than persistence in ${h1.model_better_share != null ? Math.round(h1.model_better_share * 100) + '%' : '—'} of forecasts.</p>
    <p class="hint">${esc(m.model.note)}</p>`;
}
function casesCounts(s) {
  return [60, 120].map(L => { const r = s[String(L)] || {}; return `+${L / 60} h, ${r.n_forecasts ?? '—'} forecasts of ${r.n_bands ?? '—'} band identities`; }).join('; ');
}
function casesDates(m) {
  const f = s => new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'UTC' }).format(new Date(tIso(s)));
  return `${f(m.start_utc)} to ${f(m.end_utc)} UTC, ${new Date(tIso(m.start_utc)).getUTCFullYear()}`;
}
function casesModeText(modeEl, shownT) {
  modeEl.innerHTML = esc('PAST STORM') + (C.man ? `<span class="long"> · ${esc(C.man.title)}</span>` : '') +
    (isFinite(shownT) ? `<span class="long"> · ${esc(fmtDate(shownT))} UTC</span>` : '');
  modeEl.className = 'mode cases';
}
function casesModelTime() {
  const m = C.man && C.man.model;
  if (!m) return '—';
  return timeHtml('Cross-validation model', esc(`${m.family} ${m.config}, version ${m.fold + 1} of ${m.folds}, trained without this storm`), 'trained without this storm');
}
function casesBanner() {
  const m = (C.man && C.man.model) || {};
  return ['info', 'Past storm: cross-validation model.',
    `Forecasts here come from ${esc(m.label || 'a cross-validation version of LakeBand trained without this storm')} ` +
    `(${esc(m.family || '')} ${esc(m.config || '')}, version ${(m.fold ?? 0) + 1} of ${esc(m.folds ?? 5)}; this storm’s group ${esc(m.validation_group || '')} was left out of its training). ` +
    `It is not the live model, which trained on every one of these storms. Each forecast can be checked against where the band actually went.`,
    'Model trained without this storm.'];
}
async function casesLoad(id) {
  const e = (C.index.cases || []).find(c => c.id === id) || (C.index.cases || [])[0];
  if (!e) throw new Error('no storms in cases/index.json');
  const switching = !!C.man;                     // a storm chosen from the list (not the first load with its URL)
  C.id = e.id; C.chunks.clear(); C.frames.clear();
  C.man = await getBundle(`${CASES_BASE}${e.id}/case.json.gz`);
  C.where = {};
  for (const [lake, L] of Object.entries(C.man.lake)) C.where[lake] = new Map(L.frames.map(x => [x[0], x[1]]));
  S.status = { mode: 'case', event: C.man.title, source: 'cases' };
  S.latest = null; S.latestLakes = {}; S.fixed = {}; S.cache.clear(); S.scene = null;
  if (switching) { S.band = null; S.point = null; S.farOnly = null; }
  S.lakes = [...C.man.lakes].sort();
  S.index = casesIndex();
  S.follow = false; S.step = -1;
  rebuildSteps();
  const sel = $('cases-select'); if (sel) sel.value = C.id;
  const q = new URLSearchParams(location.search); q.set('case', C.id);
  history.replaceState(null, '', `${location.pathname}?${q}`);
  try { localStorage.setItem('lakeband.case', C.id); } catch (_) { /* private mode: nothing remembered */ }
  const at = tIso(C.man.open_at_utc);
  let k = 0; S.steps.forEach((s, i) => { if (s <= at) k = i; });
  await setStep(k, false);
}
async function casesBoot() {
  try {
    C.index = await getJSON(`${CASES_BASE}index.json`, true);
    const sel = $('cases-select');
    if (sel) {
      sel.innerHTML = C.index.cases.map(c => `<option value="${esc(c.id)}">${esc(c.title)}</option>`).join('');
      sel.addEventListener('change', () => { stopPlay(); S.view = null; casesLoad(sel.value).catch(casesFail); });
    }
    let want = params.get('case');
    if (!want) try { want = localStorage.getItem('lakeband.case'); } catch (_) { want = null; }
    await casesLoad(want);
  } catch (e) { casesFail(e); }
}
function casesFail(e) {
  $('banners').innerHTML = `<p class="banner bad"><strong>Cannot read the past storms.</strong>${esc(e.message || e)}.</p>`;
}
function casesUi() {
  const line = document.querySelector('#top .titleline'), mode = $('mode');
  if (!line || !mode) return;
  const box = document.createElement('span');
  box.className = 'casesbar';
  const go = q => { for (const k of ['t', 'band', 'pt', 'view', 'case']) q.delete(k); return q; };
  if (!CASES) {
    box.innerHTML = '<button type="button" id="cases-open" class="casesbtn">Past storms</button>';
    line.insertBefore(box, mode.nextSibling);
    $('cases-open').addEventListener('click', () => { const q = go(new URLSearchParams(location.search)); q.set('case', ''); location.search = q.toString(); });
    return;
  }
  box.classList.add('on');
  box.innerHTML = '<label class="casessel"><span class="long">Storm</span> <select id="cases-select" aria-label="Past storm"></select></label>' +
    '<button type="button" id="cases-live" class="casesbtn">Back to live</button>';
  line.insertBefore(box, mode.nextSibling);
  $('cases-live').addEventListener('click', () => { const q = go(new URLSearchParams(location.search)); const s = q.toString(); location.search = s ? '?' + s : ''; if (!s) location.href = location.pathname; });
  $('latest').textContent = 'End';
  const pw = $('persistwrap');
  if (pw) {
    const lab = document.createElement('label');
    lab.className = 'toggle'; lab.id = 'actualwrap';
    lab.innerHTML = `<input type="checkbox" id="actual"${C.showActual ? ' checked' : ''}> Show where each band actually was
      <span class="sub">The band LakeBand’s verification matched to it in the radar scan nearest the valid time: a black line with a white core.</span>`;
    pw.after(lab);
    $('actual').addEventListener('change', e => { C.showActual = e.target.checked; renderAll(); });
  }
}
function casesShotInfo() {
  const fcs = S.scene && S.lead ? forecastsAt(S.scene, S.lead) : [];
  return { id: C.id, title: C.man && C.man.title, model: C.man && C.man.model.label, fold: C.man && C.man.model.fold,
    steps: S.steps.length, actual_drawn: C.showActual ? fcs.filter(f => f.properties.verify && f.properties.verify.actual).length : 0,
    forecasts: fcs.length, summary_rows: $('scorecard').hidden ? 0 : $('scorecard').querySelectorAll('tbody tr').length,
    chunks_loaded: C.chunks.size, scroll_width: document.documentElement.scrollWidth,
    wider_than_screen: [...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > Math.min(window.innerWidth, screen.width) + 0.5)
      .slice(0, 6).map(e => `${(e.closest('[id]') || {}).id || ''}>${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''}${e.className && typeof e.className === 'string' ? '.' + e.className.split(' ').join('.') : ''}`) };
}
// ================================================================== CASES MODE (past storms) — end

// ------------------------------------------------------------------ polling and start
async function poll() {
  try {
    let status, latest;
    if (BUNDLES) {                             // the pointer only; a bundle is fetched when its name changes
      latest = await getJSON(`${BASE}latest.json`, true);
      status = { mode: latest.mode || 'live', event: latest.event || null, now: latest.clock_utc,
                 heartbeat: latest.updated_utc, scorecard: latest.scorecard, publishing: latest.publishing,
                 forecaster: latest.forecaster, source: 'bundles' };
      latest = { ...latest, lakes: Object.fromEntries(Object.entries(latest.lakes || {}).map(([k, v]) => [k, { ...v, bands: v.path }])) };
    } else {
      [status, latest] = await Promise.all([getJSON(`${BASE}status.json`, true).catch(() => null), getJSON(`${BASE}latest.json`, true)]);
    }
    const changed = JSON.stringify(BUNDLES ? latest.bundles : latest) !== JSON.stringify(BUNDLES ? (S.latest || {}).bundles : S.latest);
    S.status = status; S.latest = latest;
    if (BUNDLES) {                             // the display mosaic named by the pointer (mosaic.py)
      S.mosaicIx = latest.mosaic && latest.mosaic.names && latest.mosaic.names.length ? latest.mosaic : null;
      if (S.mosaicIx && !S.mosaicBounds) S.mosaicBounds = await getJSON(BASE + S.mosaicIx.bounds).catch(() => null);
    }
    if (changed || !S.steps.length) {
      S.latestLakes = latestLakes(latest);
      S.lakes = Object.keys(S.latestLakes).sort();
      S.index = await loadIndex();
      for (const lake of BUNDLES ? [] : S.lakes) {   // make sure the newest files are in the index even if listing lags
        const ix = (S.index[lake] = S.index[lake] || {});
        for (const kind of ['radar', 'bands', 'forecast']) {
          const v = S.latestLakes[lake];
          const pth = kind === 'radar' ? (v.radar || v.radar_image) : v[kind]; if (!pth || typeof pth !== 'string') continue;
          const stamp = pth.split('/').pop().replace(/\.(png|geojson)$/, '');
          ix[kind] = ix[kind] || [];
          if (STAMP_RE.test(stamp) && !ix[kind].includes(stamp)) { ix[kind].push(stamp); ix[kind].sort(); }
        }
      }
      const keep = S.follow;
      rebuildSteps();
      await setStep(keep ? S.steps.length - 1 : S.step, keep);
    } else renderHeader();
  } catch (e) {
    $('banners').innerHTML = `<p class="banner bad"><strong>Cannot read ${BUNDLES ? 'the published files' : 'the run folder'}.</strong>${esc(e.message)}.${BUNDLES ? '' : ' Is app/serve.py running?'}</p>`;
  }
}
function applyUrlView() {
  const v = params.get('view');
  if (v) { const [lon, lat, ppd] = v.split(',').map(Number); if ([lon, lat, ppd].every(isFinite)) S.view = { lon, lat, ppd }; }
}
async function start() {
  buildLeadButtons();
  const lp = +params.get('lead');
  setLead(LEADS.includes(lp) ? lp : 0);
  if (params.get('band')) S.band = params.get('band');
  if (params.get('pt')) { const [lon, lat] = params.get('pt').split(',').map(Number); if (isFinite(lon) && isFinite(lat)) S.point = { lon, lat }; }
  $('play').addEventListener('click', togglePlay);
  $('persist').checked = S.showPersist;
  $('persist').addEventListener('change', e => { S.showPersist = e.target.checked; renderAll(); });
  $('leadtable').addEventListener('click', e => { const b = e.target.closest('[data-band]'); if (b) { S.band = b.dataset.band; S.point = null; renderAll(); } });
  $('trainarea').checked = S.showTrain;
  $('trainarea').addEventListener('change', e => { S.showTrain = e.target.checked; renderAll(); });
  $('faronly').checked = S.showFarOnly;
  $('faronly').addEventListener('change', e => { S.showFarOnly = e.target.checked; if (!S.showFarOnly) S.farOnly = null; renderAll(); });
  $('scrub').addEventListener('input', e => setStep(+e.target.value));
  $('prev').addEventListener('click', () => setStep(S.step - 1));
  $('next').addEventListener('click', () => setStep(S.step + 1));
  $('latest').addEventListener('click', () => setStep(S.steps.length - 1, true));
  $('zoom-in').addEventListener('click', () => zoomAt(1.5, W / 2, H / 2));
  $('zoom-out').addEventListener('click', () => zoomAt(1 / 1.5, W / 2, H / 2));
  $('zoom-fit').addEventListener('click', () => { fitView(); draw(); });
  window.addEventListener('resize', resize);
  const [basemap, sites] = await Promise.all([getJSON('data/basemap.json').catch(() => null), getJSON('data/radars.json').catch(() => null)]);
  S.basemap = basemap; S.sites = sites ? sites.sites : {};
  resize();
  casesUi();                                   // the Past storms button (live), or the storm list (Cases mode)
  if (CASES) await casesBoot(); else await poll();
  const tp = params.get('t');
  if (tp) {
    const t = /^\d{8}T\d{4}Z$/.test(tp) ? parseStamp(tp) : Date.parse(tp);
    let k = 0; S.steps.forEach((s, i) => { if (s <= t) k = i; });
    await setStep(k, false);
  }
  if (!CASES) setInterval(poll, 30000);        // a past storm does not change: nothing to poll
  document.body.dataset.ready = '1';
}
// for the screenshot tool (app/tools/shoot.mjs): what is on the screen, from the page's own state
window.__shotInfo = () => ({
  step: isFinite(S.steps[S.step]) ? new Date(S.steps[S.step]).toISOString() : null, lead: S.lead,
  two_zone: hasFar(S.scene), far_only_shown: S.showFarOnly, far_only_in_scan: S.scene ? farOnlyObjects(S.scene).length : 0,
  bands: S.scene ? currentBands(S.scene).map(f => ({ id: f.properties.band_id, group: f.properties.display_group || null,
    zone: f.properties.zone || null, far_line: !!f.properties.centerline_far, peak_dbz: f.properties.peak_dbz })) : [],
  source: SOURCE.kind, issue_kind: S.scene ? forecastMeta(S.scene).kind : null,
  issued: S.scene && isFinite(forecastMeta(S.scene).iss) ? new Date(forecastMeta(S.scene).iss).toISOString() : null,
  updates_line: !$('updates').hidden, scorecard_rows: $('scorecard').hidden ? 0 : $('scorecard').querySelectorAll('tbody tr').length,
  labels: S.lastLabels || [], view: S.view, forecaster: (({ model, used, role, label, short, comparison, sample }) =>
    ({ model, used, role, label, short, comparison, in_sample: sample ? sample.in_sample : null }))(forecasterOf(S.scene)),
  persistence_shown: S.showPersist && !!S.lead && forecasterOf(S.scene).comparison,
  lead_rows: S.scene && S.lead ? forecastsAt(S.scene, S.lead).map(f => ({ id: f.properties.band_id,
    exists: f.properties.probability ? f.properties.probability.exists : null, source: f.properties.probability_source })) : [],
  cases: CASES ? casesShotInfo() : null,
});
start();
