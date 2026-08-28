# terminal-display

Terminal emulator and system stats dashboard for a Waveshare 7.5" V2 e-ink display
(800×480) on Raspberry Pi. Switch between them with F11.

**Terminal emulator** — a real shell (optionally inside tmux) rendered to the e-ink
panel, driven by an attached USB/BT keyboard or an SSH session. Run `claude` or any
other long-lived CLI session inside it.

**Stats dashboard** — CPU, RAM, disk, network, load averages, and top processes.

## Setup

```bash
# Install dependencies
poetry install

# Terminal emulator — dev preview (saves frames to output/*.bmp)
python eink_terminal.py --local

# Stats dashboard — dev preview
python main.py --once --local
open output/terminal.bmp

# Run on Pi (systemd manages this automatically)
python eink_terminal.py   # terminal emulator
python main.py            # stats dashboard
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

Restarting picks up any code/config changes. The service uses `KillMode=process`,
so only the Python process is signalled — tmux and everything running inside it
(terminal tabs, `nano` Notes sessions, any `claude` session) survive the restart.
The new process reattaches to the existing tmux session on startup.

**Careful if you're working inside a shell driven by that same service** —
e.g. an SSH/Claude Code session typed into the terminal-emulator's tmux
session, or a terminal tab itself. Restarting from there kills your own
session mid-command, since it's a child process of what's being restarted.
Run the restart from a separate connection (another SSH window, or the
keyboard attached directly to the Pi) instead.
