#!/usr/bin/env bash
# Launch the Tk usage widget in the background.
#
# - Stops any widget currently recorded in widget.pid
# - Deletes the existing widget.log and widget.pid
# - Starts a fresh widget under nohup, redirecting stdout+stderr to
#   widget.log (lines are prefixed with [YYMMDD-HHMMSS] by the widget
#   itself — see _install_timestamped_logging in widget.py)
# - Writes the new pid to widget.pid
#
# The widget needs an X display. If $DISPLAY isn't set the script bails
# out early so we don't spawn a process that immediately dies with a
# Tkinter error.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PID_FILE="$ROOT_DIR/widget.pid"
LOG_FILE="$ROOT_DIR/widget.log"
ENTRY="$ROOT_DIR/src/check_limit/widget.py"

if [[ -z "${DISPLAY:-}" ]]; then
    echo "DISPLAY is not set — the Tk widget needs a desktop session." >&2
    echo "Run this from the graphical session (or set DISPLAY=:0)." >&2
    exit 1
fi

if [[ -f "$PID_FILE" ]]; then
    old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
        echo "Stopping existing widget (pid=$old_pid)..."
        kill "$old_pid" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$old_pid" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$old_pid" 2>/dev/null; then
            echo "Widget (pid=$old_pid) didn't exit on TERM — sending KILL."
            kill -9 "$old_pid" 2>/dev/null || true
        fi
    fi
fi

rm -f "$PID_FILE" "$LOG_FILE"

nohup python3 -u "$ENTRY" > "$LOG_FILE" 2>&1 < /dev/null &
new_pid=$!
echo "$new_pid" > "$PID_FILE"
disown "$new_pid" 2>/dev/null || true

echo "Widget started (pid=$new_pid)"
echo "  log: $LOG_FILE"
echo "  pid: $PID_FILE"
echo "Tail with: tail -f $LOG_FILE"
