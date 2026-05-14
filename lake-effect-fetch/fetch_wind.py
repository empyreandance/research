#!/usr/bin/env python3
"""
fetch_wind.py — Pre-fetch wind data from NWS (US) and ECCC HRDPS (Canada).
Runs hourly via GitHub Actions. Outputs wind_data.json.

US points:  NWS API gridpoint forecasts (windDirection, windSpeed, temperature)
CA points:  ECCC GeoMet WMS GetFeatureInfo
              HRDPS.CONTINENTAL_WSPD  — wind speed (m/s → converted to km/h)
              HRDPS.CONTINENTAL_WD    — wind direction (degrees, meteorological)
              HRDPS.CONTINENTAL_TT    — temperature (°C)

Requires: aiohttp (pip install aiohttp)
"""

import json, math, asyncio, time, re
import aiohttp
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ===== POINT DEFINITIONS (must match HTML WIND_POINTS exactly) =====
WIND_POINTS = [
    # Superior — US (20) + CA (8)
    (46.65,-91.8,"Superior","nws"),(46.65,-91.0,"Superior","nws"),
    (46.65,-90.2,"Superior","nws"),(46.65,-89.4,"Superior","nws"),
    (46.65,-88.6,"Superior","nws"),(46.65,-87.8,"Superior","nws"),
    (46.65,-87.0,"Superior","nws"),(46.65,-86.2,"Superior","nws"),
    (46.65,-85.4,"Superior","nws"),(46.65,-84.8,"Superior","nws"),
    (47.15,-91.8,"Superior","nws"),(47.15,-91.0,"Superior","nws"),
    (47.15,-90.2,"Superior","nws"),(47.15,-89.4,"Superior","nws"),
    (47.15,-88.6,"Superior","nws"),(47.15,-87.8,"Superior","nws"),
    (47.15,-87.0,"Superior","nws"),(47.15,-86.2,"Superior","nws"),
    (47.15,-85.4,"Superior","eccc"),(47.15,-84.8,"Superior","eccc"),
    (47.7,-89.5,"Superior","eccc"),(47.7,-88.5,"Superior","eccc"),
    (47.7,-87.5,"Superior","eccc"),(47.7,-86.5,"Superior","eccc"),
    (47.7,-85.5,"Superior","eccc"),(48.1,-88.0,"Superior","eccc"),
    (48.1,-87.0,"Superior","eccc"),(48.1,-86.0,"Superior","eccc"),
    # Michigan — US only (24)
    (41.8,-87.3,"Michigan","nws"),(41.8,-86.7,"Michigan","nws"),
    (41.8,-86.1,"Michigan","nws"),(42.3,-87.3,"Michigan","nws"),
    (42.3,-86.7,"Michigan","nws"),(42.3,-86.1,"Michigan","nws"),
    (42.8,-87.3,"Michigan","nws"),(42.8,-86.7,"Michigan","nws"),
    (42.8,-86.1,"Michigan","nws"),(43.3,-87.3,"Michigan","nws"),
    (43.3,-86.7,"Michigan","nws"),(43.3,-86.1,"Michigan","nws"),
    (43.8,-87.3,"Michigan","nws"),(43.8,-86.7,"Michigan","nws"),
    (43.8,-86.1,"Michigan","nws"),(44.3,-87.3,"Michigan","nws"),
    (44.3,-86.7,"Michigan","nws"),(44.3,-86.1,"Michigan","nws"),
    (44.8,-87.3,"Michigan","nws"),(44.8,-86.7,"Michigan","nws"),
    (44.8,-86.1,"Michigan","nws"),(45.3,-87.3,"Michigan","nws"),
    (45.3,-86.7,"Michigan","nws"),(45.3,-86.1,"Michigan","nws"),
    # Huron — US (18) + CA (6)
    (43.3,-83.2,"Huron","nws"),(43.3,-82.7,"Huron","nws"),
    (43.3,-82.2,"Huron","eccc"),(43.8,-83.2,"Huron","nws"),
    (43.8,-82.7,"Huron","nws"),(43.8,-82.2,"Huron","eccc"),
    (44.3,-83.2,"Huron","nws"),(44.3,-82.7,"Huron","nws"),
    (44.3,-82.2,"Huron","eccc"),(44.8,-83.2,"Huron","nws"),
    (44.8,-82.7,"Huron","nws"),(44.8,-82.2,"Huron","eccc"),
    (45.2,-83.2,"Huron","nws"),(45.2,-82.7,"Huron","nws"),
    (45.2,-82.2,"Huron","eccc"),(45.6,-83.2,"Huron","nws"),
    (45.6,-82.7,"Huron","eccc"),(45.6,-82.2,"Huron","eccc"),
    (43.5,-81.5,"Huron","eccc"),(44.0,-81.2,"Huron","eccc"),
    (44.5,-81.0,"Huron","eccc"),(45.0,-81.3,"Huron","eccc"),
    (45.5,-81.8,"Huron","eccc"),(45.8,-82.3,"Huron","eccc"),
    # Erie — US (13) + CA (5)
    (41.6,-83.0,"Erie","nws"),(41.6,-82.3,"Erie","nws"),
    (41.6,-81.6,"Erie","nws"),(41.6,-80.9,"Erie","nws"),
    (41.6,-80.2,"Erie","nws"),(41.6,-79.5,"Erie","nws"),
    (42.1,-83.0,"Erie","eccc"),(42.1,-82.3,"Erie","eccc"),
    (42.1,-81.6,"Erie","eccc"),(42.1,-80.9,"Erie","nws"),
    (42.1,-80.2,"Erie","nws"),(42.1,-79.5,"Erie","nws"),
    (42.5,-79.2,"Erie","nws"),
    (42.4,-81.8,"Erie","eccc"),(42.4,-81.0,"Erie","eccc"),
    (42.5,-80.2,"Erie","eccc"),(42.6,-79.5,"Erie","eccc"),
    (42.8,-79.0,"Erie","eccc"),
    # Ontario — US (10) + CA (6)
    (43.3,-79.2,"Ontario","eccc"),(43.3,-78.4,"Ontario","nws"),
    (43.3,-77.6,"Ontario","nws"),(43.3,-76.8,"Ontario","nws"),
    (43.3,-76.2,"Ontario","nws"),(43.6,-79.2,"Ontario","eccc"),
    (43.6,-78.4,"Ontario","nws"),(43.6,-77.6,"Ontario","nws"),
    (43.6,-76.8,"Ontario","nws"),(43.6,-76.2,"Ontario","nws"),
    (43.8,-79.0,"Ontario","eccc"),(43.9,-78.2,"Ontario","eccc"),
    (44.0,-77.3,"Ontario","eccc"),(44.1,-76.5,"Ontario","eccc"),
    (43.7,-79.3,"Ontario","eccc"),(44.0,-76.8,"Ontario","eccc"),
    # Mid-lake points (NWS grids extend over water)
    (46.9,-91.5,"Superior","nws"),(46.9,-90.5,"Superior","nws"),
    (46.9,-89.5,"Superior","nws"),(46.9,-88.5,"Superior","nws"),
    (46.9,-87.5,"Superior","nws"),(46.9,-86.5,"Superior","nws"),
    (46.9,-85.5,"Superior","nws"),(46.9,-84.8,"Superior","eccc"),
    (42.0,-86.4,"Michigan","nws"),(42.5,-86.4,"Michigan","nws"),
    (43.0,-86.4,"Michigan","nws"),(43.5,-86.4,"Michigan","nws"),
    (44.0,-86.4,"Michigan","nws"),(44.5,-86.4,"Michigan","nws"),
    (45.0,-86.4,"Michigan","nws"),
    (43.5,-82.0,"Huron","eccc"),(44.0,-82.0,"Huron","eccc"),
    (44.5,-82.0,"Huron","eccc"),(45.0,-82.0,"Huron","eccc"),
    (45.5,-82.0,"Huron","eccc"),
    (41.9,-82.5,"Erie","eccc"),(41.9,-81.8,"Erie","nws"),
    (41.9,-81.0,"Erie","nws"),(41.9,-80.3,"Erie","nws"),
    (41.9,-79.8,"Erie","nws"),(41.9,-79.2,"Erie","nws"),
    (43.5,-79.0,"Ontario","nws"),(43.5,-78.2,"Ontario","nws"),
    (43.5,-77.5,"Ontario","nws"),(43.5,-76.8,"Ontario","nws"),
    (43.5,-76.3,"Ontario","nws"),
]

