# EFI grib-catcher — deploy

The WPC EFI GRIB lands on the public FTP only briefly and unpredictably, then
gets purged. GitHub Actions cron is throttled (fires every few hours), so CI
can't catch it. This always-on daemon on the Mac Studio polls the FTP every
~45s, grabs the GRIB the instant it appears, mirrors it durably to R2, and
fires a `repository_dispatch` so the `efi-daily` workflow decodes it promptly.

```
ftp.wpc.ncep.noaa.gov/efi/  ──(poll 45s)──▶  grib_catcher.py (Mac Studio)
                                                 │ rclone copyto
                                                 ▼
                              r2:hrrr-tool/efi-grib/latest.grb2  (+ latest.txt)
                              public: hrrr-data.alexcooke.co/efi-grib/latest.grb2
                                                 │ gh repository_dispatch
                                                 ▼
                              efi-daily workflow ── fetch_and_process.py --mode auto
                                 └ FTP empty → pulls GRIB from the R2 mirror → grids
```

The R2 mirror makes the catch **durable**; the dispatch makes processing
**prompt**. If the dispatch fails (no token), the GRIB is still on R2 and the
next scheduled run picks it up.

## One-time setup

1. **GitHub token for the dispatch trigger.** Create a fine-grained PAT scoped
   to the `empyreandance/research` repo with **Contents: read & write** (enough
   for `repository_dispatch`), then drop it in a gitignored env file:

   ```bash
   mkdir -p ~/efi-catcher
   cat > ~/efi-catcher/.env <<'EOF'
   GH_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxx
   # Optional overrides:
   # EFI_POLL_SECONDS=45
   # EFI_R2_REMOTE=r2:hrrr-tool/efi-grib
   # EFI_DISPATCH_REPO=empyreandance/research
   EOF
   chmod 600 ~/efi-catcher/.env
   ```

   (Without a token the daemon still mirrors to R2; it just won't trigger CI
   immediately — it falls back to the `gh` CLI if that's authed.)

2. **Smoke-test it in the foreground** before installing:

   ```bash
   set -a; . ~/efi-catcher/.env; set +a
   python3 ~/research/efi-tool/grib_catcher.py
   # Ctrl-C after you see "efi grib-catcher up ..."
   ```

3. **Install the LaunchDaemon** (same pattern as the rest of the suite):

   ```bash
   chmod +x ~/research/efi-tool/deploy/run-catcher.sh
   sudo cp ~/research/efi-tool/deploy/com.alexcooke.efi-grib-catcher.plist /Library/LaunchDaemons/
   sudo chown root:wheel /Library/LaunchDaemons/com.alexcooke.efi-grib-catcher.plist
   sudo chmod 644        /Library/LaunchDaemons/com.alexcooke.efi-grib-catcher.plist
   sudo launchctl bootout   system /Library/LaunchDaemons/com.alexcooke.efi-grib-catcher.plist 2>/dev/null || true
   sudo launchctl bootstrap system /Library/LaunchDaemons/com.alexcooke.efi-grib-catcher.plist
   ```

## Check / logs

```bash
launchctl list | grep efi-grib-catcher
tail -f ~/efi-catcher/catcher.out      # poll + catch activity
cat ~/efi-catcher/last_seen.txt        # last GRIB filename caught
curl -s https://hrrr-data.alexcooke.co/efi-grib/latest.txt   # what's mirrored now
```

## Notes

- Runs as `UserName alex`, so `rclone` uses the existing `~/.config/rclone`
  `r2:` remote (scoped to `hrrr-tool` + `ohx-qlcs`). The GRIB lives under the
  `efi-grib/` prefix in the existing `hrrr-tool` bucket — no new bucket/token.
- **R2 cost:** ~2 PUTs per model cycle (a few cycles/day) + CI reads — trivially
  under the free tier. Pointers are uploaded `Cache-Control: no-store`.
- Uninstall: `sudo launchctl bootout system /Library/LaunchDaemons/com.alexcooke.efi-grib-catcher.plist && sudo rm /Library/LaunchDaemons/com.alexcooke.efi-grib-catcher.plist`
