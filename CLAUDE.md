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
| `src/preview_server.py` | HTTP server: mirrors the display image over LAN, accepts remote/mobile keyboard input into the PTY, serves the on-device settings editor, notes, and clipboard |
| `src/session_logger.py` | `TabLogger` — optional rotating, ANSI-stripped on-disk log of a tab's output (`terminal_log_enabled`) |
| `src/llm_chat.py` | Offline chat REPL for a GGUF model via `llama-cpp-python` — no network calls. Launched in its own tab by "Chat with local LLM" / Ctrl+N; see `terminal_llm_*` in `config/config.yaml` |
| `src/markdown_renderer.py` | Parses/paginates Markdown into 800×480 PIL images (headers, bold/italic, lists, code, quotes, hr) — no hardware/app dependency |
| `src/markdown_viewer_mixin.py` | Full-screen paginated Markdown viewer over the notes file: PgUp/PgDn page, any other key closes. F6 → "View notes as Markdown" |

Hotkeys: F1 SSH picker, F2 close tab, Ctrl+T new tab, Ctrl+N cycle mode
(terminal → notes → local LLM chat, opening that mode's tab on first use —
see `_cycle_mode` in `src/tabs_mixin.py`), Ctrl+Left/Right switch tabs,
Alt+1..9 jump to tab N, F3 kill process, F4 service manager, F5 power menu,
F6 command palette (includes "Rename tab", "Notes", "Chat with local LLM"),
F7 dark mode, F8 clipboard, F9/F12 font size, F10 full refresh, F11 switch to
stats dashboard, PgUp/PgDn scroll, Ctrl+F scrollback search, Ctrl+\ toggle
split pane (left/right), Ctrl+] swap split-pane focus, Ctrl+Space copy mode
(arrows move a selection cursor over the visible screen, Space marks the
anchor, Enter yanks the range — or the whole line with no anchor — into the
F8 clipboard and beams it to a QR for phone copy, Esc cancels; see
`_toggle_copy_mode` / `_handle_copy_key` in `src/eink_terminal_app.py`),
Ctrl+/ help overlay (lists every hotkey; ↑↓ to browse, Enter runs the
selected one, Esc closes — see `_HELP_ITEMS` / `_run_help_action` in
`src/eink_terminal_app.py`).

Shift+Enter inserts a literal newline instead of submitting — sent as a bare
LF from a directly-attached keyboard (vs. plain Enter's CR; see
`evdev_input.py`'s `_translate`), so a message can span multiple lines
before it's sent. Consumed by `llm_chat.py`'s raw-mode composer
(`_read_composer`); a plain shell just sees a newline like any pasted
multi-line text.

Notes (`terminal_notes_file`, default `data/notes.txt`): F6 → "Notes" or
Ctrl+N opens the file in `nano` in its own tab — just a plain text file, no
custom editor. Readable as raw text (with a Copy button, PIN-gated the same
as `/beam`/`/clipboard` if `preview_server_pin` is set) at `/notes` on the
preview server, so long notes can be copied off the device without a QR
code. See `_open_notes` in `src/tabs_mixin.py` and `_get_notes_path` /
`_read_notes` in `src/preview_server.py`.

Typeable mode-switch commands: `notes`, `llmchat`, `terminal` do the same
thing as Ctrl+N/F6 but from a shell prompt, in any tab — each just signals
the running app (real-time signals SIGRTMIN+1/+2/+3, same PID-file mechanism
as the existing `settings`/`clear-eink` commands; see `_write_signal_script`
in `src/shell_mixin.py`). Inside `llm_chat.py` itself, typing `/notes` or
`/terminal` does the same by shelling out to the `notes`/`terminal` command
— the chat process keeps running in the background so cycling back to LLM
chat mode resumes the same conversation. `llm_chat.py` also has `/help`
(prints a boxed command list), `/menu` (an interactive picker over the same
commands — ↑↓ to browse, Enter runs the highlighted one, Esc/Ctrl+C
cancels; see `_show_menu`/`_read_menu_key`), and `/reset` (clears history).

Restart Terminal (F6 → "Restart terminal (saves notes first)"): kills and
respawns every tab — the plain shell, any `nano`/Notes session, any running
`llm_chat.py` — for a clean slate, without a full `systemctl restart` (so no
sudo, and it doesn't kill this session's own shell the way a service restart
would). Before tearing anything down it snapshots the notes file to
`data/notes_snapshots/notes-<timestamp>.txt` (last 10 kept), since a `nano`
session getting SIGTERM'd has no chance to save on its own — that snapshot
only protects what was last written with Ctrl+O in nano, not in-buffer edits
that were never saved. See `_restart_terminal` / `_backup_notes` in
`src/tabs_mixin.py` (`_reset_session` in `src/eink_terminal_app.py` does the
actual tab teardown/respawn — it's the same method idle-reset already uses).

Markdown viewer (F6 → "View notes as Markdown"): a paginated, *rendered* —
not raw-text — view of the notes file, drawn straight to the panel with PIL
(headers, **bold**, *italic* as underline, `inline code`, fenced code blocks,
bullet/numbered lists, blockquotes, horizontal rules). PgUp/PgDn flip pages;
any other key closes back to the terminal. It bypasses the normal pyte/
terminal render pipeline entirely — same "push a custom full-screen image
straight to the driver" approach as the web UI's "send text to display"
feature, just paginated instead of one-shot. See `src/markdown_renderer.py`
(`render_markdown_pages` — parsing/pagination, no EinkTerminal dependency)
and `src/markdown_viewer_mixin.py` (`_show_markdown`/`_handle_markdown_key` —
the app-side state and PgUp/PgDn/Esc key handling).

Background tabs that produce output while you're on another tab get flagged
in the status-bar tab chip as `•N` (e.g. `[2/3 build] •4`) until you switch
to them — see `_Tab.activity` / `_tab_indicator` in `src/eink_terminal_app.py`.

Session logging (`terminal_log_enabled`, off by default): each tab's output
is stripped of ANSI escape codes and appended to a rotating file under
`terminal_log_dir` (default `data/terminal_logs/`), so a long-running build
or `claude` session's scrollback survives idle-reset or a shell crash and
can be grepped after the fact. See `src/session_logger.py` (`TabLogger`) and
`EinkTerminal._make_tab_logger`.

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
- `preview_server_pin: ""` — PIN-gates the preview server's mutating/sensitive endpoints (settings, remote input, uploads, clipboard, notes); empty disables the gate (default, matches prior behavior)
- `terminal_llm_model_path` / `terminal_llm_context_size` / `terminal_llm_max_tokens` / `terminal_llm_threads` / `terminal_llm_system_prompt` — local LLM chat (`src/llm_chat.py`): GGUF file, context window, response length cap, CPU threads, and system prompt. No network calls — inference runs fully on-device via `llama-cpp-python`
- `terminal_notes_file: data/notes.txt` — plain text file opened by the Notes mode/palette entry (in `nano`) and served as raw text at `/notes`

## Waveshare Driver

`src/waveshare_epd/` contains the Pi hardware driver for the Waveshare panel.
On macOS the driver import is skipped — `output/terminal.bmp` is the preview.

## Dev Tips

- On macOS `main.py` auto-detects Darwin and skips hardware push.
- Edit `src/render.py` → re-run `--once --local` → open `output/terminal.bmp`.
- `src/system_stats.py` can be tested standalone: `poetry run python src/system_stats.py`
- To add a new panel: add a section in `render.py` and a toggle key in `config.yaml`.
