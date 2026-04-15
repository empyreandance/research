#!/usr/bin/env python3
"""
hourly_update.py
================
Hourly job that fetches new AFDs, embeds their Long Term sections with
the V3 model, finds historical analogs, computes signal levels, and
writes updated JSON files for the frontend.

Designed to run under GitHub Actions with corpus and model stored in
Cloudflare R2.

Environment variables required:
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    R2_ENDPOINT
    R2_BUCKET (default: caast-corpus)

Usage:
    python scripts/hourly_update.py
"""

import json
import os
import re
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import numpy as np
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
CACHE_DIR = SCRIPT_DIR / ".caast_cache"
OUTPUT_DIR = SCRIPT_DIR / "data"
STATE_FILE = OUTPUT_DIR / "last_run.json"
SIGNALS_FILE = OUTPUT_DIR / "signals.json"
ANALOGS_DIR = OUTPUT_DIR / "analogs"

R2_BUCKET = os.environ.get("R2_BUCKET", "caast-corpus")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_ENDPOINT = os.environ.get("R2_ENDPOINT")

IEM_BASE_URL = "https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json"
IEM_TEXT_URL = "https://mesonet.agron.iastate.edu/api/1/nwstext/{pil}"

SEVERE_LABELS = {"tornado", "severe_wind", "severe_hail"}
TOP_K = 10
SIGNAL_K = 100  # K used for signal computation; TOP_K is for display
MAX_TEXT_LEN = 1500

# How far back to look for new AFDs (hours) on first run
INITIAL_LOOKBACK_HOURS = 24

ALL_WFOS = sorted([
    "AFC", "AFG", "AJK", "ABR", "APX", "ARX", "BIS", "BOU", "CYS", "DDC",
    "DLH", "DMX", "DTX", "DVN", "EAX", "FGF", "FSD", "GID", "GJT", "GLD",
    "GRB", "GRR", "ICT", "ILX", "IND", "IWX", "JKL", "LBF", "LMK", "LOT",
    "LSX", "MKX", "MPX", "MQT", "OAX", "PAH", "PUB", "RIW", "SGF", "TOP",
    "UNR", "AKQ", "ALY", "BGM", "BOX", "BTV", "BUF", "CAE", "CAR", "CHS",
    "CLE", "CTP", "GSP", "GYX", "ILM", "ILN", "LWX", "MHX", "OKX", "PBZ",
    "PHI", "RAH", "RLX", "RNK", "GUM", "HFO", "ABQ", "AMA", "BMX", "BRO",
    "CRP", "EPZ", "EWX", "FFC", "FWD", "HGX", "HNX", "HUN", "JAN", "JAX",
    "KEY", "LCH", "LIX", "LUB", "LZK", "MAF", "MEG", "MFL", "MLB", "MOB",
    "MRX", "OHX", "OUN", "SHV", "SJT", "SJU", "TAE", "TBW", "TSA", "BOI",
    "BYZ", "EKA", "FGZ", "GGW", "LKN", "LOX", "MFR", "MSO", "MTR", "OTX",
    "PDT", "PIH", "PQR", "PSR", "REV", "SEW", "SGX", "SLC", "STO", "TFX",
    "TWC", "VEF",
])

# Section parsing regexes — V3 trained on long_term, extended, and discussion
LONG_TERM_START = re.compile(
    r"^\s*\.?LONG\s*TERM[./\s]",
    re.IGNORECASE | re.MULTILINE,
)
EXTENDED_START = re.compile(
    r"^\s*\.?EXTENDED[./\s]",
    re.IGNORECASE | re.MULTILINE,
)
DISCUSSION_START = re.compile(
    r"^\s*\.?DISCUSSION[./\s]",
    re.IGNORECASE | re.MULTILINE,
)
SECTION_END = re.compile(
    r"(?:^\s*\.[A-Z]|^\s*&&|^\s*\$\$"
    r"|^(?:SHORT\s*TERM|AVIATION|UPDATE|MARINE|FIRE\s*WEATHER|HYDROLOGY"
    r"|PRELIMINARY|CLIMATE|SPOTTER|WATCHES|HEADLINES)[.\s/])",
    re.MULTILINE,
)


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# R2 client
# ---------------------------------------------------------------------------
def get_r2_client():
    if not all([R2_ACCESS_KEY, R2_SECRET_KEY, R2_ENDPOINT]):
        raise RuntimeError("R2 credentials not set in environment")
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def download_from_r2(s3, key, local_path):
    """Download a file from R2 if not already cached."""
    local_path = Path(local_path)
    if local_path.exists() and local_path.stat().st_size > 0:
        return local_path
    local_path.parent.mkdir(parents=True, exist_ok=True)
    log(f"Downloading {key} from R2...")
    s3.download_file(R2_BUCKET, key, str(local_path))
    return local_path


