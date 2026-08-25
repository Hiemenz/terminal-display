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
| `src/help_sheet.py` | Renders the full command reference as a two-column 800×480 cheat sheet (Ctrl+/ or the `commands` command) — no app dependency |
| `src/command_watch.py` | `CommandWatcher` — pure bookkeeping over per-tab foreground commands; reports long-running ones finishing |
| `src/claude_attention.py` | `AttentionWatcher` / `pane_state` — pure logic deciding when a `claude` tab has stopped for a human, from the pane's own text |
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
F8 clipboard and beams it to a QR for phone copy, Esc cancels; the yank also
goes into tmux's paste buffer, so Ctrl+B ] drops it into any pane on the
machine — see `_toggle_copy_mode` / `_handle_copy_key` /
`_copy_to_tmux_buffer` in `src/text_actions_mixin.py`),
Ctrl+/ full-screen command sheet: every command at once in two columns,
drawn straight to the panel (PgUp/PgDn if it ever needs a second page, any
other key closes), with the tab lifecycle spelled out underneath — `exit` or
F2 closes a tab, the last tab won't close, and exiting its shell leaves the
"Shell exited" bar (Enter restarts, Ctrl+C quits, auto-restart after 10 s).
The sheet also carries a QR to the web settings page in its bottom-right
(`_settings_url` in `src/palette_help_mixin.py` → `qr_url` in
`render_help_pages`): the sheet lists what you *press* on the device, and
everything you'd rather type lives in that browser page — a QR is the only
way to hand a phone a LAN URL off an e-ink panel. It costs the last column
its bottom few rows, which the sheet can afford because `_paginate` packs by
real row height rather than line count (a section gap is 6px, not a whole
line). `test_the_whole_reference_is_one_page` is the guard: the sheet must
stay one page.

`_HELP_SECTIONS` in `src/terminal_state.py` is the single source: the flat
`_HELP_ITEMS` the runnable picker reads (↑↓ to browse, Enter runs the
selected one — `_toggle_help` / `_run_help_action`) is derived from it, so
the two views can't drift. See `src/help_sheet.py` and `_show_help_sheet` in
`src/palette_help_mixin.py`.

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

Typeable commands: `notes`, `llmchat`, `terminal` do the same thing as
Ctrl+N/F6 but from a shell prompt, in any tab, and `commands` opens the
Ctrl+/ command sheet (named that because `help` is a bash builtin) — each
just signals the running app (real-time signals SIGRTMIN+1/+2/+3/+4, same
PID-file mechanism as the existing `settings`/`clear-eink` commands; see
`_write_signal_script` in `src/shell_mixin.py`). The scripts live in
`data/bin` **and** are symlinked into `~/.local/bin`: tmux outlives this
process (`KillMode=process`), so shells in a pre-existing session keep the
PATH the tmux server started with — which can point at a bindir that has
since moved, silently breaking every typed command. `~/.local/bin` is
already first on the login PATH, so it reaches old panes, new panes and SSH
sessions alike (`_link_commands_into_user_bin`). Inside `llm_chat.py` itself, typing `/notes` or
`/terminal` does the same by shelling out to the `notes`/`terminal` command
— the chat process keeps running in the background so cycling back to LLM
chat mode resumes the same conversation. `llm_chat.py` also has `/help`
(prints a boxed command list), `/menu` (an interactive picker over the same
commands — ↑↓ to browse, Enter runs the highlighted one, Esc/Ctrl+C
cancels; see `_show_menu`/`_read_menu_key`), and `/reset` (clears history).

Copying text off the device: `/screen` on the preview server serves whatever
is on the panel right now as selectable text with a Copy button (`/screen.txt`
for raw text), PIN-gated and HTML-escaped like `/beam` and `/notes`. That's
the path for a long stack trace, where reading a QR code is miserable. The
app publishes the text each loop via `set_screen_text`.

**"claude is waiting" / "claude needs you"** (`terminal_claude_attention`,
tmux mode only): when a tab running `claude` stops for an approval or an
answer, the status bar says so. This is the thing the panel is glanced at
for, and a long agent turn looks exactly like idle to everything else here —
no keyboard input, so the panel deep-sleeps right when the session starts
wanting you.

The signal is the pane's own text, not the transcript on disk: Claude Code
prints `esc to interrupt` in its footer for as long as it is busy, so its
presence is "working" and its absence is "your move" (with `Do you want
to…` / `Do you trust…` separated out as `approval`, since that one cannot
proceed without you). The transcript would also answer this, but it only
gains an entry when a turn *completes*, so it lags a live session by however
long the current step takes. Only panes already running `claude` are
captured, so the usual case costs one `tmux list-panes` and no
`capture-pane` at all. `terminal_claude_attention_seconds` (default 30)
keeps it quiet: a turn that resolved in four seconds was never something you
walked away from. Decision logic is pure and lives in
`src/claude_attention.py`; the poll is `_poll_claude_attention` in
`src/eink_terminal_app.py`.

