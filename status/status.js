"use strict";
/*
 * Suite status dashboard.
 * Polls four feeds every 30s and repaints status rows in place (no reload).
 *
 * Freshness source per feed (see Phase-1 spec + decisions):
 *   HRRR     — manifest body `.updated` (cycle publish time); cycle from `.current_cycle`
 *   QLCS     — `Last-Modified` header. The sidecar re-uploads latest.json once per
 *              new MRMS scan (~every 2 min), not every 10s, so freshness is judged on
 *              a few-minute cadence (see THRESH.qlcs); the body `scan_time` is also
 *              shown so a stalled poller is visible in text.
 *   Dewpoint — body `.generated_utc` (truer "data generated" time than upload)
 *   Mac      — heartbeat body `.updated`, plus all the host metrics
 *
 * A 404 means "not migrated / not running yet" (graceful placeholder, not an error);
 * a network/CORS failure means "offline".
 */

const REFRESH_MS = 30000;

const URLS = {
  hrrr: "https://hrrr-data.alexcooke.co/manifest.json",
  qlcs: "https://ohx-data.alexcooke.co/data/latest.json",
  dewpoint: "https://dewpoint-data.alexcooke.co/dewpoint_efi_latest.json",
  heartbeat: "https://hrrr-data.alexcooke.co/status/mac-heartbeat.json",
};

// Color thresholds in seconds: [green-under, yellow-under]; at/above 2nd => red.
const THRESH = {
  hrrr: [90 * 60, 4 * 3600],
  qlcs: [4 * 60, 10 * 60],  // MRMS ~2min cadence + ~45s processing: green <4min, slow <10min
  dewpoint: [7 * 3600, 13 * 3600],   // runs every 6h (05:30/11:30/17:30/23:30 UTC)
  heartbeat: [10 * 60, 30 * 60],
};

// ---- helpers ---------------------------------------------------------------

function ageSeconds(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 1000);
}

function fmtAge(sec) {
  if (sec == null) return "unknown";
  sec = Math.round(sec);
  if (sec < 90) return `${sec}s`;
  const m = Math.round(sec / 60);
  if (m < 90) return `${m}m`;
  const h = Math.floor(sec / 3600);
  const remM = Math.round((sec % 3600) / 60);
  if (h < 48) return remM ? `${h}h ${remM}m` : `${h}h`;
  const d = Math.floor(sec / 86400);
  const remH = Math.floor((sec % 86400) / 3600);
  return remH ? `${d}d ${remH}h` : `${d}d`;
}