def ensure_model(s3):
    """Download and extract the V3 model if not cached."""
    model_dir = CACHE_DIR / "model" / "finetuned"
    if (model_dir / "config.json").exists():
        log(f"Model already cached at {model_dir}")
        return model_dir

    log("Downloading model from R2...")
    tar_path = CACHE_DIR / "caast_model.tar.gz"
    download_from_r2(s3, "model/caast_model.tar.gz", tar_path)

    log("Extracting model...")
    extract_dir = CACHE_DIR / "model"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(extract_dir)

    tar_path.unlink()  # save disk space
    log(f"Model ready at {model_dir}")
    return model_dir


def ensure_office_embeddings(s3, office):
    """Download and extract one office's embedding shard if not cached."""
    office_dir = CACHE_DIR / "embeddings" / office
    if (office_dir / "embeddings.npy").exists():
        return office_dir

    tar_path = CACHE_DIR / f"{office}.tar.gz"
    try:
        download_from_r2(s3, f"embeddings/{office}.tar.gz", tar_path)
    except Exception as e:
        log(f"  WARNING: could not download embeddings for {office}: {e}")
        return None

    extract_dir = CACHE_DIR / "embeddings"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(extract_dir)

    tar_path.unlink()
    return office_dir


def ensure_labels(s3):
    """Download and extract the labels directory if not cached."""
    labels_dir = CACHE_DIR / "labels_cwa_100km"
    if labels_dir.exists() and any(labels_dir.iterdir()):
        return labels_dir

    log("Downloading labels from R2...")
    tar_path = CACHE_DIR / "caast_labels.tar.gz"
    download_from_r2(s3, "labels/caast_labels.tar.gz", tar_path)

    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(CACHE_DIR)

    tar_path.unlink()
    return labels_dir


def ensure_thresholds(s3):
    """Download thresholds.json from R2."""
    local_path = CACHE_DIR / "thresholds.json"
    download_from_r2(s3, "thresholds.json", local_path)
    with open(local_path) as f:
        return json.load(f)


def ensure_office_metadata(s3):
    """Download office_metadata.json from R2 (verification-derived lift values).

    Always re-downloads since this file is tiny (~20KB) and we want to pick up
    any updates without requiring a cache bump. Falls back to None if download
    fails, in which case lift values come from thresholds.json calibration data.
    """
    local_path = CACHE_DIR / "office_metadata.json"
    # Force re-download even if cached
    if local_path.exists():
        local_path.unlink()
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        log("Downloading office_metadata.json from R2...")
        s3.download_file(R2_BUCKET, "office_metadata.json", str(local_path))
        with open(local_path) as f:
            data = json.load(f)
        log(f"  Loaded verification lift data for {len(data)} offices")
        return data
    except Exception as e:
        log(f"  Office metadata not in R2 yet, using calibration values: {e}")
        return None


# ---------------------------------------------------------------------------
# AFD fetching
# ---------------------------------------------------------------------------
def load_last_run():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            data = json.load(f)
        return datetime.fromisoformat(data["last_run"])
    return datetime.now(timezone.utc) - timedelta(hours=INITIAL_LOOKBACK_HOURS)


def save_last_run(ts):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"last_run": ts.isoformat()}, f)


def fetch_new_afds(since_dt):
    """Fetch AFDs issued since `since_dt` from IEM for all offices."""
    new_afds = []
    since_str = since_dt.strftime("%Y-%m-%dT%H:%MZ")

    for wfo in ALL_WFOS:
        pil = f"AFD{wfo}"
        try:
            r = requests.get(
                IEM_BASE_URL,
                params={"pil": pil, "sdate": since_str},
                timeout=30,
            )
            if r.status_code != 200:
                continue
            products = r.json().get("data", [])
            for p in products:
                new_afds.append({
                    "wfo": wfo,
                    "product_id": p.get("product_id"),
                    "valid": p.get("valid"),
                })
        except Exception as e:
            log(f"  WARNING: IEM fetch failed for {wfo}: {e}")
            continue

    log(f"Found {len(new_afds)} new AFDs since {since_str}")
    return new_afds


