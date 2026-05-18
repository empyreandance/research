"""
Operational Dewpoint EFI/SOT Pipeline

Downloads the latest GEFS ensemble cycle from NOMADS, computes daily
max dewpoint (as q) for each member, and generates EFI/SOT maps against
the M-climate.

Usage:
    python run_efi.py                     # latest cycle
    python run_efi.py --date 20260516     # specific date
    python run_efi.py --date 20260516 --days 1 2 3   # specific lead days
"""

import argparse, sys, time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import requests
import xarray as xr

sys.path.insert(0, str(Path(__file__).parent))
from hdw.efi import compute_efi, compute_sot

# ── Config ───────────────────────────────────────────────────────────

MCLIMATE_DIR = Path("data/mclimate")
OUTPUT_DIR = Path("output")
NOMADS_BASE = ("https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod")

N_MEMBERS = 31  # control + 30 perturbations
EPSILON = 0.622
LAT_BOUNDS = (24.0, 50.0)
LON_BOUNDS = (234.0, 294.0)  # 0-360

FHOURS_PER_DAY = {}
for _d in range(1, 8):
    FHOURS_PER_DAY[_d] = list(range((_d - 1) * 24 + 6, _d * 24 + 1, 6))


def sat_vp(T_c):
    return 6.112 * np.exp(17.67 * T_c / (T_c + 243.5))


def d2m_to_q(d2m_K, P_sfc_hPa):
    """Convert 2m dewpoint (K) to specific humidity (kg/kg)."""
    Td_C = d2m_K - 273.15
    e = sat_vp(Td_C)
    q = EPSILON * e / (P_sfc_hPa - e * (1 - EPSILON))
    return np.maximum(q, 0.0)


def q_to_td(q, P=1013.25):
    """Convert specific humidity to dewpoint (°C) for display."""
    q = np.asarray(q, dtype=np.float64)
    e = q * P / (0.622 + q * 0.378)
    e = np.maximum(e, 1e-6)
    return 243.5 * np.log(e / 6.112) / (17.67 - np.log(e / 6.112))


# ── Download ─────────────────────────────────────────────────────────

def member_filename(member_num, cycle_hour, fhour):
    if member_num == 0:
        prefix = "gec00"
    else:
        prefix = f"gep{member_num:02d}"
    return f"{prefix}.t{cycle_hour:02d}z.pgrb2s.0p25.f{fhour:03d}"


def download_member_hour(date_str, cycle_hour, member_num, fhour, tmp_dir):
    """Download one GEFS file and extract d2m, sp, subset to CONUS."""
    fname = member_filename(member_num, cycle_hour, fhour)
    url = (f"{NOMADS_BASE}/gefs.{date_str}/{cycle_hour:02d}/atmos/"
           f"pgrb2sp25/{fname}")

    cache = tmp_dir / f"m{member_num:02d}_f{fhour:03d}.npz"
    if cache.exists():
        data = np.load(cache)
        return data["q"]

    try:
        r = requests.get(url, timeout=120)
        if r.status_code != 200:
            return None
    except Exception:
        return None

    grib_path = tmp_dir / f"{fname}.grib2"
    grib_path.write_bytes(r.content)

    try:
        # 2m dewpoint
        ds_2m = xr.open_dataset(
            str(grib_path), engine="cfgrib",
            backend_kwargs={"filter_by_keys": {
                "typeOfLevel": "heightAboveGround", "level": 2,
            }},
        )
        # Surface pressure
        ds_sfc = xr.open_dataset(
            str(grib_path), engine="cfgrib",
            backend_kwargs={"filter_by_keys": {"typeOfLevel": "surface"}},
        )

        d2m = ds_2m["d2m"].values
        lat = ds_2m.latitude.values
        lon = ds_2m.longitude.values

        sp = ds_sfc["sp"].values
        if np.nanmean(sp) > 2000:
            sp = sp / 100.0  # Pa to hPa

        # Subset to CONUS
        lat_mask = (lat >= LAT_BOUNDS[0]) & (lat <= LAT_BOUNDS[1])
        lon_mask = (lon >= LON_BOUNDS[0]) & (lon <= LON_BOUNDS[1])
        d2m_conus = d2m[np.ix_(lat_mask, lon_mask)]
        sp_conus = sp[np.ix_(lat_mask, lon_mask)]

        q = d2m_to_q(d2m_conus, sp_conus)

        ds_2m.close()
        ds_sfc.close()

        np.savez_compressed(cache, q=q.astype(np.float32))
        return q

    except Exception as e:
        print(f"    Error reading m{member_num:02d} f{fhour:03d}: {e}")
        return None
    finally:
        grib_path.unlink(missing_ok=True)


