#!/usr/bin/env bash
# Cron entry point. Example crontab line (every 15 minutes):
#   */15 * * * * cd /path/to/sentry-autofix && bin/run.sh >> logs/cron.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

# cron runs with a minimal PATH; make sure gh, claude and python3 are found.
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

exec python3 -m autofix "${1:-all}" "${@:2}"
