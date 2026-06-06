# Terminal Display

System stats dashboard for an 800×480 Waveshare 7.5" V2 e-ink display on Raspberry Pi.

## Quick Start

```bash
poetry install
python main.py --once --local   # render once, view output/terminal.bmp
python main.py --local          # loop forever (macOS dev mode)
python main.py                  # loop forever + push to e-ink (Pi)
```

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | Pipeline orchestrator: collect → render → display |
| `src/system_stats.py` | Collects CPU, RAM, disk, network, load, top procs via psutil |
| `src/render.py` | Renders 800×480 PIL image from stats dict. `render(stats, config)` |
| `src/display.py` | Thin CLI wrapper; calls `display_eink.display_image()` |
| `src/display_eink.py` | Hardware driver. macOS: saves BMP only. Pi: pushes to Waveshare panel |
| `src/config_loader.py` | `load_config(path=None)` — canonical config loader |
| `src/refresh_tracker.py` | Tracks last full e-ink refresh (avoids burn-in) |
| `config/config.yaml` | All tunable settings |
| `output/terminal.bmp` | Most recent rendered image |

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
│              HH:MM:SS  (52pt clock)              │
│              Day, Mon DD YYYY                    │
│ hostname                              up Xh Ym   │
├──────────────────────────────────────────────────┤
│   LEFT COLUMN (377px)   │  RIGHT COLUMN (377px)  │
│   [ CPU ]               │  [ Network ]           │
│   [ Memory ]            │  [ Top Processes ]     │
│   [ Disk ]              │                        │
│   [ Load Average ]      │                        │
├──────────────────────────────────────────────────┤
│ platform: Darwin/Linux                           │
└──────────────────────────────────────────────────┘
```

## Config Options

- `dark_mode: true` — white text on black background
- `update_interval: 30` — seconds between refreshes
- `night_mode: true` / `night_start` / `night_end` — skip night hours
- `show_cpu/memory/disk/network/load/top_processes: true` — toggle panels
- `disk_path: "/"` — disk to monitor
- `network_interface: ""` — auto-detect, or set e.g. `eth0`
- `top_process_count: 5` — how many processes to list

## Waveshare Driver

`src/waveshare_epd/` contains the Pi hardware driver for the Waveshare panel.
On macOS the driver import is skipped — `output/terminal.bmp` is the preview.

## Dev Tips

- On macOS `main.py` auto-detects Darwin and skips hardware push.
- Edit `src/render.py` → re-run `--once --local` → open `output/terminal.bmp`.
- `src/system_stats.py` can be tested standalone: `poetry run python src/system_stats.py`
- To add a new panel: add a section in `render.py` and a toggle key in `config.yaml`.