def download_ensemble_day(date_str, cycle_hour, lead_day, tmp_dir):
    """
    Download all members for one lead day, compute daily max q.
    Returns array shape (n_members, lat, lon).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    fhours = FHOURS_PER_DAY[lead_day]
    total_downloads = N_MEMBERS * len(fhours)
    completed = [0]

    # Download all member/hour combos in parallel
    def dl_one(args):
        member, fh = args
        q = download_member_hour(date_str, cycle_hour, member, fh, tmp_dir)
        completed[0] += 1
        print(f"\r    Downloaded {completed[0]}/{total_downloads} "
              f"({100*completed[0]//total_downloads}%)", end="", flush=True)
        return member, fh, q

    all_results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks = [(m, fh) for m in range(N_MEMBERS) for fh in fhours]
        futures = {pool.submit(dl_one, t): t for t in tasks}
        for fut in as_completed(futures):
            member, fh, q = fut.result()
            if q is not None:
                all_results.setdefault(member, []).append(q)

    print()  # newline after progress

    member_maxq = []
    for member in range(N_MEMBERS):
        hour_values = all_results.get(member, [])
        if hour_values:
            stacked = np.stack(hour_values, axis=0)
            member_maxq.append(np.nanmax(stacked, axis=0))

    if not member_maxq:
        return None

    return np.stack(member_maxq, axis=0)  # (n_members, lat, lon)


# ── EFI/SOT Computation ─────────────────────────────────────────────

def compute_dewpoint_efi(ensemble_q, mclimate_file, doy):
    """
    Compute EFI and SOT for max dewpoint.

    ensemble_q: (n_members, lat, lon) daily max q from GEFS
    mclimate_file: path to mclimate_q2m_d{ld}.npz
    doy: day of year for the valid date
    """
    mc = np.load(mclimate_file)
    mc_pctls = mc["q_max_pctls"]  # (366, N_QUANTILES, lat, lon)
    probs = mc["probs"]           # (N_QUANTILES,)

    mc_at_doy = mc_pctls[doy - 1]  # (N_QUANTILES, lat, lon)

    # EFI
    efi = compute_efi(ensemble_q, mc_at_doy, probs)

    # SOT (upper tail — anomalously moist)
    p90_idx = np.argmin(np.abs(probs - 0.90))
    p99_idx = np.argmin(np.abs(probs - 0.99))
    sot_upper = compute_sot(ensemble_q, mc_at_doy[p90_idx], mc_at_doy[p99_idx])

    # SOT (lower tail — anomalously dry)
    p10_idx = np.argmin(np.abs(probs - 0.10))
    p01_idx = np.argmin(np.abs(probs - 0.01))
    Q_f_10 = np.percentile(ensemble_q, 10, axis=0)
    Qc_10 = mc_at_doy[p10_idx]
    Qc_01 = mc_at_doy[p01_idx]
    denom = Qc_10 - Qc_01
    with np.errstate(divide="ignore", invalid="ignore"):
        sot_lower = (Qc_10 - Q_f_10) / denom
    sot_lower = np.where(denom == 0, np.nan, sot_lower)

    return efi, sot_upper, sot_lower


# ── Rendering ────────────────────────────────────────────────────────

def render_combined_map(efi, sot_upper, sot_lower, lat, lon, title, outpath):
    """Render EFI as shading with SOT as black contours, ECMWF style."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    lon_plot = np.where(lon > 180, lon - 360, lon)
    lon_2d, lat_2d = np.meshgrid(lon_plot, lat)

    # ECMWF-style levels: no shading between -0.5 and +0.5
    levels = [-1.0, -0.99, -0.95, -0.9, -0.8, -0.7, -0.6, -0.5,
              0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0]

    # Colors matching ECMWF: negative = cool (pink/blue), positive = warm (yellow/brown/red)
    colors_list = [
        "#d4b9da",  # -1.0 to -0.99  pink
        "#c994c7",  # -0.99 to -0.95
        "#4292c6",  # -0.95 to -0.9  blue
        "#6baed6",  # -0.9 to -0.8
        "#9ecae1",  # -0.8 to -0.7
        "#c6dbef",  # -0.7 to -0.6
        "#deebf7",  # -0.6 to -0.5
        # gap: -0.5 to +0.5 is white (handled by BoundaryNorm)
        "#ffffb2",  # +0.5 to +0.6  yellow
        "#fecc5c",  # +0.6 to +0.7
        "#fd8d3c",  # +0.7 to +0.8
        "#f03b20",  # +0.8 to +0.9  orange-red
        "#bd7826",  # +0.9 to +0.95 brown
        "#8c510a",  # +0.95 to +0.99 dark brown
        "#67000d",  # +0.99 to +1.0 deep red
    ]

    # Insert white for the -0.5 to +0.5 gap
    all_colors = colors_list[:7] + ["#ffffff"] + colors_list[7:]
    all_levels = levels[:8] + [0.5] + levels[8:]
    # Actually, BoundaryNorm needs n_colors = len(levels) - 1
    # levels has 16 entries -> 15 color bins, but we want white in the gap
    # Rebuild properly:
    full_levels = [-1.0, -0.99, -0.95, -0.9, -0.8, -0.7, -0.6, -0.5,
                   0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0]
    full_colors = [
        "#d4b9da",  # -1.0 to -0.99
        "#c994c7",  # -0.99 to -0.95
        "#4292c6",  # -0.95 to -0.9
        "#6baed6",  # -0.9 to -0.8
        "#9ecae1",  # -0.8 to -0.7
        "#c6dbef",  # -0.7 to -0.6
        "#deebf7",  # -0.6 to -0.5
        "#ffffff",  # -0.5 to +0.5 (white gap)
        "#ffffb2",  # +0.5 to +0.6
        "#fecc5c",  # +0.6 to +0.7
        "#fd8d3c",  # +0.7 to +0.8
        "#f03b20",  # +0.8 to +0.9
        "#bd7826",  # +0.9 to +0.95
        "#8c510a",  # +0.95 to +0.99
        "#67000d",  # +0.99 to +1.0
    ]

    cmap = mcolors.ListedColormap(full_colors)
    norm = mcolors.BoundaryNorm(full_levels, cmap.N)

    proj = ccrs.LambertConformal(central_longitude=-96)
    fig, ax = plt.subplots(figsize=(14, 8), subplot_kw={"projection": proj})
    ax.set_extent([-125, -67, 24, 50], crs=ccrs.PlateCarree())
    ax.add_feature(cfeature.STATES, linewidth=0.5, edgecolor="gray")
    ax.add_feature(cfeature.COASTLINE, linewidth=0.8)
    ax.add_feature(cfeature.BORDERS, linewidth=0.5)

    # EFI shading
    mesh = ax.pcolormesh(
        lon_plot, lat, efi,
        transform=ccrs.PlateCarree(),
        cmap=cmap, norm=norm,
    )

    # ── SOT contours with manual gap-based labeling ──
    from scipy.ndimage import label as ndlabel
    import math

    sot_levels = [1.0, 2.0, 5.0, 8.0]
    fmt_upper = {1.0: "1", 2.0: "2", 5.0: "5", 8.0: "8"}
    fmt_lower = {1.0: "-1", 2.0: "-2", 5.0: "-5", 8.0: "-8"}

    sot_upper_clean = np.where(np.isnan(sot_upper), 0, sot_upper)
    sot_lower_clean = np.where(np.isnan(sot_lower), 0, sot_lower)

    def draw_sot_with_gaps(ax, field, lat, lon_plot, levels, fmt,
                           min_spacing_deg=6, min_region_pixels=40,
                           gap_deg=1.5):
        """Draw contour lines with gaps for labels. No masking."""
        import matplotlib.pyplot as _plt

        # Extract contour paths using a plain axes (avoids cartopy fragmentation)
        fig_tmp, ax_tmp = _plt.subplots()
        cs_tmp = ax_tmp.contour(lon_plot, lat, field, levels=levels)
        all_paths = {}
        for i, lev in enumerate(cs_tmp.levels):
            all_paths[lev] = [seg.copy() for seg in cs_tmp.allsegs[i]]
        _plt.close(fig_tmp)

        # Compute label positions using grid sampling
        label_positions = []
        for lev in levels:
            region_mask = field >= lev
            labeled, n_regions = ndlabel(region_mask)
            big_regions = set()
            for r in range(1, n_regions + 1):
                if np.sum(labeled == r) >= min_region_pixels:
                    big_regions.add(r)

            for yi in range(2, len(lat) - 2, 3):
                for xi in range(2, len(lon_plot) - 2, 3):
                    neighbors = field[max(0, yi-1):yi+2, max(0, xi-1):xi+2]
                    if not (neighbors.min() <= lev <= neighbors.max()):
                        continue
                    rid = labeled[yi, xi]
                    if rid == 0:
                        rid = labeled[max(0, yi-1):yi+2, max(0, xi-1):xi+2].max()
                    if rid not in big_regions:
                        continue
                    y, x = lat[yi], lon_plot[xi]
                    too_close = any(
                        abs(py - y) + abs(px - x) < min_spacing_deg
                        for py, px, *_ in label_positions
                    )
                    if too_close:
                        continue
                    # Contour angle from gradient
                    dy = float(field[min(yi+1, len(lat)-1), xi] - field[max(yi-1, 0), xi])
                    dx = float(field[yi, min(xi+1, len(lon_plot)-1)] - field[yi, max(xi-1, 0)])
                    angle = math.degrees(math.atan2(-dy, dx)) + 90
                    label_positions.append((y, x, lev, angle))

        # Draw contour paths with gaps at label positions
        for lev, paths in all_paths.items():
            for path in paths:
                # path is (N, 2) with columns (lon, lat)
                # Split path wherever it passes near a label
                segments = []
                current = []
                for pt in path:
                    px, py = pt[0], pt[1]
                    near_label = any(
                        abs(ly - py) + abs(lx - px) < gap_deg
                        for ly, lx, ll, _ in label_positions if ll == lev
                    )
                    if near_label:
                        if len(current) > 1:
                            segments.append(np.array(current))
                        current = []
                    else:
                        current.append(pt)
                if len(current) > 1:
                    segments.append(np.array(current))

                for seg in segments:
                    ax.plot(seg[:, 0], seg[:, 1], "k-", linewidth=1.5,
                            transform=ccrs.PlateCarree())

        # Place text labels
        for y, x, lev, angle in label_positions:
            ax.text(x, y, fmt[lev], fontsize=9, fontweight="bold",
                    ha="center", va="center", rotation=angle,
                    rotation_mode="anchor", transform=ccrs.PlateCarree(),
                    zorder=5)

    draw_sot_with_gaps(ax, sot_upper_clean, lat, lon_plot, sot_levels, fmt_upper)
    draw_sot_with_gaps(ax, sot_lower_clean, lat, lon_plot, sot_levels, fmt_lower)

    # Colorbar
    cb = plt.colorbar(mesh, ax=ax, shrink=0.7, extend="both",
                       spacing="uniform")
    cb.set_label("Dewpoint EFI", fontsize=11)
    cb.set_ticks([-1, -0.9, -0.8, -0.7, -0.6, -0.5,
                  0.5, 0.6, 0.7, 0.8, 0.9, 1])
    cb.ax.set_title("SOT contours at\n±1, ±2, ±5, ±8", fontsize=8, pad=8)

    ax.set_title(title, fontsize=12)

    fig.savefig(outpath, dpi=150, bbox_inches="tight")
    plt.close()


