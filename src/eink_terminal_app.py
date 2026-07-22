"""
E-ink terminal emulator core.

Features:
  - tmux auto-session: attaches to or creates a named tmux session so shell
    state survives app restarts (config: terminal_use_tmux, terminal_tmux_session)
  - Scrollback: PgUp/PgDn scroll through pyte history (without tmux)
  - Idle screensaver: switches to stats after N seconds of no input
  - Status bar extras: shows current time, working directory, and git branch
  - Alert overlays: system alerts (high CPU, low disk, SSH logins) appear in
    the status bar without covering terminal content
  - Split view: 600px terminal + 200px live stats sidebar
  - Session logging: optionally persist each tab's output (ANSI-stripped) to
    a rotating file on disk, surviving idle-reset/shell-exit (config:
    terminal_log_enabled, terminal_log_dir, terminal_log_max_bytes,
    terminal_log_max_files) — see src/session_logger.py

On-display config editor:
  Open it by typing `settings` (or `eink`) at the shell prompt, or via F6 →
  ⚙ Settings. The `settings` command signals this process (SIGUSR1) to pop the
  editor over the terminal — see _install_command_scripts / _on_settings_signal.

Hotkeys:
  F6        — command palette (first entry opens the on-display config editor)
  F9        — decrease font size (−2 pt)
  F12       — increase font size (+2 pt)
  F10       — force full display refresh (clear ghosting)
  F11       — switch to stats dashboard
  PgUp      — scroll up through history (no-tmux mode only)
  PgDn      — scroll down / return to live
  Ctrl+C    — kill foreground process (forwarded normally)
  Ctrl+/    — help overlay: every hotkey, one line each; ↑↓ to browse,
              Enter to run the selected one (new tab, close tab, toggle
              split, next/prev tab, ...), Esc to close without acting
  Alt+1..9  — jump straight to tab N
  Ctrl+Space — copy mode: arrows move a cursor over the visible screen,
              Space drops an anchor, Enter yanks the anchor→cursor range (or
              the whole line under the cursor with no anchor) into the F8
              clipboard and beams it to a QR for phone copy. Esc cancels.
              See _toggle_copy_mode / _handle_copy_key / _copy_confirm.

EinkTerminal's ~130 methods are split across mixin modules by feature area
(shell/PTY, hotkeys, tabs, overlay pickers, settings, palette/help, text
actions, search, split pane, network, markdown viewer) — see shell_mixin.py,
hotkeys_mixin.py, tabs_mixin.py, picker_overlays_mixin.py, settings_mixin.py,
palette_help_mixin.py, text_actions_mixin.py, search_mixin.py,
split_pane_mixin.py, network_mixin.py, markdown_viewer_mixin.py. This file
keeps __init__, the
render/idle/refresh state machine, and the main select() loop — the pieces
most tightly coupled to per-frame timing. Shared constants, the per-tab
dataclass, and small pure helpers live in terminal_state.py so both this
file and the mixins can import them without a circular import.
"""
from __future__ import annotations

import logging
import os
import queue as _queue
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from PIL import ImageDraw

from alert_monitor import AlertMonitor
from display_eink import EinkDriver
from evdev_input import EvdevKeyboard, find_keyboard
from hotkeys_mixin import HotkeysMixin
from markdown_viewer_mixin import MarkdownViewerMixin
from network_mixin import NetworkMixin
from palette_help_mixin import PaletteHelpMixin
from picker_overlays_mixin import PickerOverlaysMixin
from preview_server import start_if_enabled as _start_preview
from sd_watchdog import Watchdog
from search_mixin import SearchMixin
from settings_mixin import SettingsMixin
from shell_mixin import ShellMixin
from split_pane_mixin import SplitPaneMixin
from tabs_mixin import TabsMixin
from terminal_renderer import (
    SPLIT_TERMINAL_W,
    _find_mono_font,
    render_mini_stats,
    render_screen,
    render_screen_partial,
    render_split_lr,
    terminal_dimensions,
)
from terminal_state import (
    _HELP_ITEMS,
    _POWER_ITEMS,
    _RENDER_DEBOUNCE,
    _REPO_ROOT,
    _STATS_UPDATE_SEC,
    _STATUS_CACHE_TTL,
    _filter_pty_output,
    _get_local_ip,
    _get_uptime,
    _Tab,
)
from text_actions_mixin import TextActionsMixin

logger = logging.getLogger(__name__)