Long-running commands announce themselves in the status bar when they finish
("make done in 2m14s (build)"), controlled by `terminal_long_command_seconds`
(default 30, 0 disables; tmux mode only). One `tmux list-panes -a` covers
every tab, so the poll is a single subprocess every 3 s. The decision logic
is pure and lives in `src/command_watch.py` (`CommandWatcher`); the poll is
`_poll_finished_commands` in `src/eink_terminal_app.py`.

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
- `display_sleep_shows_screensaver: true` — draw the lock screen before deep-sleeping instead of leaving the terminal on the glass
- `claude_weekly_token_budget: 0` — yardstick for the "used N%" line (0 = compare against your own 4-week average)
- `screensaver_show_qr: false` — the lock screen's wake QR code
- `screensaver_show_claude_usage: true` / `terminal_claude_usage_ttl: 300` — lock-screen Claude Code activity panel (local estimate, not quota) and how often the transcripts are rescanned
- `claude_trend_days: 14` — daily token bars on the lock-screen tile (0 hides them)
- `terminal_claude_attention: true` / `terminal_claude_attention_seconds: 30` / `terminal_claude_attention_interval: 5` — status-bar note when a `claude` tab stops for an approval or an answer (tmux mode only)
- `terminal_long_command_seconds: 30` — announce a command finishing when it ran at least this long (0 = off, tmux mode only)
- `terminal_notes_file: data/notes.txt` — plain text file opened by the Notes mode/palette entry (in `nano`) and served as raw text at `/notes`

## Lock Screen: Claude Activity Panel

The screensaver can show how much Claude Code work has gone through recently
— messages and tokens over the last 5 h and 7 d — read from the session
transcripts under `~/.claude/projects/*/*.jsonl`
(`src/claude_usage.py`, `screensaver_show_claude_usage`). The panel deep-sleeps onto this image rather than onto the terminal
(`display_sleep_shows_screensaver`): e-ink retains whatever was last drawn,
so sleeping on the terminal left the session on the glass and looked exactly
like a display that never slept. It sits in the
bottom-right corner as a single tile with the week-progress bar (the week
runs Tuesday 23:00 → Tuesday 23:00, so it reads as how far through the week
this usage happened). The wake QR that used to own that corner is off by
default (`screensaver_show_qr`); turned back on, the tile stacks above it
rather than over it. With the activity panel disabled the week bar falls
back to its own box in the top-left.

The tile has no title bar and no busiest-project line: on a panel this size
every row has to earn its place, and neither told you anything you'd act on.
It is sized to stay a corner note on top of the screensaver photo rather than
the subject of it — `_TILE_FONT` / `_TILE_LINE_H` / `_DAILY_BAR_H` in
`src/render.py` are the knobs, and at their current values it covers under a
tenth of the panel.
"local est." moved onto the trend row instead, and it is there for a reason:
**this is not your quota status.** Claude Code's real 5-hour and weekly limits are enforced
server-side and written nowhere on disk — `/usage` fetches them live — so
what's shown is what the local transcripts record going through, in tokens,
which is not the unit the limits are counted in.

