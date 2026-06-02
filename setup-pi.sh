#!/usr/bin/env bash
# setup-pi.sh — Install terminal-display on a Raspberry Pi and configure
# auto-updates from GitHub every hour.
#
# Usage (run as the pi user, NOT root):
#   bash setup-pi.sh
#
# What it does:
#   1. Enables SPI (required for the Waveshare e-ink panel)
#   2. Installs system packages (git, python3, spidev, gpiozero)
#   3. Installs Poetry
#   4. Clones or updates the repo at ~/terminal-display
#   5. Installs Python dependencies via Poetry
#   6. Creates a systemd service that starts the stats dashboard on boot
#   7. Adds a cron job that pulls from GitHub every hour and restarts the
#      service if anything changed

set -euo pipefail

REPO_URL="https://github.com/Hiemenz/terminal-display.git"
INSTALL_DIR="$HOME/terminal-display"
SERVICE_NAME="terminal-display"
PYTHON_BIN="$(which python3)"

# ── Colours ────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[setup]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
die()   { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ── Must run as non-root ────────────────────────────────────────────────────
[ "$EUID" -eq 0 ] && die "Run this script as the pi user, not root. Use 'bash setup-pi.sh'."

# ── 1. Enable SPI ──────────────────────────────────────────────────────────
info "Enabling SPI interface..."
if ! grep -q "^dtparam=spi=on" /boot/firmware/config.txt 2>/dev/null && \
   ! grep -q "^dtparam=spi=on" /boot/config.txt 2>/dev/null; then
    # Raspberry Pi OS Bookworm uses /boot/firmware/config.txt
    CONFIG_FILE="/boot/firmware/config.txt"
    [ -f "$CONFIG_FILE" ] || CONFIG_FILE="/boot/config.txt"
    sudo sh -c "echo 'dtparam=spi=on' >> $CONFIG_FILE"
    warn "SPI enabled — a reboot is required after this script finishes."
    REBOOT_NEEDED=true
else
    info "SPI already enabled."
    REBOOT_NEEDED=false
fi

# ── 2. System packages ──────────────────────────────────────────────────────
info "Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    git \
    python3 \
    python3-pip \
    python3-venv \
    python3-spidev \
    python3-gpiozero \
    python3-rpi.gpio \
    libopenjp2-7 \
    libfreetype6-dev \
    libjpeg-dev \
    fonts-dejavu-core

# ── 3. Install Poetry ───────────────────────────────────────────────────────
if ! command -v poetry &>/dev/null; then
    info "Installing Poetry..."
    curl -sSL https://install.python-poetry.org | python3 -
    export PATH="$HOME/.local/bin:$PATH"
    # Persist PATH for future shells
    if ! grep -q 'poetry' "$HOME/.bashrc" 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    fi
else
    info "Poetry already installed: $(poetry --version)"
fi

# ── 4. Clone or update repo ─────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Repo already cloned — pulling latest from main..."
    git -C "$INSTALL_DIR" pull origin main
else
    info "Cloning $REPO_URL → $INSTALL_DIR ..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# ── 5. Python dependencies ──────────────────────────────────────────────────
info "Installing Python dependencies..."
cd "$INSTALL_DIR"
# Tell Poetry to use the system python and put venv inside the project
poetry config virtualenvs.in-project true
# Add spidev/gpiozero from system site-packages if needed
poetry config virtualenvs.options.system-site-packages true
poetry install --no-interaction --no-root

# ── 6. Systemd service ──────────────────────────────────────────────────────
info "Creating systemd service: $SERVICE_NAME..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Terminal Display — e-ink stats dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$(which poetry) run python main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME" || warn "Service start failed — check: journalctl -u $SERVICE_NAME -f"
info "Service status:"
sudo systemctl status "$SERVICE_NAME" --no-pager -l | head -20

# ── 7. Hourly auto-update cron job ──────────────────────────────────────────
info "Setting up hourly GitHub auto-update..."

UPDATE_SCRIPT="$INSTALL_DIR/auto-update.sh"
cat > "$UPDATE_SCRIPT" <<'SCRIPT'
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
    sudo systemctl restart terminal-display
else
    echo "$(date): No changes ($(echo "$AFTER" | cut -c1-8))" | logger -t terminal-display-update
fi
SCRIPT

chmod +x "$UPDATE_SCRIPT"

# Add sudoers entry so the pi user can restart the service without a password
SUDOERS_LINE="$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart $SERVICE_NAME, /bin/systemctl restart terminal-display"
SUDOERS_FILE="/etc/sudoers.d/terminal-display"
if ! sudo test -f "$SUDOERS_FILE"; then
    echo "$SUDOERS_LINE" | sudo tee "$SUDOERS_FILE" > /dev/null
    sudo chmod 440 "$SUDOERS_FILE"
    info "Sudoers entry added for passwordless service restart."
fi

# Install cron job (runs at :00 every hour)
CRON_JOB="0 * * * * $UPDATE_SCRIPT >> $INSTALL_DIR/update.log 2>&1"
EXISTING=$(crontab -l 2>/dev/null || true)
if echo "$EXISTING" | grep -qF "auto-update.sh"; then
    info "Cron job already installed."
else
    (echo "$EXISTING"; echo "$CRON_JOB") | crontab -
    info "Cron job installed — will pull from GitHub every hour on the hour."
fi

# ── Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN} Setup complete!${NC}"
echo ""
echo "  Service:     sudo systemctl status $SERVICE_NAME"
echo "  Logs:        journalctl -u $SERVICE_NAME -f"
echo "  Update log:  tail -f $INSTALL_DIR/update.log"
echo "  Force pull:  $UPDATE_SCRIPT"
echo ""
if [ "${REBOOT_NEEDED:-false}" = true ]; then
    echo -e "${YELLOW}  ⚠  SPI was just enabled — please reboot: sudo reboot${NC}"
    echo ""
fi
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
