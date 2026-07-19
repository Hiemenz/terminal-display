"""EinkTerminal mixin: tab lifecycle (new/close/switch/goto) and busy-detection
used by the idle-reset state machine to avoid killing a live session."""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import signal
import subprocess
import time

from session_logger import TabLogger
from terminal_state import (
    _MODE_CYCLE,
    _MODE_LLM,
    _MODE_NOTES,
    _MODE_TERMINAL,
    _REPO_ROOT,
    _Tab,
)

logger = logging.getLogger(__name__)

_MODE_TITLES = {_MODE_NOTES: 'notes', _MODE_LLM: 'llm'}


class TabsMixin:
    """Tab creation/switching and the busy-check idle-reset relies on."""

    def _current_tab(self):
        tabs = getattr(self, '_tabs', None)
        if tabs and 0 <= self._active_tab < len(tabs):
            return tabs[self._active_tab]
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
        """Status-bar tab chip: '[2/3 projdir] •4' — the count, the active
        tab's short working-dir/title, and a bullet per background tab number
        with unseen output. Empty when only one tab is open."""
        if len(self._tabs) <= 1:
            return ''
        base = f'{self._active_tab + 1}/{len(self._tabs)}'
        tab = self._current_tab()
        name = self._tab_title(tab) if tab else ''
        indicator = f'[{base} {name}]' if name else f'[{base}]'
        busy = [str(i + 1) for i, t in enumerate(self._tabs) if t.activity]
        if busy:
            indicator += ' •' + ','.join(busy)
        return indicator

    def _make_tab_logger(self):
        """Return a fresh TabLogger for a newly spawned tab, or None if
        terminal_log_enabled is off."""
        if not self._log_enabled:
            return None
        self._tab_log_seq += 1
        return TabLogger(self._log_dir, f'tab{self._tab_log_seq}',
                          max_bytes=self._log_max_bytes,
                          max_files=self._log_max_files)

    def _sync_active_tab(self):
        if self._tabs and 0 <= self._active_tab < len(self._tabs):
            self._tabs[self._active_tab].scroll_pages = self._scroll_pages

    def _new_tab(self, cmd: str = None, mode: str = ''):
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
                               tmux_session=new_session, logger=self._make_tab_logger(),
                               mode=mode, title=_MODE_TITLES.get(mode, '')))
        self._active_tab = len(self._tabs) - 1
        self._scroll_pages = 0
        if cmd:
            try:
                os.write(self._pty_master, (cmd + '\n').encode())
            except OSError:
                pass
        self._render(force_full=True)

    # ─── Modes (Ctrl+N / F6 palette): terminal / notes / local LLM chat ────────

    def _notes_path(self) -> str:
        rel = str(self._config.get('terminal_notes_file', '') or 'data/notes.txt')
        return rel if os.path.isabs(rel) else os.path.join(_REPO_ROOT, rel)

    def _open_mode_tab(self, mode: str, cmd: str):
        """Jump to the existing tab for `mode` if one is open, else open one."""
        for i, t in enumerate(self._tabs):
            if t.mode == mode:
                self._goto_tab(i)
                return
        self._new_tab(cmd=cmd, mode=mode)

    def _open_notes(self):
        path = self._notes_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            pass
        self._open_mode_tab(_MODE_NOTES, 'nano ' + shlex.quote(path))

    def _open_llm_chat(self):
        script = os.path.join(_REPO_ROOT, 'src', 'llm_chat.py')
        cmd = f'cd {shlex.quote(_REPO_ROOT)} && poetry run python3 {shlex.quote(script)}'
        self._open_mode_tab(_MODE_LLM, cmd)

    def _open_terminal(self):
        """Jump to a plain shell tab if one is open, else start one. Used by
        Ctrl+N cycling back around and by the `terminal` shell command."""
        self._open_mode_tab(_MODE_TERMINAL, None)

    def _cycle_mode(self):
        """Ctrl+N: jump straight to the next mode (terminal -> notes -> llm ->
        terminal ...), creating that mode's tab on first use. Skips the
        command palette entirely — the whole point is a single keypress."""
        tab = self._current_tab()
        current = tab.mode if tab else _MODE_TERMINAL
        if current not in _MODE_CYCLE:
            current = _MODE_TERMINAL
        nxt = _MODE_CYCLE[(_MODE_CYCLE.index(current) + 1) % len(_MODE_CYCLE)]
        if nxt == _MODE_TERMINAL:
            self._open_terminal()
        elif nxt == _MODE_NOTES:
            self._open_notes()
        else:
            self._open_llm_chat()

    _NOTES_SNAPSHOT_KEEP = 10

    def _backup_notes(self):
        """Snapshot the notes file to data/notes_snapshots/ before a restart
        tears down its tab. nano gets SIGTERM'd/hung-up with no chance to
        save, so this only protects what was last saved to disk — anything
        typed but not yet written with Ctrl+O in nano can't be recovered."""
        path = self._notes_path()
        if not os.path.isfile(path):
            return
        try:
            snap_dir = os.path.join(_REPO_ROOT, 'data', 'notes_snapshots')
            os.makedirs(snap_dir, exist_ok=True)
            stamp = time.strftime('%Y%m%d-%H%M%S')
            dest = os.path.join(snap_dir, f'notes-{stamp}.txt')
            if os.path.exists(dest):
                dest = os.path.join(snap_dir, f'notes-{stamp}-{os.getpid()}.txt')
            shutil.copy2(path, dest)
            snaps = sorted(f for f in os.listdir(snap_dir)
                           if f.startswith('notes-') and f.endswith('.txt'))
            for old in snaps[:-self._NOTES_SNAPSHOT_KEEP]:
                try:
                    os.remove(os.path.join(snap_dir, old))
                except OSError:
                    pass
        except OSError:
            logger.warning('notes backup failed', exc_info=True)

    def _restart_terminal(self):
        """F6 'Restart Terminal': back up notes, then nuke and respawn every
        tab — including any running llm_chat.py or nano session — for a
        clean slate, without needing `systemctl restart` (and the sudo/full
        service bounce that implies)."""
        self._backup_notes()
        self._reset_session()

    def _close_tab(self):
        if len(self._tabs) <= 1:
            return
        t = self._tabs[self._active_tab]
        if t.logger:
            t.logger.close()
        if t.child_pid:
            try: os.kill(t.child_pid, signal.SIGTERM)
            except (ProcessLookupError, OSError): pass
        if t.pty_master is not None and t.pty_master >= 0:
            try: os.close(t.pty_master)
            except OSError: pass
        # Clean up split pane if open
        if t.split_dir:
            if t.pane2_pid:
                try: os.kill(t.pane2_pid, signal.SIGTERM)
                except (ProcessLookupError, OSError): pass
            if t.pane2_master >= 0:
                try: os.close(t.pane2_master)
                except OSError: pass
        del self._tabs[self._active_tab]
        self._active_tab = min(self._active_tab, len(self._tabs) - 1)
        t2 = self._tabs[self._active_tab]
        self._screen = t2.screen; self._stream = t2.stream
        self._pty_master = t2.pty_master; self._child_pid = t2.child_pid
        self._scroll_pages = t2.scroll_pages
        t2.activity = False
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
        t2.activity = False   # landing here counts as having seen its output
        # A copy-mode selection points at row/col indices in the *old* tab's
        # screen buffer — meaningless (or wrong) once we've switched away.
        self._copy_active = False
        self._copy_anchor = None
        self._sync_pty_winsize()
        try: os.kill(self._child_pid, signal.SIGWINCH)
        except (ProcessLookupError, OSError): pass
        self._render(force_full=True)
    _SHELL_COMMANDS = {'bash', 'zsh', 'sh', 'dash', 'fish', 'tmux'}

    def _tab_is_busy(self, tab: '_Tab') -> bool:
        """True if the tab's foreground process is something other than the
        login shell — e.g. `claude`, vim, a long build. Idle-reset must not
        kill a tab like this out from under the user just because no key was
        pressed for a while; the process itself may still be working."""
        if self._use_tmux and tab.tmux_session:
            try:
                r = subprocess.run(
                    ['tmux', 'list-panes', '-t', tab.tmux_session,
                     '-F', '#{pane_current_command}'],
                    capture_output=True, text=True, timeout=2,
                )
                names = r.stdout.split()
                return any(n not in self._SHELL_COMMANDS for n in names)
            except Exception:
                return False
        if tab.pty_master is not None and tab.pty_master >= 0 and tab.child_pid:
            try:
                fg_pgid = os.tcgetpgrp(tab.pty_master)
                return fg_pgid != os.getpgid(tab.child_pid)
            except (OSError, ProcessLookupError):
                return False
        return False