def fetch_afd_text(product_id):
    """Fetch the full text of an AFD by product ID."""
    try:
        r = requests.get(IEM_TEXT_URL.format(pil=product_id), timeout=30)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return None


def extract_section(text):
    """Extract the best available section from an AFD.
    Priority: long_term > extended > discussion. Returns (section_text, section_type)."""
    for pattern, name in [(LONG_TERM_START, "long_term"),
                          (EXTENDED_START, "extended"),
                          (DISCUSSION_START, "discussion")]:
        m = pattern.search(text)
        if not m:
            continue
        start = m.end()
        end_m = SECTION_END.search(text[start:])
        end = start + end_m.start() if end_m else len(text)
        section = text[start:end].strip()
        if len(section) >= 50:
            return section, name
    return None, None


# ---------------------------------------------------------------------------
# Embedding + search
# ---------------------------------------------------------------------------
_MODEL = None


def get_model(model_dir):
    global _MODEL
    if _MODEL is None:
        log("Loading V3 model...")
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(str(model_dir))
        log("Model loaded")
    return _MODEL


def load_office_corpus(office_dir, labels_dir, office):
    """Load embeddings, metadata, and labels for one office."""
    emb_path = office_dir / "embeddings.npy"
    meta_path = office_dir / "metadata.jsonl"
    label_path = labels_dir / f"{office}_labels.jsonl"

    if not all(p.exists() for p in [emb_path, meta_path, label_path]):
        return None

    embeddings = np.load(emb_path)

    metadata = []
    with open(meta_path) as f:
        for line in f:
            if line.strip():
                metadata.append(json.loads(line))

    label_lookup = {}
    daily_lookup = {}
    with open(label_path) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                key = (r.get("source_file", ""), r["section"])
                label_lookup[key] = r["label"]
                daily_lookup[key] = r.get("daily_breakdown", {})

    labels = [label_lookup.get((m.get("source_file", ""), m["section"]))
              for m in metadata]
    daily_breakdowns = [daily_lookup.get((m.get("source_file", ""), m["section"]), {})
                        for m in metadata]

    return {
        "embeddings": embeddings,
        "metadata": metadata,
        "labels": labels,
        "daily_breakdowns": daily_breakdowns,
    }


def find_analogs(query_emb, corpus, top_k=TOP_K, max_k=None):
    """Find top-K analogs by cosine similarity, deduplicating by date.

    AFDs are reissued multiple times per day with nearly identical text.
    We collapse all sections from the same office on the same day to the
    single highest-similarity match, then take the top K unique days.
    """
    embeddings = corpus["embeddings"]
    metadata = corpus["metadata"]
    labels = corpus["labels"]
    daily_breakdowns = corpus["daily_breakdowns"]

    # L2 normalize
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1
    embeddings_norm = embeddings / norms
    query_norm = query_emb / (np.linalg.norm(query_emb) + 1e-8)

    similarities = embeddings_norm @ query_norm

    # Sort all by similarity descending, then walk and deduplicate by date
    order = np.argsort(-similarities)

    seen_dates = set()
    analogs = []
    for i in order:
        label = labels[i]
        if label is None:
            continue

        m = metadata[i]
        dt_str = m.get("datetime_utc", "")
        if not dt_str:
            continue

        # Date key: YYYY-MM-DD
        date_key = dt_str[:10]
        if date_key in seen_dates:
            continue
        seen_dates.add(date_key)

        analogs.append({
            "datetime_utc": dt_str,
            "source_file": m.get("source_file", ""),
            "similarity": float(similarities[i]),
            "label": label,
            "is_severe": label in SEVERE_LABELS,
            "daily_breakdown": daily_breakdowns[i],
        })

        if len(analogs) >= (max_k or top_k):
            break

    return analogs


MIN_CONFIDENCE_SIMILARITY = 0.70


def compute_percentile(value, distribution):
    """Estimate percentile of value within an office's signal distribution.

    Distribution is the dict from thresholds.json with keys min/p10/p30/p50/p70/p90/max.
    Uses piecewise linear interpolation between known percentiles.
    """
    if not distribution:
        return None
    points = [
        (distribution.get("min", -1.0), 0),
        (distribution.get("p10"), 10),
        (distribution.get("p30"), 30),
        (distribution.get("p50"), 50),
        (distribution.get("p70"), 70),
        (distribution.get("p90"), 90),
        (distribution.get("max", 1.0), 100),
    ]
    points = [(v, p) for v, p in points if v is not None]
    if not points:
        return None
    if value <= points[0][0]:
        return 0
    if value >= points[-1][0]:
        return 100
    for i in range(len(points) - 1):
        v0, p0 = points[i]
        v1, p1 = points[i + 1]
        if v0 <= value <= v1:
            if v1 == v0:
                return int(p0)
            frac = (value - v0) / (v1 - v0)
            return int(round(p0 + frac * (p1 - p0)))
    return None


