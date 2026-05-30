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
