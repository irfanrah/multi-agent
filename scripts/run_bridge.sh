#!/usr/bin/env bash
# Launch the Slack bridge in the background.
#
# - Stops any bridge currently recorded in slack_bridge.pid
# - Deletes the existing slack_bridge.log and slack_bridge.pid
# - Starts a fresh bridge under nohup, redirecting stdout+stderr to
#   slack_bridge.log (lines are prefixed with [YYMMDD-HHMMSS] by the
#   bridge itself — see _install_timestamped_logging in main.py)
# - Writes the new pid to slack_bridge.pid
#
# Run from anywhere — the script resolves paths relative to the repo root.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PID_FILE="$ROOT_DIR/slack_bridge.pid"
LOG_FILE="$ROOT_DIR/slack_bridge.log"
ENTRY="$ROOT_DIR/src/slack_bridge/main.py"

if [[ -f "$PID_FILE" ]]; then
    old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
        echo "Stopping existing bridge (pid=$old_pid)..."
        kill "$old_pid" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$old_pid" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$old_pid" 2>/dev/null; then
            echo "Bridge (pid=$old_pid) didn't exit on TERM — sending KILL."
            kill -9 "$old_pid" 2>/dev/null || true
        fi
    fi
fi

rm -f "$PID_FILE" "$LOG_FILE"

# `python3 -u` keeps stdout/stderr unbuffered so the log reflects events live.
nohup python3 -u "$ENTRY" > "$LOG_FILE" 2>&1 < /dev/null &
new_pid=$!
echo "$new_pid" > "$PID_FILE"
disown "$new_pid" 2>/dev/null || true

echo "Bridge started (pid=$new_pid)"
echo "  log: $LOG_FILE"
echo "  pid: $PID_FILE"
echo "Tail with: tail -f $LOG_FILE"
