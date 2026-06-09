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
"""
import fcntl
import getpass
import json
import logging
import os
import pwd
from dataclasses import dataclass
import pty
import queue as _queue
import re
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import tty

import pyte

from alert_monitor import AlertMonitor
from PIL import ImageDraw
from terminal_renderer import (
    render_screen, render_screen_partial, render_mini_stats, terminal_dimensions,
    _find_mono_font, TERMINAL_H, SPLIT_TERMINAL_W,
)
from display_eink import EinkDriver
from sd_watchdog import Watchdog
from preview_server import start_if_enabled as _start_preview
from preview_server import _save_config_values
from evdev_input import EvdevKeyboard, find_keyboard

logger = logging.getLogger(__name__)

@dataclass
class _Tab:
    screen: 'pyte.Screen'
    stream: 'pyte.ByteStream'
    pty_master: int
    child_pid: int
    title: str = ''
    scroll_pages: int = 0
    tmux_session: str = ''


_RENDER_DEBOUNCE  = 0.02   # seconds — lower now that hw writes are async
_STATUS_CACHE_TTL = 5.0    # seconds between CWD/branch re-reads
_STATS_UPDATE_SEC = 30     # seconds between split-view stats refreshes
_MIN_FONT = 8
_MAX_FONT = 32

_URL_RE = re.compile(r'https?://[^\s\x00-\x1f"\'<>]{6,}')


def _fmt_speed(bps: float) -> str:
    mbps = bps * 8 / 1_000_000
    if mbps < 0.1:
        return '<0.1 Mbps'
    elif mbps < 10.0:
        return f'{mbps:.1f} Mbps'
    return f'{mbps:.0f} Mbps'


def _get_uptime() -> str:
    """System uptime as 'Xd Yh Zm' (drops leading zero units). Cheap: reads /proc."""
    try:
        with open('/proc/uptime') as f:
            secs = int(float(f.read().split()[0]))
    except Exception:
        return ''
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f'{days}d {hours}h {mins}m'
    if hours:
        return f'{hours}h {mins}m'
    return f'{mins}m'


def _filter_pty_output(data: bytes, pty_master_fd) -> bytes:
    """
    Strip DCS escape sequences before feeding output to pyte.

    pyte doesn't fully handle Device Control String (DCS) sequences; instead
    it renders their content as visible garbage text.  Fish shell sends two
    DCS-based probes on startup:
      - XTGETTCAP  (\\x1bP+q<hex-caps>\\x1b\\) — capability queries
      - Primary/Secondary/Tertiary DA (\\x1b[c, \\x1b[>c, \\x1b[=c)

    We strip DCS sequences entirely and write the expected (negative) responses
    back to the PTY so the shell gets an answer immediately instead of printing
    a timeout warning.
    """
    out = bytearray()
    i, n = 0, len(data)

    while i < n:
        c = data[i]
        nxt = data[i + 1] if i + 1 < n else -1

        # ── DCS  ESC P ... ST  (ST = ESC \  or  C1 0x9C) ────────────────────
        if c == 0x1B and nxt == 0x50:
            end = -1
            content_end = -1
            for k in range(i + 2, n):
                if data[k] == 0x9C:                                 # C1 ST
                    content_end = k
                    end = k + 1
                    break
                if data[k] == 0x1B and k + 1 < n and data[k + 1] == 0x5C:  # ESC \
                    content_end = k
                    end = k + 2
                    break
            if end < 0:
                # Incomplete DCS at end of chunk — discard remainder
                break
            content = data[i + 2:content_end]
            if content.startswith(b'+q') and pty_master_fd is not None:
                # XTGETTCAP: respond "not supported" for every capability queried
                try:
                    os.write(pty_master_fd, b'\x1bP0+r\x1b\\')
                except OSError:
                    pass
            i = end  # skip the entire DCS sequence

        # ── CSI c  (Device Attributes request) ───────────────────────────────
        elif c == 0x1B and nxt == 0x5B:   # ESC [
            # Scan for CSI final byte (0x40–0x7E)
            j = i + 2
            while j < n and (0x20 <= data[j] <= 0x3F):
                j += 1
            if j < n and data[j] == 0x63:  # final byte 'c'
                params = data[i + 2:j]
                try:
                    if params in (b'', b'0') and pty_master_fd is not None:
                        os.write(pty_master_fd, b'\x1b[?62;c')      # Primary DA
                    elif params == b'>' and pty_master_fd is not None:
                        os.write(pty_master_fd, b'\x1b[>0;10;1c')   # Secondary DA
                    elif params == b'=' and pty_master_fd is not None:
                        os.write(pty_master_fd, b'\x1bP!|00000000\x1b\\')  # Tertiary DA
                except OSError:
                    pass
                # Don't pass DA requests to pyte — they're queries, not display content
                i = j + 1
            else:
                # Normal CSI — pass through to pyte unchanged
                out.append(c)
                i += 1

        else:
            out.append(c)
            i += 1

    return bytes(out)

# Function key escape sequences (xterm/VT220)
_F7   = b'\x1b[18~'   # dark/light mode toggle
_F8   = b'\x1b[19~'   # paste from file
_F9   = b'\x1b[20~'
_F10  = b'\x1b[21~'
_F1   = b'\x1bOP'     # new tab / SSH picker
_F2   = b'\x1bOQ'     # close tab
_F3   = b'\x1bOR'     # process kill overlay
_F4   = b'\x1bOS'     # service manager overlay
_F5   = b'\x1b[15~'  # power menu
_F6   = b'\x1b[17~'   # command palette
_F11  = b'\x1b[23~'

_POWER_ITEMS = ['  Shutdown', '  Reboot', '  Cancel']

# On-display config editor (opened from the F6 command palette). Each entry is
# (config_key, type, label, options). Only bool/select are editable here so the
# whole thing is keyboard-navigable on the e-ink panel without text entry.
# Anything more exotic stays in the web editor at /config.
_SETTINGS_SCHEMA = [
    ('terminal_show_qr',              'bool',   'QR Code',          None),
    ('terminal_cursor_style',         'select', 'Cursor',          ['block', 'underline']),
    ('terminal_dark_mode',            'bool',   'Dark Mode',        None),
    ('terminal_font_size',            'select', 'Font Size',        [8, 10, 12, 14, 16, 18, 20]),
    ('terminal_font_path',            'select', 'Font',             '__FONTS__'),
    ('terminal_split_view',           'bool',   'Split View',       None),
    ('display_sleep_minutes',         'select', 'Panel Sleep (min)', [0, 2, 5, 10, 15, 30]),
    ('screensaver_sleep_minutes',     'select', 'Screensaver (min)', [0, 5, 10, 15, 30, 60]),
    ('terminal_start_dir',            'select', 'Start Dir',        ['home', 'last', 'root']),
    ('terminal_prompt_custom',        'bool',   'Custom Prompt',    None),
    ('terminal_prompt_show_user',     'bool',   'Prompt: User',     None),
    ('terminal_prompt_show_host',     'bool',   'Prompt: Host',     None),
    ('terminal_prompt_show_cwd',      'bool',   'Prompt: Dir',      None),
    ('terminal_prompt_show_git',      'bool',   'Prompt: Git',      None),
]

# Display-level settings take effect instantly on Save (no service restart).
_SETTINGS_LIVE = {
    'terminal_cursor_style', 'terminal_show_qr', 'terminal_dark_mode',
    'terminal_font_size', 'terminal_font_path', 'terminal_split_view',
    'display_sleep_minutes', 'screensaver_sleep_minutes',
}
# Shell-level settings only affect newly-spawned shells, so saving them
# restarts the service to respawn. (Everything not in _SETTINGS_LIVE.)
_SETTINGS_SHELL = {
    'terminal_start_dir', 'terminal_prompt_custom', 'terminal_prompt_show_user',
    'terminal_prompt_show_host', 'terminal_prompt_show_cwd',
    'terminal_prompt_show_git', 'terminal_prompt_symbol',
}
_SETTINGS_OPEN = '⚙ Settings (e-ink config)'
_SNIPPETS_OPEN = '✎ Snippets (saved commands)'
_BIGTEXT_OPEN  = '🔍 Big text (read mode)'
_BEAM_OPEN     = '📱 Beam screen to phone'
_HUD_TOGGLE    = '📊 Toggle refresh stats HUD'
# Palette actions that open an overlay / run in-app instead of typing a command.
_PALETTE_ACTIONS = (_SETTINGS_OPEN, _SNIPPETS_OPEN, _BIGTEXT_OPEN, _BEAM_OPEN, _HUD_TOGGLE)
_F12  = b'\x1b[24~'
_CTRL_LEFT  = b'\x1b[1;5D'   # cycle tabs
_CTRL_RIGHT = b'\x1b[1;5C'
_PGUP = b'\x1b[5~'
_PGDN = b'\x1b[6~'

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_local_ip() -> str:
    """Return the Pi's primary LAN IP address, or '' on failure."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except Exception:
        return ''


def _read_wifi_signal() -> int | None:
    try:
        with open('/proc/net/wireless') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[0].rstrip(':').startswith('w'):
                    return int(float(parts[3].rstrip('.')))
    except Exception:
        pass
    return None


class EinkTerminal:
    """Runs a shell in a PTY and mirrors output to the e-ink display."""

    def __init__(self, config: dict, local: bool = False):
        self._config    = config
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
        # Set by the `clear-eink` shell command (SIGUSR2); handled in the loop.
        self._clear_requested = False
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

        # tmux
        self._use_tmux     = config.get('terminal_use_tmux', False) and bool(shutil.which('tmux'))
        self._tmux_session = config.get('terminal_tmux_session', 'eink')

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
        self._last_status_pub = 0.0  # throttle for the /status panel publish
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

        # Beam-to-phone: a pinned QR linking to the captured screen text.
        self._beam_url: str = ''
        self._beam_until_mono: float = 0.0

        # Clipboard picker
        self._clipboard: list = []
        self._clipboard_idx: int = 0
        self._clipboard_active: bool = False
        self._clipboard_path = os.path.join(_REPO_ROOT, 'data', 'clipboard.json')
        self._clipboard = self._load_clipboard()

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

    # ─── In-shell commands (typeable) ─────────────────────────────────────────

    def _install_command_scripts(self):
        """Drop helper commands into data/bin and prepend it to PATH, so the
        spawned shell can open in-app overlays by name.

        Each script signals this process (its PID is in /tmp/eink-terminal-active,
        written by run()) — e.g. `settings` sends SIGUSR1 to open the config
        editor. Generated at launch so they always match the running app."""
        bindir = os.path.join(_REPO_ROOT, 'data', 'bin')
        try:
            os.makedirs(bindir, exist_ok=True)
            # `settings` (and the `eink` alias) → SIGUSR1 → open config editor.
            script = (
                '#!/bin/sh\n'
                '# Opens the e-ink on-display Settings editor. Auto-generated by\n'
                '# eink_terminal_app.py — edits will be overwritten on launch.\n'
                'pidfile=/tmp/eink-terminal-active\n'
                'if [ -r "$pidfile" ] && kill -USR1 "$(cat "$pidfile")" 2>/dev/null; then\n'
                '    exit 0\n'
                'fi\n'
                'echo "e-ink terminal not running (no $pidfile)" >&2\n'
                'exit 1\n'
            )
            for name in ('settings', 'eink'):
                path = os.path.join(bindir, name)
                with open(path, 'w') as f:
                    f.write(script)
                os.chmod(path, 0o755)
            # `clear-eink` → SIGUSR2 → clear the screen + scrollback and do a
            # whole-panel ghost-clearing refresh (keeps the running shell).
            clear_script = (
                '#!/bin/sh\n'
                '# Clears the e-ink terminal (screen + ghosting). Auto-generated\n'
                '# by eink_terminal_app.py — edits will be overwritten on launch.\n'
                'pidfile=/tmp/eink-terminal-active\n'
                'if [ -r "$pidfile" ] && kill -USR2 "$(cat "$pidfile")" 2>/dev/null; then\n'
                '    exit 0\n'
                'fi\n'
                'echo "e-ink terminal not running (no $pidfile)" >&2\n'
                'exit 1\n'
            )
            for name in ('clear-eink',):
                path = os.path.join(bindir, name)
                with open(path, 'w') as f:
                    f.write(clear_script)
                os.chmod(path, 0o755)
        except OSError as e:
            logger.warning('could not install command scripts: %s', e)
            return
        # Prepend to the parent's PATH so the forked shell — and tmux, which
        # inherits the client environment — both find the commands.
        cur = os.environ.get('PATH', '')
        if bindir not in cur.split(os.pathsep):
            os.environ['PATH'] = bindir + os.pathsep + cur

    def _on_settings_signal(self, signum, frame):
        """SIGUSR1 handler — set a flag for the main loop. Kept minimal so it's
        safe to run from a signal context (no rendering or I/O here)."""
        self._open_settings_requested = True

    def _on_clear_signal(self, signum, frame):
        """SIGUSR2 handler (`clear-eink`) — flag a screen clear for the loop."""
        self._clear_requested = True

    def _on_shutdown_signal(self, signum, frame):
        """SIGINT/SIGTERM — request a graceful shutdown instead of crashing.

        In raw-keyboard mode Ctrl+C is forwarded to the shell as a byte and this
        never fires; it matters when stdin is left cooked (evdev keyboard, or an
        SSH session driving the app) so a stray Ctrl+C cleans up the panel
        instead of killing the process mid-refresh."""
        self._running = False

    # ─── Screen ──────────────────────────────────────────────────────────────

    def _init_screen(self):
        tw = SPLIT_TERMINAL_W if self._split_view else 800
        cols, rows, cw, ch = terminal_dimensions(self._font_size, self._font_path, tw)
        if hasattr(self, '_driver'):
            self._driver.set_cell_size(cw, ch)
        if self._use_tmux:
            self._screen = pyte.Screen(cols, rows)
        else:
            history = self._config.get('terminal_scrollback', 500)
            self._screen = pyte.HistoryScreen(cols, rows, history=history)
        self._stream = pyte.ByteStream(self._screen)
        self._scroll_pages = 0

    # ─── PTY ─────────────────────────────────────────────────────────────────

    def _spawn_shell(self, tmux_session: str = None):
        session = tmux_session if tmux_session is not None else self._tmux_session
        # Resolve everything that needs the filesystem in the parent, before the
        # fork, so the forked child only execs.
        start_dir = self._resolve_start_dir()
        promptcmd = self._build_prompt_command()   # None unless custom prompt is on
        pid, master_fd = pty.fork()
        if pid == 0:
            os.environ['TERM'] = 'xterm-256color'
            if self._use_tmux:
                os.execvp('tmux', self._tmux_launch_argv(session, start_dir, promptcmd))
            else:
                if start_dir:
                    try:
                        os.chdir(start_dir)
                    except OSError:
                        pass
                if promptcmd:
                    # An interactive shell wired up with our custom prompt. The
                    # leading 'exec' replaces this sh, so no extra layer remains.
                    os.execvp('/bin/sh', ['/bin/sh', '-c', promptcmd])
                shell = os.environ.get('SHELL') or pwd.getpwuid(os.getuid()).pw_shell or '/bin/bash'
                # Start as a login shell (argv[0] prefixed with '-') so it behaves
                # like a normal console terminal: sources /etc/profile + ~/.profile,
                # sets PATH, shows the user's real prompt, prints MOTD/last-login.
                argv0 = '-' + os.path.basename(shell)
                os.execvp(shell, [argv0])
            os._exit(1)
        self._child_pid = pid
        self._pty_master = master_fd
        self._sync_pty_winsize()

    def _tmux_launch_argv(self, session: str, start_dir: str, promptcmd: str) -> list:
        """argv for `tmux new-session -A` (attach-or-create).

        When a custom prompt is active we run our custom-prompt shell as the pane
        command AND set it as the session's default-command, so every future
        window/pane (split, new-window, …) gets the same prompt — not just the
        first one. Attaching an existing session keeps its panes/cwd ('last')."""
        argv = ['tmux', 'new-session', '-A', '-s', session]
        if start_dir:
            argv += ['-c', start_dir]
        if promptcmd:
            # initial pane command, then default-command for subsequent panes.
            argv += [promptcmd, ';', 'set-option', 'default-command', promptcmd]
        return argv

    # ─── Start directory ──────────────────────────────────────────────────────

    @property
    def _last_cwd_file(self) -> str:
        return os.path.join(_REPO_ROOT, 'data', 'last_cwd.txt')

    def _resolve_start_dir(self) -> str | None:
        """Turn the terminal_start_dir preference into a concrete directory.
        Falls back to $HOME for anything missing/invalid. None = don't force."""
        pref = str(self._start_dir_pref or 'home').strip()
        home = os.path.expanduser('~')
        if pref == 'home':
            return home
        if pref == 'root':
            return '/'
        if pref == 'last':
            return self._read_last_cwd() or home
        path = os.path.expanduser(pref)
        return path if os.path.isdir(path) else home

    def _read_last_cwd(self) -> str | None:
        try:
            d = open(self._last_cwd_file).read().strip()
            return d if d and os.path.isdir(d) else None
        except OSError:
            return None

    def _save_last_cwd(self):
        """Persist the active shell's current directory so 'last' can resume it
        after a restart/reboot. tmux: query the active pane; else read /proc."""
        d = None
        try:
            if self._use_tmux and shutil.which('tmux'):
                r = subprocess.run(
                    ['tmux', 'display-message', '-p', '-t', self._tmux_session,
                     '#{pane_current_path}'],
                    capture_output=True, text=True, timeout=1)
                d = r.stdout.strip()
            elif self._child_pid:
                d = os.readlink('/proc/%d/cwd' % self._child_pid)
        except Exception:
            d = None
        if d and os.path.isdir(d):
            try:
                os.makedirs(os.path.dirname(self._last_cwd_file), exist_ok=True)
                with open(self._last_cwd_file, 'w') as f:
                    f.write(d + '\n')
            except OSError:
                pass

    # ─── Custom shell prompt (bash / zsh / fish) ──────────────────────────────

    # Per-shell prompt escapes for the user@host:cwd segments. bash and zsh
    # share the git command substitution (POSIX $()); fish is built separately.
    _PROMPT_ESC = {
        'bash': {'user': r'\u', 'host': r'\h', 'cwd': r'\w'},
        'zsh':  {'user': '%n',  'host': '%m',  'cwd': '%~'},
    }

    def _detect_shell(self) -> str:
        """Basename of the user's login shell: 'bash', 'zsh', 'fish', …"""
        shell = os.environ.get('SHELL') or ''
        if not shell:
            try:
                shell = pwd.getpwuid(os.getuid()).pw_shell
            except Exception:
                shell = ''
        return os.path.basename(shell or '/bin/bash')

    def _build_prompt_string(self, kind: str) -> str:
        """PS1/PROMPT string for a POSIX-prompt shell (bash or zsh) from the
        enabled parts. Quoted so the shell expands the escapes + git command
        substitution fresh on each prompt."""
        esc = self._PROMPT_ESC[kind]
        ident = ''
        if self._prompt_show_user:
            ident += esc['user']
        if self._prompt_show_host:
            ident += ('@' if ident else '') + esc['host']
        segs = []
        if ident:
            segs.append(ident)
        if self._prompt_show_cwd:
            segs.append(esc['cwd'])
        prompt = ':'.join(segs)
        if self._prompt_show_git:
            # Show " (branch)" only inside a git work tree; silent otherwise.
            prompt += r'$(__b=$(git branch --show-current 2>/dev/null); [ -n "$__b" ] && printf " (%s)" "$__b")'
        sym = self._prompt_symbol or '$'
        return (prompt + ' ' if prompt else '') + sym + ' '

    def _build_ps1(self) -> str:
        """Bash PS1 (kept as a named helper; preview/tests use it)."""
        return self._build_prompt_string('bash')

    def _build_fish_prompt(self) -> str:
        """A `function fish_prompt … end` definition (fish has no PS1)."""
        ident = []
        if self._prompt_show_user:
            ident.append('$USER')
        if self._prompt_show_host:
            ident.append('(prompt_hostname)')
        id_str = "'@'".join(ident)
        parts = []
        if id_str:
            parts.append(id_str)
        if self._prompt_show_cwd:
            parts.append('(prompt_pwd)')
        head = "':'".join(parts)
        sym = (self._prompt_symbol or '$').replace("'", r"\'")
        lines = ['function fish_prompt']
        if head:
            lines.append('    echo -n ' + head)
        if self._prompt_show_git:
            lines.append('    set -l __b (git branch --show-current 2>/dev/null)')
            lines.append('    test -n "$__b"; and echo -n " ($__b)"')
        lines.append("    echo -n ' %s '" % sym)
        lines.append('end')
        return '\n'.join(lines)

    def _build_prompt_command(self) -> str | None:
        """Shell command that launches an interactive shell wired up with our
        custom prompt — used both as the tmux pane command / default-command and
        (via `sh -c`) for the non-tmux exec. None when the custom prompt is off.

        Honors the user's actual $SHELL: zsh and fish get their native prompt
        mechanism; everything else (and unknown shells) uses bash, since the
        POSIX prompt escapes are bash syntax."""
        if not self._prompt_custom:
            return None
        shell = self._detect_shell()
        if shell == 'zsh' and shutil.which('zsh'):
            zdotdir = self._write_zsh_dotdir()
            if zdotdir:
                return 'exec env ZDOTDIR=%s zsh -i' % shlex.quote(zdotdir)
        elif shell == 'fish' and shutil.which('fish'):
            return 'exec fish -i -C %s' % shlex.quote(self._build_fish_prompt())
        rc = self._write_bash_rcfile()
        if rc:
            return 'exec bash --rcfile %s -i' % shlex.quote(rc)
        return None

    def _prompt_rcfile_path(self) -> str:
        return os.path.join(_REPO_ROOT, 'data', 'eink_bashrc')

    def _write_bash_rcfile(self) -> str | None:
        """bash rcfile that replays login-shell startup (so PATH, aliases, etc.
        match a normal terminal), then pins our PS1 last. Returns its path, or
        None on write failure. Used as `bash --rcfile <path> -i`."""
        ps1 = self._build_prompt_string('bash').replace("'", "'\\''")
        content = (
            "# Auto-generated by the e-ink terminal — regenerated on launch from\n"
            "# config.yaml. Replays login-shell startup, then pins the custom PS1.\n"
            "[ -r /etc/profile ] && . /etc/profile\n"
            'for __f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do\n'
            '    [ -r "$__f" ] && { . "$__f"; break; }\n'
            "done\n"
            "PS1='" + ps1 + "'\n"
        )
        path = self._prompt_rcfile_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write(content)
        except OSError:
            return None
        return path

    def _write_zsh_dotdir(self) -> str | None:
        """Create a ZDOTDIR whose .zshrc sources the user's real one then pins
        PROMPT. .zshenv is chained back too so login env is preserved despite the
        overridden ZDOTDIR. Returns the dir, or None on failure."""
        prompt = self._build_prompt_string('zsh').replace("'", "'\\''")
        d = os.path.join(_REPO_ROOT, 'data', 'eink_zdotdir')
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, '.zshenv'), 'w') as f:
                f.write('[ -r "$HOME/.zshenv" ] && source "$HOME/.zshenv"\n')
            with open(os.path.join(d, '.zshrc'), 'w') as f:
                f.write(
                    "# Auto-generated by the e-ink terminal.\n"
                    '[ -r "$HOME/.zshrc" ] && source "$HOME/.zshrc"\n'
                    "setopt prompt_subst\n"
                    "PROMPT='" + prompt + "'\n"
                )
        except OSError:
            return None
        return d

    # ─── Prompt preview (shown live in the settings editor) ────────────────────

    def _prompt_preview(self) -> str:
        """A representative expansion of the configured prompt parts, using real
        user/host but placeholder dir/branch — so the editor can show the effect
        before saving. Reads staged (pending) values via _settings_value."""
        if not self._settings_value('terminal_prompt_custom',
                                    self._config.get('terminal_prompt_custom', False)):
            return '(custom prompt off)'

        def on(key):
            return bool(self._settings_value(key, self._config.get(key, True)))

        try:
            user = getpass.getuser()
        except Exception:
            user = 'user'
        host = socket.gethostname().split('.')[0]
        ident = ''
        if on('terminal_prompt_show_user'):
            ident += user
        if on('terminal_prompt_show_host'):
            ident += ('@' if ident else '') + host
        segs = [ident] if ident else []
        if on('terminal_prompt_show_cwd'):
            segs.append('~/project')
        s = ':'.join(segs)
        if on('terminal_prompt_show_git'):
            s += ' (main)'
        sym = self._settings_value('terminal_prompt_symbol',
                                   self._config.get('terminal_prompt_symbol', '$')) or '$'
        return (s + ' ' if s else '') + sym

    def _sync_pty_winsize(self):
        tw = SPLIT_TERMINAL_W if self._split_view else 800
        cols, rows, _, _ = terminal_dimensions(self._font_size, self._font_path, tw)
        winsize = struct.pack('HHHH', rows, cols, 0, 0)
        try:
            fcntl.ioctl(self._pty_master, termios.TIOCSWINSZ, winsize)
        except Exception as e:
            logger.warning('Could not set PTY window size: %s', e)

    # ─── TTY raw mode ─────────────────────────────────────────────────────────

    def _enter_raw(self):
        if self._evdev_kb:
            return  # evdev handles input; stdin raw mode not needed
        try:
            self._old_tty = termios.tcgetattr(self._stdin_fd)
            tty.setraw(self._stdin_fd)
        except termios.error:
            pass  # not a real TTY (e.g. systemd without StandardInput=tty)

    def _exit_raw(self):
        if self._old_tty is not None:
            try:
                termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._old_tty)
            except Exception:
                pass

    # ─── Scrollback ───────────────────────────────────────────────────────────

    def _scroll_up(self):
        if self._use_tmux or not hasattr(self._screen, 'prev_page'):
            return
        self._screen.prev_page()
        self._scroll_pages += 1
        self._render(force_full=True)

    def _scroll_down(self):
        if self._use_tmux or not hasattr(self._screen, 'next_page'):
            return
        if self._scroll_pages > 0:
            self._screen.next_page()
            self._scroll_pages -= 1
            self._render(force_full=True)

    def _snap_to_live(self):
        """Snap back to live view if currently scrolled up."""
        while self._scroll_pages > 0 and hasattr(self._screen, 'next_page'):
            self._screen.next_page()
            self._scroll_pages -= 1

    # ─── Hotkeys ─────────────────────────────────────────────────────────────

    def _handle_hotkeys(self, data: bytes) -> bytes:
        if _F1 in data:
            self._toggle_sshpick()
            data = data.replace(_F1, b'')
        if _F2 in data:
            self._close_tab()
            data = data.replace(_F2, b'')
        if _CTRL_RIGHT in data:
            self._switch_tab(+1)
            data = data.replace(_CTRL_RIGHT, b'')
        if _CTRL_LEFT in data:
            self._switch_tab(-1)
            data = data.replace(_CTRL_LEFT, b'')
        if _F3 in data:
            self._toggle_prockill()
            data = data.replace(_F3, b'')
        if _F4 in data:
            self._toggle_svcmgr()
            data = data.replace(_F4, b'')
        if _F5 in data:
            self._toggle_power()
            data = data.replace(_F5, b'')
        if _F6 in data:
            self._toggle_palette()
            data = data.replace(_F6, b'')
        if _F7 in data:
            self._toggle_dark_mode()
            data = data.replace(_F7, b'')
        if _F8 in data:
            self._toggle_clipboard()
            data = data.replace(_F8, b'')
        if _F9 in data:
            self._change_font(-2)
            data = data.replace(_F9, b'')
        if _F10 in data:
            self._force_full_refresh()
            data = data.replace(_F10, b'')
        if _F11 in data:
            self._switch_to_stats()
            data = data.replace(_F11, b'')
        if _F12 in data:
            self._change_font(+2)
            data = data.replace(_F12, b'')
        if _PGUP in data:
            self._scroll_up()
            data = data.replace(_PGUP, b'')
        if _PGDN in data:
            self._scroll_down()
            data = data.replace(_PGDN, b'')
        return data

    def _toggle_dark_mode(self):
        self._dark_mode = not self._dark_mode
        self._render(force_full=True)

    def _paste_from_file(self):
        try:
            with open(self._paste_file, 'rb') as f:
                content = f.read()
            if self._pty_master is not None and content:
                # Write in chunks to avoid overflowing PTY input buffer
                chunk = 4096
                for i in range(0, len(content), chunk):
                    os.write(self._pty_master, content[i:i + chunk])
                    if len(content) > chunk:
                        import time as _t; _t.sleep(0.01)
        except FileNotFoundError:
            self._alert_monitor._push(f'Paste: {self._paste_file} not found')

    def _change_font(self, delta: int):
        new_size = max(_MIN_FONT, min(_MAX_FONT, self._font_size + delta))
        if new_size == self._font_size:
            return
        self._font_size = new_size
        self._reflow_shell()
        self._render(force_full=True)

    def _reflow_shell(self):
        """Re-derive the terminal grid (after a font-size or split-view change)
        and tell the child shell its window resized."""
        self._init_screen()
        self._img_cache = None   # layout changed — drop the incremental cache
        self._sync_pty_winsize()
        if self._child_pid:
            try:
                os.kill(self._child_pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass

    # ─── Tab management ──────────────────────────────────────────────────────

    def _current_tab(self):
        if self._tabs and 0 <= self._active_tab < len(self._tabs):
            return self._tabs[self._active_tab]
        return None

    def _tab_title(self, tab) -> str:
        if tab.title:
            return tab.title
        if tab.child_pid and tab.child_pid > 0:
            try:
                p = f'/proc/{tab.child_pid}/cwd'
                if os.path.exists(p):
                    return os.path.basename(os.readlink(p)) or 'shell'
            except Exception:
                pass
        return 'shell'

    def _tab_indicator(self) -> str:
        """Status-bar tab chip: '[2/3 projdir]' — the count plus the active
        tab's short working-dir/title. Empty when only one tab is open."""
        if len(self._tabs) <= 1:
            return ''
        base = f'{self._active_tab + 1}/{len(self._tabs)}'
        tab = self._current_tab()
        name = self._tab_title(tab) if tab else ''
        return f'[{base} {name}]' if name else f'[{base}]'

    def _sync_active_tab(self):
        if self._tabs and 0 <= self._active_tab < len(self._tabs):
            self._tabs[self._active_tab].scroll_pages = self._scroll_pages

    def _new_tab(self, cmd: str = None):
        self._sync_active_tab()
        if self._tabs and 0 <= self._active_tab < len(self._tabs):
            t = self._tabs[self._active_tab]
            t.screen = self._screen; t.stream = self._stream
            t.pty_master = self._pty_master; t.child_pid = self._child_pid
        self._init_screen()
        new_session = f'{self._tmux_session}-{len(self._tabs) + 1}'
        self._spawn_shell(tmux_session=new_session)
        self._tabs.append(_Tab(screen=self._screen, stream=self._stream,
                               pty_master=self._pty_master, child_pid=self._child_pid,
                               tmux_session=new_session))
        self._active_tab = len(self._tabs) - 1
        self._scroll_pages = 0
        if cmd:
            try:
                os.write(self._pty_master, (cmd + '\n').encode())
            except OSError:
                pass
        self._render(force_full=True)

    def _close_tab(self):
        if len(self._tabs) <= 1:
            return
        t = self._tabs[self._active_tab]
        if t.child_pid:
            try: os.kill(t.child_pid, signal.SIGTERM)
            except (ProcessLookupError, OSError): pass
        if t.pty_master is not None and t.pty_master >= 0:
            try: os.close(t.pty_master)
            except OSError: pass
        del self._tabs[self._active_tab]
        self._active_tab = min(self._active_tab, len(self._tabs) - 1)
        t2 = self._tabs[self._active_tab]
        self._screen = t2.screen; self._stream = t2.stream
        self._pty_master = t2.pty_master; self._child_pid = t2.child_pid
        self._scroll_pages = t2.scroll_pages
        self._sync_pty_winsize()
        try: os.kill(self._child_pid, signal.SIGWINCH)
        except (ProcessLookupError, OSError): pass
        self._render(force_full=True)

    def _switch_tab(self, delta: int):
        if not self._tabs: return
        self._goto_tab((self._active_tab + delta) % len(self._tabs))

    def _goto_tab(self, idx: int):
        if idx == self._active_tab or not self._tabs: return
        self._sync_active_tab()
        t = self._tabs[self._active_tab]
        t.screen = self._screen; t.stream = self._stream
        t.pty_master = self._pty_master; t.child_pid = self._child_pid
        t.scroll_pages = self._scroll_pages
        self._active_tab = idx
        t2 = self._tabs[idx]
        self._screen = t2.screen; self._stream = t2.stream
        self._pty_master = t2.pty_master; self._child_pid = t2.child_pid
        self._scroll_pages = t2.scroll_pages
        self._sync_pty_winsize()
        try: os.kill(self._child_pid, signal.SIGWINCH)
        except (ProcessLookupError, OSError): pass
        self._render(force_full=True)

    # ─── SSH bookmarks picker ─────────────────────────────────────────────────

    def _load_ssh_bookmarks(self) -> tuple:
        bookmarks = self._config.get('terminal_ssh_bookmarks', [])
        strings, hosts = [], []
        for b in bookmarks:
            name = b.get('name', b.get('host', '?'))
            user = b.get('user', '')
            host = b.get('host', '')
            strings.append(f"  {name:<20}  {user+'@'+host if user else host}")
            hosts.append(b)
        return strings, hosts

    def _toggle_sshpick(self):
        if self._sshpick_active:
            self._sshpick_active = False
            self._render(); return
        items, hosts = self._load_ssh_bookmarks()
        if not items:
            self._new_tab(); return
        self._sshpick_items = items
        self._sshpick_hosts = hosts
        self._sshpick_idx = 0
        self._sshpick_active = True
        self._palette_active = self._clipboard_active = False
        self._prockill_active = self._svcmgr_active = self._power_active = False
        self._render()

    def _handle_sshpick_key(self, data: bytes) -> bytes:
        if not self._sshpick_active: return data
        if b'\x1b[A' in data:
            self._sshpick_idx = max(0, self._sshpick_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._sshpick_idx = min(len(self._sshpick_items) - 1, self._sshpick_idx + 1)
            self._render(); return b''
        if b'\r' in data or b'\n' in data:
            if self._sshpick_hosts:
                b = self._sshpick_hosts[self._sshpick_idx]
                parts = ['ssh']
                port = b.get('port', 22)
                if port and port != 22:
                    parts += ['-p', str(port)]
                user = b.get('user', '')
                host = b.get('host', '')
                parts.append(f"{user}@{host}" if user else host)
                self._sshpick_active = False
                self._new_tab(cmd=' '.join(parts))
            return b''
        if b'\x1b' in data:
            self._sshpick_active = False; self._render(); return b''
        return b''

    # ─── Process kill overlay (F3) ───────────────────────────────────────────

    def _load_process_list(self) -> tuple:
        import psutil
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            try:
                procs.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        procs.sort(key=lambda p: p.get('cpu_percent') or 0, reverse=True)
        strings, pids = [], []
        for p in procs[:15]:
            strings.append(f"{p.get('pid',0):>6}  {p.get('cpu_percent') or 0:>5.1f}%  {p.get('memory_percent') or 0:>4.1f}%  {(p.get('name') or '')[:22]}")
            pids.append(p.get('pid', 0))
        return strings, pids

    def _toggle_prockill(self):
        if self._prockill_active:
            self._prockill_active = False
        else:
            self._prockill_items, self._prockill_pids = self._load_process_list()
            self._prockill_idx = 0
            self._prockill_active = True
            self._palette_active = self._clipboard_active = False
            self._svcmgr_active = self._power_active = False
        self._render()

    def _handle_prockill_key(self, data: bytes) -> bytes:
        if not self._prockill_active:
            return data
        if b'\x1b[A' in data:
            self._prockill_idx = max(0, self._prockill_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._prockill_idx = min(len(self._prockill_items) - 1, self._prockill_idx + 1)
            self._render(); return b''
        if b'\r' in data or b'\n' in data:
            if self._prockill_pids:
                try:
                    import psutil
                    psutil.Process(self._prockill_pids[self._prockill_idx]).send_signal(signal.SIGTERM)
                except (ProcessLookupError, PermissionError, Exception):
                    pass
                self._prockill_items, self._prockill_pids = self._load_process_list()
                self._prockill_idx = min(self._prockill_idx, max(0, len(self._prockill_items) - 1))
                self._render()
            return b''
        if b'\x1b' in data:
            self._prockill_active = False; self._render(); return b''
        return b''

    # ─── Service manager overlay (F4) ────────────────────────────────────────

    def _load_service_list(self) -> tuple:
        names = self._config.get('terminal_services', [])
        strings = []
        for name in names:
            try:
                r = subprocess.run(['systemctl', 'is-active', name],
                                   capture_output=True, text=True, timeout=1)
                status = r.stdout.strip() or 'unknown'
            except Exception:
                status = 'unknown'
            strings.append(f"{'●' if status == 'active' else '○'}  {name:<30}  {status}")
        return strings, list(names)

    def _toggle_svcmgr(self):
        if self._svcmgr_active:
            self._svcmgr_active = False
        else:
            self._svcmgr_items, self._svcmgr_names = self._load_service_list()
            self._svcmgr_idx = 0
            self._svcmgr_active = True
            self._palette_active = self._clipboard_active = False
            self._prockill_active = self._power_active = False
        self._render()

    def _handle_svcmgr_key(self, data: bytes) -> bytes:
        if not self._svcmgr_active:
            return data
        if b'\x1b[A' in data:
            self._svcmgr_idx = max(0, self._svcmgr_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._svcmgr_idx = min(len(self._svcmgr_items) - 1, self._svcmgr_idx + 1)
            self._render(); return b''
        if data in (b'r', b'R'): self._svcmgr_action('restart'); return b''
        if data in (b's', b'S'): self._svcmgr_action('stop');    return b''
        if data in (b'a', b'A'): self._svcmgr_action('start');   return b''
        if b'\x1b' in data:
            self._svcmgr_active = False; self._render(); return b''
        return b''

    def _svcmgr_action(self, action: str):
        if not self._svcmgr_names:
            return
        name = self._svcmgr_names[self._svcmgr_idx]
        try:
            subprocess.Popen(['sudo', 'systemctl', action, name])
        except Exception as e:
            logger.warning('svcmgr %s %s: %s', action, name, e)
        def _reload():
            time.sleep(1.0)
            self._svcmgr_items, self._svcmgr_names = self._load_service_list()
            if self._svcmgr_active:
                self._render()
        threading.Thread(target=_reload, daemon=True).start()

    # ─── Power menu (F5) ─────────────────────────────────────────────────────

    def _toggle_power(self):
        if self._power_active:
            self._power_active = False
        else:
            self._power_active = True
            self._power_idx = 2  # Cancel default — safest
            self._palette_active = self._clipboard_active = False
            self._prockill_active = self._svcmgr_active = False
        self._render()

    def _handle_power_key(self, data: bytes) -> bytes:
        if not self._power_active:
            return data
        if b'\x1b[A' in data:
            self._power_idx = max(0, self._power_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._power_idx = min(len(_POWER_ITEMS) - 1, self._power_idx + 1)
            self._render(); return b''
        if b'\r' in data or b'\n' in data:
            if self._power_idx == 0:
                subprocess.Popen(['sudo', 'shutdown', '-h', 'now'])
                self._running = False
            elif self._power_idx == 1:
                subprocess.Popen(['sudo', 'reboot'])
                self._running = False
            else:
                self._power_active = False
                self._render()
            return b''
        if b'\x1b' in data:
            self._power_active = False; self._render(); return b''
        return b''

    # ─── On-display config editor (Settings) ─────────────────────────────────

    def _settings_value(self, key, default=None):
        """Effective value: staged edit if present, else live config."""
        if key in self._settings_pending:
            return self._settings_pending[key]
        return self._config.get(key, default)

    def _settings_options(self, opts):
        """Resolve a schema options spec to a concrete list (fonts are dynamic)."""
        if opts == '__FONTS__':
            return self._available_fonts()
        return opts

    def _available_fonts(self) -> list:
        """Monospace fonts on the system (cached). '' = auto-detect."""
        if getattr(self, '_font_choices', None) is None:
            import glob
            fonts, seen = [''], set()
            for pat in ('/usr/share/fonts/truetype/**/*.ttf',
                        '/usr/share/fonts/**/*.ttf',
                        os.path.expanduser('~/.fonts/**/*.ttf')):
                for p in sorted(glob.glob(pat, recursive=True)):
                    if 'mono' in os.path.basename(p).lower() and p not in seen:
                        seen.add(p)
                        fonts.append(p)
            self._font_choices = fonts[:12]
        return self._font_choices

    def _settings_display_value(self, key, val) -> str:
        if key == 'terminal_font_path':
            return os.path.splitext(os.path.basename(val))[0] if val else 'auto'
        return str(val)

    def _settings_rows(self) -> list:
        """Build the overlay list: one row per setting + Save/Cancel actions."""
        rows = []
        for key, typ, label, _opts in _SETTINGS_SCHEMA:
            val = self._settings_value(key)
            if typ == 'bool':
                vstr = 'on' if val else 'off'
            else:
                vstr = self._settings_display_value(key, val)
            mark = '*' if key in self._settings_pending else ' '
            rows.append(f'{mark} {label:<16}[ {vstr} ]')
        # Shell-level edits restart on save; display-level edits apply instantly.
        save_label = ('  » Save & Restart' if set(self._settings_pending) & _SETTINGS_SHELL
                      else '  » Save (apply now)')
        rows.append(save_label)
        rows.append('  » Cancel (discard)')
        return rows

    def _toggle_settings(self):
        if self._settings_active:
            self._settings_active = False
        else:
            self._settings_pending = {}
            self._settings_idx = 0
            self._settings_active = True
            self._palette_active = self._clipboard_active = False
            self._prockill_active = self._svcmgr_active = False
            self._power_active = self._sshpick_active = False
        self._render()

    def _settings_change(self, delta: int):
        """Cycle/toggle the value of the setting under the cursor."""
        if self._settings_idx >= len(_SETTINGS_SCHEMA):
            return  # on an action row
        key, typ, _label, opts = _SETTINGS_SCHEMA[self._settings_idx]
        cur = self._settings_value(key)
        if typ == 'bool':
            self._settings_pending[key] = not bool(cur)
        elif typ == 'select':
            opts = self._settings_options(opts)
            if opts:
                try:
                    i = opts.index(cur)
                except ValueError:
                    i = 0
                self._settings_pending[key] = opts[(i + delta) % len(opts)]
        self._render()

    def _apply_live(self, key, value):
        """Apply a display-level setting to the running app immediately, so Save
        doesn't need a jarring full restart. self._config has already been
        updated, so QR (read from config at render time) needs nothing here."""
        if key == 'terminal_cursor_style':
            self._cursor_style = value
        elif key == 'terminal_dark_mode':
            self._dark_mode = bool(value)
        elif key == 'terminal_font_size':
            size = max(_MIN_FONT, min(_MAX_FONT, int(value)))
            if size != self._font_size:
                self._font_size = size
                self._reflow_shell()
        elif key == 'terminal_font_path':
            if value != self._font_path:
                self._font_path = value
                self._reflow_shell()
        elif key == 'terminal_split_view':
            if bool(value) != self._split_view:
                self._split_view = bool(value)
                self._reflow_shell()
        elif key == 'screensaver_sleep_minutes':
            self._idle_timeout = int(value) * 60
        elif key == 'display_sleep_minutes':
            self._sleep_timeout = int(value) * 60
        # terminal_show_qr: render reads self._config — no attribute to update.

    def _settings_save(self):
        """Persist staged changes. Display-level changes apply live; shell-level
        changes (prompt, start dir) restart the service to respawn the shell."""
        pending = dict(self._settings_pending)
        self._settings_active = False
        self._settings_pending = {}
        if not pending:
            self._render()
            return
        config_path = os.path.join(_REPO_ROOT, 'config', 'config.yaml')
        try:
            _save_config_values(config_path, pending)
        except Exception as e:
            logger.warning('settings save failed: %s', e)
        self._config.update(pending)   # keep the in-memory config current

        needs_restart = bool(set(pending) & _SETTINGS_SHELL)
        for key, value in pending.items():
            if key in _SETTINGS_LIVE:
                self._apply_live(key, value)
        self._render(force_full=True)

        if needs_restart:
            try:
                subprocess.Popen(['sudo', 'systemctl', 'restart', 'eink-display'])
                self._running = False
            except Exception as e:
                logger.warning('settings restart failed: %s', e)

    def _handle_settings_key(self, data: bytes) -> bytes:
        if not self._settings_active:
            return data
        n_rows = len(_SETTINGS_SCHEMA) + 2  # + Save + Cancel
        if b'\x1b[A' in data:
            self._settings_idx = max(0, self._settings_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._settings_idx = min(n_rows - 1, self._settings_idx + 1)
            self._render(); return b''
        if b'\x1b[C' in data or b' ' in data:   # Right / Space — next value
            self._settings_change(+1); return b''
        if b'\x1b[D' in data:                   # Left — previous value
            self._settings_change(-1); return b''
        if b'\r' in data or b'\n' in data:
            if self._settings_idx == len(_SETTINGS_SCHEMA):       # Save
                self._settings_save()
            elif self._settings_idx == len(_SETTINGS_SCHEMA) + 1: # Cancel
                self._settings_active = False; self._render()
            else:
                self._settings_change(+1)                         # toggle/cycle
            return b''
        if b'\x1b' in data:
            self._settings_active = False; self._render(); return b''
        return b''

    # ─── Palette actions (in-app, not shell commands) ─────────────────────────

    def _run_palette_action(self, action: str):
        if action == _SETTINGS_OPEN:
            self._toggle_settings()
        elif action == _SNIPPETS_OPEN:
            self._toggle_snippets()
        elif action == _BIGTEXT_OPEN:
            self._enter_big_text()
        elif action == _BEAM_OPEN:
            self._beam_to_phone()
        elif action == _HUD_TOGGLE:
            self._show_refresh_hud = not self._show_refresh_hud
            self._render(force_full=True)

    # ─── Snippets picker (curated saved_commands.txt) ─────────────────────────

    def _snippets_path(self) -> str:
        return os.path.join(_REPO_ROOT, 'config', 'saved_commands.txt')

    def _load_snippets(self) -> list:
        items = []
        try:
            for line in open(self._snippets_path()):
                cmd = line.strip()
                if cmd and not cmd.startswith('#') and cmd not in items:
                    items.append(cmd)
        except OSError:
            pass
        return items

    def _toggle_snippets(self):
        if self._snippets_active:
            self._snippets_active = False
        else:
            self._snippets_items = self._load_snippets()
            self._snippets_idx = 0
            self._snippets_active = True
            self._palette_active = self._clipboard_active = False
        self._render()

    def _handle_snippets_key(self, data: bytes) -> bytes:
        if not self._snippets_active:
            return data
        if b'\x1b[A' in data:
            self._snippets_idx = max(0, self._snippets_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._snippets_idx = min(len(self._snippets_items) - 1, self._snippets_idx + 1)
            self._render(); return b''
        if b'\r' in data or b'\n' in data:
            if self._snippets_items:
                cmd = self._snippets_items[self._snippets_idx]
                self._snippets_active = False
                self._render()
                if self._pty_master is not None:
                    os.write(self._pty_master, (cmd + '\n').encode())
            return b''
        if b'\x1b' in data:
            self._snippets_active = False; self._render(); return b''
        return b''

    # ─── Big text (momentary read mode) ───────────────────────────────────────

    def _enter_big_text(self):
        if self._big_text_active:
            return
        self._big_text_prev_font = self._font_size
        big = min(_MAX_FONT, max(self._font_size + 8, 24))
        if big == self._font_size:
            return  # already as large as it gets
        self._big_text_active = True
        self._font_size = big
        self._reflow_shell()
        self._render(force_full=True)

    def _exit_big_text(self):
        if not self._big_text_active:
            return
        self._big_text_active = False
        self._font_size = self._big_text_prev_font or self._font_size
        self._reflow_shell()
        self._render(force_full=True)

    # ─── Beam screen to phone ─────────────────────────────────────────────────

    def _screen_text(self) -> str:
        """Plain text of the visible screen (trailing blank lines/spaces trimmed)."""
        lines = []
        for row_idx in range(self._screen.lines):
            row = self._screen.buffer[row_idx]
            line = ''.join(row[c].data for c in range(self._screen.columns))
            lines.append(line.rstrip())
        while lines and not lines[-1]:
            lines.pop()
        return '\n'.join(lines)

    def _beam_to_phone(self):
        """Capture the visible screen text, hand it to the preview server, and
        pin a QR linking to the page that shows it (copyable on a phone)."""
        text = self._screen_text()
        ip = _get_local_ip()
        port = self._config.get('preview_server_port', 8080)
        server = getattr(self, '_preview_server', None)
        if server is not None and ip:
            server.set_beam_text(text)
            self._beam_url = f'http://{ip}:{port}/beam'
            self._beam_until_mono = time.monotonic() + 120  # show QR for 2 min
        self._render(force_full=True)

    # ─── Command palette ─────────────────────────────────────────────────────

    def _load_palette_items(self) -> list:
        # Leading entries are in-app actions (open an overlay / run in-app)
        # rather than shell commands — handled specially in _handle_palette_key.
        items = list(_PALETTE_ACTIONS)
        saved = os.path.join(_REPO_ROOT, 'config', 'saved_commands.txt')
        if os.path.exists(saved):
            try:
                for line in open(saved):
                    cmd = line.strip()
                    if cmd and not cmd.startswith('#') and cmd not in items:
                        items.append(cmd)
            except Exception:
                pass
        _ZSH_META = re.compile(r'^: \d+:\d+;')
        for hpath in [os.path.expanduser('~/.bash_history'), os.path.expanduser('~/.zsh_history')]:
            if os.path.exists(hpath):
                try:
                    hist = []
                    for line in reversed(open(hpath, errors='replace').readlines()):
                        cmd = _ZSH_META.sub('', line.strip()).strip()
                        if cmd and cmd not in hist:
                            hist.append(cmd)
                        if len(hist) >= 30:
                            break
                    for cmd in hist:
                        if cmd not in items:
                            items.append(cmd)
                    break
                except Exception:
                    pass
        return items[:50]

    def _toggle_palette(self):
        if self._palette_active:
            self._palette_active = False
        else:
            self._palette_items = self._load_palette_items()
            self._palette_idx = 0
            self._palette_active = True
            self._clipboard_active = False
        self._render()

    def _handle_palette_key(self, data: bytes) -> bytes:
        if not self._palette_active:
            return data
        if b'\x1b[A' in data:
            self._palette_idx = max(0, self._palette_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._palette_idx = min(len(self._palette_items) - 1, self._palette_idx + 1)
            self._render(); return b''
        if b'\r' in data or b'\n' in data:
            if self._palette_items:
                cmd = self._palette_items[self._palette_idx]
                self._palette_active = False
                if cmd in _PALETTE_ACTIONS:
                    self._run_palette_action(cmd)
                    return b''
                self._render()
                if self._pty_master is not None:
                    os.write(self._pty_master, (cmd + '\n').encode())
            return b''
        if b'\x1b' in data:
            self._palette_active = False; self._render(); return b''
        self._palette_active = False; self._render()
        return data

    # ─── Clipboard ───────────────────────────────────────────────────────────

    def _load_clipboard(self) -> list:
        try:
            items = json.load(open(self._clipboard_path))
            return [i for i in items if isinstance(i, dict) and 'text' in i][:20]
        except Exception:
            return []

    def _toggle_clipboard(self):
        if self._clipboard_active:
            self._clipboard_active = False
        elif self._clipboard:
            self._clipboard_idx = 0
            self._clipboard_active = True
            self._palette_active = False
        else:
            self._paste_from_file()
        self._render()

    def _handle_clipboard_key(self, data: bytes) -> bytes:
        if not self._clipboard_active:
            return data
        if b'\x1b[A' in data:
            self._clipboard_idx = max(0, self._clipboard_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._clipboard_idx = min(len(self._clipboard) - 1, self._clipboard_idx + 1)
            self._render(); return b''
        if b'\r' in data or b'\n' in data:
            if self._clipboard:
                text = self._clipboard[self._clipboard_idx].get('text', '')
                self._clipboard_active = False
                self._render()
                if self._pty_master is not None and text:
                    os.write(self._pty_master, (text + '\n').encode())
            return b''
        if b'\x1b' in data:
            self._clipboard_active = False; self._render(); return b''
        self._clipboard_active = False; self._render()
        return data

    def _toggle_url_qr(self):
        self._show_url_qr = not self._show_url_qr
        if not self._show_url_qr:
            self._last_url = ''
        self._render(force_full=False)

    def _scan_for_url(self) -> str:
        """Scan visible screen buffer bottom-to-top, return first URL found."""
        for row_idx in range(self._screen.lines - 1, -1, -1):
            row = self._screen.buffer[row_idx]
            line = ''.join(row[c].data for c in range(self._screen.columns))
            m = _URL_RE.search(line)
            if m:
                return m.group(0)
        return ''

    def _start_network_monitor_thread(self):
        """Run speedtest.net every speedtest_interval seconds (default 20 min).
        Falls back to a 5-second local throughput sample if speedtest is unavailable."""
        iface    = self._config.get('network_interface', '') or 'wlan0'
        interval = self._config.get('speedtest_interval', 1200)

        def _run_speedtest() -> tuple:
            """Returns (up_bps, down_bps). Tries speedtest-cli, then local sample."""
            # Try speedtest-cli Python package
            try:
                import speedtest as _st
                s = _st.Speedtest(secure=True)
                s.get_best_server()
                down_bps = s.download()
                up_bps   = s.upload()
                logger.info('Speedtest: ↓%.1f Mbps ↑%.1f Mbps',
                            down_bps / 1e6, up_bps / 1e6)
                return up_bps, down_bps
            except Exception:
                pass
            # Try speedtest-cli subprocess
            try:
                import json as _json
                r = subprocess.run(
                    ['speedtest-cli', '--json', '--secure'],
                    capture_output=True, text=True, timeout=60,
                )
                if r.returncode == 0:
                    d = _json.loads(r.stdout)
                    return d.get('upload', 0), d.get('download', 0)
            except Exception:
                pass
            # Fall back to 5-second local throughput sample
            try:
                import psutil as _psutil
                per = _psutil.net_io_counters(pernic=True)
                c0  = per.get(iface) or _psutil.net_io_counters()
                time.sleep(5)
                per = _psutil.net_io_counters(pernic=True)
                c1  = per.get(iface) or _psutil.net_io_counters()
                up   = (c1.bytes_sent - c0.bytes_sent) / 5.0 * 8
                down = (c1.bytes_recv - c0.bytes_recv) / 5.0 * 8
                return up, down
            except Exception:
                return 0.0, 0.0

        def _loop():
            while self._running:
                up_bps, down_bps = _run_speedtest()
                ip = _get_local_ip()
                with self._net_stats_lock:
                    self._net_stats = {
                        'ip':          ip,
                        'up':          _fmt_speed(up_bps),
                        'down':        _fmt_speed(down_bps),
                        'dirty':       True,
                        'wifi_signal': _read_wifi_signal(),
                    }
                if interval <= 0:
                    break
                deadline = time.monotonic() + interval
                while self._running and time.monotonic() < deadline:
                    time.sleep(30)

        threading.Thread(target=_loop, daemon=True, name='net-monitor').start()

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
                           tmux_session=self._tmux_session)]
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

    def _show_card(self, card: dict):
        """Render a rich card (note/countdown/todo/qr) from the web /card endpoint.

        Treated like a text message: shown full-screen and dismissed by any key.
        """
        try:
            from render import render_card
            img = render_card(card, self._config)
            self._driver.full_refresh(img)
            self._last_image = img
            self._in_text_message = True   # any key dismisses it, like a message
            self._screensaver_show_mono = time.monotonic()
            logger.info('Card shown: kind=%s', card.get('kind'))
        except Exception as e:
            logger.warning('Card render error: %s', e)

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
            from render import render_screensaver
            from preview_server import get_screensaver_images

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
            self._screensaver_show_mono = time.monotonic()
            logger.info('Screensaver activated — img=%s mode=%s', os.path.basename(image_path), mode)
            self._driver.sleep()   # power down panel; wakes automatically on next full_refresh
        except Exception as e:
            logger.warning('Screensaver render error: %s', e)

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
        status_info = self._get_status_info()
        if status_info is not None:
            tab_str = self._tab_indicator()
            uptime  = status_info[3] if len(status_info) > 3 else ''
            status_info = (status_info[0], status_info[1], status_info[2], tab_str, uptime)
        alerts = self._alert_monitor.active()

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

        found = self._scan_for_url()
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
            show_qr = self._show_url_qr and self._config.get('terminal_show_qr', True)
            url_qr = self._last_url if show_qr else None

        with self._net_stats_lock:
            net_stats = dict(self._net_stats) if self._net_stats else None

        if self._palette_active and self._palette_items:
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
        else:
            overlay = None

        tab_bar = [(self._tab_title(t), i == self._active_tab)
                   for i, t in enumerate(self._tabs)] if self._tabs else None

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
            and start_row == self._last_start_row
        )

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
                           pty_master=self._pty_master, child_pid=self._child_pid)]
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
                # Monitor ALL tab PTYs so background tabs stay current
                for tab in self._tabs:
                    if tab.pty_master is not None and tab.pty_master >= 0:
                        fds.append(tab.pty_master)
                r, _, _ = select.select(fds, [], [], _RENDER_DEBOUNCE)
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
            if self._reset_timeout > 0 and not self._did_idle_reset:
                if (now - self._last_input) > self._reset_timeout:
                    self._reset_session(render=not (in_screensaver or panel_asleep))
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
                        data = self._handle_hotkeys(data)
                        data = self._handle_prockill_key(data)
                        data = self._handle_svcmgr_key(data)
                        data = self._handle_power_key(data)
                        data = self._handle_settings_key(data)
                        data = self._handle_snippets_key(data)
                        data = self._handle_palette_key(data)
                        data = self._handle_clipboard_key(data)
                        data = self._handle_sshpick_key(data)
                        if data and self._pty_master is not None:
                            try:
                                os.write(self._pty_master, data)
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
                    data = self._handle_hotkeys(data)
                    data = self._handle_prockill_key(data)
                    data = self._handle_svcmgr_key(data)
                    data = self._handle_power_key(data)
                    data = self._handle_settings_key(data)
                    data = self._handle_snippets_key(data)
                    data = self._handle_palette_key(data)
                    data = self._handle_clipboard_key(data)
                    data = self._handle_sshpick_key(data)
                    if data and self._pty_master is not None:
                        try:
                            os.write(self._pty_master, data)
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
                        if tab_i == self._active_tab and not in_screensaver:
                            self._last_activity = now
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

            # ── Web input (phone keyboard via preview server) ─────────────────
            if self._web_input_queue is not None:
                try:
                    while True:
                        text = self._web_input_queue.get_nowait()
                        if text and self._pty_master is not None:
                            try:
                                os.write(self._pty_master, text.encode('utf-8'))
                            except OSError:
                                continue
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
                        elif action == 'clear':
                            in_screensaver = False; panel_asleep = False
                            self._in_text_message = False
                            self._last_input = now
                            self._clear_screen()
                        elif action == 'wake':
                            in_screensaver = False; panel_asleep = False
                            self._in_text_message = False
                            self._last_input = now
                            self._render(force_full=True)
                            self._last_full_refresh_mono = time.monotonic()
                        elif action == 'font_inc':
                            in_screensaver = False; panel_asleep = False
                            self._last_input = now
                            self._change_font(2)
                        elif action == 'font_dec':
                            in_screensaver = False; panel_asleep = False
                            self._last_input = now
                            self._change_font(-2)
                        elif action == 'dark_toggle':
                            in_screensaver = False; panel_asleep = False
                            self._last_input = now
                            self._toggle_dark_mode()
                        elif action == 'card':
                            in_screensaver = False; panel_asleep = False
                            self._show_card(cmd.get('card', {}))
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

            # ── Publish live status for the web /status panel (≤1/s) ──────────
            if self._preview_server is not None and (now - self._last_status_pub) >= 1.0:
                self._last_status_pub = now
                idle = now - self._last_input
                if in_screensaver:
                    state = 'screensaver'
                elif panel_asleep:
                    state = 'asleep'
                else:
                    state = 'active'
                self._preview_server.set_status({
                    'state': state,
                    'idle_secs': round(idle, 1),
                    'sleep_in_secs': (round(max(0, self._sleep_timeout - idle))
                                      if self._sleep_timeout > 0 else None),
                    'screensaver_in_secs': (round(max(0, self._idle_timeout - idle))
                                            if self._idle_timeout > 0 else None),
                    'last_full_refresh_secs': round(now - self._last_full_refresh_mono),
                    'font_size': self._font_size,
                    'dark_mode': self._dark_mode,
                })

            # ── Debounced render ──────────────────────────────────────────────
            if has_pending and not in_screensaver and not panel_asleep and (now - last_render) >= _RENDER_DEBOUNCE:
                self._render()
                self._screen.dirty.clear()
                has_pending = False
                last_render = now
            elif not in_screensaver and not panel_asleep and self._periodic_flash_due(now):
                # Deferred whole-panel ghost-clearing flash, fired in a quiet gap
                # so it never interrupts active typing.
                self._render(force_flash=True)
                self._screen.dirty.clear()
                last_render = now

    def _shell_exited_handler(self) -> bool:
        msg = (
            b'\r\n\x1b[7m  Shell exited. '
            b'Press Enter to restart or Ctrl+C to quit.  \x1b[0m\r\n'
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
        while True:
            self._watchdog.ping()   # this loop can block for a while awaiting a key
            r, _, _ = select.select([input_fd], [], [], 1.0)
            if not r:
                continue
            try:
                key = os.read(input_fd, 10)
            except OSError:
                return False
            if b'\r' in key or b'\n' in key:
                self._init_screen()
                self._spawn_shell()
                if self._tabs and 0 <= self._active_tab < len(self._tabs):
                    t = self._tabs[self._active_tab]
                    t.screen = self._screen; t.stream = self._stream
                    t.pty_master = self._pty_master; t.child_pid = self._child_pid
                self._render(force_full=True)
                return True
            if b'\x03' in key:
                self._running = False
                return False
