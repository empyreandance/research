#!/usr/bin/env python3
"""
grib_catcher.py
===============
Continuously watch the WPC EFI FTP for the (often short-lived) EFI GRIB2 file,
grab it the instant it appears, mirror it durably to R2, and trigger the GHA
pipeline so the grids refresh promptly.

Why this exists
---------------
The WPC public FTP (ftp.wpc.ncep.noaa.gov/efi/) posts the EFI GRIB only briefly
and unpredictably, then purges it.  A 30-minute poller misses that window — and
GitHub Actions cron is throttled (it fires every few hours, not on schedule), so
CI can't be the catcher either.  This always-on daemon on the Mac Studio polls
every ~45s, and the moment a new GRIB shows up it:

  1. downloads it in the same FTP session (before it disappears),
  2. mirrors the raw bytes to r2:hrrr-tool/efi-grib/latest.grb2 (durable, public
     at hrrr-data.alexcooke.co/efi-grib/latest.grb2), and
  3. fires a repository_dispatch so the efi-daily workflow runs immediately and
     decodes it (fetch_and_process.py falls back to the R2 mirror when the FTP
     has already dropped the file).

R2 makes the catch durable; the dispatch makes processing prompt.  If the GitHub
token is missing the dispatch is skipped, but the GRIB is still safely on R2 for
the next scheduled run to pick up.

Runs as a launchd LaunchDaemon (KeepAlive) — see deploy/.

Config (environment, all optional):
    EFI_POLL_SECONDS     poll interval        (default 45)
    EFI_R2_REMOTE        rclone dest          (default r2:hrrr-tool/efi-grib)
    EFI_R2_PUBLIC        public base URL      (default https://hrrr-data.alexcooke.co/efi-grib)
    EFI_DISPATCH_REPO    owner/repo           (default empyreandance/research)
    EFI_CATCHER_DIR      state + logs dir     (default ~/efi-catcher)
    GH_TOKEN             GitHub token for repository_dispatch (else falls back to `gh`)
"""
import ftplib
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

FTP_HOST = "ftp.wpc.ncep.noaa.gov"
FTP_DIR = "/efi"

POLL_SECONDS = int(os.environ.get("EFI_POLL_SECONDS", "45"))
R2_REMOTE = os.environ.get("EFI_R2_REMOTE", "r2:hrrr-tool/efi-grib")
PUBLIC_BASE = os.environ.get("EFI_R2_PUBLIC", "https://hrrr-data.alexcooke.co/efi-grib")
DISPATCH_REPO = os.environ.get("EFI_DISPATCH_REPO", "empyreandance/research")
DISPATCH_EVENT = "efi-grib-available"

STATE_DIR = Path(os.environ.get("EFI_CATCHER_DIR", str(Path.home() / "efi-catcher")))
STATE_FILE = STATE_DIR / "last_seen.txt"


def log(msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  {msg}", flush=True)


def load_last():
    try:
        return STATE_FILE.read_text().strip()
    except Exception:
        return ""


def save_last(name):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(name)


def poll_and_grab(last):
    """
    One FTP session: list the EFI dir and, if the newest GRIB is new, download
    it immediately in the SAME session (the file is ephemeral, so detecting and
    fetching must not be two round trips).

    Returns (name, local_path):
      (name, Path)  newly grabbed file
      (name, None)  newest file is already the one we have (name == last)
      (None, None)  nothing on the server, or an error this cycle
    """
    ftp = None
    try:
        ftp = ftplib.FTP(FTP_HOST, timeout=60)
        ftp.login()
        ftp.cwd(FTP_DIR)
        files = []
        ftp.retrlines("NLST", files.append)
        gribs = sorted(f for f in files if f.endswith((".grb2", ".grib2")))
        name = gribs[-1] if gribs else None

        if not name or name == last:
            return name, None

        # New file present — pull it now, on this connection.
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".grb2.part")
        os.close(fd)
        tmp = Path(tmp)
        with open(tmp, "wb") as fh:
            ftp.retrbinary(f"RETR {name}", fh.write)
        return name, tmp
    except Exception as e:
        log(f"FTP poll/grab error: {e}")
        return None, None
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                pass


def mirror_to_r2(local, name):
    """Upload the raw GRIB + a pointer naming it. Raises on failure."""
    subprocess.run(
        ["rclone", "copyto", str(local), f"{R2_REMOTE}/latest.grb2",
         "--header-upload", "Cache-Control: no-store"],
        check=True, capture_output=True, text=True,
    )
    fd, ptr = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    ptr = Path(ptr)
    try:
        ptr.write_text(name)
        subprocess.run(
            ["rclone", "copyto", str(ptr), f"{R2_REMOTE}/latest.txt",
             "--header-upload", "Cache-Control: no-store"],
            check=True, capture_output=True, text=True,
        )
    finally:
        ptr.unlink(missing_ok=True)


def trigger_gha():
    """
    Fire repository_dispatch so the efi-daily workflow runs now.  Prefer a
    GH_TOKEN (reliable for a boot-time daemon); fall back to the gh CLI.
    Best-effort: the GRIB is already durable on R2 either way.
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    payload = json.dumps({"event_type": DISPATCH_EVENT}).encode()
    if token:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{DISPATCH_REPO}/dispatches",
            data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "efi-grib-catcher",
            },
        )
        urllib.request.urlopen(req, timeout=20).read()
        return "token"
    # Fallback: gh CLI (uses keyring auth; may be unavailable at boot).
    subprocess.run(
        ["gh", "api", f"repos/{DISPATCH_REPO}/dispatches",
         "-f", f"event_type={DISPATCH_EVENT}"],
        check=True, capture_output=True, text=True,
    )
    return "gh"


def handle(name, local):
    log(f"NEW GRIB on FTP: {name}  ({local.stat().st_size / 1e6:.1f} MB) — mirroring")
    try:
        mirror_to_r2(local, name)
        log(f"mirrored → {R2_REMOTE}/latest.grb2")
    except subprocess.CalledProcessError as e:
        log(f"rclone mirror failed: {e.stderr or e} — will retry next poll")
        return False
    except Exception as e:
        log(f"mirror failed: {e} — will retry next poll")
        return False

    try:
        how = trigger_gha()
        log(f"triggered GHA repository_dispatch ({DISPATCH_EVENT}) via {how}")
    except Exception as e:
        # Non-fatal: the GRIB is on R2, so the next scheduled run still gets it.
        log(f"dispatch trigger failed (GRIB is safe on R2): {e}")

    save_last(name)
    return True


def main():
    log(f"efi grib-catcher up — poll {POLL_SECONDS}s, repo {DISPATCH_REPO}, dest {R2_REMOTE}")
    last = load_last()
    if last:
        log(f"resuming; last caught: {last}")
    while True:
        name, local = poll_and_grab(last)
        if local is not None:
            try:
                if handle(name, local):
                    last = name
            finally:
                local.unlink(missing_ok=True)
        elif name and name != last:
            log(f"{name} detected but not grabbed this cycle — retrying")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
