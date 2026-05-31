#!/usr/bin/env bash
# Install the eink-display systemd service on Raspberry Pi.
# Run once as root (or with sudo) after cloning the repo.
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_SRC="$REPO/systemd/eink-display.service"
SERVICE_DST="/etc/systemd/system/eink-display.service"
LAUNCHER="$REPO/systemd/launcher.sh"

# ── Sanity checks ─────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash systemd/install.sh"
  exit 1
fi

# Detect repo owner (the non-root user who owns pyproject.toml)
REPO_USER=$(stat -c '%U' "$REPO/pyproject.toml" 2>/dev/null || echo pi)
REPO_GROUP=$(stat -c '%G' "$REPO/pyproject.toml" 2>/dev/null || echo pi)
echo "Repo owner: $REPO_USER:$REPO_GROUP"

# ── Patch the service file with actual paths ──────────────────────────────────
sed \
  -e "s|/home/pi/terminal-display|$REPO|g" \
  -e "s|User=pi|User=$REPO_USER|g" \
  -e "s|Group=pi|Group=$REPO_GROUP|g" \
  "$SERVICE_SRC" > "$SERVICE_DST"

chmod 644 "$SERVICE_DST"
chmod +x "$LAUNCHER"

echo "Installed → $SERVICE_DST"

# ── Enable and start ──────────────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable eink-display.service
systemctl start  eink-display.service

echo ""
echo "Service status:"
systemctl status eink-display.service --no-pager -l

echo ""
echo "Useful commands:"
echo "  sudo systemctl status  eink-display"
echo "  sudo systemctl restart eink-display"
echo "  sudo journalctl -u eink-display -f"