NWS_HEADERS = {"User-Agent": "LakeEffectFetchMap/2.0 (research.alexcooke.co)"}
GEOMET = "https://geo.weather.gc.ca/geomet"
OUTPUT_PATH = Path("wind_data.json")  # relative to CWD (repo root when run by Actions)


# ===== NWS =====

def parse_nws_ts(prop):
    if not prop or "values" not in prop:
        return []
    out = []
    for entry in prop["values"]:
        iso, dur = entry["validTime"].split("/")
        start = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        match = re.match(r"PT(\d+)H", dur)
        hrs = int(match.group(1)) if match else 1
        for h in range(hrs):
            t = start + timedelta(hours=h)
            out.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "v": entry["value"]})
    return out


async def fetch_nws_point(session, idx, lat, lon, sem):
    async with sem:
        try:
            url1 = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
            async with session.get(url1, headers=NWS_HEADERS) as r1:
                if r1.status != 200:
                    return idx, None
                pd = await r1.json()
            grid_url = pd["properties"]["forecastGridData"]
            async with session.get(grid_url, headers=NWS_HEADERS) as r2:
                if r2.status != 200:
                    return idx, None
                gd = await r2.json()
            props = gd["properties"]
            return idx, {
                "windDir": parse_nws_ts(props.get("windDirection")),
                "windSpd": parse_nws_ts(props.get("windSpeed")),
                "temp": parse_nws_ts(props.get("temperature")),
            }
        except Exception as e:
            print(f"  NWS pt {idx} ({lat:.2f},{lon:.2f}): {e}")
            return idx, None


