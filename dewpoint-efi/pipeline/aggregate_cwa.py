"""
CWA aggregation for Dewpoint EFI.

Builds a CWA mask grid (once), then uses it to aggregate gridded
EFI/SOT to per-CWA max values. Updates the JSON output.

Usage:
    python aggregate_cwa.py              # aggregate latest run
    python aggregate_cwa.py --build-mask # rebuild CWA mask from shapefile
"""

import argparse, json, sys
from pathlib import Path
import numpy as np

OUTPUT_DIR = Path("output")
DATA_DIR = Path("data")
CWA_MASK_FILE = DATA_DIR / "cwa_mask.npz"
CWA_SHAPEFILE_DIR = DATA_DIR / "cwa_shapefile"
CWA_SHAPEFILE_URL = "https://www.weather.gov/source/gis/Shapefiles/WSOM/w_18mr25.zip"

# CONUS grid (must match run_efi.py)
LATS = np.arange(50.0, 24.0 - 0.125, -0.25)
LONS_360 = np.arange(234.0, 294.0 + 0.125, 0.25)
LONS = np.where(LONS_360 > 180, LONS_360 - 360, LONS_360)


def build_cwa_mask():
    """
    Download CWA shapefile and build a grid mask.
    Each grid cell gets an integer CWA index.
    Saves mask + CWA ID list to cwa_mask.npz.
    """
    import geopandas as gpd
    from shapely.geometry import Point
    import zipfile, io, requests

    CWA_SHAPEFILE_DIR.mkdir(parents=True, exist_ok=True)

    shp_files = list(CWA_SHAPEFILE_DIR.glob("*.shp"))
    if not shp_files:
        print("Downloading CWA shapefile...")
        resp = requests.get(CWA_SHAPEFILE_URL, timeout=60)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            zf.extractall(CWA_SHAPEFILE_DIR)
        shp_files = list(CWA_SHAPEFILE_DIR.glob("*.shp"))

    gdf = gpd.read_file(shp_files[0])

    # Find CWA ID column
    id_col = None
    for candidate in ["CWA", "WFO", "SITE_ID", "ID"]:
        if candidate in gdf.columns:
            id_col = candidate
            break
    if id_col is None:
        for col in gdf.columns:
            if gdf[col].dtype == object and col != "geometry":
                id_col = col
                break

    gdf = gdf.rename(columns={id_col: "cwa"})
    gdf["cwa"] = gdf["cwa"].str.upper().str.strip()
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    print(f"Loaded {len(gdf)} CWA polygons")

    # Build spatial index
    sindex = gdf.sindex

    # For each grid point, find which CWA it belongs to
    cwa_list = sorted(gdf["cwa"].unique())
    cwa_to_idx = {c: i + 1 for i, c in enumerate(cwa_list)}  # 0 = no CWA

    mask = np.zeros((len(LATS), len(LONS)), dtype=np.int16)
    total = len(LATS) * len(LONS)
    done = 0

    print(f"Building mask for {len(LATS)}x{len(LONS)} = {total:,} grid points...")

    for yi, lat in enumerate(LATS):
        for xi, lon in enumerate(LONS):
            pt = Point(lon, lat)
            candidates = list(sindex.intersection(pt.bounds))
            for idx in candidates:
                if gdf.geometry.iloc[idx].contains(pt):
                    cwa_id = gdf["cwa"].iloc[idx]
                    mask[yi, xi] = cwa_to_idx.get(cwa_id, 0)
                    break
        done += len(LONS)
        if (yi + 1) % 10 == 0:
            print(f"  row {yi+1}/{len(LATS)} ({100*done//total}%)")

    # Also store CWA polygons as simplified GeoJSON for the viewer
    polygons = {}
    for _, row in gdf.iterrows():
        cwa_id = row["cwa"]
        if cwa_id in cwa_to_idx:
            geom = row.geometry.simplify(0.05)
            if geom.geom_type == "MultiPolygon":
                coords = [list(p.exterior.coords) for p in geom.geoms]
            elif geom.geom_type == "Polygon":
                coords = [list(geom.exterior.coords)]
            else:
                coords = []
            polygons[cwa_id] = coords

    CWA_MASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        CWA_MASK_FILE,
        mask=mask,
        cwa_list=np.array(cwa_list),
    )
    # Save polygons separately as JSON
    with open(DATA_DIR / "cwa_polygons.json", "w") as f:
        json.dump(polygons, f)

    assigned = np.sum(mask > 0)
    print(f"Mask saved → {CWA_MASK_FILE}")
    print(f"  {assigned:,} of {total:,} cells assigned to a CWA ({100*assigned//total}%)")
    print(f"  {len(cwa_list)} CWAs")
    return mask, cwa_list


