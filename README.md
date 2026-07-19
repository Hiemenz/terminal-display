# terminal-display

System stats dashboard for a Waveshare 7.5" V2 e-ink display (800×480) on Raspberry Pi.

Shows a terminal-aesthetic screen with:
- 🕐 Big live clock + date
- 🖥  CPU usage + bar
- 🧠 RAM usage + bar  
- 💾 Disk usage + bar
- 📡 Network I/O
- ⚖️  Load averages (1/5/15m)
- 🔝 Top processes by CPU

## Setup

```bash
# Install dependencies
poetry install

# Render a preview (macOS / no hardware)
python main.py --once --local
open output/terminal.bmp

# Run forever on Pi
python main.py
```

## Configuration

Edit `config/config.yaml`:
```yaml
dark_mode: true          # white-on-black terminal look
update_interval: 30      # seconds between refreshes
night_mode: true         # skip 2am–7am
disk_path: "/"
top_process_count: 5
```

## Architecture

`main.py` → `system_stats.collect()` → `render.render()` → `display_eink.display_image()`

On macOS: saves `output/terminal.bmp`. On Pi: pushes to e-ink hardware.

## Deploying changes

On the Pi, both the stats dashboard and the terminal emulator normally run
under one systemd service:

```bash
sudo systemctl restart eink-display
```

Restarting picks up any code/config changes, and — since `KillMode=control-group`
— cleanly kills everything the service spawned: all terminal tabs, any `nano`
Notes session, any running `llm_chat.py` chat. It comes back with one fresh
shell tab.

**Careful if you're working inside a shell driven by that same service** —
e.g. an SSH/Claude Code session typed into the terminal-emulator's tmux
session, or a terminal tab itself. Restarting from there kills your own
session mid-command, since it's a child process of what's being restarted.
Run the restart from a separate connection (another SSH window, or the
keyboard attached directly to the Pi) instead.
