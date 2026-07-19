"""EinkTerminal mixin: command palette actions, the help overlay (Ctrl+/),
and the snippets picker."""
from __future__ import annotations

import os
import re

from terminal_state import (
    _BEAM_OPEN,
    _BIGTEXT_OPEN,
    _HELP_ITEMS,
    _HUD_TOGGLE,
    _LLM_CHAT_OPEN,
    _MARKDOWN_VIEW,
    _NOTES_OPEN,
    _PALETTE_ACTIONS,
    _RENAME_TAB,
    _REPO_ROOT,
    _RESTART_TERMINAL,
    _SETTINGS_OPEN,
    _SNIPPETS_OPEN,
)


class PaletteHelpMixin:
    """Command palette dispatch, help overlay, and saved-command snippets."""

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
        elif action == _RENAME_TAB:
            self._start_rename()
        elif action == _NOTES_OPEN:
            self._open_notes()
        elif action == _LLM_CHAT_OPEN:
            self._open_llm_chat()
        elif action == _RESTART_TERMINAL:
            self._restart_terminal()
        elif action == _MARKDOWN_VIEW:
            self._open_markdown_notes()

    # ─── Help overlay (Ctrl+/) ─────────────────────────────────────────────────

    @staticmethod
    def _format_help_row(label: str, keys: str) -> str:
        return f'{label:<22}{keys:>12}'

    def _toggle_help(self):
        if self._help_active:
            self._help_active = False
        else:
            self._help_idx = 0
            self._help_active = True
            self._palette_active = self._clipboard_active = False
            self._prockill_active = self._svcmgr_active = self._power_active = False
            self._sshpick_active = self._search_active = self._copy_active = False
        self._render()

    def _handle_help_key(self, data: bytes) -> bytes:
        if not self._help_active:
            return data
        if b'\x1b[A' in data:
            self._help_idx = max(0, self._help_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._help_idx = min(len(_HELP_ITEMS) - 1, self._help_idx + 1)
            self._render(); return b''
        if b'\r' in data or b'\n' in data:
            if _HELP_ITEMS:
                label, _keys = _HELP_ITEMS[self._help_idx]
                self._help_active = False
                self._run_help_action(label)
            return b''
        if b'\x1b' in data:
            self._help_active = False; self._render(); return b''
        return b''

    def _run_help_action(self, label: str):
        # Mirrors _handle_hotkeys' dispatch for the same key — each target
        # method already renders itself, except the two special-cased below.
        if label == 'New Tab':
            self._new_tab()
        elif label == 'Close Tab':
            self._close_tab()
            self._render()   # unlike the F2 path, the overlay needs clearing
        elif label == 'Next Tab':
            self._switch_tab(+1)
        elif label == 'Prev Tab':
            self._switch_tab(-1)
        elif label == 'Jump to Tab N':
            self._goto_tab(0)   # picked from the menu — no N to type, default to tab 1
        elif label == 'Toggle Split Pane':
            self._toggle_split_pane()
        elif label == 'Swap Split Focus':
            self._swap_pane_focus()
        elif label == 'Rename Tab':
            self._start_rename()
        elif label == 'Cycle Mode':
            self._cycle_mode()
        elif label == 'Notes':
            self._open_notes()
        elif label == 'Chat with local LLM':
            self._open_llm_chat()
        elif label == 'Restart Terminal':
            self._restart_terminal()
        elif label == 'View Notes as Markdown':
            self._open_markdown_notes()
        elif label == 'SSH Picker':
            self._toggle_sshpick()
        elif label == 'Command Palette':
            self._toggle_palette()
        elif label == 'Kill Process':
            self._toggle_prockill()
        elif label == 'Service Manager':
            self._toggle_svcmgr()
        elif label == 'Power Menu':
            self._toggle_power()
        elif label == 'Dark Mode':
            self._toggle_dark_mode()
        elif label == 'Clipboard':
            self._toggle_clipboard()
        elif label == 'Copy Mode':
            self._toggle_copy_mode()
        elif label == 'Font Smaller':
            self._change_font(-2)
        elif label == 'Font Larger':
            self._change_font(+2)
        elif label == 'Full Refresh':
            # Not _force_full_refresh(): that flashes self._last_image, which
            # at this point is still the cached frame with the help overlay
            # baked in. Re-render first so the flash shows a clean screen.
            self._render(force_full=True)
        elif label == 'Switch to Dashboard':
            self._switch_to_stats()
        elif label == 'Scrollback Search':
            self._toggle_search()
        elif label == 'Scroll Up':
            self._scroll_up()
        elif label == 'Scroll Down':
            self._scroll_down()

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
            self._help_active = self._copy_active = False
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
            self._help_active = self._copy_active = False
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
