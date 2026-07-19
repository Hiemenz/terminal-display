"""EinkTerminal mixin: top-level hotkey dispatch (_handle_hotkeys) and the
small per-key actions (dark mode, paste, font size, reflow) it delegates to."""
from __future__ import annotations

import fcntl
import os
import signal
import struct
import termios
import time

import pyte

from terminal_renderer import terminal_dimensions
from terminal_state import (
    _ALT_DIGITS,
    _CTRL_BACKSLASH,
    _CTRL_BRACKETRIGHT,
    _CTRL_F,
    _CTRL_LEFT,
    _CTRL_N,
    _CTRL_RIGHT,
    _CTRL_SLASH,
    _CTRL_SPACE,
    _CTRL_T,
    _F1,
    _F2,
    _F3,
    _F4,
    _F5,
    _F6,
    _F7,
    _F8,
    _F9,
    _F10,
    _F11,
    _F12,
    _MAX_FONT,
    _MIN_FONT,
    _PGDN,
    _PGUP,
)


class HotkeysMixin:
    """Hotkey byte-sequence dispatch and the small actions it triggers directly."""

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
        if _CTRL_F in data:
            self._toggle_search()
            data = data.replace(_CTRL_F, b'')
        if _CTRL_T in data:
            self._new_tab()
            data = data.replace(_CTRL_T, b'')
        if _CTRL_N in data:
            self._cycle_mode()
            data = data.replace(_CTRL_N, b'')
        if _CTRL_BACKSLASH in data:
            self._toggle_split_pane()
            data = data.replace(_CTRL_BACKSLASH, b'')
        if _CTRL_BRACKETRIGHT in data:
            self._swap_pane_focus()
            data = data.replace(_CTRL_BRACKETRIGHT, b'')
        if _CTRL_SLASH in data:
            self._toggle_help()
            data = data.replace(_CTRL_SLASH, b'')
        if _CTRL_SPACE in data:
            self._toggle_copy_mode()
            data = data.replace(_CTRL_SPACE, b'')
        for seq, n in _ALT_DIGITS.items():
            if seq in data:
                if n <= len(self._tabs):
                    self._goto_tab(n - 1)
                data = data.replace(seq, b'')
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
                        time.sleep(0.01)
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
        # The grid is being rebuilt — a stale copy-mode cursor/anchor could
        # land out of bounds, so just drop the in-progress selection.
        self._copy_active = False
        self._copy_anchor = None
        self._init_screen()
        self._img_cache = None   # layout changed — drop the incremental cache
        self._sync_pty_winsize()
        if self._child_pid:
            try:
                os.kill(self._child_pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass
        # Resize split pane if open on the active tab
        tab = self._current_tab()
        if tab and tab.split_dir and tab.pane2_master >= 0:
            half_w = self._split_half_w()
            cols, rows, _, _ = terminal_dimensions(self._font_size, self._font_path, half_w)
            tab.pane2_screen = pyte.Screen(cols, rows)
            tab.pane2_stream = pyte.ByteStream(tab.pane2_screen)
            winsize = struct.pack('HHHH', rows, cols, 0, 0)
            try:
                fcntl.ioctl(tab.pane2_master, termios.TIOCSWINSZ, winsize)
            except Exception:
                pass
            if tab.pane2_pid:
                try: os.kill(tab.pane2_pid, signal.SIGWINCH)
                except (ProcessLookupError, OSError): pass
