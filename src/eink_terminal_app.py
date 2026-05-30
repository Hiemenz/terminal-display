"""
E-ink terminal emulator core.

Forks a shell subprocess over a PTY, reads keyboard input in raw mode,
renders the terminal buffer via pyte, and pushes frames to the e-ink display.

Refresh strategy:
  - Partial refresh (fast, no flash) for every normal update.
  - Full refresh (slow, flash) every terminal_full_refresh_every updates to clear ghosting.
  - Immediate full refresh on font-size change or explicit F10 press.

Hotkeys:
  F9        — decrease font size (−2 pt)
  F12       — increase font size (+2 pt)
  F10       — force full display refresh
  F11       — switch to stats dashboard (launches main.py, exits terminal)
  Ctrl+C    — kill foreground process (forwarded to PTY as normal)
"""
import fcntl
import logging
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty

import pyte

from terminal_renderer import render_screen, terminal_dimensions, TERMINAL_H
from display_eink import EinkDriver

logger = logging.getLogger(__name__)

_RENDER_DEBOUNCE = 0.05   # seconds to wait for more PTY output before rendering
_MIN_FONT = 8
_MAX_FONT = 32

# Escape sequences for function keys (xterm/VT220)
_F9  = b'\x1b[20~'
_F10 = b'\x1b[21~'
_F11 = b'\x1b[23~'
_F12 = b'\x1b[24~'

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EinkTerminal:
    """Runs a shell in a PTY and mirrors output to the e-ink display."""

    def __init__(self, config: dict, local: bool = False):
        self._config = config
        self._font_size: int = config.get('terminal_font_size', 14)
        self._font_path: str = config.get('terminal_font_path', '')
        self._dark_mode: bool = config.get('terminal_dark_mode', True)
        self._full_every: int = config.get('terminal_full_refresh_every', 20)

        self._driver = EinkDriver(local=local)
        self._screen: pyte.Screen = None
        self._stream: pyte.ByteStream = None
        self._pty_master: int = None
        self._child_pid: int = None
        self._running = False
        self._partial_count = 0
        self._last_image = None
        self._stdin_fd = sys.stdin.fileno()
        self._old_tty: list = None

        self._init_screen()

    # ─── Screen / PTY ────────────────────────────────────────────────────────

    def _init_screen(self):
        cols, rows, _, _ = terminal_dimensions(self._font_size, self._font_path)
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)

    def _spawn_shell(self):
        shell = os.environ.get('SHELL', '/bin/bash')
        pid, master_fd = pty.fork()
        if pid == 0:
            os.execvp(shell, [shell])
            os._exit(1)
        self._child_pid = pid
        self._pty_master = master_fd
        self._sync_pty_winsize()

    def _sync_pty_winsize(self):
        cols, rows, _, _ = terminal_dimensions(self._font_size, self._font_path)
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

    # ─── Hotkeys ─────────────────────────────────────────────────────────────

    def _handle_hotkeys(self, data: bytes) -> bytes:
        """Strip hotkey sequences from data, act on them. Return remaining bytes."""
        if _F11 in data:
            self._switch_to_stats()
            data = data.replace(_F11, b'')
        if _F10 in data:
            self._force_full_refresh()
            data = data.replace(_F10, b'')
        if _F12 in data:
            self._change_font(+2)
            data = data.replace(_F12, b'')
        if _F9 in data:
            self._change_font(-2)
            data = data.replace(_F9, b'')
        return data

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

    # ─── Rendering ───────────────────────────────────────────────────────────

    def _render(self, force_full: bool = False):
        img = render_screen(
            self._screen,
            self._font_size,
            dark_mode=self._dark_mode,
            font_path=self._font_path,
        )
        self._last_image = img
        do_full = force_full or (self._partial_count >= self._full_every)

        if do_full:
            self._driver.full_refresh(img)
            self._partial_count = 0
        else:
            dirty_pixel_rows = self._dirty_pixel_rows()
            if dirty_pixel_rows:
                self._driver.partial_refresh_rows(img, dirty_pixel_rows)
                self._partial_count += 1
            # No dirty rows = nothing visible changed; skip the display push entirely

    def _dirty_pixel_rows(self) -> set:
        """Convert pyte's dirty terminal-row set to a set of pixel rows."""
        _, _, _, ch = terminal_dimensions(self._font_size, self._font_path)
        pixel_rows = set()
        for term_row in self._screen.dirty:
            y_start = term_row * ch
            y_end = min(y_start + ch, TERMINAL_H)
            for py in range(y_start, y_end):
                pixel_rows.add(py)
        return pixel_rows



    # ─── Main loop ───────────────────────────────────────────────────────────

    def run(self):
        self._spawn_shell()
        self._enter_raw()
        self._running = True
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

        while self._running:
            try:
                r, _, _ = select.select(
                    [self._stdin_fd, self._pty_master], [], [], _RENDER_DEBOUNCE
                )
            except (ValueError, OSError):
                break

            now = time.monotonic()

            # ── Keyboard input ────────────────────────────────────────────────
            if self._stdin_fd in r:
                try:
                    data = os.read(self._stdin_fd, 256)
                except OSError:
                    break
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
                        self._stream.feed(chunk)
                        has_pending = True
                except OSError:
                    # Shell exited
                    if not self._shell_exited_handler():
                        break
                    has_pending = True

            # ── Debounced render ──────────────────────────────────────────────
            if has_pending and (now - last_render) >= _RENDER_DEBOUNCE:
                self._render()
                self._screen.dirty.clear()
                has_pending = False
                last_render = now

    def _shell_exited_handler(self) -> bool:
        """
        Shell process has exited. Show a message and wait for Enter (restart)
        or Ctrl+C (quit). Returns True to continue the main loop (restarted),
        False to exit.
        """
        msg = (
            b'\r\n\x1b[7m  Shell exited. '
            b'Press Enter to restart or Ctrl+C to quit.  \x1b[0m\r\n'
        )
        self._stream.feed(msg)
        self._render(force_full=True)
        self._screen.dirty.clear()

        # Close old PTY
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
            if b'\x03' in key:  # Ctrl+C
                self._running = False
                return False
