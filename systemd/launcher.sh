#!/usr/bin/env bash
# Reads startup_mode from config/config.yaml and launches the appropriate script.
# Called by systemd. Uses uv for the Python environment.
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$REPO/config/config.yaml"

# Prefer the in-project venv (created by `uv sync`), then bare python3.
PYTHON="$REPO/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

# Parse startup_mode from config (grep + awk, no Python needed at this point)
MODE=$(grep -E '^startup_mode:' "$CONFIG" | awk '{print $2}' | tr -d '"' | tr -d "'")
MODE="${MODE:-terminal}"

case "$MODE" in
  stats)
    exec "$PYTHON" "$REPO/main.py"
    ;;
  terminal|*)
    exec "$PYTHON" "$REPO/eink_terminal.py"
    ;;
esac
