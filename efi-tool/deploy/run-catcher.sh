#!/usr/bin/env bash
#
# launchd entry point for the EFI grib-catcher. Sources the gitignored env file
# (holds GH_TOKEN for the repository_dispatch trigger; rclone uses the user's
# own ~/.config/rclone since the daemon runs as UserName alex) and execs the
# poller. KeepAlive in the plist restarts this if it ever exits.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

ENV_FILE="${EFI_CATCHER_ENV:-$HOME/efi-catcher/.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

exec /usr/bin/python3 /Users/alex/research/efi-tool/grib_catcher.py