def compute_signal(analogs, thresholds, office, office_metadata=None, display_analogs=None):
    """Determine signal level from analog severe fraction vs office baseline."""
    if not analogs:
        return None

    # Confidence check
    mean_similarity = float(np.mean([a["similarity"] for a in analogs]))
    if mean_similarity < MIN_CONFIDENCE_SIMILARITY:
        return {
            "level": "INSUFFICIENT_MATCH",
            "severe_fraction": float(sum(1 for a in analogs if a["is_severe"]) / len(analogs)),
            "mean_similarity": mean_similarity,
            "n_severe": sum(1 for a in analogs if a["is_severe"]),
            "n_total": len(analogs),
            "reason": f"top-{len(analogs)} mean similarity {mean_similarity:.2f} below confidence floor {MIN_CONFIDENCE_SIMILARITY}",
        }

    severe_fraction = sum(1 for a in analogs if a["is_severe"]) / len(analogs)
    # If display_analogs provided, compute display-only severe fraction for UI
    if display_analogs:
        n_severe_display = sum(1 for a in display_analogs if a["is_severe"])
        n_total_display = len(display_analogs)
    else:
        n_severe_display = sum(1 for a in analogs if a["is_severe"])
        n_total_display = len(analogs)

    office_thresh = thresholds.get("per_office", {}).get(office)
    if office_thresh is None:
        office_thresh = thresholds["global"]
        base_rate = None
        signal_distribution = None
        mean_asf = None
    else:
        base_rate = office_thresh.get("base_rate", 0.5)
        signal_distribution = office_thresh.get("signal_distribution")
        mean_asf = office_thresh.get("mean_analog_severe_fraction")

    if base_rate is not None:
        signal_value = severe_fraction - base_rate
    else:
        signal_value = severe_fraction - 0.5

    high_thresh = office_thresh.get("thresholds", {}).get("high", office_thresh.get("high", 0))
    mod_thresh = office_thresh.get("thresholds", {}).get("moderate", office_thresh.get("moderate", 0))
    low_thresh = office_thresh.get("thresholds", {}).get("low", office_thresh.get("low", 0))

    if signal_value >= high_thresh:
        level = "HIGH"
    elif signal_value >= mod_thresh:
        level = "MODERATE"
    elif signal_value >= low_thresh:
        level = "LOW"
    else:
        level = "QUIET"

    # Percentile within office's signal distribution
    percentile = compute_percentile(signal_value, signal_distribution)

    # Office utility (mean lift over baseline) — prefer verification values
    office_lift = None
    if office_metadata and office in office_metadata:
        office_lift = office_metadata[office]["lift"]
        # Also override base_rate with verification value for consistency
        base_rate = office_metadata[office]["base_rate"]
    elif mean_asf is not None and base_rate is not None:
        office_lift = mean_asf - base_rate

    return {
        "level": level,
        "severe_fraction": float(severe_fraction),
        "mean_similarity": mean_similarity,
        "base_rate": float(base_rate) if base_rate is not None else None,
        "signal_value": float(signal_value),
        "percentile": percentile,
        "office_lift": float(office_lift) if office_lift is not None else None,
        "n_severe": n_severe_display,
        "n_total": n_total_display,
        "n_severe_signal": sum(1 for a in analogs if a["is_severe"]),
        "n_total_signal": len(analogs),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log("=" * 60)
    log("CAAST hourly update starting")

    s3 = get_r2_client()

    # Setup: download model, labels, thresholds, office metadata
    model_dir = ensure_model(s3)
    labels_dir = ensure_labels(s3)
    thresholds = ensure_thresholds(s3)
    office_metadata = ensure_office_metadata(s3)

    # Determine window
    last_run = load_last_run()
    now = datetime.now(timezone.utc)
    log(f"Last run: {last_run.isoformat()}")
    log(f"Current time: {now.isoformat()}")

    # Fetch new AFDs
    new_afds = fetch_new_afds(last_run)

    # Group by office (may be empty)
    by_office = {}
    for afd in new_afds:
        by_office.setdefault(afd["wfo"], []).append(afd)

    # Load existing signals if present (to preserve offices with no new AFDs)
    signals = {}
    if SIGNALS_FILE.exists():
        with open(SIGNALS_FILE) as f:
            signals = json.load(f)

    # Seed every calibrated office with static metadata (base rate + lift)
    # so the Office Utility view works for all calibrated offices, not just
    # the ones that have processed a new AFD recently.
    #
    # Prefer values from office_metadata.json (derived from V3 verification —
    # matches the published precision map and uses the same dedup methodology
    # as the deployed tool). Fall back to thresholds.json calibration values
    # if office_metadata isn't available.
    per_office_thresh = thresholds.get("per_office", {})
    seed_offices = set(per_office_thresh.keys())
    if office_metadata:
        seed_offices |= set(office_metadata.keys())

    for office in seed_offices:
        # Verification numbers (preferred)
        if office_metadata and office in office_metadata:
            md = office_metadata[office]
            base_rate = md["base_rate"]
            lift = md["lift"]
            # Always overwrite with verification values (they're authoritative)
            if office not in signals:
                signals[office] = {"level": None}
            signals[office]["base_rate"] = float(base_rate)
            signals[office]["office_lift"] = float(lift)
        else:
            thresh = per_office_thresh.get(office, {})
            base_rate = thresh.get("base_rate")
            mean_asf = thresh.get("mean_analog_severe_fraction")
            if base_rate is None or mean_asf is None:
                continue
            lift = mean_asf - base_rate
            if office not in signals:
                signals[office] = {
                    "level": None,
                    "base_rate": float(base_rate),
                    "office_lift": float(lift),
                }
            else:
                if signals[office].get("office_lift") is None:
                    signals[office]["office_lift"] = float(lift)
                if signals[office].get("base_rate") is None:
                    signals[office]["base_rate"] = float(base_rate)

    if not new_afds:
        log("No new AFDs. Writing seeded signals and exiting.")
        SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SIGNALS_FILE, "w") as f:
            json.dump(signals, f, indent=2)
        save_last_run(now)
        return

    ANALOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Load model
    model = get_model(model_dir)

    # Process each office that has new AFDs
    for office, afds in by_office.items():
        log(f"Processing {office}: {len(afds)} new AFDs")

        office_dir = ensure_office_embeddings(s3, office)
        if office_dir is None:
            continue

        corpus = load_office_corpus(office_dir, labels_dir, office)
        if corpus is None:
            log(f"  No corpus for {office}, skipping")
            continue

        # Process the most recent AFD for this office
        latest_afd = sorted(afds, key=lambda a: a.get("valid") or "")[-1]
        text = fetch_afd_text(latest_afd["product_id"])
        if not text:
            continue

        long_term, section_type = extract_section(text)
        if not long_term:
            log(f"  No usable section in {latest_afd['product_id']}")
            continue

        # Embed
        query_emb = model.encode(long_term[:MAX_TEXT_LEN], convert_to_numpy=True)

        # Find analogs
        # Get K=100 for signal computation, slice to TOP_K for display
        analogs_full = find_analogs(query_emb, corpus, top_k=TOP_K, max_k=SIGNAL_K)
        analogs = analogs_full[:TOP_K]

        # Compute signal
        signal = compute_signal(analogs_full, thresholds, office, office_metadata, display_analogs=analogs)
        if signal is None:
            continue

        signal["product_id"] = latest_afd["product_id"]
        signal["afd_time"] = latest_afd["valid"]
        signal["updated"] = now.isoformat()
        # Preserve seeded verification lift if it exists
        if office in signals and signals[office].get("office_lift") is not None:
            signal["office_lift"] = signals[office]["office_lift"]
        if office in signals and signals[office].get("base_rate") is not None:
            signal["base_rate"] = signals[office]["base_rate"]
        signals[office] = signal

        # Write analogs file
        analog_record = {
            "office": office,
            "product_id": latest_afd["product_id"],
            "afd_time": latest_afd["valid"],
            "section_type": section_type,
            "updated": now.isoformat(),
            "query_preview": long_term[:300],
            "analogs": analogs,
            "signal": signal,
        }
        with open(ANALOGS_DIR / f"{office}.json", "w") as f:
            json.dump(analog_record, f, indent=2)

    # Write combined signals file
    SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNALS_FILE, "w") as f:
        json.dump(signals, f, indent=2)

    save_last_run(now)
    log(f"Updated {len(by_office)} offices")
    log("Done")


if __name__ == "__main__":
    main()