class EinkTerminal(
    ShellMixin,
    HotkeysMixin,
    TabsMixin,
    PickerOverlaysMixin,
    SettingsMixin,
    PaletteHelpMixin,
    TextActionsMixin,
    SearchMixin,
    SplitPaneMixin,
    NetworkMixin,
    MarkdownViewerMixin,
):
    """Runs a shell in a PTY and mirrors output to the e-ink display."""

    def __init__(self, config: dict, local: bool = False):
        self._config    = config
        # local=True means dev preview (macOS / --local): no real e-ink push.
        # local=False is the live/production display on real hardware.
        self._local     = local
        self._font_size = config.get('terminal_font_size', 14)
        self._font_path = config.get('terminal_font_path', '')
        self._dark_mode = config.get('terminal_dark_mode', config.get('dark_mode', False))
        self._full_refresh_interval = config.get('terminal_full_refresh_interval', 300)
        # Smart flash: once the interval elapses, wait for this many seconds of
        # no typing before the whole-panel flash (so it never interrupts active
        # use); force it anyway at 2× the interval.
        self._flash_idle_gap = config.get('terminal_flash_idle_gap', 30)
        self._needs_periodic_flash = False   # set after a content partial
        # Idle → screensaver + panel deep-sleep. screensaver_sleep_minutes (in
        # minutes, editable on-device) takes precedence over the legacy seconds.
        _sleep_min = config.get('screensaver_sleep_minutes')
        self._idle_timeout = (int(_sleep_min) * 60 if _sleep_min is not None
                              else config.get('terminal_idle_timeout', 0))
        # Earlier panel deep-sleep: power the panel down (image retained) after a
        # shorter idle window, WITHOUT showing the screensaver yet. The screensaver
        # still waits for _idle_timeout above. 0 = disable the early sleep.
        _disp_sleep = config.get('display_sleep_minutes')
        self._sleep_timeout = int(_disp_sleep) * 60 if _disp_sleep is not None else 0
        # While the screensaver is showing the panel is deep-slept and never
        # repainted. Re-flash it every screensaver_refresh_minutes so a static
        # image doesn't slowly burn in / ghost over a long idle, and so the
        # screensaver reclaims the panel if anything else drew to it meanwhile.
        # 0 = never refresh (show once, then leave the panel alone until input).
        self._screensaver_refresh = int(config.get('screensaver_refresh_minutes', 120) or 0) * 60
        # Idle reset: after a longer idle window, kill the shell/tmux session and
        # start a brand-new one, so whoever returns gets a clean terminal.
        # terminal_reset_minutes (minutes; 0 = never). Fires once per idle period.
        self._reset_timeout = int(config.get('terminal_reset_minutes', 60) or 0) * 60
        self._did_idle_reset = False
        self._busy_check_mono = 0.0
        self._reset_deferred_busy = False
        # Set by the `clear-eink` shell command (SIGUSR2); handled in the loop.
        self._clear_requested = False
        # Set by the `notes`/`llmchat`/`terminal` shell commands (real-time
        # signals SIGRTMIN+1/+2/+3) so a mode can be switched to by typing a
        # command from any shell tab, not just Ctrl+N/F6. Handled in the loop.
        self._notes_requested = False
        self._llm_chat_requested = False
        self._terminal_requested = False
        self._split_view  = config.get('terminal_split_view', False)
        self._status_extras  = config.get('terminal_status_bar_extras', True)
        self._cursor_style   = config.get('terminal_cursor_style', 'block')

        # Custom shell prompt (PS1) injected after the shell's rc loads. Each
        # part can be toggled on/off from config / the on-display editor.
        self._prompt_custom    = config.get('terminal_prompt_custom', False)
        self._prompt_show_user = config.get('terminal_prompt_show_user', True)
        self._prompt_show_host = config.get('terminal_prompt_show_host', True)
        self._prompt_show_cwd  = config.get('terminal_prompt_show_cwd', True)
        self._prompt_show_git  = config.get('terminal_prompt_show_git', True)
        self._prompt_symbol    = config.get('terminal_prompt_symbol', '$')

        # Where new shells start: 'home', 'root', 'last' (resume previous dir),
        # or an explicit path. 'last' is persisted to data/last_cwd.txt.
        self._start_dir_pref = config.get('terminal_start_dir', 'home')

        # Session logging: persist each tab's output (ANSI stripped) to a
        # rotating file under _log_dir, so scrollback survives idle-reset /
        # shell-exit and can be grepped later. Off by default.
        self._log_enabled   = config.get('terminal_log_enabled', False)
        self._log_dir       = (config.get('terminal_log_dir', '')
                               or os.path.join(_REPO_ROOT, 'data', 'terminal_logs'))
        self._log_max_bytes = int(config.get('terminal_log_max_bytes', 0) or 1_000_000)
        self._log_max_files = int(config.get('terminal_log_max_files', 0) or 40)
        self._tab_log_seq   = 0

        # tmux
        self._use_tmux     = config.get('terminal_use_tmux', False) and bool(shutil.which('tmux'))
        self._tmux_session = config.get('terminal_tmux_session', 'eink')
        # Wake on SSH: typing in any attached tmux client (e.g. `tmux attach`
        # over SSH) counts as user input — it wakes the panel from deep sleep /
        # screensaver and keeps it awake while the remote session is active.
        self._wake_on_ssh        = config.get('terminal_wake_on_ssh', True)
        self._tmux_activity_seen = 0.0   # newest #{client_activity} handled
        self._tmux_poll_mono     = 0.0   # last poll (throttled to every 2 s)

        self._driver      = EinkDriver(local=local,
                                       partial_refresh_limit=config.get('partial_refresh_before_full', 30),
                                       flicker_free=config.get('terminal_flicker_free_partial', False),
                                       region_flash=config.get('terminal_region_flash', True),
                                       du_adaptive=config.get('terminal_du_adaptive', True),
                                       du_frames_text=config.get('terminal_du_frames_text', 0x14),
                                       du_frames_heavy=config.get('terminal_du_frames_heavy', 0x1A),
                                       du_heavy_threshold=config.get('terminal_du_heavy_threshold', 0.22))
        self._screen      = None
        self._stream      = None
        self._pty_master  = None
        self._child_pid   = None
        self._running     = False
        self._last_image      = None
        self._img_cache       = None   # cached 800×480 image for incremental renders
        self._last_cursor_row = None   # cursor row at last render
        self._last_start_row  = 0      # viewport start row at last render
        self._stdin_fd    = sys.stdin.fileno()
        self._old_tty     = None
        # True once stdin hits EOF (app launched detached / stdin = /dev/null).
        # Without this, select() reports the EOF fd ready every iteration and the
        # loop spins at 100% CPU while resetting the idle timer — the panel never
        # sleeps. Once set, we stop watching stdin and rely on evdev/web input.
        self._stdin_eof   = False

        # Status bar item visibility. 'host' is the machine name shown alongside
        # the working directory; falls back to the system hostname.
        _host = config.get('device_label', '') or socket.gethostname()
        self._bar_config = {
            'show_host':  config.get('terminal_status_bar_show_host',  True),
            'show_time':  config.get('terminal_status_bar_show_time',  True),
            'show_cwd':   config.get('terminal_status_bar_show_cwd',   True),
            'show_ip':    config.get('terminal_status_bar_show_ip',    True),
            'show_speed': config.get('terminal_status_bar_show_speed', True),
            'show_uptime': config.get('terminal_status_bar_show_uptime', True),
            'host':       _host,
        }

        # Status bar is deprioritized: it is only repainted (and thus refreshed
        # on the panel) at most once per this interval, so frequent time/net/
        # uptime ticks don't drive a display refresh every few seconds.
        self._status_bar_interval = config.get('terminal_status_bar_interval', 300)
        self._last_status_render  = 0.0   # monotonic of last status-bar repaint
        self._status_force        = False # set when an alert change must update now

        # evdev keyboard (preferred over stdin when a desktop is running)
        kbd_path = config.get('terminal_keyboard_device', 'auto')
        prefer_bt = config.get('terminal_keyboard_prefer_bluetooth', False)
        self._kbd_path = kbd_path if kbd_path != 'auto' else ''
        self._prefer_bt = prefer_bt
        self._last_kbd_probe = 0.0
        _dev = find_keyboard(self._kbd_path, prefer_bt)
        self._evdev_kb: EvdevKeyboard | None = EvdevKeyboard(_dev) if _dev else None
        if self._evdev_kb:
            logger.info('Using evdev keyboard: %s', _dev.path)
        else:
            logger.info('evdev keyboard not found — will retry on hot-plug')

        # Scrollback state (only when not using tmux)
        self._scroll_pages = 0

        # Idle tracking. _last_activity tracks ANY activity including terminal
        # output (used for flash timing and pausing stats updates). _last_input
        # tracks only real user input (keyboard / web) and drives the idle
        # screensaver + panel deep-sleep — otherwise a program that prints
        # periodically (spinner, htop, log tail) would keep the panel awake.
        self._last_activity = time.monotonic()
        self._last_input = time.monotonic()
        self._last_full_refresh_mono = time.monotonic()

        # Status bar extras cache
        self._status_cache: tuple = None   # (timestamp, time_str, cwd, branch)

        # Alerts
        self._hq_render    = config.get('terminal_hq_render', True)
        self._paste_file   = os.path.expanduser(
            config.get('terminal_paste_file', '~/eink-paste.txt')
        )
        self._alert_monitor = AlertMonitor(config)
        self._web_input_queue = None   # set in run() when preview server starts

        # Split-view stats
        self._stats_data: dict = None
        self._stats_dirty  = False
        self._stats_lock   = threading.Lock()

        # Screensaver cycle state
        self._screensaver_cycle_idx  = 0
        self._screensaver_last_cycle = 0.0
        self._screensaver_is_cycle   = False  # set by _show_screensaver per rotation set
        self._screensaver_show_mono  = 0.0   # when screensaver was last shown (for grace period)

        # Text message (send-to-display) state
        self._in_text_message = False
        self._display_queue = None   # set in run() after server starts
        self._preview_server = None  # set in run() after server starts

        # Tab management
        self._tabs: list = []
        self._active_tab: int = 0

        # SSH bookmarks picker
        self._sshpick_active = False
        self._sshpick_items: list = []
        self._sshpick_hosts: list = []
        self._sshpick_idx: int = 0

        # Process kill overlay (F3)
        self._prockill_active = False
        self._prockill_items: list = []
        self._prockill_pids: list = []
        self._prockill_idx: int = 0

        # Service manager overlay (F4)
        self._svcmgr_active = False
        self._svcmgr_items: list = []
        self._svcmgr_names: list = []
        self._svcmgr_idx: int = 0

        # Power menu (F5)
        self._power_active = False
        self._power_idx: int = 2  # Cancel selected by default

        # Help overlay (Ctrl+/)
        self._help_active = False
        self._help_idx: int = 0

        # Command palette
        self._palette_active = False
        self._palette_items: list = []
        self._palette_idx: int = 0

        # On-display config editor (Settings overlay)
        self._settings_active = False
        self._settings_idx: int = 0
        self._settings_pending: dict = {}   # key -> staged value (not yet saved)
        # Set by the SIGUSR1 handler when the `settings` shell command is run, so
        # the editor can be opened by typing a command (not just F6). Handled in
        # the main loop — the handler itself only flips the flag (signal-safe).
        self._open_settings_requested = False

        # Snippets picker (saved_commands.txt only — curated, no history)
        self._snippets_active = False
        self._snippets_items: list = []
        self._snippets_idx: int = 0

        # Refresh-stats debug HUD (toggled from the command palette)
        self._show_refresh_hud = False

        # "Big text" momentary read mode — any key restores the prior font.
        self._big_text_active = False
        self._big_text_prev_font: int = 0

        # Markdown viewer (F6 > "View notes as Markdown") — a paginated,
        # rendered-not-raw view of the notes file. PgUp/PgDn flip pages, any
        # other key closes back to the terminal. See markdown_viewer_mixin.py.
        self._markdown_active = False
        self._markdown_pages: list = []
        self._markdown_page_idx: int = 0

        # Beam-to-phone: a pinned QR linking to the captured screen text.
        self._beam_url: str = ''
        self._beam_until_mono: float = 0.0

        # Clipboard picker
        self._clipboard: list = []
        self._clipboard_idx: int = 0
        self._clipboard_active: bool = False
        self._clipboard_path = os.path.join(_REPO_ROOT, 'data', 'clipboard.json')
        self._clipboard = self._load_clipboard()

        # Scrollback search (Ctrl+F)
        self._search_active: bool = False
        self._search_query: str = ''
        # Each entry: (display_text, is_history, history_idx)
        self._search_results: list = []
        self._search_idx: int = 0

        # Tab rename overlay
        self._rename_active: bool = False
        self._rename_query: str = ''

        # Copy mode (Ctrl+Space): char-wise on-screen text selection. Cursor
        # moves with arrows over the currently visible screen; Space drops an
        # anchor; Enter yanks the anchor→cursor range (or the whole line under
        # the cursor if no anchor was set) into the clipboard + beam QR.
        self._copy_active: bool = False
        self._copy_row: int = 0
        self._copy_col: int = 0
        self._copy_anchor: tuple | None = None

        # URL QR overlay
        self._last_url: str = ''
        self._show_url_qr: bool = True

        # Network stats (IP + speeds), updated by background thread
        self._net_stats: dict = {}
        self._net_stats_lock = threading.Lock()

        self._init_screen()

        # Install the `settings`/`eink` shell commands so the on-display config
        # editor can be opened by typing, not just F6. Must run before the shell
        # is spawned so the child (and tmux) inherit the updated PATH.
        self._install_command_scripts()
    def _force_full_refresh(self):
        if self._last_image is not None:
            self._driver.flash_refresh(self._last_image)
            self._last_full_refresh_mono = time.monotonic()
    def _clear_screen(self):
        """Clear the active terminal's screen + scrollback and ghost-clear the
        panel, leaving the running shell intact (the `clear-eink` command)."""
        try:
            if self._use_tmux:
                subprocess.run(
                    ['tmux', 'clear-history', '-t', self._tmux_session],
                    capture_output=True, timeout=1,
                )
        except Exception:
            pass
        try:
            self._screen.reset()
        except Exception:
            pass
        self._scroll_pages = 0
        # Ctrl+L: ask the shell's line editor to repaint a clean prompt at the top.
        if self._pty_master is not None:
            try:
                os.write(self._pty_master, b'\x0c')
            except OSError:
                pass
        self._render(force_full=True)
        if self._last_image is not None:
            self._driver.flash_refresh(self._last_image)
        self._last_full_refresh_mono = time.monotonic()
    def _reset_session(self, render: bool = True):
        """Kill the shell (and tmux session/tabs) and start a brand-new one, so a
        returning user gets a fresh terminal. Used by the idle auto-reset."""
        logger.info('Idle reset — starting a fresh shell')
        # Tear down every tab's child + any per-tab tmux sessions.
        for tab in self._tabs:
            if tab.logger:
                tab.logger.close()
            if tab.child_pid:
                try: os.kill(tab.child_pid, signal.SIGTERM)
                except (ProcessLookupError, OSError): pass
            if tab.pty_master is not None and tab.pty_master >= 0:
                try: os.close(tab.pty_master)
                except OSError: pass
            if self._use_tmux and tab.tmux_session:
                try:
                    subprocess.run(['tmux', 'kill-session', '-t', tab.tmux_session],
                                   capture_output=True, timeout=2)
                except Exception:
                    pass
            if tab.split_dir:
                if tab.pane2_pid:
                    try: os.kill(tab.pane2_pid, signal.SIGTERM)
                    except (ProcessLookupError, OSError): pass
                if tab.pane2_master >= 0:
                    try: os.close(tab.pane2_master)
                    except OSError: pass
        if self._use_tmux:
            try:
                subprocess.run(['tmux', 'kill-session', '-t', self._tmux_session],
                               capture_output=True, timeout=2)
            except Exception:
                pass
        # Fresh screen + shell, collapsed back to a single tab.
        self._init_screen()
        self._spawn_shell()
        self._tabs = [_Tab(screen=self._screen, stream=self._stream,
                           pty_master=self._pty_master, child_pid=self._child_pid,
                           tmux_session=self._tmux_session, logger=self._make_tab_logger())]
        self._active_tab = 0
        self._scroll_pages = 0
        if render:
            self._render(force_full=True)

    def _show_text_message(self, text: str, label: str = ''):
        """Display custom text on the e-ink screen (from web /message endpoint)."""
        try:
            from render import render_text_message
            img = render_text_message(text, label, self._config)
            self._driver.full_refresh(img)
            self._last_image = img
            self._in_text_message = True
            self._screensaver_show_mono = time.monotonic()
        except Exception as e:
            logger.warning('Text message render error: %s', e)

    def _switch_to_stats(self):
        self._running = False
        for tab in self._tabs:
            if tab.child_pid:
                try:
                    os.kill(tab.child_pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
        if self._child_pid:
            try:
                os.kill(self._child_pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        main_py = os.path.join(_REPO_ROOT, 'main.py')
        subprocess.Popen(
            [sys.executable, main_py],
            close_fds=True,
            start_new_session=True,
        )

    def _sleep_panel(self):
        """Deep-sleep the e-ink panel without changing what's on it.

        Powers the panel down (no current draw) while retaining the current
        terminal image. The next full/flash refresh — on user input or when the
        screensaver finally kicks in — wakes it automatically. Unlike
        _show_screensaver this does not render anything, so the terminal stays
        visible behind the dark glass until someone returns.
        """
        try:
            self._driver.sleep()
            logger.info('Panel deep-sleep — image retained, awaiting input or screensaver')
        except Exception as e:
            logger.warning('Panel sleep error: %s', e)

    def _show_screensaver(self):
        """Render the screensaver to the display.

        In 'cycle' mode, advances through gallery photos every N minutes.
        In 'static' mode (default), always shows the gallery-selected image.
        Always shows a QR code overlay pointing to the preview server.
        """
        try:
            from preview_server import get_screensaver_images
            from render import render_screensaver

            static_path = self._config.get('screensaver_image_path', 'assets/test.jpg')
            if not os.path.isabs(static_path):
                static_path = os.path.join(_REPO_ROOT, static_path)
            photos_dir = os.path.join(_REPO_ROOT, 'assets', 'gallery')

            # Resolve the rotation set: 2+ selected photos cycle, 1 shows static,
            # none falls back to screensaver_mode / the static image.
            names, is_cycle = get_screensaver_images(photos_dir, self._config)
            self._screensaver_is_cycle = is_cycle and len(names) >= 2
            if names:
                if self._screensaver_is_cycle:
                    cycle_secs = self._config.get('screensaver_cycle_interval', 5) * 60
                    now = time.monotonic()
                    if self._screensaver_last_cycle == 0.0:
                        # First activation: show current photo without advancing.
                        self._screensaver_last_cycle = now
                    elif (now - self._screensaver_last_cycle) >= cycle_secs:
                        self._screensaver_cycle_idx += 1
                        self._screensaver_last_cycle = now
                    image_path = os.path.join(photos_dir, names[self._screensaver_cycle_idx % len(names)])
                else:
                    image_path = os.path.join(photos_dir, names[0])
            else:
                image_path = static_path

            port = self._config.get('preview_server_port', 8080)
            ip = _get_local_ip()
            qr_url = f'http://{ip}:{port}/config' if ip else ''

            img = render_screensaver(image_path, qr_url, self._config)
            # Must be a flash (ordered _FULL task): full_refresh(flash=False) only
            # sets _pending_partial, which the sleep() below immediately cancels —
            # the screensaver would never reach the panel.
            self._driver.flash_refresh(img)
            self._last_image = img
            logger.info('Screensaver activated — img=%s cycle=%s',
                        os.path.basename(image_path), self._screensaver_is_cycle)
        except Exception as e:
            logger.warning('Screensaver render error: %s', e)
        finally:
            # Even if the render failed, record the activation and power the
            # panel down — otherwise the loop believes the screensaver is up
            # while the panel stays awake on a stale frame.
            self._screensaver_show_mono = time.monotonic()
            self._driver.sleep()   # wakes automatically on next full_refresh

    # ─── SSH / tmux input detection ───────────────────────────────────────────

    def _tmux_input_seen(self, now: float) -> bool:
        """True when an attached tmux client sent input since the last check.

        This is how SSH'ing in and typing into the shared tmux session wakes
        the e-ink: #{client_activity} bumps only on client *input* (never on
        program output), so a spinner or log tail can't hold the panel awake.
        Polled at most every 2 s, and skipped while local input is fresh
        (local keys already reset the idle timer)."""
        if not (self._use_tmux and self._wake_on_ssh):
            return False
        if (now - self._tmux_poll_mono) < 2.0 or (now - self._last_input) < 2.0:
            return False
        self._tmux_poll_mono = now
        try:
            r = subprocess.run(['tmux', 'list-clients', '-F', '#{client_activity}'],
                               capture_output=True, text=True, timeout=1)
            newest = max((float(s) for s in r.stdout.split()), default=0.0)
        except Exception:
            return False
        if newest <= 0.0:
            return False
        if self._tmux_activity_seen == 0.0:
            self._tmux_activity_seen = newest    # baseline on first poll
            return False
        if newest > self._tmux_activity_seen:
            self._tmux_activity_seen = newest
            return True
        return False

    # ─── Status bar info ──────────────────────────────────────────────────────

    def _get_status_info(self) -> tuple:
        """Return (time_str, cwd, git_branch, uptime), cached for _STATUS_CACHE_TTL seconds."""
        if not self._status_extras:
            return None
        now = time.monotonic()
        if self._status_cache and now - self._status_cache[0] < _STATUS_CACHE_TTL:
            return self._status_cache[1:]

        import datetime
        time_str = datetime.datetime.now().strftime('%H:%M')
        cwd = self._get_cwd()
        branch = self._get_git_branch(cwd) if cwd else ''
        uptime = _get_uptime()
        self._status_cache = (now, time_str, cwd, branch, uptime)
        return time_str, cwd, branch, uptime

    def _get_cwd(self) -> str:
        try:
            if self._use_tmux:
                r = subprocess.run(
                    ['tmux', 'display-message', '-p', '-t', self._tmux_session,
                     '#{pane_current_path}'],
                    capture_output=True, text=True, timeout=0.5,
                )
                cwd = r.stdout.strip()
            elif self._child_pid:
                cwd = os.readlink(f'/proc/{self._child_pid}/cwd')
            else:
                return ''
        except Exception:
            return ''
        home = os.path.expanduser('~')
        return ('~' + cwd[len(home):]) if cwd.startswith(home) else cwd

    def _get_git_branch(self, cwd: str) -> str:
        try:
            r = subprocess.run(
                ['git', '-C', cwd, 'branch', '--show-current'],
                capture_output=True, text=True, timeout=0.5,
            )
            return r.stdout.strip()
        except Exception:
            return ''

    # ─── Split-view stats thread ──────────────────────────────────────────────

    def _start_stats_thread(self):
        def _loop():
            sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))
            from system_stats import collect as _collect
            while self._running:
                try:
                    data = _collect(self._config)
                    with self._stats_lock:
                        self._stats_data = data
                        self._stats_dirty = True
                except Exception as e:
                    logger.warning('Stats update error: %s', e)
                time.sleep(_STATS_UPDATE_SEC)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()

    # ─── Rendering ───────────────────────────────────────────────────────────

    def _refresh_kind(self, force_full: bool, force_flash: bool,
                      heavy_change: bool) -> str:
        """Pick the panel update for this frame:
          'full'    — clean whole-screen repaint, no flash (overlays, resize).
          'flash'   — whole-panel ghost-clearing flash: the deferred periodic
                      flash (force_flash), or to resync a near-total redraw.
          'partial' — incremental update (the driver region-flashes changed rows
                      on its own count-based cadence)."""
        if force_full:
            return 'full'
        if force_flash or heavy_change:
            return 'flash'
        return 'partial'

    def _periodic_flash_due(self, now: float = None) -> bool:
        """True when the deferred whole-panel ghost-clearing flash should fire:
        the interval has elapsed since the last full refresh, there's been
        partial activity since, and we're either in a quiet (no-typing) gap or
        past 2× the interval (forced so it can't be starved by constant typing)."""
        if not self._needs_periodic_flash or self._full_refresh_interval <= 0:
            return False
        if now is None:
            now = time.monotonic()
        since = now - self._last_full_refresh_mono
        if since < self._full_refresh_interval:
            return False
        quiet = (now - self._last_activity) >= self._flash_idle_gap
        return quiet or since >= 2 * self._full_refresh_interval

    def _draw_refresh_hud(self, img):
        """Overlay a small debug box of live refresh counters (top-left)."""
        s = self._driver.stats()
        age = s.get('last_flash_age')
        lines = [
            'REFRESH HUD',
            f"part {s['partial']}  reg {s['region']}  full {s['full']}",
            f"bytes {s['bytes']}  du {s['du_frames']}f  font {self._font_size}",
            f"last flash {int(age)}s ago" if age is not None else 'last flash --',
        ]
        fg = 255 if self._dark_mode else 0
        bg = 0 if self._dark_mode else 255
        draw = ImageDraw.Draw(img)
        font = _find_mono_font('', 11)
        pad, lh, x0, y0 = 4, 13, 4, 4
        w = max(int(draw.textlength(ln, font=font)) for ln in lines) + pad * 2
        h = lh * len(lines) + pad * 2
        draw.rectangle([x0, y0, x0 + w, y0 + h], fill=bg, outline=fg)
        y = y0 + pad
        for ln in lines:
            draw.text((x0 + pad, y), ln, font=font, fill=fg)
            y += lh

    def _render(self, force_full: bool = False, force_flash: bool = False):
        tw = SPLIT_TERMINAL_W if self._split_view else 800
        alerts = self._alert_monitor.active()
        if self._copy_active:
            hint = ('mark set — ' if self._copy_anchor else '') + \
                   'COPY MODE  ↑↓←→ move · Space mark · Enter yank · Esc cancel'
            alerts = [hint]

        # The status bar is deprioritized: only repaint it on a full render, when
        # the throttle interval has elapsed, or when an alert change forces it.
        # Otherwise the cached status-bar pixels are left untouched so it never
        # triggers a (frequent, oversized) partial refresh of its own.
        now_m = time.monotonic()
        draw_status = (force_full or self._status_force or
                       (now_m - self._last_status_render) >= self._status_bar_interval)
        if draw_status:
            self._last_status_render = now_m
            self._status_force = False

        # Scan only the rows that changed since the last render — a URL can
        # only appear on a changed line, and a full-buffer scan per keystroke
        # is wasteful. Cold cache / full repaints still scan everything.
        dirty_rows = (None if force_full or self._img_cache is None
                      else self._screen.dirty)
        found = self._scan_for_url(dirty_rows)
        if found:
            self._last_url = found
        elif not self._last_url:
            _ip = _get_local_ip()
            _port = self._config.get('preview_server_port', 8080)
            if _ip:
                self._last_url = f'http://{_ip}:{_port}/config'
        # A fresh "beam to phone" QR takes precedence over the ambient URL QR.
        if self._beam_url and time.monotonic() < self._beam_until_mono:
            url_qr = self._beam_url
        else:
            self._beam_url = ''
            # Ambient QR overlay is dev-preview only: it obscures terminal
            # content underneath, so the live display keeps that space for
            # actual shell output instead.
            show_qr = (self._local and self._show_url_qr
                       and self._config.get('terminal_show_qr', True))
            url_qr = self._last_url if show_qr else None

        with self._net_stats_lock:
            net_stats = dict(self._net_stats) if self._net_stats else None

        if self._help_active:
            items = [self._format_help_row(label, keys) for label, keys in _HELP_ITEMS]
            overlay = (items, self._help_idx,
                       'Help  [Enter=run  ↑↓ navigate  Esc=close]')
        elif self._palette_active and self._palette_items:
            overlay = (self._palette_items, self._palette_idx, 'Commands')
        elif self._snippets_active and self._snippets_items:
            overlay = (self._snippets_items, self._snippets_idx,
                       'Snippets  [Enter=run  Esc=cancel]')
        elif self._clipboard_active and self._clipboard:
            overlay = (
                [c.get('label', c.get('text', '')) for c in self._clipboard],
                self._clipboard_idx, 'Clipboard',
            )
        elif self._prockill_active and self._prockill_items:
            overlay = (self._prockill_items, self._prockill_idx,
                       'Kill Process  [Enter=SIGTERM  Esc=cancel]')
        elif self._svcmgr_active and self._svcmgr_items:
            overlay = (self._svcmgr_items, self._svcmgr_idx,
                       'Services  [R=restart  S=stop  A=start  Esc=close]')
        elif self._power_active:
            overlay = (_POWER_ITEMS, self._power_idx, 'Power  [Enter to confirm]')
        elif self._settings_active:
            # Title doubles as a live prompt preview so you see the effect first.
            title = 'Settings  ·  prompt: %s  ·  ←→ change  Esc close' % self._prompt_preview()
            overlay = (self._settings_rows(), self._settings_idx, title)
        elif self._sshpick_active and self._sshpick_items:
            overlay = (self._sshpick_items, self._sshpick_idx,
                       'SSH Bookmarks  [Enter=connect  Esc=cancel]')
        elif self._search_active:
            items = ([r[0] for r in self._search_results]
                     if self._search_results else ['(no matches)'])
            q = self._search_query or ''
            n = len(self._search_results)
            title = (f'Find: {q}  [{n} match{"es" if n != 1 else ""}]  '
                     f'[Enter=jump  ↑↓ navigate  Esc=close]')
            overlay = (items, self._search_idx, title)
        elif self._rename_active:
            q = self._rename_query
            overlay = ([f'New name: {q}█'], 0,
                       'Rename Tab  [Enter=confirm  Esc=cancel  Backspace=delete]')
        else:
            overlay = None

        tab_bar = [(self._tab_title(t), i == self._active_tab)
                   for i, t in enumerate(self._tabs)] if self._tabs else None

        # ── Split-pane render (left/right, two separate PTYs) ─────────────────
        cur_tab = self._current_tab()
        if cur_tab and cur_tab.split_dir == 'h' and cur_tab.pane2_screen is not None:
            status_info = self._get_status_info()
            if status_info is not None:
                tab_str = self._tab_indicator()
                pane_str = '[L]' if cur_tab.pane_focus == 0 else '[R]'
                uptime = status_info[3] if len(status_info) > 3 else ''
                status_info = (status_info[0], status_info[1], status_info[2],
                               (pane_str + ' ' + tab_str).strip(), uptime)
            img = render_split_lr(
                self._screen,
                cur_tab.pane2_screen,
                focus=cur_tab.pane_focus,
                font_size=self._font_size,
                dark_mode=self._dark_mode,
                font_path=self._font_path,
                status_info=status_info,
                alerts=alerts if alerts else None,
                bar_config=self._bar_config,
                cursor_style=self._cursor_style,
            )
            self._img_cache = None   # split bypasses incremental cache
            self._last_cursor_row = self._screen.cursor.y
            self._last_image = img
            kind = self._refresh_kind(force_full, force_flash, heavy_change=False)
            if kind in ('full', 'flash'):
                self._driver.full_refresh(img)
                self._last_full_refresh_mono = time.monotonic()
                self._needs_periodic_flash = False
            else:
                self._driver.partial_refresh_diff(img)
                self._needs_periodic_flash = True
            return

        # Compute viewport start_row (for scroll detection).
        _, vis_rows, _, _ = terminal_dimensions(self._font_size, self._font_path, tw)
        start_row = (max(0, self._screen.cursor.y - vis_rows + 1)
                     if self._screen.cursor.y >= vis_rows else 0)

        # If almost the entire screen changed at once (clear, vim/less redraw,
        # tab switch), partial updates would leave the panel out of sync and
        # ghosting; resync with a full flash refresh instead.
        heavy_change = len(self._screen.dirty) >= max(8, int(vis_rows * 0.85))

        # Use incremental rendering when the cache is warm and no large change
        # (overlay, scroll, split sidebar) invalidates the full layout.
        use_incremental = (
            self._img_cache is not None
            and not force_full
            and overlay is None
            and not self._split_view
            and not self._show_refresh_hud   # HUD repaints a corner each frame
            and not self._copy_active        # copy-mode highlight needs a full repaint
            and start_row == self._last_start_row
        )

        # Status info spawns subprocesses (tmux cwd, git branch) — gather it
        # only when the status bar will actually be repainted this frame.
        status_info = None
        if draw_status or not use_incremental:
            status_info = self._get_status_info()
            if status_info is not None:
                tab_str = self._tab_indicator()
                uptime  = status_info[3] if len(status_info) > 3 else ''
                status_info = (status_info[0], status_info[1], status_info[2],
                               tab_str, uptime)

        if use_incremental:
            img = render_screen_partial(
                self._screen,
                self._img_cache,
                set(self._screen.dirty),
                self._last_cursor_row,
                start_row,
                self._font_size,
                dark_mode=self._dark_mode,
                font_path=self._font_path,
                terminal_width=tw,
                status_info=status_info,
                alerts=alerts if alerts else None,
                net_stats=net_stats,
                url_qr=url_qr,
                bar_config=self._bar_config,
                draw_status=draw_status,
                cursor_style=self._cursor_style,
            )
        else:
            # A full render always repaints the status bar; keep the throttle in sync.
            self._last_status_render = now_m
            img = render_screen(
                self._screen,
                self._font_size,
                dark_mode=self._dark_mode,
                font_path=self._font_path,
                terminal_width=tw,
                status_info=status_info,
                alerts=alerts if alerts else None,
                hq=self._hq_render,
                url_qr=url_qr,
                net_stats=net_stats,
                overlay=overlay,
                tab_bar=tab_bar,
                bar_config=self._bar_config,
                select=self._copy_render_range() if self._copy_active else None,
                cursor_style=self._cursor_style,
            )
            # Overlay split-view sidebar
            if self._split_view:
                with self._stats_lock:
                    stats = self._stats_data
                    self._stats_dirty = False
                render_mini_stats(img, stats, dark_mode=self._dark_mode)
            # Warm the cache for subsequent incremental renders.
            self._img_cache      = img
            self._last_start_row = start_row

        if self._show_refresh_hud:
            self._draw_refresh_hud(img)

        self._last_cursor_row = self._screen.cursor.y
        self._last_image = img
        kind = self._refresh_kind(force_full, force_flash, heavy_change)
        if kind == 'full':
            # Clean full repaint with no flash (overlays, font change, resize).
            self._driver.full_refresh(img)
            self._last_full_refresh_mono = time.monotonic()
            self._needs_periodic_flash = False
        elif kind == 'flash':
            # Deferred periodic ghost-clearing flash, or a resync flash for a
            # near-total redraw so it lands cleanly.
            self._driver.flash_refresh(img)
            self._last_full_refresh_mono = time.monotonic()
            self._needs_periodic_flash = False
        else:
            self._driver.partial_refresh_diff(img)
            self._needs_periodic_flash = True   # ghosting accrues until a flash

    # ─── Main entry point ─────────────────────────────────────────────────────

    def run(self):
        try:
            with open('/tmp/eink-terminal-active', 'w') as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

        # Let the `settings` shell command open the config editor (see
        # _install_command_scripts). The handler only flips a flag; the loop acts.
        try:
            signal.signal(signal.SIGUSR1, self._on_settings_signal)
            signal.signal(signal.SIGUSR2, self._on_clear_signal)
            # `notes`/`llmchat`/`terminal` shell commands (see
            # _install_command_scripts) — real-time signals since SIGUSR1/2 are
            # already spoken for by settings/clear-eink above.
            signal.signal(signal.SIGRTMIN + 1, self._on_notes_signal)
            signal.signal(signal.SIGRTMIN + 2, self._on_llm_chat_signal)
            signal.signal(signal.SIGRTMIN + 3, self._on_terminal_signal)
            # Graceful shutdown so a stray Ctrl+C / `systemctl stop` puts the
            # panel to sleep cleanly rather than crashing mid-refresh.
            signal.signal(signal.SIGINT, self._on_shutdown_signal)
            signal.signal(signal.SIGTERM, self._on_shutdown_signal)
        except (ValueError, OSError):
            pass  # not on the main thread (e.g. some test harnesses) — skip

        self._spawn_shell()
        self._enter_raw()
        if self._evdev_kb:
            self._evdev_kb.grab()
        self._running = True
        self._last_activity = time.monotonic()
        self._last_input = time.monotonic()

        # systemd watchdog: the unit sets WatchdogSec, so we must ping or get
        # SIGABRT'd ~every minute (which would full-refresh the panel on every
        # restart and reset the idle timer). No-ops off-systemd. See sd_watchdog.
        self._watchdog = Watchdog()
        self._watchdog.ready()

        # Wrap initial shell in a Tab
        self._tabs = [_Tab(screen=self._screen, stream=self._stream,
                           pty_master=self._pty_master, child_pid=self._child_pid,
                           logger=self._make_tab_logger())]
        self._active_tab = 0

        if self._split_view:
            self._start_stats_thread()

        self._start_network_monitor_thread()

        _config_path = os.path.join(_REPO_ROOT, 'config', 'config.yaml')
        server = _start_preview(self._config, os.path.join(_REPO_ROOT, 'output', 'terminal.bmp'),
                                photos_dir=os.path.join(_REPO_ROOT, 'assets', 'gallery'),
                                config_path=_config_path,
                                clipboard_path=self._clipboard_path)
        if server is not None:
            self._preview_server  = server
            self._web_input_queue = server.input_queue
            self._display_queue   = server.display_queue
        self._render(force_full=True)

        try:
            self._loop()
        finally:
            # Remember where the shell ended so terminal_start_dir: last can
            # resume there after a restart/reboot.
            self._save_last_cwd()
            try:
                os.unlink('/tmp/eink-terminal-active')
            except Exception:
                pass
            self._exit_raw()
            if self._evdev_kb:
                self._evdev_kb.ungrab()
            self._driver.sleep()
            if self._child_pid:
                try:
                    os.waitpid(self._child_pid, os.WNOHANG)
                except ChildProcessError:
                    pass

    def _evdev_disconnect(self):
        """Called when the evdev keyboard is removed."""
        try:
            self._evdev_kb.ungrab()
        except Exception:
            pass
        logger.info('evdev keyboard disconnected — watching for reconnect')
        self._evdev_kb = None
        self._last_kbd_probe = 0.0

    def _loop(self):
        last_render = 0.0
        has_pending = False
        last_alert_tick = 0.0
        in_screensaver = False
        panel_asleep = False   # panel deep-slept early (image retained, no screensaver yet)

        while self._running:
            now = time.monotonic()
            self._watchdog.ping(now)   # keep systemd from SIGABRT-restarting us

            # Hot-plug: probe for keyboard every 2 s when none is present
            if self._evdev_kb is None and (now - self._last_kbd_probe) >= 2.0:
                self._last_kbd_probe = now
                dev = find_keyboard(self._kbd_path, self._prefer_bt)
                if dev is not None:
                    self._evdev_kb = EvdevKeyboard(dev)
                    self._evdev_kb.grab()
                    logger.info('Hot-plugged keyboard: %s', dev.path)

            try:
                fds = []
                if self._evdev_kb is None:
                    if not self._stdin_eof:
                        fds.append(self._stdin_fd)
                else:
                    fds.append(self._evdev_kb.fileno())
                # Monitor ALL tab PTYs (primary + split pane secondary) so
                # background tabs and split panes stay current.
                for tab in self._tabs:
                    if tab.pty_master is not None and tab.pty_master >= 0:
                        fds.append(tab.pty_master)
                    if tab.pane2_master >= 0:
                        fds.append(tab.pane2_master)
                # Adaptive tick: poll fast only while output/renders are
                # pending or input was seen in the last couple of seconds;
                # otherwise relax so an idle terminal doesn't spin at 50 Hz.
                # select() still returns instantly on any fd activity, so
                # keystrokes and PTY output stay snappy.
                if has_pending or (now - self._last_activity) < 2.0:
                    timeout = _RENDER_DEBOUNCE
                elif in_screensaver or panel_asleep:
                    timeout = 1.0
                else:
                    timeout = 0.5
                r, _, _ = select.select(fds, [], [], timeout)
            except (ValueError, OSError):
                if self._evdev_kb is not None:
                    self._evdev_disconnect()
                    continue
                break

            now = time.monotonic()

            # ── `settings` command requested the config editor (SIGUSR1) ──────
            if self._open_settings_requested:
                self._open_settings_requested = False
                self._last_input = now   # count as activity (don't sleep on us)
                if in_screensaver or panel_asleep or self._in_text_message:
                    in_screensaver = False
                    panel_asleep = False
                    self._in_text_message = False
                if not self._settings_active:
                    self._toggle_settings()   # opens the overlay and renders
                continue

            # ── `clear-eink` command requested a screen clear (SIGUSR2) ────────
            if self._clear_requested:
                self._clear_requested = False
                self._last_input = now
                self._did_idle_reset = False
                if in_screensaver or panel_asleep or self._in_text_message:
                    in_screensaver = False
                    panel_asleep = False
                    self._in_text_message = False
                self._clear_screen()
                continue

            # ── `notes`/`llmchat`/`terminal` commands requested a mode switch
            # (SIGRTMIN+1/+2/+3) — same idea as settings/clear-eink above, but
            # jump straight to (or open) that mode's tab instead.
            if self._notes_requested:
                self._notes_requested = False
                self._last_input = now
                if in_screensaver or panel_asleep or self._in_text_message:
                    in_screensaver = False
                    panel_asleep = False
                    self._in_text_message = False
                self._open_notes()
                continue

            if self._llm_chat_requested:
                self._llm_chat_requested = False
                self._last_input = now
                if in_screensaver or panel_asleep or self._in_text_message:
                    in_screensaver = False
                    panel_asleep = False
                    self._in_text_message = False
                self._open_llm_chat()
                continue

            if self._terminal_requested:
                self._terminal_requested = False
                self._last_input = now
                if in_screensaver or panel_asleep or self._in_text_message:
                    in_screensaver = False
                    panel_asleep = False
                    self._in_text_message = False
                self._open_terminal()
                continue

            # ── Early panel deep-sleep ────────────────────────────────────────
            # Before the screensaver kicks in, power the panel down once a shorter
            # idle window passes. The terminal image is retained behind the dark
            # glass; any input wakes it. Skipped if it would land at/after the
            # screensaver threshold (then the screensaver handles sleeping).
            if (self._sleep_timeout > 0 and not panel_asleep and not in_screensaver
                    and not self._in_text_message):
                idle = now - self._last_input
                if idle > self._sleep_timeout and not (
                        self._idle_timeout > 0 and idle > self._idle_timeout):
                    panel_asleep = True
                    self._sleep_panel()
                    continue

            # ── Idle screensaver check ────────────────────────────────────────
            if self._idle_timeout > 0:
                idle = now - self._last_input
                if idle > self._idle_timeout and not in_screensaver and not self._in_text_message:
                    in_screensaver = True
                    panel_asleep = False   # screensaver supersedes the bare deep-sleep
                    self._show_screensaver()
                    continue  # skip stale r — next iteration runs a fresh select

            # ── Idle reset: after a longer window, start a brand-new shell ─────
            # so a returning user lands on a clean terminal. Once per idle
            # period; doesn't wake the panel if the screensaver is showing.
            # Skipped entirely while a foreground process (e.g. `claude`, a
            # long build) is still running — only an idle shell prompt gets
            # reset, so a session like Claude Code never gets killed just for
            # sitting untouched.
            if self._reset_timeout > 0 and not self._did_idle_reset:
                if (now - self._last_input) > self._reset_timeout:
                    if (now - self._busy_check_mono) >= 2.0:
                        self._busy_check_mono = now
                        self._reset_deferred_busy = any(
                            self._tab_is_busy(t) for t in self._tabs)
                    if not self._reset_deferred_busy:
                        try:
                            self._reset_session(render=not (in_screensaver or panel_asleep))
                        except Exception as e:
                            logger.error('Idle reset failed: %s', e)
                        self._did_idle_reset = True
                        continue

            # ── Keyboard input (evdev path) ───────────────────────────────────
            if self._evdev_kb is not None and self._evdev_kb.fileno() in r:
                try:
                    data = self._evdev_kb.read()
                except OSError:
                    self._evdev_disconnect()
                    continue
                if data:
                    self._last_activity = now
                    self._last_input = now
                    self._did_idle_reset = False
                    grace = now - self._screensaver_show_mono < 2.0
                    if in_screensaver or panel_asleep or self._in_text_message:
                        if not grace:
                            in_screensaver = False
                            panel_asleep = False
                            self._in_text_message = False
                            self._render(force_full=True)
                            self._last_full_refresh_mono = time.monotonic()
                        # swallow the wake key regardless
                    else:
                        if self._big_text_active:
                            self._exit_big_text()
                            data = b''   # swallow the key that dismissed read mode
                        if self._scroll_pages > 0:
                            self._snap_to_live()
                            has_pending = True
                        data = self._handle_markdown_key(data)
                        data = self._handle_hotkeys(data)
                        data = self._handle_help_key(data)
                        data = self._handle_search_key(data)
                        data = self._handle_rename_key(data)
                        data = self._handle_prockill_key(data)
                        data = self._handle_svcmgr_key(data)
                        data = self._handle_power_key(data)
                        data = self._handle_settings_key(data)
                        data = self._handle_snippets_key(data)
                        data = self._handle_palette_key(data)
                        data = self._handle_clipboard_key(data)
                        data = self._handle_sshpick_key(data)
                        data = self._handle_copy_key(data)
                        if data:
                            pty_dest = self._get_focused_pty()
                            if pty_dest is not None:
                                try:
                                    os.write(pty_dest, data)
                                except OSError:
                                    pass

            # ── Keyboard input (stdin / TTY path) ────────────────────────────
            elif self._evdev_kb is None and self._stdin_fd in r:
                try:
                    data = os.read(self._stdin_fd, 256)
                except OSError:
                    break
                if not data:
                    # EOF: stdin is closed (detached launch / stdin = /dev/null).
                    # Stop watching it so select() doesn't spin and the idle timer
                    # is left alone; fall back to evdev hot-plug / web input.
                    self._stdin_eof = True
                    continue
                self._last_activity = now
                self._last_input = now
                self._did_idle_reset = False
                grace = now - self._screensaver_show_mono < 2.0
                if in_screensaver or panel_asleep or self._in_text_message:
                    if not grace:
                        in_screensaver = False
                        panel_asleep = False
                        self._in_text_message = False
                        self._render(force_full=True)
                        self._last_full_refresh_mono = time.monotonic()
                    # swallow the wake key regardless
                else:
                    if self._big_text_active:
                        self._exit_big_text()
                        data = b''   # swallow the key that dismissed read mode
                    if self._scroll_pages > 0:
                        self._snap_to_live()
                        has_pending = True
                    data = self._handle_markdown_key(data)
                    data = self._handle_hotkeys(data)
                    data = self._handle_help_key(data)
                    data = self._handle_search_key(data)
                    data = self._handle_rename_key(data)
                    data = self._handle_prockill_key(data)
                    data = self._handle_svcmgr_key(data)
                    data = self._handle_power_key(data)
                    data = self._handle_settings_key(data)
                    data = self._handle_snippets_key(data)
                    data = self._handle_palette_key(data)
                    data = self._handle_clipboard_key(data)
                    data = self._handle_sshpick_key(data)
                    data = self._handle_copy_key(data)
                    if data:
                        pty_dest = self._get_focused_pty()
                        if pty_dest is not None:
                            try:
                                os.write(pty_dest, data)
                            except OSError:
                                pass

            # ── PTY output (all tabs) ─────────────────────────────────────────
            for tab_i, tab in enumerate(self._tabs):
                if tab.pty_master is None or tab.pty_master < 0:
                    continue
                if tab.pty_master not in r:
                    continue
                try:
                    chunk = os.read(tab.pty_master, 4096)
                    if chunk:
                        chunk = _filter_pty_output(chunk, tab.pty_master)
                        if chunk:
                            if tab_i == self._active_tab and self._scroll_pages > 0 and not in_screensaver:
                                self._snap_to_live()
                            tab.stream.feed(chunk)
                            if tab.logger:
                                tab.logger.write(chunk)
                        if tab_i == self._active_tab and not in_screensaver:
                            self._last_activity = now
                            has_pending = True
                        elif tab_i != self._active_tab and chunk and not tab.activity:
                            tab.activity = True
                            self._status_force = True   # show the bullet promptly
                            has_pending = True
                except OSError:
                    if tab_i == self._active_tab:
                        if not self._shell_exited_handler():
                            break
                        has_pending = True
                    else:
                        try: os.close(tab.pty_master)
                        except OSError: pass
                        tab.pty_master = -1
                        if tab.child_pid:
                            try: os.waitpid(tab.child_pid, os.WNOHANG)
                            except (OSError, ChildProcessError): pass
                        tab.child_pid = None
                # ── Split pane secondary PTY output ──────────────────────────
                if tab.pane2_master >= 0 and tab.pane2_master in r:
                    try:
                        chunk = os.read(tab.pane2_master, 4096)
                        if chunk:
                            chunk = _filter_pty_output(chunk, tab.pane2_master)
                            if chunk and tab.pane2_stream:
                                tab.pane2_stream.feed(chunk)
                            if tab_i == self._active_tab and not in_screensaver:
                                self._last_activity = now
                                has_pending = True
                    except OSError:
                        try: os.close(tab.pane2_master)
                        except OSError: pass
                        tab.pane2_master = -1
                        if tab.pane2_pid:
                            try: os.waitpid(tab.pane2_pid, os.WNOHANG)
                            except (OSError, ChildProcessError): pass
                        tab.pane2_pid = 0
                        if tab_i == self._active_tab:
                            tab.split_dir = ''
                            tab.pane_focus = 0
                            self._render(force_full=True)

            # ── Web input (phone keyboard via preview server) ─────────────────
            if self._web_input_queue is not None:
                try:
                    while True:
                        text = self._web_input_queue.get_nowait()
                        if text:
                            pty_dest = self._get_focused_pty()
                            if pty_dest is not None:
                                try:
                                    os.write(pty_dest, text.encode('utf-8'))
                                except OSError:
                                    pass
                            self._last_activity = now
                            self._last_input = now
                            self._did_idle_reset = False
                            if in_screensaver or panel_asleep or self._in_text_message:
                                in_screensaver = False
                                panel_asleep = False
                                self._in_text_message = False
                                self._render(force_full=True)
                                self._last_full_refresh_mono = time.monotonic()
                            else:
                                has_pending = True
                except _queue.Empty:
                    pass

            # ── SSH / tmux input (wake from sleep, keep awake while active) ────
            if self._tmux_input_seen(now):
                self._last_activity = now
                self._last_input = now
                self._did_idle_reset = False
                if in_screensaver or panel_asleep or self._in_text_message:
                    in_screensaver = False
                    panel_asleep = False
                    self._in_text_message = False
                    self._render(force_full=True)
                    self._last_full_refresh_mono = time.monotonic()

            # ── Display command queue (from web server) ───────────────────────
            if self._display_queue is not None:
                try:
                    while True:
                        cmd = self._display_queue.get_nowait()
                        action = cmd.get('type', '')
                        if action == 'message':
                            self._show_text_message(
                                cmd.get('text', ''), cmd.get('label', ''))
                            in_screensaver = False
                        elif action == 'screensaver':
                            in_screensaver = True
                            self._in_text_message = False
                            self._show_screensaver()
                        elif action == 'toggle_qr':
                            self._toggle_url_qr()
                        elif action == 'force_refresh':
                            self._force_full_refresh()
                except _queue.Empty:
                    pass

            # ── Alert tick ────────────────────────────────────────────────────
            # Alerts bypass the status-bar throttle: a warning must appear (and
            # clear) promptly, so force the status bar to repaint this render.
            if now - last_alert_tick >= 1.0:
                if self._alert_monitor.tick() and not in_screensaver and not panel_asleep:
                    self._status_force = True
                    has_pending = True  # alert changed — re-render status bar
                last_alert_tick = now

            _idle = now - self._last_activity

            # ── Split-view stats update ───────────────────────────────────────
            if self._split_view and not in_screensaver and not panel_asleep and _idle < 60.0:
                with self._stats_lock:
                    stats_dirty = self._stats_dirty
                if stats_dirty:
                    has_pending = True

            # ── Network stats update ──────────────────────────────────────────
            # These only affect the (deprioritized) status bar, so don't drive a
            # render on their own — they ride along on the next throttled status
            # repaint or terminal-content render. Clear the dirty flag so it
            # doesn't accumulate.
            if not in_screensaver and not panel_asleep and _idle < 60.0:
                status_due = (now - self._last_status_render) >= self._status_bar_interval
                with self._net_stats_lock:
                    if self._net_stats.get('dirty'):
                        self._net_stats['dirty'] = False
                        if status_due:
                            has_pending = True

            # ── Cycle screensaver: swap image when interval elapses ───────────
            # _screensaver_is_cycle is set by _show_screensaver from the rotation
            # set (2+ selected photos), so this picks up web selections live.
            if in_screensaver and self._screensaver_is_cycle:
                cycle_secs = self._config.get('screensaver_cycle_interval', 5) * 60
                if self._screensaver_last_cycle > 0.0 and (now - self._screensaver_last_cycle) >= cycle_secs:
                    self._show_screensaver()

            # ── Periodic screensaver refresh ──────────────────────────────────
            # The screensaver deep-sleeps the panel and never repaints. Re-flash
            # it every screensaver_refresh_minutes so a long idle doesn't ghost /
            # burn in the static image, and so the screensaver reclaims the panel
            # if something else drew to it in the meantime. _show_screensaver
            # resets _screensaver_show_mono and re-sleeps the panel.
            elif (in_screensaver and self._screensaver_refresh > 0
                    and self._screensaver_show_mono > 0.0
                    and (now - self._screensaver_show_mono) >= self._screensaver_refresh):
                self._show_screensaver()

            # ── Debounced render ──────────────────────────────────────────────
            # Skip when a full-screen custom image owns the panel (Markdown
            # viewer, big-text read mode, or a web "send text" message) — PTY
            # output from a background process must not repaint over them.
            _fullscreen_overlay = (self._markdown_active or self._big_text_active
                                   or self._in_text_message)
            if (has_pending and not in_screensaver and not panel_asleep
                    and not _fullscreen_overlay
                    and (now - last_render) >= _RENDER_DEBOUNCE):
                self._render()
                self._screen.dirty.clear()
                _ct = self._current_tab()
                if _ct and _ct.pane2_screen:
                    _ct.pane2_screen.dirty.clear()
                has_pending = False
                last_render = now
            elif (not in_screensaver and not panel_asleep and not _fullscreen_overlay
                    and self._periodic_flash_due(now)):
                # Deferred whole-panel ghost-clearing flash, fired in a quiet gap
                # so it never interrupts active typing.
                self._render(force_flash=True)
                self._screen.dirty.clear()
                _ct = self._current_tab()
                if _ct and _ct.pane2_screen:
                    _ct.pane2_screen.dirty.clear()
                last_render = now

    def _shell_exited_handler(self) -> bool:
        _AUTO_RESTART_SECS = 10
        msg = (
            b'\r\n\x1b[7m  Shell exited. '
            b'Press Enter to restart, Ctrl+C to quit, '
            b'or wait 10 s for auto-restart.  \x1b[0m\r\n'
        )
        self._stream.feed(msg)
        self._render(force_full=True)
        self._screen.dirty.clear()

        try:
            os.close(self._pty_master)
        except OSError:
            pass
        self._pty_master = None
        self._child_pid = None
        if self._tabs and 0 <= self._active_tab < len(self._tabs):
            self._tabs[self._active_tab].pty_master = None
            self._tabs[self._active_tab].child_pid = None

        input_fd = self._evdev_kb.fileno() if self._evdev_kb else self._stdin_fd
        deadline = time.monotonic() + _AUTO_RESTART_SECS
        while True:
            self._watchdog.ping()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break   # auto-restart after timeout
            try:
                r, _, _ = select.select([input_fd], [], [], min(1.0, remaining))
            except OSError:
                break   # bad fd — auto-restart
            if not r:
                continue
            # Use the evdev wrapper so raw input-event structs are decoded to
            # terminal bytes (e.g. Enter → b'\r'). Fall back to os.read for stdin.
            if self._evdev_kb is not None:
                try:
                    key = self._evdev_kb.read()
                except OSError:
                    break   # keyboard disconnected — auto-restart
            else:
                try:
                    key = os.read(input_fd, 10)
                except OSError:
                    break   # stdin closed — auto-restart
            if not key:
                break       # stdin EOF — auto-restart
            if b'\r' in key or b'\n' in key:
                break       # user pressed Enter — restart immediately
            if b'\x03' in key:
                self._running = False
                return False

        self._init_screen()
        self._spawn_shell()
        if self._tabs and 0 <= self._active_tab < len(self._tabs):
            t = self._tabs[self._active_tab]
            t.screen = self._screen; t.stream = self._stream
            t.pty_master = self._pty_master; t.child_pid = self._child_pid
        self._render(force_full=True)
        return True