The tile's top line is whose turn it is right now — `claude waiting 4m20s
(terminal-display)` — from `session_state()` in `src/claude_usage.py`, which
reads the tail of the most recently written transcript and asks what the
last entry left it as: an assistant turn with no tool call means the session
is yours to move, anything else means it still has the ball. Sidechain
(subagent) entries are skipped, since a subagent finishing says nothing
about the session you're looking at, and a session with nothing in the last
30 minutes reports nothing at all. This is the path for sessions running
anywhere on the machine; a session in a local tab is caught faster by
`src/claude_attention.py` above.

Under the numbers is a bar per day for the last `claude_trend_days` (default
14, 0 hides them), captioned with what the tallest bar is worth so the chart
carries a scale rather than being pure shape. Each day gets an equal slot
with the bar centred in it (`_DAILY_BAR_FILL`, 0.5 — the chart spans the full
width however skinny the bars are; much below 0.4 and the thinnest bars start
dropping out on the panel's 1-bit dither) from `daily_totals()` — the shape of the fortnight, which
four weekly totals cannot show, since a steady fortnight and one enormous
Tuesday have the same average. Bars are scaled to the tallest day in the
window (the question is which days were heavy relative to each other, not
against any absolute number), combine input+output the way the weekly
history does, and exclude cache reads — including them would make it a chart
of cache reads. The last bar is today and is drawn hollow, because a short
solid bar would read as a quiet day rather than an early hour. It comes
straight from the transcripts rather than `stats_history`, so it stays
correct for days the device spent asleep.

The tile also carries the last four completed weeks in tokens with their
average (`weekly_totals` / `weekly_baseline`, one scan feeding both), and a
"used N%" line for the week that repeats this week as a single combined
figure — the 5 h / 7 d rows split input from output, while the weekly
history is combined totals, and comparing them shouldn't require mental
arithmetic. There is no local
source for the real weekly limit, so the yardstick is either
`claude_weekly_token_budget` if you set one, or — at 0 — your own average
over the preceding 4 weeks, and the line says which ("of budget" vs "of a
usual week"). Weeks with no activity are left out of that average: an idle
week isn't evidence of a light workload, and averaging zeros in would make
any active week look enormous.

Cache reads are reported separately from `sent` (input + cache creation):
they dwarf everything else and bill at a fraction of the rate, so folding
them together would make the number meaningless. The scan takes about a
second over ~130 MB, so it is cached for `terminal_claude_usage_ttl` seconds
and only ever runs as the panel goes to sleep. Every part of it is
defensive — the panel is decoration, and nothing about it may stop the
display sleeping.

## Refresh Behavior & Debugging Flashes

Every whole-panel write records *why* it fired — `clear`, `periodic`,
`heavy-redraw`, `force-refresh`, `screensaver`, `help-sheet`, and the
driver's own `baseline` / `partial-limit` / `region-fallback`:

```
INFO root: E-ink full flash refresh (reason=clear, deep=True, hw_sleeping=False, 3/min)
```

The rolling per-minute rate is in the log line and in the refresh HUD, with a
breakdown of the top causes — "it flashes too much" is otherwise only
diagnosable by correlating timestamps by hand. The knobs behind all of this
live in the settings page's **Refresh & Ghosting** section.

Two rules worth knowing before touching `_refresh_kind`:

- A **scroll is not a redraw.** pyte marks every line dirty when it scrolls,
  so treating an all-dirty frame as a near-total redraw made every scrolled
  line flash the panel. Scrolling is flagged separately (`scrolled` on the
  tracked screen), and with flicker-free partials a near-total redraw needs
  no resync flash at all — the DU waveform rewrites every pixel against the
  previous frame.
- A **deep flash is for a real clear.** `epd.Clear()` plus `epd.display()`
  write identical bytes for a blank frame, so a clear would flash twice for
  one wipe; `_frame_is_blank` skips the redundant one (dark mode and
  content-heavy frames still get it).

## Terminal Conformance Tests

pyte drops any CSI final byte it doesn't implement — no exception, no log
line. That is how `clear` stayed broken: tmux spells it SU (`ESC[22S`), pyte
ignored it, and the panel kept showing text the user had just cleared while
every test stayed green.

`tools/record_pty_stream.py` records both halves of the contract from a real
run — every byte written to the PTY, and what tmux says its pane contained
afterwards (`capture-pane -p`) — into `tests/fixtures/pty_streams/`.
`tests/test_terminal_conformance.py` replays them and diffs the grid, so CI
needs neither tmux nor a terminal.

```bash
python tools/record_pty_stream.py scrolling -- ls -la /etc   # what tmux forwards
python tools/record_pty_stream.py raw_htop --raw -- htop     # the program's own escapes
python tools/record_pty_stream.py grow --resize 100x30 -- ls # grid changes mid-draw
```

`--raw` records the non-tmux input path and still uses tmux as the oracle by
`cat`ting the bytes into a pane (with `stty -echo`, or a TUI that queries the
terminal gets tmux's *reply* echoed into the grid instead of its own text).
`--resize` moves the grid partway through the stream — what F9/F12 do to a
program that is already drawing — and is stored as a marker in the chunk list
so the replay resizes at the same point.

Two guards keep the suite honest: `test_harness_catches_a_dropped_escape`
requires that fixtures exercising SU/SD *fail* under stock pyte, and
`test_corpus_still_exercises` requires the corpus to keep covering SU, ED,
EL, CUP, DECSTBM, the alternate screen and SGR. A fixture can stop covering
a sequence silently — a program changes how it draws, someone re-records on
a different tmux — and without those the suite narrows without anyone
noticing.

## Waveshare Driver

`src/waveshare_epd/` contains the Pi hardware driver for the Waveshare panel.
On macOS the driver import is skipped — `output/terminal.bmp` is the preview.

## Dev Tips

- On macOS `main.py` auto-detects Darwin and skips hardware push.
- Edit `src/render.py` → re-run `--once --local` → open `output/terminal.bmp`.
- `src/system_stats.py` can be tested standalone: `poetry run python src/system_stats.py`
- To add a new panel: add a section in `render.py` and a toggle key in `config.yaml`.
