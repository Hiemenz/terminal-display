#!/usr/bin/env bash
# stop.sh — Stop the terminal-display program.
# On Pi: stops the eink-display systemd service.
# On macOS: kills the running main.py process.

set -euo pipefail

if [[ "$(uname)" == "Linux" ]]; then
    SERVICE="eink-display"
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
        sudo systemctl stop "$SERVICE"
        echo "Stopped $SERVICE service."
    else
        echo "$SERVICE is not running."
    fi
else
    PIDS=$(pgrep -f "python.*main\.py" 2>/dev/null || true)
    if [[ -n "$PIDS" ]]; then
        kill $PIDS
        echo "Stopped terminal-display (PIDs: $PIDS)."
    else
        echo "terminal-display is not running."
    fi
fi