function fmtUptime(sec) {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

// Pick green/yellow/red from an age and a [g, y] threshold pair.
function classifyAge(sec, [green, yellow]) {
  if (sec == null) return "unknown";
  if (sec < green) return "fresh";
  if (sec < yellow) return "slow";
  return "stale";
}

const PILL_LABEL = { fresh: "🟢 fresh", slow: "🟡 slow", stale: "🔴 stale" };

function paint(cardId, state, summary, { dim = false } = {}) {
  const card = document.getElementById(cardId);
  if (!card) return;
  const pill = card.querySelector("[data-pill]");
  const sum = card.querySelector("[data-summary]");
  pill.className = "pill " + (state.cls || "unknown");
  pill.textContent = state.label;
  sum.textContent = summary;
  sum.classList.toggle("dim", dim || state.cls === "unknown");
}

function st(cls) {
  return { cls, label: PILL_LABEL[cls] || "—" };
}
const ST_UNMIGRATED = { cls: "unknown", label: "—" };
const ST_OFFLINE = { cls: "stale", label: "🔴 offline" };

// fetch JSON, surfacing status + headers so callers can branch on 404.
async function getFeed(url) {
  const r = await fetch(url, { cache: "no-store" });
  let body = null;
  if (r.ok) {
    try { body = await r.json(); } catch (_) { /* leave null */ }
  }
  return { status: r.status, ok: r.ok, lastModified: r.headers.get("last-modified"), body };
}

// ---- per-feed updates ------------------------------------------------------

async function updateHRRR() {
  try {
    const f = await getFeed(URLS.hrrr);
    if (f.status === 404) return paint("card-hrrr", ST_UNMIGRATED, "not migrated yet", { dim: true });
    if (!f.ok || !f.body) return paint("card-hrrr", ST_OFFLINE, "manifest unreachable");
    const age = ageSeconds(f.body.updated);
    const cls = classifyAge(age, THRESH.hrrr);
    const cyc = f.body.current_cycle || "?";
    const ext = f.body.current_extended_cycle ? ` · extended ${f.body.current_extended_cycle}` : "";
    paint("card-hrrr", st(cls), `Cycle ${cyc} · ${fmtAge(age)} old · ~hourly${ext}`);
  } catch (_) {
    paint("card-hrrr", ST_OFFLINE, "offline");
  }
}

async function updateQLCS() {
  try {
    const f = await getFeed(URLS.qlcs);
    if (f.status === 404) return paint("card-qlcs", ST_UNMIGRATED, "not migrated yet", { dim: true });
    if (!f.ok) return paint("card-qlcs", ST_OFFLINE, "feed unreachable");
    // Prefer Last-Modified (sidecar liveness); fall back to body generated_at.
    let age = f.lastModified ? ageSeconds(f.lastModified) : null;
    if (age == null && f.body) age = ageSeconds(f.body.generated_at);
    const cls = classifyAge(age, THRESH.qlcs);
    let frame = "";
    if (f.body && f.body.scan_time != null) frame = ` · frame ${fmtAge(ageSeconds(f.body.scan_time))} old`;
    paint("card-qlcs", st(cls), `Updated ${fmtAge(age)} ago · ~10s feed${frame}`);
  } catch (_) {
    paint("card-qlcs", ST_OFFLINE, "offline");
  }
}

async function updateDewpoint() {
  try {
    const f = await getFeed(URLS.dewpoint);
    if (f.status === 404) return paint("card-dewpoint", ST_UNMIGRATED, "not migrated yet", { dim: true });
    if (!f.ok || !f.body) return paint("card-dewpoint", ST_OFFLINE, "feed unreachable");
    const age = ageSeconds(f.body.generated_utc);
    const cls = classifyAge(age, THRESH.dewpoint);
    const cyc = f.body.cycle ? `${f.body.cycle} ` : "";
    paint("card-dewpoint", st(cls), `${cyc}· ${fmtAge(age)} old · ~6-hourly`);
  } catch (_) {
    paint("card-dewpoint", ST_OFFLINE, "offline");
  }
}

function setBar(barId, textId, usedGb, totalGb) {
  const pct = totalGb > 0 ? Math.min(100, (usedGb / totalGb) * 100) : 0;
  const bar = document.getElementById(barId);
  bar.style.width = pct.toFixed(1) + "%";
  bar.classList.toggle("slow", pct >= 75 && pct < 90);
  bar.classList.toggle("stale", pct >= 90);
  document.getElementById(textId).textContent = `${Math.round(usedGb)} GB / ${Math.round(totalGb)} GB`;
}

const DAEMON_SHORT = {
  "com.alexcooke.hrrr-ingest": "hrrr",
  "com.alexcooke.ohx-qlcs": "qlcs",
  "com.alexcooke.ohx-qlcs-sync": "qlcs-sync",
};

async function updateMac() {
  const metrics = document.getElementById("mac-metrics");
  try {
    const f = await getFeed(URLS.heartbeat);
    if (f.status === 404) {
      metrics.hidden = true;
      return paint("card-mac", ST_UNMIGRATED, "heartbeat not running yet", { dim: true });
    }
    if (!f.ok || !f.body) {
      metrics.hidden = true;
      return paint("card-mac", ST_OFFLINE, "heartbeat unreachable");
    }
    const hb = f.body;
    const age = ageSeconds(hb.updated);
    const cls = classifyAge(age, THRESH.heartbeat);
    paint("card-mac", st(cls), `heartbeat ${fmtAge(age)} ago`);

    // Detailed panel.
    metrics.hidden = false;
    document.getElementById("mac-uptime").textContent = fmtUptime(hb.uptime_seconds || 0);

    const cores = hb.cpu_cores || 1;
    const loadPct = (hb.load_avg || []).map((l) => Math.round((l / cores) * 100) + "%").join(" / ");
    document.getElementById("mac-load").textContent = loadPct || "–";

    setBar("mac-mem-bar", "mac-mem-text", hb.memory_used_gb, hb.memory_total_gb);
    // Disk: show FREE space. A headless service can't read Finder's
    // purgeable-aware "used", so disk_used_gb over-reports reclaimable cache;
    // free space is the stable, meaningful "will it fill up?" number.
    setBar("mac-disk-bar", "mac-disk-text", hb.disk_used_gb, hb.disk_total_gb);
    const diskFreeGb = Math.max(0, (hb.disk_total_gb || 0) - (hb.disk_used_gb || 0));
    document.getElementById("mac-disk-text").textContent =
      `${diskFreeGb} GB free of ${hb.disk_total_gb} GB`;

    const dEl = document.getElementById("mac-daemons");
    dEl.innerHTML = "";
    const daemons = hb.daemons || {};
    for (const [label, short] of Object.entries(DAEMON_SHORT)) {
      const d = daemons[label];
      const ok = d && d.loaded && (d.last_exit === 0 || d.last_exit == null);
      const span = document.createElement("span");
      span.className = ok ? "ok" : "bad";
      span.textContent = `${ok ? "✓" : "✗"} ${short}`;
      dEl.appendChild(span);
    }

    let hbText = fmtAge(age);
    if (hb.hrrr_inflight != null) hbText += ` · ingesting f${String(hb.hrrr_inflight).padStart(2, "0")}`;
    document.getElementById("mac-hb-age").textContent = hbText;
  } catch (_) {
    metrics.hidden = true;
    paint("card-mac", ST_OFFLINE, "offline");
  }
}

// ---- GHA-driven tools (assets/status/tools.json = last successful run) ------

const GHA_TOOLS = [
  { key: "wind",       label: "Wind",            thresh: [7 * 3600, 13 * 3600] }, // cron hourly, ~6h in practice (GH throttling)
  { key: "caast",      label: "CAAST",           thresh: [3 * 3600, 6 * 3600] },  // hourly (+ throttle margin)
  { key: "efi",        label: "EFI",             thresh: [7 * 3600, 13 * 3600] }, // cron */30, ~6h in practice
  { key: "apt",        label: "Apparent Temp",   thresh: [26 * 3600, 50 * 3600] }, // daily
  { key: "temp",       label: "Temperature Map", thresh: [26 * 3600, 50 * 3600] }, // daily
  { key: "streak",     label: "Streak Map",      thresh: [26 * 3600, 50 * 3600] }, // daily
  { key: "time_since", label: "Time-Since-Temp", thresh: [26 * 3600, 50 * 3600] }, // daily
];

function ensureToolCard(key, label) {
  let card = document.getElementById(`card-${key}`);
  if (!card) {
    card = document.createElement("section");
    card.className = "card";
    card.id = `card-${key}`;
    card.innerHTML =
      `<div class="card-head"><span class="pill" data-pill>· · ·</span><h2>${label}</h2></div>` +
      `<p class="summary" data-summary>Checking…</p>`;
    document.getElementById("gha-tools").appendChild(card);
  }
  return `card-${key}`;
}

async function updateGhaTools() {
  let tools = null;
  try {
    const r = await fetch("/assets/status/tools.json", { cache: "no-store" });
    if (r.ok) tools = (await r.json()).tools || {};
  } catch (_) { /* leave null → offline */ }
  for (const t of GHA_TOOLS) {
    const id = ensureToolCard(t.key, t.label);
    if (!tools) { paint(id, ST_OFFLINE, "status manifest unreachable"); continue; }
    const upd = tools[t.key] && tools[t.key].updated;
    if (!upd) { paint(id, ST_UNMIGRATED, "no successful run yet", { dim: true }); continue; }
    const age = ageSeconds(upd);
    paint(id, st(classifyAge(age, t.thresh)), `ran ${fmtAge(age)} ago`);
  }
}

// ---- loop ------------------------------------------------------------------

function stamp() {
  const t = new Date();
  document.getElementById("last-refresh").textContent =
    "updated " + t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

async function refresh() {
  await Promise.allSettled([updateHRRR(), updateQLCS(), updateDewpoint(), updateMac(), updateGhaTools()]);
  stamp();
}

refresh();
setInterval(refresh, REFRESH_MS);
// Repaint promptly when returning to the tab/phone.
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