# ===== ECCC =====

def latest_hrdps_run():
    """Most recent HRDPS run likely available (~3h processing delay)."""
    now = datetime.now(timezone.utc) - timedelta(hours=3)
    run_hour = (now.hour // 6) * 6
    return now.replace(hour=run_hour, minute=0, second=0, microsecond=0)


async def eccc_wms_value(session, layer, lat, lon, time_str, sem):
    """WMS GetFeatureInfo point query → single numeric value."""
    async with sem:
        e = 0.25
        url = (
            f"{GEOMET}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetFeatureInfo"
            f"&BBOX={lat-e},{lon-e},{lat+e},{lon+e}"
            f"&CRS=EPSG:4326&WIDTH=10&HEIGHT=10"
            f"&LAYERS={layer}&QUERY_LAYERS={layer}"
            f"&INFO_FORMAT=application/json&I=5&J=5"
            f"&TIME={time_str}"
        )
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                features = data.get("features", [])
                if features:
                    for v in features[0].get("properties", {}).values():
                        if isinstance(v, (int, float)):
                            return v
                return None
        except:
            return None


async def fetch_eccc_point(session, idx, lat, lon, sem):
    """Fetch HRDPS wind speed, direction, and temperature for a Canadian point."""
    run_time = latest_hrdps_run()

    wind_dir_series = []
    wind_spd_series = []
    temp_series = []

    for fh in range(1, 49, 3):
        valid_time = run_time + timedelta(hours=fh)
        time_str = valid_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        wspd_task = eccc_wms_value(session, "HRDPS.CONTINENTAL_WSPD", lat, lon, time_str, sem)
        wdir_task = eccc_wms_value(session, "HRDPS.CONTINENTAL_WD", lat, lon, time_str, sem)
        tt_task = eccc_wms_value(session, "HRDPS.CONTINENTAL_TT", lat, lon, time_str, sem)

        wspd, wdir, tt = await asyncio.gather(wspd_task, wdir_task, tt_task)

        if wspd is not None and wdir is not None:
            speed_kmh = wspd * 3.6
            wind_dir_series.append({"t": time_str, "v": round(wdir, 1)})
            wind_spd_series.append({"t": time_str, "v": round(speed_kmh, 1)})

        if tt is not None:
            temp_series.append({"t": time_str, "v": round(tt, 1)})

    if wind_dir_series:
        return idx, {
            "windDir": wind_dir_series,
            "windSpd": wind_spd_series,
            "temp": temp_series,
        }
    else:
        print(f"  ECCC pt {idx} ({lat:.2f},{lon:.2f}): no wind data retrieved")
        return idx, None


# ===== 850MB TEMPERATURE (via ECCC HRDPS for ALL points — covers US + Canada) =====

async def fetch_850mb_for_point(session, idx, lat, lon, run_time, sem):
    """Fetch HRDPS 850mb temp for any point (US or Canadian) via ECCC WMS."""
    series = []
    for fh in range(1, 49, 3):
        valid_time = run_time + timedelta(hours=fh)
        time_str = valid_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        val = await eccc_wms_value(session, "HRDPS.CONTINENTAL.PRES_TT.850", lat, lon, time_str, sem)
        if val is not None:
            series.append({"t": time_str, "v": round(val, 1)})
    return idx, series if series else None


# ===== SST (via NOAA ERDDAP — JPL MUR SST, daily 1km satellite analysis) =====

MUR_SST_URL = "https://coastwatch.pfeg.noaa.gov/erddap/griddap/jplMURSST41.json"

# Lake centers for nudging failed shore points toward open water
LAKE_CENTERS = {
    "Superior": (47.5, -87.5),
    "Michigan": (43.5, -86.5),
    "Huron": (44.8, -82.0),
    "Erie": (42.0, -81.2),
    "Ontario": (43.6, -77.8),
}

async def _query_sst(session, lat, lon, sem):
    """Single SST query. Returns value in °C or None."""
    async with sem:
        url = f"{MUR_SST_URL}?analysed_sst%5Blast%5D%5B({lat})%5D%5B({lon})%5D"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                text = await resp.text()
                data = json.loads(text)
                rows = data.get("table", {}).get("rows", [])
                if rows:
                    sst_c = rows[0][-1]
                    if sst_c is not None and isinstance(sst_c, (int, float)):
                        return round(sst_c, 2)
                return None
        except:
            return None


async def fetch_sst_for_point(session, idx, lat, lon, lake, sem):
    """Fetch SST, retrying with nudges toward lake center if shore pixel is masked."""
    val = await _query_sst(session, lat, lon, sem)
    if val is not None:
        return idx, val

    # Nudge 20%, 40%, 60% toward lake center and retry
    clat, clon = LAKE_CENTERS.get(lake, (lat, lon))
    for frac in [0.2, 0.4, 0.6]:
        nlat = lat + (clat - lat) * frac
        nlon = lon + (clon - lon) * frac
        val = await _query_sst(session, nlat, nlon, sem)
        if val is not None:
            return idx, val

    return idx, None


# ===== MAIN =====

async def fetch_all():
    nws_sem = asyncio.Semaphore(8)
    eccc_sem = asyncio.Semaphore(10)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Phase 1: Wind + surface temp (NWS for US, ECCC for CA)
        tasks = []
        for i, (lat, lon, lake, source) in enumerate(WIND_POINTS):
            if source == "nws":
                tasks.append(fetch_nws_point(session, i, lat, lon, nws_sem))
            else:
                tasks.append(fetch_eccc_point(session, i, lat, lon, eccc_sem))
        results = await asyncio.gather(*tasks)

    points = {}
    nws_ok = eccc_ok = nws_fail = eccc_fail = 0
    for idx, data in results:
        source = WIND_POINTS[idx][3]
        if data is not None:
            points[str(idx)] = data
            if source == "nws": nws_ok += 1
            else: eccc_ok += 1
        else:
            if source == "nws": nws_fail += 1
            else: eccc_fail += 1

    print(f"  NWS:  {nws_ok} ok, {nws_fail} failed")
    print(f"  ECCC: {eccc_ok} ok, {eccc_fail} failed")
    wind_by_lake = {}
    for i, (lat, lon, lake, source) in enumerate(WIND_POINTS):
        if lake not in wind_by_lake:
            wind_by_lake[lake] = [0, 0]
        wind_by_lake[lake][1] += 1
        if str(i) in points:
            wind_by_lake[lake][0] += 1
    for lake in ["Superior", "Michigan", "Huron", "Erie", "Ontario"]:
        if lake in wind_by_lake:
            ok, total = wind_by_lake[lake]
            print(f"    {lake}: {ok}/{total}")

    # Phase 2: 850mb temperature for ALL successful points via ECCC HRDPS
    run_time = latest_hrdps_run()
    t850_sem = asyncio.Semaphore(12)
    print(f"  Fetching 850mb temps via HRDPS.CONTINENTAL.PRES_TT.850...")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        t850_tasks = []
        for idx_str in points:
            idx = int(idx_str)
            lat, lon = WIND_POINTS[idx][0], WIND_POINTS[idx][1]
            t850_tasks.append(fetch_850mb_for_point(session, idx, lat, lon, run_time, t850_sem))
        t850_results = await asyncio.gather(*t850_tasks)

    t850_ok = 0
    for idx, series in t850_results:
        if series:
            points[str(idx)]["temp850"] = series
            t850_ok += 1

    print(f"  850mb: {t850_ok}/{len(points)} points")

    # Phase 3: Real-time SST for ALL successful points via JPL MUR SST
    sst_sem = asyncio.Semaphore(8)
    print(f"  Fetching SST via JPL MUR SST (NOAA ERDDAP)...")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        sst_tasks = []
        for idx_str in points:
            idx = int(idx_str)
            lat, lon, lake = WIND_POINTS[idx][0], WIND_POINTS[idx][1], WIND_POINTS[idx][2]
            sst_tasks.append(fetch_sst_for_point(session, idx, lat, lon, lake, sst_sem))
        sst_results = await asyncio.gather(*sst_tasks)

    sst_ok = 0
    sst_by_lake = {}
    for idx, sst_val in sst_results:
        lake = WIND_POINTS[idx][2]
        if lake not in sst_by_lake:
            sst_by_lake[lake] = [0, 0]  # [ok, total]
        sst_by_lake[lake][1] += 1
        if sst_val is not None:
            points[str(idx)]["sst"] = sst_val
            sst_ok += 1
            sst_by_lake[lake][0] += 1

    print(f"  SST:  {sst_ok}/{len(points)} points")
    for lake in ["Superior", "Michigan", "Huron", "Erie", "Ontario"]:
        if lake in sst_by_lake:
            ok, total = sst_by_lake[lake]
            print(f"    {lake}: {ok}/{total}")
    return points, nws_ok + eccc_ok


def main():
    t0 = time.time()
    nws_count = sum(1 for p in WIND_POINTS if p[3] == "nws")
    eccc_count = sum(1 for p in WIND_POINTS if p[3] == "eccc")
    print(f"Fetching {len(WIND_POINTS)} wind points ({nws_count} NWS, {eccc_count} ECCC)...")

    points, success = asyncio.run(fetch_all())

    output = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totalPoints": len(WIND_POINTS),
        "successPoints": success,
        "points": points,
    }

    OUTPUT_PATH.write_text(json.dumps(output))
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    elapsed = time.time() - t0
    print(f"Done: {success}/{len(WIND_POINTS)} points in {elapsed:.1f}s ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
