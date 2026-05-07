"""
Download hourly ASOS METAR data from Iowa Environmental Mesonet.
Saves one gzipped CSV per station to data/raw/.
Writes failed_stations.csv at end of run.
"""

import argparse
import csv
import datetime as dt
import gzip
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

VARIABLES = ['tmpf', 'dwpf', 'sknt']
RAW_DIR = Path('data/raw')
FAILED_LOG = Path('data/processed/failed_stations.csv')

HEADERS = {
    'User-Agent': 'apparent-temp-anomaly-research/0.1',
}

REQUEST_DELAY = 2.0
RETRY_BACKOFFS = [30, 60, 120]

_blocked_event = threading.Event()


def format_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m{seconds % 60:02d}s'
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f'{h}h{m:02d}m'


def build_url(station_id, start_year, end_year):
    data_params = '&'.join(f'data={v}' for v in VARIABLES)
    return (
        'https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?'
        f'station={station_id}'
        f'&{data_params}'
        f'&year1={start_year}&month1=1&day1=1&hour1=0&minute1=0'
        f'&year2={end_year}&month2=12&day2=31&hour2=23&minute2=59'
        '&tz=Etc/UTC&format=onlycomma&latlon=no'
        '&missing=empty&trace=empty&direct=no&report_type=3'
    )


def fetch_station(station_id, start_year, end_year):
    if _blocked_event.is_set():
        return ('CANCELLED', station_id, 'block detected by another worker')

    output_path = RAW_DIR / f'{station_id}.csv.gz'
    if output_path.exists() and output_path.stat().st_size > 1000:
        return ('SKIP', station_id, 'already downloaded')

    url = build_url(station_id, start_year, end_year)
    last_error = 'unknown'

    for attempt, backoff in enumerate([0] + RETRY_BACKOFFS):
        if _blocked_event.is_set():
            return ('CANCELLED', station_id, 'block detected during retry')

        if backoff > 0:
            time.sleep(backoff)

        try:
            with requests.get(url, headers=HEADERS, timeout=600, stream=True) as r:
                if r.status_code == 503:
                    last_error = '503 Service Unavailable'
                    continue
                if r.status_code in (301, 302, 307, 308):
                    location = r.headers.get('Location', '')
                    if 'sorry' in location or 'blocked' in location:
                        _blocked_event.set()
                        return ('BLOCKED', station_id, location)
                    last_error = f'unexpected redirect to {location}'
                    continue
                r.raise_for_status()

                chunks = []
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        chunks.append(chunk)

            text = b''.join(chunks).decode('utf-8', errors='replace')
            lines = text.split('\n')
            if len(lines) < 10:
                last_error = f'only {len(lines)} lines returned'
                continue

            with gzip.open(output_path, 'wt') as f:
                f.write(text)

            size_kb = output_path.stat().st_size // 1024
            return ('OK', station_id, f'{len(lines)} rows, {size_kb} KB')

        except requests.exceptions.RequestException as e:
            last_error = str(e)[:200]
            continue
        except Exception as e:
            return ('FAIL', station_id, f'unexpected: {e}')

    return ('FAIL', station_id, last_error)


def fetch_station_with_delay(station_id, start_year, end_year):
    result = fetch_station(station_id, start_year, end_year)
    time.sleep(REQUEST_DELAY)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start-year', type=int, default=1995)
    parser.add_argument('--end-year', type=int, default=2024)
    parser.add_argument('--workers', type=int, default=5)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--stations', nargs='+', default=None)
    parser.add_argument('--retry-failed', action='store_true',
                        help='Only retry stations listed in failed_stations.csv')
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_LOG.parent.mkdir(parents=True, exist_ok=True)

    if args.retry_failed:
        if not FAILED_LOG.exists():
            print(f'No failed log at {FAILED_LOG}, nothing to retry')
            return
        failed_df = pd.read_csv(FAILED_LOG)
        station_ids = failed_df['station_id'].tolist()
        print(f'Retrying {len(station_ids)} previously-failed stations')
    elif args.stations:
        station_ids = args.stations
    else:
        df = pd.read_csv('data/processed/stations.csv')
        station_ids = df['station_id'].tolist()
        if args.limit:
            station_ids = station_ids[:args.limit]

    start_dt = dt.datetime.now()
    start_t = time.time()
    print(f'Started at {start_dt.strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'Downloading {len(station_ids)} stations from {args.start_year} to {args.end_year}')
    print(f'Using {args.workers} workers, {REQUEST_DELAY}s post-request delay each')
    print()

    n_ok = n_skip = n_fail = n_empty = n_blocked = n_cancelled = 0
    failed_records = []

    pbar = tqdm(
        total=len(station_ids),
        unit='station',
        desc='Downloading',
        dynamic_ncols=True,
        smoothing=0,  # cumulative-average rate, stable ETA
    )

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(fetch_station_with_delay, sid, args.start_year, args.end_year): sid
            for sid in station_ids
        }
        for future in as_completed(futures):
            try:
                status, sid, detail = future.result()
            except Exception as e:
                status, sid, detail = 'FAIL', futures.get(future, '?'), f'exception: {e}'

            i = pbar.n + 1
            tqdm.write(f'[{i}/{len(station_ids)}] {status} {sid}: {detail}')

            if status == 'OK':
                n_ok += 1
            elif status == 'SKIP':
                n_skip += 1
            elif status == 'BLOCKED':
                n_blocked += 1
                failed_records.append({'station_id': sid, 'reason': f'BLOCKED: {detail}'})
                tqdm.write('  BLOCKED detected. Cancelling remaining workers.')
                for f in futures:
                    if not f.done():
                        f.cancel()
                pbar.update(1)
                break
            elif status == 'CANCELLED':
                n_cancelled += 1
                failed_records.append({'station_id': sid, 'reason': f'CANCELLED: {detail}'})
            else:
                n_fail += 1
                failed_records.append({'station_id': sid, 'reason': f'{status}: {detail}'})

            pbar.update(1)

            elapsed = time.time() - start_t
            done = pbar.n
            if done > 0:
                eta_seconds = elapsed * (len(station_ids) - done) / done
                pbar.set_postfix(
                    ok=n_ok,
                    skip=n_skip,
                    fail=n_fail + n_blocked,
                    elapsed=format_duration(elapsed),
                    eta=format_duration(eta_seconds),
                    refresh=False,
                )

    pbar.close()

    end_dt = dt.datetime.now()
    total_elapsed = time.time() - start_t

    if failed_records:
        with open(FAILED_LOG, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['station_id', 'reason'])
            writer.writeheader()
            writer.writerows(failed_records)
        print(f'\nWrote {len(failed_records)} failed stations to {FAILED_LOG}')

    print(f'\nStarted: {start_dt.strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'Ended:   {end_dt.strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'Elapsed: {format_duration(total_elapsed)}')
    print(f'Summary: {n_ok} downloaded, {n_skip} skipped, {n_empty} empty, '
          f'{n_fail} failed, {n_blocked} blocked, {n_cancelled} cancelled')


if __name__ == '__main__':
    main()