# ── Grid ─────────────────────────────────────────────────────────────

def get_conus_grid():
    """Return CONUS lat/lon arrays matching the M-climate grid."""
    lats = np.arange(50.0, 24.0 - 0.125, -0.25)
    lons = np.arange(234.0, 294.0 + 0.125, 0.25)
    return lats, lons


# ── Main ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Dewpoint EFI operational run")
    ap.add_argument("--date", type=str, default=None, help="YYYYMMDD")
    ap.add_argument("--cycle", type=int, default=0, help="Cycle hour (0, 6, 12, 18)")
    ap.add_argument("--days", type=int, nargs="+", default=[1, 2, 3, 4, 5, 6, 7])
    a = ap.parse_args()

    if a.date:
        date_str = a.date
    else:
        date_str = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y%m%d")

    cycle_hour = a.cycle
    init_dt = datetime.strptime(date_str, "%Y%m%d")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(f"_tmp_efi_{date_str}")
    tmp_dir.mkdir(exist_ok=True)

    lats, lons = get_conus_grid()
    all_days = {}  # accumulate data for JSON output

    print(f"Dewpoint EFI — GEFS {date_str} {cycle_hour:02d}Z")
    print(f"  Lead days: {a.days}")
    print(f"  Members: {N_MEMBERS}")
    print()

    for lead_day in a.days:
        valid_dt = init_dt + timedelta(days=lead_day)
        doy = valid_dt.timetuple().tm_yday
        valid_str = valid_dt.strftime("%Y-%m-%d")

        mc_file = MCLIMATE_DIR / f"mclimate_q2m_d{lead_day}.npz"
        if not mc_file.exists():
            print(f"  Day {lead_day}: M-climate not found ({mc_file}), skipping")
            continue

        print(f"  Day {lead_day} (valid {valid_str}, DOY {doy}):")
        print(f"    Downloading ensemble...", end=" ", flush=True)

        t0 = time.time()
        ensemble_q = download_ensemble_day(date_str, cycle_hour, lead_day, tmp_dir)

        if ensemble_q is None:
            print("FAILED — no data")
            continue

        n_mem = ensemble_q.shape[0]
        elapsed = time.time() - t0
        print(f"{n_mem} members in {elapsed:.0f}s")

        print(f"    Computing EFI/SOT...", end=" ", flush=True)
        efi, sot_upper, sot_lower = compute_dewpoint_efi(
            ensemble_q, mc_file, doy
        )
        print("done")

        # Stats
        print(f"    EFI range: {np.nanmin(efi):.3f} to {np.nanmax(efi):.3f}")
        print(f"    SOT upper range: {np.nanmin(sot_upper):.3f} to {np.nanmax(sot_upper):.3f}")
        print(f"    SOT lower range: {np.nanmin(sot_lower):.3f} to {np.nanmax(sot_lower):.3f}")

        # Render combined map
        tag = f"{date_str}_{cycle_hour:02d}Z_d{lead_day}"

        combined_path = OUTPUT_DIR / f"efi_td_{tag}.png"

        # Compute valid window for title
        fhours = FHOURS_PER_DAY[lead_day]
        valid_start = init_dt + timedelta(hours=fhours[0])
        valid_end = init_dt + timedelta(hours=fhours[-1])
        vs = valid_start.strftime("%HZ %a %b %d")
        ve = valid_end.strftime("%HZ %a %b %d %Y")
        fh_range = f"{fhours[0]}-{fhours[-1]}h forecast"

        render_combined_map(
            efi, sot_upper, sot_lower, lats, lons,
            f"Dewpoint EFI (shaded) and SOT (contours)\n"
            f"{fh_range} valid {vs} to {ve}\n"
            f"GEFS {date_str} {cycle_hour:02d}Z ({n_mem} members)",
            combined_path,
        )
        print(f"    → {combined_path}")

        # Save raw grids for web viewer
        grid_path = OUTPUT_DIR / f"grid_{tag}.npz"
        np.savez_compressed(grid_path, efi=efi.astype(np.float32),
                           sot_upper=sot_upper.astype(np.float32),
                           sot_lower=sot_lower.astype(np.float32))

        # Accumulate for JSON
        all_days[lead_day] = {
            "efi": efi, "sot_upper": sot_upper, "sot_lower": sot_lower,
            "valid_str": valid_str, "fh_range": fh_range,
            "valid_start": valid_start.isoformat(),
            "valid_end": valid_end.isoformat(),
        }

    # ── Write JSON summary ──
    summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "init_date": date_str,
        "cycle": f"{cycle_hour:02d}Z",
        "days": {},
    }
    for ld, data in all_days.items():
        summary["days"][str(ld)] = {
            "valid": data["valid_str"],
            "fh_range": data["fh_range"],
            "valid_start": data["valid_start"],
            "valid_end": data["valid_end"],
            "efi_min": round(float(np.nanmin(data["efi"])), 3),
            "efi_max": round(float(np.nanmax(data["efi"])), 3),
            "sot_upper_max": round(float(np.nanmax(data["sot_upper"])), 3),
            "sot_lower_max": round(float(np.nanmax(data["sot_lower"])), 3),
            "map": f"efi_td_{date_str}_{cycle_hour:02d}Z_d{ld}.png",
        }

    json_path = OUTPUT_DIR / "dewpoint_efi_latest.json"
    import json
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Summary → {json_path}")

    # Cleanup temp files (keep cache for reruns)
    print(f"\nDone. Maps in {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
