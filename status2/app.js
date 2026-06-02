"use strict";
const REFRESH_MS = 30000;  // public mirror: matches ~30s R2 publish
const $ = (id) => document.getElementById(id);
const netHist = [];           // rolling {down, up} bytes/s for the sparkline
const MAXPTS = 90;

function fmtRate(bps) {
  if (bps < 1024) return `${Math.round(bps)} B/s`;
  if (bps < 1048576) return `${(bps / 1024).toFixed(1)} KB/s`;
  return `${(bps / 1048576).toFixed(2)} MB/s`;
}
function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `up ${d}d ${h}h` : h ? `up ${h}h ${m}m` : `up ${m}m`;
}
function ageStr(iso) {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m ago`;
}
function barClass(pct) { return pct >= 90 ? "bad" : pct >= 75 ? "warn" : ""; }

function drawSpark() {
  const c = $("net-spark"), ctx = c.getContext("2d");
  const w = c.width = c.clientWidth * devicePixelRatio, h = c.height = 48 * devicePixelRatio;
  ctx.clearRect(0, 0, w, h);
  if (netHist.length < 2) return;
  const max = Math.max(1, ...netHist.map((p) => Math.max(p.down, p.up)));
  const line = (key, color) => {
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 1.5 * devicePixelRatio;
    netHist.forEach((p, i) => {
      const x = (i / (MAXPTS - 1)) * w, y = h - (p[key] / max) * (h - 2) - 1;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
  };
  line("down", getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#58a6ff");
  line("up", getComputedStyle(document.body).getPropertyValue("--ok").trim() || "#2fd07f");
}

function renderCores(cores) {
  const el = $("cores");
  if (el.children.length !== cores.length) {
    el.innerHTML = cores.map(() => '<div class="core"><i></i></div>').join("");
  }
  cores.forEach((pct, i) => {
    const bar = el.children[i].firstChild;
    bar.style.height = pct + "%";
    bar.style.background = pct >= 90 ? "var(--bad)" : pct >= 60 ? "var(--warn)" : "var(--accent)";
  });
}

function renderDaemons(d) {
  $("daemons").innerHTML = Object.values(d).map((x) => {
    const running = x.state === "running";
    const ok = x.loaded && (x.last_exit === 0 || x.last_exit === null);
    const cls = !x.loaded ? "bad" : running ? "run" : ok ? "ok" : "bad";
    const note = running ? "running" : x.loaded ? (x.last_exit ? `exit ${x.last_exit}` : "loaded") : "not loaded";
    return `<span class="daemon ${cls}"><span class="led"></span>${x.short} <span class="muted small">${note}</span></span>`;
  }).join("");
}

function hrrrRun(r) {
  if (!r) return '<span class="muted">none in log window</span>';
  const dur = r.duration_s != null ? ` · ${Math.floor(r.duration_s / 60)}m` : "";
  const status = r.complete
    ? `<span class="tag ok">done ${ageStr(r.promoted || r.start)}</span>`
    : '<span class="tag run">running</span>';
  return `${r.cycle} · <b>${r.done}/${r.total ?? "?"}</b> FH${dur} ${status}`;
}

function renderHrrr(h) {
  $("hrrr-now").textContent = h.running ? "● ingesting" : "idle";
  const c = h.current;
  $("hrrr-current").innerHTML = c
    ? `${c.cycle} (${c.kind || "?"}) · <b>${c.done}/${c.total ?? "?"}</b> FH` +
      (c.inflight_fh != null ? ` · f${String(c.inflight_fh).padStart(2, "0")}` : "")
    : '<span class="muted">idle</span>';
  $("hrrr-19h").innerHTML = hrrrRun(h.latest_19h);
  $("hrrr-48h").innerHTML = hrrrRun(h.latest_48h);
}

function renderQlcs(q) {
  $("qlcs-now").textContent = q.scan_time ? `scan ${ageStr(q.scan_time)}` : "—";
  $("qlcs-log").textContent = (q.recent || []).join("\n") || "—";
}

function renderUsage(a) {
  const el = $("usage"), note = $("usage-note");
  if (!a || !a.ok) {
    note.textContent = a && a.reason ? a.reason : "unavailable";
    el.innerHTML = '<span class="muted">—</span>';
    return;
  }
  note.textContent = "page views · 24h / 7d / 30d";
  const fmt = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));
  const head = `<div class="hrrr-row muted small"><span class="label"></span>` +
    `<span>24h / 7d / 30d</span></div>`;
  const rows = (a.tools || []).map((t) =>
    `<div class="hrrr-row"><span class="label">${t.label}</span>` +
    `<span><b>${fmt(t.views["24h"])}</b> / ${fmt(t.views["7d"])} / ${fmt(t.views["30d"])}</span></div>`
  ).join("");
  el.innerHTML = head + (rows || '<span class="muted">no hits yet</span>');
}

function renderGhaTools(list) {
  const el = $("gha-tools");
  if (!list || !list.length) { el.innerHTML = '<span class="muted">—</span>'; return; }
  const color = { fresh: "var(--ok)", slow: "var(--warn)", stale: "var(--bad)" };
  el.innerHTML = list.map((t) => {
    const c = color[t.state] || "#888";
    const age = t.age_s == null ? "—"
      : t.age_s < 5400 ? Math.round(t.age_s / 60) + "m"
      : t.age_s < 172800 ? Math.round(t.age_s / 3600) + "h"
      : Math.round(t.age_s / 86400) + "d";
    return `<span class="daemon"><span class="led" style="background:${c}"></span>` +
      `${t.label} <span class="muted small">${age}</span></span>`;
  }).join("");
}

function renderDewpoint(d) {
  const el = $("dewpoint");
  if (!d || !d.ok) { el.innerHTML = '<span class="muted">unreachable</span>'; return; }
  el.innerHTML = `<span class="big">${ageStr(d.generated_utc)}</span>` +
    `<span class="muted small"> · cycle ${d.cycle || "—"} · runs on ${d.where || "cloud"}</span>`;
}

async function tick() {
  try {
    const r = await fetch("https://hrrr-data.alexcooke.co/status2/stats.json", { cache: "no-store" });
    const s = await r.json();
    $("livedot").classList.remove("stale");
    $("refresh").textContent = "updated " + new Date().toLocaleTimeString();
    $("uptime").textContent = fmtUptime(s.uptime_s);

    $("cpu-pct").textContent = s.cpu.overall + "%";
    renderCores(s.cpu.cores);
    $("loadavg").textContent = `load ${s.cpu.loadavg.join(" / ")} · ${s.cpu.count} cores`;

    $("mem-text").textContent = `${s.mem.used_gb} / ${s.mem.total_gb} GB`;
    const mb = $("mem-bar"); mb.style.width = s.mem.percent + "%"; mb.className = "bar-fill " + barClass(s.mem.percent);

    $("net-down").textContent = fmtRate(s.net.down_bps);
    $("net-up").textContent = fmtRate(s.net.up_bps);
    netHist.push({ down: s.net.down_bps, up: s.net.up_bps });
    while (netHist.length > MAXPTS) netHist.shift();
    drawSpark();

    $("disk-text").textContent = `${s.disk.free_gb} GB free / ${s.disk.total_gb} GB`;
    const db = $("disk-bar"); db.style.width = s.disk.percent + "%"; db.className = "bar-fill " + barClass(s.disk.percent);

    const p = s.power;
    $("power-text").textContent = p ? `${p.soc_watts} W` : "—";
    $("power-sub").textContent = p ? `SoC · ≈ ${p.wall_watts_estimate} W wall` : "unavailable";
    const e = s.energy;
    if (e && e.samples > 1) {
      const cov = e.span_hours < 23.5 ? ` · ${e.span_hours}h so far` : "";
      $("power-24h").textContent = `last 24h: ${e.wall_kwh} kWh${cov}`;
    } else {
      $("power-24h").textContent = "last 24h: collecting…";
    }

    renderDaemons(s.daemons);
    renderHrrr(s.hrrr);
    renderQlcs(s.qlcs);
    renderDewpoint(s.dewpoint);
    renderGhaTools(s.gha_tools);
    renderUsage(s.analytics);
  } catch (e) {
    $("livedot").classList.add("stale");
    $("refresh").textContent = "disconnected — retrying…";
  }
}

tick();
setInterval(tick, REFRESH_MS);
