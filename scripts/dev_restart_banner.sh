#!/bin/bash
# =============================================================================
# scripts/dev_restart_banner.sh — arm/disarm the "dev restart in progress" banner
# =============================================================================
# WHY THIS EXISTS (operator directive, 2026-07-13):
#   The operator's household lives on the ONE live app. During dev I sometimes
#   MUST restart it (a mounted-volume code change that --reload no longer picks
#   up leaves code-on-disk and the running process out of sync -> "weird state"
#   -> saves fail). Restarting is correct; the harm on 2026-07-13 12:04 was that
#   the restart was INVISIBLE — his phone's save aborted and the UI looked broken
#   with no explanation.
#
#   This arms a flag that the reloading page (nginx/html/reloading.html) reads
#   WHILE THE APP IS DOWN (served statically by nginx, so it survives the very
#   outage it explains) and renders a dark-red bordered banner:
#       "App restarting due to ongoing development ... this is intentional."
#
#   AUTO-RESOLVE: the flag carries an `until` timestamp. The reloading page hides
#   the banner once now >= until, so a forgotten flag cannot show "dev restart"
#   forever — it silently reverts to the normal startup page after the window.
#
# USAGE:
#   scripts/dev_restart_banner.sh on  [minutes] [message]   # arm (default 5 min)
#   scripts/dev_restart_banner.sh off                       # disarm now
#   scripts/dev_restart_banner.sh status                    # show current flag
#
# The flag file is bind-mounted into nginx (nginx/html -> /etc/nginx/html), so
# writing it takes effect immediately with NO nginx reload and NO app restart.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The flag lives under static/ (served by nginx's existing `location /static/`,
# a DIRECTORY bind mount): inode-safe to rewrite, served WITHOUT the app proxy so
# the reloading page can read it while the app is down, and needs no nginx.conf
# change (nginx.conf is a single-file mount — editing it does not reach the
# container without a restart; static/ sidesteps that entirely).
FLAG="${SCRIPT_DIR}/static/dev-restart.json"
DEFAULT_MSG="App restarting due to ongoing development. Your data is safe — this is intentional, not a failure."

cmd="${1:-status}"

case "$cmd" in
  on)
    minutes="${2:-5}"
    message="${3:-$DEFAULT_MSG}"
    # ISO-8601 UTC 'until' = now + minutes. The reloading page compares against
    # Date.now(), so UTC with a trailing Z is unambiguous across the phone's tz.
    until_ts="$(date -u -d "+${minutes} minutes" +%Y-%m-%dT%H:%M:%SZ)"
    # Escape the message for JSON (quotes/backslashes) via python for safety.
    python3 - "$FLAG" "$until_ts" "$message" <<'PY'
import json, sys
flag, until_ts, message = sys.argv[1], sys.argv[2], sys.argv[3]
with open(flag, "w") as f:
    json.dump({"active": True, "until": until_ts, "message": message}, f, indent=2)
    f.write("\n")
PY
    echo "dev-restart banner ARMED until ${until_ts} (${minutes} min): ${message}"
    ;;
  off)
    python3 - "$FLAG" "$DEFAULT_MSG" <<'PY'
import json, sys
flag, message = sys.argv[1], sys.argv[2]
with open(flag, "w") as f:
    json.dump({"active": False, "until": None, "message": message}, f, indent=2)
    f.write("\n")
PY
    echo "dev-restart banner DISARMED"
    ;;
  status)
    cat "$FLAG"
    ;;
  *)
    echo "usage: $0 {on [minutes] [message] | off | status}" >&2
    exit 2
    ;;
esac