def load_cwa_mask():
    if not CWA_MASK_FILE.exists():
        print("CWA mask not found. Building...")
        return build_cwa_mask()
    data = np.load(CWA_MASK_FILE, allow_pickle=True)
    return data["mask"], list(data["cwa_list"])


def aggregate():
    """Aggregate gridded EFI/SOT to per-CWA max values."""
    mask, cwa_list = load_cwa_mask()
    cwa_to_idx = {c: i + 1 for i, c in enumerate(cwa_list)}

    # Load the existing JSON
    json_path = OUTPUT_DIR / "dewpoint_efi_latest.json"
    if not json_path.exists():
        print(f"No JSON found at {json_path}. Run run_efi.py first.")
        return

    with open(json_path) as f:
        summary = json.load(f)

    date_str = summary["init_date"]
    cycle = summary["cycle"].replace("Z", "")

    # Per-CWA aggregation
    by_cwa = {}
    for cwa_id in cwa_list:
        cwa_idx = cwa_to_idx[cwa_id]
        cwa_mask = mask == cwa_idx
        n_cells = int(np.sum(cwa_mask))
        if n_cells == 0:
            continue

        cwa_days = {}
        for day_str, day_info in summary["days"].items():
            day = int(day_str)
            tag = f"{date_str}_{cycle}Z_d{day}"
            grid_file = OUTPUT_DIR / f"grid_{tag}.npz"
            if not grid_file.exists():
                continue

            grids = np.load(grid_file)
            efi = grids["efi"]
            sot_upper = grids["sot_upper"]
            sot_lower = grids["sot_lower"]

            efi_masked = efi[cwa_mask]
            sot_u_masked = sot_upper[cwa_mask]
            sot_l_masked = sot_lower[cwa_mask]

            cwa_days[day_str] = {
                "efi_max": round(float(np.nanmax(efi_masked)), 3),
                "efi_min": round(float(np.nanmin(efi_masked)), 3),
                "sot_upper_max": round(float(np.nanmax(sot_u_masked)), 3),
                "sot_lower_max": round(float(np.nanmax(sot_l_masked)), 3),
            }

        if cwa_days:
            by_cwa[cwa_id] = {"days": cwa_days, "n_cells": n_cells}

    summary["by_cwa"] = by_cwa
    summary["cwa_list"] = sorted(by_cwa.keys())

    # Output per-CWA grid files for D3 map viewer
    grids_dir = OUTPUT_DIR / "grids"
    grids_dir.mkdir(parents=True, exist_ok=True)

    # Load CWA polygons
    poly_path = DATA_DIR / "cwa_polygons.json"
    polygons = {}
    if poly_path.exists():
        with open(poly_path) as f:
            polygons = json.load(f)

    lats_list = LATS.tolist()
    lons_list = LONS.tolist()

    for cwa_id in by_cwa:
        cwa_idx = cwa_to_idx[cwa_id]
        cwa_cells = np.where(mask == cwa_idx)
        if len(cwa_cells[0]) == 0:
            continue

        # Bounding box with padding
        yi_min = max(0, int(cwa_cells[0].min()) - 5)
        yi_max = min(len(LATS) - 1, int(cwa_cells[0].max()) + 5)
        xi_min = max(0, int(cwa_cells[1].min()) - 5)
        xi_max = min(len(LONS) - 1, int(cwa_cells[1].max()) + 5)

        sub_lats = lats_list[yi_min:yi_max + 1]
        sub_lons = lons_list[xi_min:xi_max + 1]

        # Build mask for this CWA within the subgrid
        sub_mask = (mask[yi_min:yi_max + 1, xi_min:xi_max + 1] == cwa_idx).astype(int).tolist()

        efi_fields = {}
        sot_fields = {}        # upper (moist) tail SOT
        sot_lower_fields = {}  # lower (dry) tail SOT

        for day_str in summary["days"]:
            day = int(day_str)
            tag = f"{date_str}_{cycle}Z_d{day}"
            grid_file = OUTPUT_DIR / f"grid_{tag}.npz"
            if not grid_file.exists():
                continue

            grids = np.load(grid_file)
            efi_sub = grids["efi"][yi_min:yi_max + 1, xi_min:xi_max + 1]
            sot_u_sub = grids["sot_upper"][yi_min:yi_max + 1, xi_min:xi_max + 1]
            sot_l_sub = grids["sot_lower"][yi_min:yi_max + 1, xi_min:xi_max + 1]

            efi_fields[day_str] = [
                [round(float(v), 3) if not np.isnan(v) else None for v in row]
                for row in efi_sub
            ]
            sot_fields[day_str] = [
                [round(float(v), 2) if not np.isnan(v) else None for v in row]
                for row in sot_u_sub
            ]
            # Dry-tail SOT, stored as its positive Zsoter value. The web viewer
            # contours this and labels it -1/-2/-5/-8.
            sot_lower_fields[day_str] = [
                [round(float(v), 2) if not np.isnan(v) else None for v in row]
                for row in sot_l_sub
            ]

        # Build polygon in GeoJSON format
        poly_geojson = None
        if cwa_id in polygons:
            coords = polygons[cwa_id]
            if len(coords) == 1:
                poly_geojson = {"type": "Polygon", "coordinates": coords}
            elif len(coords) > 1:
                poly_geojson = {"type": "MultiPolygon",
                                "coordinates": [[ring] for ring in coords]}

        # Match existing EFI viewer format exactly
        cwa_json = {
            "efi": {
                "lats": sub_lats,
                "lons": sub_lons,
                "cell_size": 0.25,
                "mask": sub_mask,
                "fields": {"td": efi_fields},
            },
            "sot": {
                "lats": sub_lats,
                "lons": sub_lons,
                "cell_size": 0.25,
                "mask": sub_mask,
                "fields": {"td": sot_fields, "td_lower": sot_lower_fields},
            },
            "polygon": poly_geojson,
        }

        grid_path = grids_dir / f"{cwa_id}.json"
        with open(grid_path, "w") as f:
            json.dump(cwa_json, f)

    print(f"Wrote {len(by_cwa)} per-CWA grid files to {grids_dir}/")

    # ── Full-CONUS grid file (the viewer's default view) ──
    # Same {efi, sot, polygon} schema as the per-CWA files, over the whole
    # grid, with mask all-ones and no polygon. Includes the dry-tail SOT.
    conus_efi, conus_sot_u, conus_sot_l = {}, {}, {}
    for day_str in summary["days"]:
        day = int(day_str)
        tag = f"{date_str}_{cycle}Z_d{day}"
        grid_file = OUTPUT_DIR / f"grid_{tag}.npz"
        if not grid_file.exists():
            continue
        grids = np.load(grid_file)
        conus_efi[day_str] = [
            [round(float(v), 3) if not np.isnan(v) else None for v in row]
            for row in grids["efi"]
        ]
        conus_sot_u[day_str] = [
            [round(float(v), 2) if not np.isnan(v) else None for v in row]
            for row in grids["sot_upper"]
        ]
        conus_sot_l[day_str] = [
            [round(float(v), 2) if not np.isnan(v) else None for v in row]
            for row in grids["sot_lower"]
        ]
    conus_mask = [[1] * len(LONS) for _ in range(len(LATS))]
    conus_json = {
        "efi": {"lats": lats_list, "lons": lons_list, "cell_size": 0.25,
                "mask": conus_mask, "fields": {"td": conus_efi}},
        "sot": {"lats": lats_list, "lons": lons_list, "cell_size": 0.25,
                "mask": conus_mask,
                "fields": {"td": conus_sot_u, "td_lower": conus_sot_l}},
        "polygon": None,
    }
    with open(grids_dir / "CONUS.json", "w") as f:
        json.dump(conus_json, f)
    print(f"Wrote CONUS grid file → {grids_dir}/CONUS.json")

    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Aggregated to {len(by_cwa)} CWAs → {json_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-mask", action="store_true")
    a = ap.parse_args()

    if a.build_mask:
        build_cwa_mask()
    else:
        aggregate()


if __name__ == "__main__":
    main()
