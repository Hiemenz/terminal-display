"""EinkTerminal mixin: left/right split pane (Ctrl+\\ toggle, Ctrl+] swap focus)."""
from __future__ import annotations

import fcntl
import os
import pty
import pwd
import signal
import struct
import subprocess
import termios

import pyte

from terminal_renderer import terminal_dimensions


class SplitPaneMixin:
    """Split-pane lifecycle: spawn/close the secondary PTY, swap input focus."""

    def _split_half_w(self) -> int:
        from terminal_renderer import SPLIT_DIVIDER_W
        return (800 - SPLIT_DIVIDER_W) // 2

    def _init_split_pane(self):
        """Spawn a second shell in a new PTY for left/right split view."""
        tab = self._current_tab()
        if tab is None or tab.split_dir:
            return
        half_w = self._split_half_w()
        cols, rows, _, _ = terminal_dimensions(self._font_size, self._font_path, half_w)
        tab.pane2_screen = pyte.Screen(cols, rows)
        tab.pane2_stream = pyte.ByteStream(tab.pane2_screen)
        new_session = f'{self._tmux_session}-p2'
        pid, master_fd = pty.fork()
        if pid == 0:
            os.environ['TERM'] = 'xterm-256color'
            start_dir = self._resolve_start_dir()
            if self._use_tmux:
                os.execvp('tmux', self._tmux_launch_argv(new_session, start_dir,
                                                          self._build_prompt_command()))
            else:
                if start_dir:
                    try: os.chdir(start_dir)
                    except OSError: pass
                promptcmd = self._build_prompt_command()
                if promptcmd:
                    os.execvp('/bin/sh', ['/bin/sh', '-c', promptcmd])
                shell = (os.environ.get('SHELL')
                         or pwd.getpwuid(os.getuid()).pw_shell or '/bin/bash')
                os.execvp(shell, ['-' + os.path.basename(shell)])
            os._exit(1)
        tab.pane2_pid = pid
        tab.pane2_master = master_fd
        tab.pane2_tmux = new_session
        tab.split_dir = 'h'
        tab.pane_focus = 0
        winsize = struct.pack('HHHH', rows, cols, 0, 0)
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    def _close_split_pane(self):
        tab = self._current_tab()
        if tab is None or not tab.split_dir:
            return
        if tab.pane2_pid:
            try: os.kill(tab.pane2_pid, signal.SIGTERM)
            except (ProcessLookupError, OSError): pass
        if tab.pane2_master >= 0:
            try: os.close(tab.pane2_master)
            except OSError: pass
        if self._use_tmux and tab.pane2_tmux:
            try:
                subprocess.run(['tmux', 'kill-session', '-t', tab.pane2_tmux],
                               capture_output=True, timeout=2)
            except Exception:
                pass
        tab.pane2_screen = None
        tab.pane2_stream = None
        tab.pane2_master = -1
        tab.pane2_pid = 0
        tab.pane2_tmux = ''
        tab.split_dir = ''
        tab.pane_focus = 0

    def _toggle_split_pane(self):
        tab = self._current_tab()
        if tab is None:
            return
        if tab.split_dir:
            self._close_split_pane()
        else:
            self._init_split_pane()
        self._img_cache = None
        self._render(force_full=True)

    def _swap_pane_focus(self):
        tab = self._current_tab()
        if tab is None or not tab.split_dir:
            return
        tab.pane_focus = 1 - tab.pane_focus
        self._render()

    def _get_focused_pty(self) -> int | None:
        """Return the PTY fd that should receive keyboard input."""
        tab = self._current_tab()
        if tab and tab.split_dir and tab.pane_focus == 1 and tab.pane2_master >= 0:
            return tab.pane2_master
        return self._pty_master
