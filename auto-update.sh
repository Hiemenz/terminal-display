#!/usr/bin/env bash
# auto-update.sh — pull latest from GitHub; restart service if code changed
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"

BEFORE=$(git rev-parse HEAD 2>/dev/null || echo "")
git pull --ff-only origin main 2>&1 | logger -t terminal-display-update
AFTER=$(git rev-parse HEAD 2>/dev/null || echo "")

if [ "$BEFORE" != "$AFTER" ]; then
    echo "$(date): New version $AFTER — reinstalling deps and restarting service" | logger -t terminal-display-update
    poetry install --no-interaction --no-root 2>&1 | logger -t terminal-display-update
    sudo systemctl restart eink-display
else
    echo "$(date): No changes ($(echo "$AFTER" | cut -c1-8))" | logger -t terminal-display-update
fi
