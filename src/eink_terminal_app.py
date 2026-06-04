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

Hotkeys:
  F9        — decrease font size (−2 pt)
  F12       — increase font size (+2 pt)
  F10       — force full display refresh (clear ghosting)
  F11       — switch to stats dashboard
  PgUp      — scroll up through history (no-tmux mode only)
  PgDn      — scroll down / return to live
  Ctrl+C    — kill foreground process (forwarded normally)
"""
import fcntl
import json
import logging
import os
from dataclasses import dataclass
import pty
import queue as _queue
import re
import select
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
from terminal_renderer import (
    render_screen, render_screen_partial, render_mini_stats, terminal_dimensions,
    TERMINAL_H, SPLIT_TERMINAL_W,
)
from display_eink import EinkDriver
from preview_server import start_if_enabled as _start_preview
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
        self._full_refresh_interval = config.get('terminal_full_refresh_interval', 60)
        self._idle_timeout = config.get('terminal_idle_timeout', 0)
        self._split_view  = config.get('terminal_split_view', False)
        self._status_extras  = config.get('terminal_status_bar_extras', True)

        # tmux
        self._use_tmux     = config.get('terminal_use_tmux', False) and bool(shutil.which('tmux'))
        self._tmux_session = config.get('terminal_tmux_session', 'eink')

        self._driver      = EinkDriver(local=local,
                                       partial_refresh_limit=config.get('partial_refresh_before_full', 30))
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

        # Status bar item visibility
        self._bar_config = {
            'show_time':  config.get('terminal_status_bar_show_time',  True),
            'show_cwd':   config.get('terminal_status_bar_show_cwd',   True),
            'show_ip':    config.get('terminal_status_bar_show_ip',    True),
            'show_speed': config.get('terminal_status_bar_show_speed', True),
        }

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

        # Idle tracking
        self._last_activity = time.monotonic()
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
        self._screensaver_show_mono  = 0.0   # when screensaver was last shown (for grace period)

        # Text message (send-to-display) state
        self._in_text_message = False
        self._display_queue = None   # set in run() after server starts

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
        pid, master_fd = pty.fork()
        if pid == 0:
            os.environ['TERM'] = 'xterm-256color'
            if self._use_tmux:
                os.execvp('tmux', ['tmux', 'new-session', '-A', '-s', session])
            else:
                shell = os.environ.get('SHELL', '/bin/bash')
                os.execvp(shell, [shell])
            os._exit(1)
        self._child_pid = pid
        self._pty_master = master_fd
        self._sync_pty_winsize()

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
        self._init_screen()
        self._sync_pty_winsize()
        if self._child_pid:
            try:
                os.kill(self._child_pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass
        self._render(force_full=True)

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

    # ─── Command palette ─────────────────────────────────────────────────────

    def _load_palette_items(self) -> list:
        items = []
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

    def _show_screensaver(self):
        """Render the screensaver to the display.

        In 'cycle' mode, advances through gallery photos every N minutes.
        In 'static' mode (default), always shows the gallery-selected image.
        Always shows a QR code overlay pointing to the preview server.
        """
        try:
            from render import render_screensaver
            from preview_server import get_screensaver_path, _list_photos

            mode = self._config.get('screensaver_mode', 'static')
            static_path = self._config.get('screensaver_image_path', 'assets/test.jpg')
            if not os.path.isabs(static_path):
                static_path = os.path.join(_REPO_ROOT, static_path)
            photos_dir = os.path.join(_REPO_ROOT, 'assets', 'gallery')

            if mode == 'cycle':
                photos = _list_photos(photos_dir)
                if photos:
                    cycle_secs = self._config.get('screensaver_cycle_interval', 5) * 60
                    now = time.monotonic()
                    if self._screensaver_last_cycle == 0.0 or \
                            (now - self._screensaver_last_cycle) >= cycle_secs:
                        self._screensaver_cycle_idx = (self._screensaver_cycle_idx + 1) % len(photos)
                        self._screensaver_last_cycle = now
                    image_path = os.path.join(photos_dir, photos[self._screensaver_cycle_idx])
                else:
                    image_path = static_path
            else:
                image_path = get_screensaver_path(photos_dir) or static_path

            port = self._config.get('preview_server_port', 8080)
            ip = _get_local_ip()
            qr_url = f'http://{ip}:{port}/config' if ip else ''

            img = render_screensaver(image_path, qr_url, self._config)
            self._driver.full_refresh(img)
            self._last_image = img
            self._screensaver_show_mono = time.monotonic()
            logger.info('Screensaver activated — img=%s mode=%s', os.path.basename(image_path), mode)
            self._driver.sleep()   # power down panel; wakes automatically on next full_refresh
        except Exception as e:
            logger.warning('Screensaver render error: %s', e)

    # ─── Status bar info ──────────────────────────────────────────────────────

    def _get_status_info(self) -> tuple:
        """Return (time_str, cwd, git_branch), cached for _STATUS_CACHE_TTL seconds."""
        if not self._status_extras:
            return None
        now = time.monotonic()
        if self._status_cache and now - self._status_cache[0] < _STATUS_CACHE_TTL:
            return self._status_cache[1:]

        import datetime
        time_str = datetime.datetime.now().strftime('%H:%M')
        cwd = self._get_cwd()
        branch = self._get_git_branch(cwd) if cwd else ''
        self._status_cache = (now, time_str, cwd, branch)
        return time_str, cwd, branch

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

    def _render(self, force_full: bool = False):
        tw = SPLIT_TERMINAL_W if self._split_view else 800
        status_info = self._get_status_info()
        if status_info is not None:
            tab_str = f'[{self._active_tab+1}/{len(self._tabs)}]' if len(self._tabs) > 1 else ''
            status_info = (status_info[0], status_info[1], status_info[2], tab_str)
        alerts = self._alert_monitor.active()

        found = self._scan_for_url()
        if found:
            self._last_url = found
        elif not self._last_url:
            _ip = _get_local_ip()
            _port = self._config.get('preview_server_port', 8080)
            if _ip:
                self._last_url = f'http://{_ip}:{_port}/config'
        show_qr = self._show_url_qr and self._config.get('terminal_show_qr', True)
        url_qr = self._last_url if show_qr else None

        with self._net_stats_lock:
            net_stats = dict(self._net_stats) if self._net_stats else None

        if self._palette_active and self._palette_items:
            overlay = (self._palette_items, self._palette_idx, 'Commands')
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

        # Use incremental rendering when the cache is warm and no large change
        # (overlay, scroll, split sidebar) invalidates the full layout.
        use_incremental = (
            self._img_cache is not None
            and not force_full
            and overlay is None
            and not self._split_view
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
            )
        else:
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

        self._last_cursor_row = self._screen.cursor.y
        self._last_image = img
        recently_active = (time.monotonic() - self._last_activity) < 60.0
        time_full = (recently_active and
                     self._full_refresh_interval > 0 and
                     (time.monotonic() - self._last_full_refresh_mono) >= self._full_refresh_interval)
        do_full = force_full or time_full

        if do_full:
            self._driver.full_refresh(img)
            self._last_full_refresh_mono = time.monotonic()
        else:
            self._driver.partial_refresh_diff(img)

    # ─── Main entry point ─────────────────────────────────────────────────────

    def run(self):
        try:
            with open('/tmp/eink-terminal-active', 'w') as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

        self._spawn_shell()
        self._enter_raw()
        if self._evdev_kb:
            self._evdev_kb.grab()
        self._running = True
        self._last_activity = time.monotonic()

        # Wrap initial shell in a Tab
        self._tabs = [_Tab(screen=self._screen, stream=self._stream,
                           pty_master=self._pty_master, child_pid=self._child_pid)]
        self._active_tab = 0

        if self._split_view:
            self._start_stats_thread()

        self._start_network_monitor_thread()

        _config_path = os.path.join(_REPO_ROOT, 'config', 'config.yaml')
        server = _start_preview(self._config, os.path.join(_REPO_ROOT, 'output', 'terminal.bmp'),
                                config_path=_config_path,
                                clipboard_path=self._clipboard_path)
        if server is not None:
            self._web_input_queue = server.input_queue
            self._display_queue   = server.display_queue
        self._render(force_full=True)

        try:
            self._loop()
        finally:
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

        while self._running:
            now = time.monotonic()

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

            # ── Idle screensaver check ────────────────────────────────────────
            if self._idle_timeout > 0:
                idle = now - self._last_activity
                if idle > self._idle_timeout and not in_screensaver and not self._in_text_message:
                    in_screensaver = True
                    self._show_screensaver()
                    continue  # skip stale r — next iteration runs a fresh select

            # ── Keyboard input (evdev path) ───────────────────────────────────
            if self._evdev_kb is not None and self._evdev_kb.fileno() in r:
                try:
                    data = self._evdev_kb.read()
                except OSError:
                    self._evdev_disconnect()
                    continue
                if data:
                    self._last_activity = now
                    grace = now - self._screensaver_show_mono < 2.0
                    if in_screensaver or self._in_text_message:
                        if not grace:
                            in_screensaver = False
                            self._in_text_message = False
                            self._render(force_full=True)
                            self._last_full_refresh_mono = time.monotonic()
                        # swallow the wake key regardless
                    else:
                        if self._scroll_pages > 0:
                            self._snap_to_live()
                            has_pending = True
                        data = self._handle_hotkeys(data)
                        data = self._handle_prockill_key(data)
                        data = self._handle_svcmgr_key(data)
                        data = self._handle_power_key(data)
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
                self._last_activity = now
                grace = now - self._screensaver_show_mono < 2.0
                if in_screensaver or self._in_text_message:
                    if not grace:
                        in_screensaver = False
                        self._in_text_message = False
                        self._render(force_full=True)
                        self._last_full_refresh_mono = time.monotonic()
                    # swallow the wake key regardless
                else:
                    if self._scroll_pages > 0:
                        self._snap_to_live()
                        has_pending = True
                    data = self._handle_hotkeys(data)
                    data = self._handle_prockill_key(data)
                    data = self._handle_svcmgr_key(data)
                    data = self._handle_power_key(data)
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

            # ── Web input (phone keyboard via preview server) ─────────────────
            if self._web_input_queue is not None:
                try:
                    while True:
                        text = self._web_input_queue.get_nowait()
                        if text and self._pty_master is not None:
                            os.write(self._pty_master, text.encode('utf-8'))
                            self._last_activity = now
                            if in_screensaver or self._in_text_message:
                                in_screensaver = False
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
                except _queue.Empty:
                    pass

            # ── Alert tick ────────────────────────────────────────────────────
            if now - last_alert_tick >= 1.0:
                if self._alert_monitor.tick() and not in_screensaver:
                    has_pending = True  # alert changed — re-render status bar
                last_alert_tick = now

            _idle = now - self._last_activity

            # ── Split-view stats update ───────────────────────────────────────
            if self._split_view and not in_screensaver and _idle < 60.0:
                with self._stats_lock:
                    stats_dirty = self._stats_dirty
                if stats_dirty:
                    has_pending = True

            # ── Network stats update ──────────────────────────────────────────
            if not in_screensaver and _idle < 60.0:
                with self._net_stats_lock:
                    if self._net_stats.get('dirty'):
                        self._net_stats['dirty'] = False
                        has_pending = True

            # ── Cycle screensaver: swap image when interval elapses ───────────
            if in_screensaver and self._config.get('screensaver_mode', 'static') == 'cycle':
                cycle_secs = self._config.get('screensaver_cycle_interval', 5) * 60
                if self._screensaver_last_cycle > 0.0 and (now - self._screensaver_last_cycle) >= cycle_secs:
                    self._show_screensaver()

            # ── Debounced render ──────────────────────────────────────────────
            if has_pending and not in_screensaver and (now - last_render) >= _RENDER_DEBOUNCE:
                self._render()
                self._screen.dirty.clear()
                has_pending = False
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
