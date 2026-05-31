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
import logging
import os
import pty
import queue as _queue
import select
import shutil
import signal
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
    render_screen, render_mini_stats, terminal_dimensions,
    TERMINAL_H, SPLIT_TERMINAL_W,
)
from display_eink import EinkDriver
from preview_server import start_if_enabled as _start_preview

logger = logging.getLogger(__name__)

_RENDER_DEBOUNCE   = 0.05   # seconds
_STATUS_CACHE_TTL  = 5.0    # seconds between CWD/branch re-reads
_STATS_UPDATE_SEC  = 30     # seconds between split-view stats refreshes
_MIN_FONT = 8
_MAX_FONT = 32


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
_F11  = b'\x1b[23~'
_F12  = b'\x1b[24~'
_PGUP = b'\x1b[5~'
_PGDN = b'\x1b[6~'

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EinkTerminal:
    """Runs a shell in a PTY and mirrors output to the e-ink display."""

    def __init__(self, config: dict, local: bool = False):
        self._config    = config
        self._font_size = config.get('terminal_font_size', 14)
        self._font_path = config.get('terminal_font_path', '')
        self._dark_mode = config.get('terminal_dark_mode', True)
        self._full_every  = config.get('terminal_full_refresh_every', 20)
        self._idle_timeout = config.get('terminal_idle_timeout', 0)
        self._split_view  = config.get('terminal_split_view', False)
        self._status_extras = config.get('terminal_status_bar_extras', True)

        # tmux
        self._use_tmux     = config.get('terminal_use_tmux', False) and bool(shutil.which('tmux'))
        self._tmux_session = config.get('terminal_tmux_session', 'eink')

        self._driver      = EinkDriver(local=local)
        self._screen      = None
        self._stream      = None
        self._pty_master  = None
        self._child_pid   = None
        self._running     = False
        self._partial_count = 0
        self._last_image  = None
        self._stdin_fd    = sys.stdin.fileno()
        self._old_tty     = None

        # Scrollback state (only when not using tmux)
        self._scroll_pages = 0

        # Idle tracking
        self._last_activity = time.monotonic()
        self._idle_refresh_interval = config.get('terminal_idle_refresh_interval', 900)
        self._last_idle_refresh = time.monotonic()

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

        self._init_screen()

    # ─── Screen ──────────────────────────────────────────────────────────────

    def _init_screen(self):
        tw = SPLIT_TERMINAL_W if self._split_view else 800
        cols, rows, _, _ = terminal_dimensions(self._font_size, self._font_path, tw)
        if self._use_tmux:
            self._screen = pyte.Screen(cols, rows)
        else:
            history = self._config.get('terminal_scrollback', 500)
            self._screen = pyte.HistoryScreen(cols, rows, history=history)
        self._stream = pyte.ByteStream(self._screen)
        self._scroll_pages = 0

    # ─── PTY ─────────────────────────────────────────────────────────────────

    def _spawn_shell(self):
        pid, master_fd = pty.fork()
        if pid == 0:
            os.environ['TERM'] = 'xterm-256color'
            if self._use_tmux:
                os.execvp('tmux', ['tmux', 'new-session', '-A', '-s', self._tmux_session])
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
        self._old_tty = termios.tcgetattr(self._stdin_fd)
        tty.setraw(self._stdin_fd)

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
        if _F7 in data:
            self._toggle_dark_mode()
            data = data.replace(_F7, b'')
        if _F8 in data:
            self._paste_from_file()
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

    def _force_full_refresh(self):
        if self._last_image is not None:
            self._driver.full_refresh(self._last_image)
            self._partial_count = 0

    def _switch_to_stats(self):
        self._running = False
        if self._child_pid:
            try:
                os.kill(self._child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        main_py = os.path.join(_REPO_ROOT, 'main.py')
        subprocess.Popen(
            [sys.executable, main_py],
            close_fds=True,
            start_new_session=True,
        )

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
        alerts = self._alert_monitor.active()

        img = render_screen(
            self._screen,
            self._font_size,
            dark_mode=self._dark_mode,
            font_path=self._font_path,
            terminal_width=tw,
            status_info=status_info,
            alerts=alerts if alerts else None,
            hq=self._hq_render,
        )

        # Overlay split-view sidebar
        if self._split_view:
            with self._stats_lock:
                stats = self._stats_data
                self._stats_dirty = False
            render_mini_stats(img, stats, dark_mode=self._dark_mode)

        self._last_image = img
        do_full = force_full or (self._partial_count >= self._full_every)

        if do_full:
            self._driver.full_refresh(img)
            self._partial_count = 0
        else:
            dirty_pixel_rows = self._dirty_pixel_rows()
            if dirty_pixel_rows:
                self._driver.partial_refresh_rows(img, dirty_pixel_rows)
            else:
                # Fallback: no dirty rows tracked (e.g. screen-clear, pyte edge case)
                # — do a full-screen partial so the display never freezes
                self._driver.partial_refresh(img)
            self._partial_count += 1

    def _dirty_pixel_rows(self) -> set:
        """Convert pyte's dirty terminal-row set to pixel rows."""
        _, _, _, ch = terminal_dimensions(
            self._font_size, self._font_path,
            SPLIT_TERMINAL_W if self._split_view else 800,
        )
        pixel_rows = set()
        for term_row in self._screen.dirty:
            y_start = term_row * ch
            y_end = min(y_start + ch, TERMINAL_H)
            pixel_rows.update(range(y_start, y_end))
        return pixel_rows

    # ─── Main entry point ─────────────────────────────────────────────────────

    def run(self):
        self._spawn_shell()
        self._enter_raw()
        self._running = True
        self._last_activity = time.monotonic()

        if self._split_view:
            self._start_stats_thread()

        server = _start_preview(self._config, os.path.join(_REPO_ROOT, 'output', 'terminal.bmp'))
        if server is not None:
            self._web_input_queue = server.input_queue
        self._render(force_full=True)

        try:
            self._loop()
        finally:
            self._exit_raw()
            self._driver.sleep()
            if self._child_pid:
                try:
                    os.waitpid(self._child_pid, os.WNOHANG)
                except ChildProcessError:
                    pass

    def _loop(self):
        last_render = 0.0
        has_pending = False
        last_alert_tick = 0.0

        while self._running:
            try:
                fds = [self._stdin_fd]
                if self._pty_master is not None:
                    fds.append(self._pty_master)
                r, _, _ = select.select(fds, [], [], _RENDER_DEBOUNCE)
            except (ValueError, OSError):
                break

            now = time.monotonic()

            # ── Idle screensaver check ────────────────────────────────────────
            if self._idle_timeout > 0:
                if now - self._last_activity > self._idle_timeout:
                    self._switch_to_stats()
                    break

            # ── Idle periodic full refresh (clears e-ink ghosting) ────────────
            if self._idle_refresh_interval > 0:
                idle = now - self._last_activity
                since_refresh = now - self._last_idle_refresh
                if idle >= self._idle_refresh_interval and since_refresh >= self._idle_refresh_interval:
                    self._force_full_refresh()
                    self._last_idle_refresh = now

            # ── Keyboard input ────────────────────────────────────────────────
            if self._stdin_fd in r:
                try:
                    data = os.read(self._stdin_fd, 256)
                except OSError:
                    break
                self._last_activity = now
                self._last_idle_refresh = now  # reset idle-refresh timer on activity
                # Snap to live before passing any key to the shell
                if self._scroll_pages > 0:
                    self._snap_to_live()
                    has_pending = True
                data = self._handle_hotkeys(data)
                if data and self._pty_master is not None:
                    try:
                        os.write(self._pty_master, data)
                    except OSError:
                        pass

            # ── PTY output ───────────────────────────────────────────────────
            if self._pty_master is not None and self._pty_master in r:
                try:
                    chunk = os.read(self._pty_master, 4096)
                    if chunk:
                        # New output snaps back to live view
                        if self._scroll_pages > 0:
                            self._snap_to_live()
                        chunk = _filter_pty_output(chunk, self._pty_master)
                        if chunk:
                            self._stream.feed(chunk)
                        has_pending = True
                except OSError:
                    if not self._shell_exited_handler():
                        break
                    has_pending = True

            # ── Web input (phone keyboard via preview server) ─────────────────
            if self._web_input_queue is not None:
                try:
                    while True:
                        text = self._web_input_queue.get_nowait()
                        if text and self._pty_master is not None:
                            os.write(self._pty_master, text.encode('utf-8'))
                            self._last_activity = now
                            has_pending = True
                except _queue.Empty:
                    pass

            # ── Alert tick ────────────────────────────────────────────────────
            if now - last_alert_tick >= 1.0:
                if self._alert_monitor.tick():
                    has_pending = True  # alert changed — re-render status bar
                last_alert_tick = now

            # ── Split-view stats update ───────────────────────────────────────
            if self._split_view:
                with self._stats_lock:
                    stats_dirty = self._stats_dirty
                if stats_dirty:
                    has_pending = True

            # ── Debounced render ──────────────────────────────────────────────
            if has_pending and (now - last_render) >= _RENDER_DEBOUNCE:
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

        while True:
            r, _, _ = select.select([self._stdin_fd], [], [], 1.0)
            if not r:
                continue
            try:
                key = os.read(self._stdin_fd, 10)
            except OSError:
                return False
            if b'\r' in key or b'\n' in key:
                self._init_screen()
                self._spawn_shell()
                self._render(force_full=True)
                return True
            if b'\x03' in key:
                self._running = False
                return False
