#!/bin/bash
# smarthome-restart-watcher.sh
#
# Host-side daemon that runs UI-requested lifecycle actions (the container can't
# run docker / start.sh itself). Mirrors 0_MOBIUS.TILES / 0_MOBIUS.NVR
# (canonical STANDARD RESTART.1-4). Installed + started by start.sh as a systemd
# service.
#
# DESIGN (2026-07-09 redesign — own-file, not a shared mutable trigger):
#   The container app (appuser, uid 999) cannot reliably OVERWRITE a file owned
#   by this watcher (elfege, uid 1000) across the container boundary — a shared
#   trigger file gets EACCES. So each side writes its OWN file:
#     - the APP writes:   /dev/shm/smarthome-restart/request
#         content = "<action> <nonce>"  (nonce = time.time(), makes repeats
#         distinct so the same action fires again). action ∈
#         { reboot | matter:stop | matter:start | matter:restart }.
#     - THIS watcher only READS request and acts when its content CHANGES; it
#       never writes request (no ownership fight).
#     - the watcher's heartbeat goes to a SEPARATE file it owns: status.
set -uo pipefail

DIR="/dev/shm/smarthome-restart"
REQUEST_FILE="$DIR/request"
STATUS_FILE="$DIR/status"
PROJECT_DIR="$HOME/0_MOBIUS.SMART_HOME"
START_LOG="$DIR/restart.log"
MATTER_CONTAINER="smarthome-matter-server"

mkdir -p "$DIR" 2>/dev/null || true
SERVICE_START="$(date)"
# Seed last_request with the CURRENT request content so a stale request from
# before this watcher started does NOT fire on boot.
last_request="$(cat "$REQUEST_FILE" 2>/dev/null || true)"
echo "smarthome-restart-watcher started: $SERVICE_START" >"$STATUS_FILE" 2>/dev/null || true
echo "[smarthome-restart-watcher] watching $REQUEST_FILE (own-file design)"

# Dispatch one request. First whitespace-delimited token is the action; the
# trailing nonce is ignored (it only exists to make repeats distinct).
act_on() {
    local content="$1"
    local action="${content%% *}"
    case "$action" in
        reboot)
            echo "[watcher] reboot request '$content' — running start.sh" | tee -a "$START_LOG"
            (
                pkill -f "0_MOBIUS.SMART_HOME/start.sh" 2>/dev/null || true
                sleep 2
                cd "$PROJECT_DIR" && ./start.sh || echo "[watcher] start.sh FAILED — see $START_LOG"
            ) >>"$START_LOG" 2>&1 &
            ;;
        matter:stop)
            echo "[watcher] matter:stop '$content'" | tee -a "$START_LOG"
            ( docker stop "$MATTER_CONTAINER" ) >>"$START_LOG" 2>&1 &
            ;;
        matter:start)
            echo "[watcher] matter:start '$content'" | tee -a "$START_LOG"
            ( docker start "$MATTER_CONTAINER" 2>/dev/null \
                || ( cd "$PROJECT_DIR" && docker compose up -d matter-server ) ) >>"$START_LOG" 2>&1 &
            ;;
        matter:restart)
            echo "[watcher] matter:restart '$content'" | tee -a "$START_LOG"
            ( docker restart "$MATTER_CONTAINER" 2>/dev/null \
                || ( cd "$PROJECT_DIR" && docker compose up -d matter-server ) ) >>"$START_LOG" 2>&1 &
            ;;
        *)
            echo "[watcher] unknown action '$action' (content='$content')" | tee -a "$START_LOG"
            ;;
    esac
}

heartbeat=$(date +%s)
while true; do
    content="$(cat "$REQUEST_FILE" 2>/dev/null || true)"
    if [[ -n "$content" && "$content" != "$last_request" ]]; then
        last_request="$content"
        echo "[watcher] new request '$content' @ $(date)" | tee -a "$START_LOG"
        act_on "$content"
    fi
    # Heartbeat to OUR OWN status file (never the app's request file).
    if (( $(date +%s) - heartbeat > 30 )); then
        echo "smarthome-restart-watcher alive | started $SERVICE_START | last: ${last_request:-none}" >"$STATUS_FILE" 2>/dev/null || true
        heartbeat=$(date +%s)
    fi
    sleep 2
done
