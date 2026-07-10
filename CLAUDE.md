# Terminal Display

Two things live on the same 800×480 Waveshare 7.5" V2 e-ink display on a
Raspberry Pi, and you switch between them with F11:

1. **Stats dashboard** (`main.py`) — CPU/RAM/disk/network cards.
2. **Terminal emulator** (`eink_terminal.py`) — a real shell (optionally
   inside tmux) rendered to the e-ink panel, driven by an attached USB/BT
   keyboard (`src/evdev_input.py`) or an SSH session typing into the same
   tmux session. This is the one you'd run `claude` or any other long-lived
   CLI session inside.

`local=True` throughout the codebase means **dev preview** (macOS, or
`--local`): no real e-ink push, frames saved to `output/*.bmp`. `local=False`
is the **live** production path on real Pi hardware. Several behaviors
(e.g. the terminal's ambient QR overlay) are deliberately dev-preview-only —
see `src/eink_terminal_app.py`'s `self._local`.

## Quick Start

```bash
poetry install
python main.py --once --local   # render once, view output/terminal.bmp
python main.py --local          # loop forever (macOS dev mode)
python main.py                  # loop forever + push to e-ink (Pi)

python eink_terminal.py --local # terminal emulator, dev preview
python eink_terminal.py         # terminal emulator, live on Pi hardware
```

## Key Files — Stats Dashboard

| File | Purpose |
|------|---------|
| `main.py` | Pipeline orchestrator: collect → render → display |
| `src/system_stats.py` | Collects CPU, RAM, disk, network, load, top procs via psutil |
| `src/render.py` | Renders 800×480 PIL image from stats dict. `render(stats, config)` |
| `src/display.py` | Thin CLI wrapper; calls `display_eink.display_image()` |
| `src/display_eink.py` | Hardware driver (`EinkDriver`). macOS/local: saves BMP only. Pi: pushes to Waveshare panel |
| `src/config_loader.py` | `load_config(path=None)` — canonical config loader |
| `src/refresh_tracker.py` | Tracks last full e-ink refresh (avoids burn-in) |
| `src/refresh_schedule.py` | Adaptive refresh cadence by time of day |
| `src/stats_history.py` | Disk-persisted ring buffer of stats samples, for sparklines |
| `src/sd_watchdog.py` | systemd watchdog pings + readiness notification |
| `src/util.py` | Shared filesystem helpers (repo/data/config paths) |
| `config/config.yaml` | All tunable settings |
| `output/terminal.bmp` | Most recent rendered image |

## Key Files — Terminal Emulator

| File | Purpose |
|------|---------|
| `eink_terminal.py` | CLI entrypoint. Parses `--local`/`--font-size`/`--config`, builds `EinkTerminal`, calls `.run()` |
| `src/eink_terminal_app.py` | The app itself: `pty.fork()`'d shell (optionally via tmux), `pyte` screen buffer, hotkeys, tabs, idle-reset/screensaver state machine, main select() loop |
| `src/evdev_input.py` | Reads raw keycodes from `/dev/input/eventX` (bypasses X11/Wayland), translates to terminal byte sequences — used when a physical keyboard is attached to the Pi directly |
| `src/terminal_renderer.py` | Renders the `pyte` screen buffer to a PIL image; draws the ambient URL QR overlay, tab bar |
| `src/alert_monitor.py` | Polls for system conditions, feeds short-lived alerts into the terminal status bar |
| `src/preview_server.py` | HTTP server: mirrors the display image over LAN, accepts remote/mobile keyboard input into the PTY, serves the on-device settings editor |

Hotkeys: F1 SSH picker, F2 close tab, Ctrl+T new tab, Ctrl+Left/Right switch
tabs, F3 kill process, F4 service manager, F5 power menu, F6 command palette
(includes "Rename tab"), F7 dark mode, F8 clipboard, F9/F12 font size, F10
full refresh, F11 switch to stats dashboard, PgUp/PgDn scroll, Ctrl+F
scrollback search, Ctrl+\ toggle split pane (left/right), Ctrl+] swap
split-pane focus.

Idle behavior (all configurable, `terminal_*` keys in `config/config.yaml`):
panel deep-sleep → screensaver → **idle reset** (kills and respawns the
shell/tmux session after `terminal_reset_minutes` of no keyboard input).
Idle reset skips tabs with a busy foreground process (checked via
`EinkTerminal._tab_is_busy`) so a long-running session — `claude`, `vim`, a
build — never gets silently killed just because no key was pressed.

## Architecture

```
main.py
  └── system_stats.collect(config)     # psutil → stats dict
  └── render.render(stats, config)     # PIL image 800×480
  └── display.send_to_display(path)    # → display_eink.display_image()
```

## Display Layout (800×480)

```
┌────────────────── TOP BAR ──────────────────────┐
│ hostname        HH:MM:SS (54pt)       up Xh Ym   │
│ platform      Day, Mon DD YYYY        IP addr    │
├──────────────────────────────────────────────────┤
│   LEFT COLUMN (377px)   │  RIGHT COLUMN (377px)  │
│   (CPU + load card)     │  (Network card + QR)   │
│   (Memory card)         │  (Processes card)      │
│   (Disk card)           │                        │
└──────────────────────────────────────────────────┘
```

Panels are rounded "cards" with filled title chips; headline metrics are
drawn big and right-aligned. Load average folds into the CPU card and the
web-UI QR code sits inside the Network card.

## Config Options

- `dark_mode: true` — white text on black background
- `update_interval: 30` — seconds between refreshes
- `night_mode: true` / `night_start` / `night_end` — skip night hours
- `show_cpu/memory/disk/network/load/top_processes/updates/ci_status: true` — toggle panels
- `disk_path: "/"` — disk to monitor
- `network_interface: ""` — auto-detect, or set e.g. `eth0`
- `top_process_count: 5` — how many processes to list
- `updates_check_interval_minutes: 60` — how often to re-poll `apt list --upgradable` for the pending-updates badge (apt-based Linux only)
- `ci_status_repo` / `ci_status_branch` / `ci_status_check_interval_minutes: 15` — GitHub Actions build-status badge, shown only when the latest run didn't succeed
- `config_snapshot_count: 10` — config saves keep this many timestamped snapshots in `data/config_snapshots/`, restorable from the settings page's History list
- `terminal_alert_health_interval: 30` / `terminal_alert_throttle` / `terminal_alert_failed_units` / `terminal_alert_storage_health` / `terminal_alert_network` / `terminal_alert_network_host` / `terminal_alert_network_fails` — system-health alerts (thermal throttle, failed systemd units, SD card read-only remount, dead network) shown in the terminal status bar
- `preview_server_pin: ""` — PIN-gates the preview server's mutating/sensitive endpoints (settings, remote input, uploads, clipboard); empty disables the gate (default, matches prior behavior)

## Waveshare Driver

`src/waveshare_epd/` contains the Pi hardware driver for the Waveshare panel.
On macOS the driver import is skipped — `output/terminal.bmp` is the preview.

## Dev Tips

- On macOS `main.py` auto-detects Darwin and skips hardware push.
- Edit `src/render.py` → re-run `--once --local` → open `output/terminal.bmp`.
- `src/system_stats.py` can be tested standalone: `poetry run python src/system_stats.py`
- To add a new panel: add a section in `render.py` and a toggle key in `config.yaml`.
