"""EinkTerminal mixin: the on-display config editor (Settings overlay, F6)."""
from __future__ import annotations

import logging
import os
import subprocess

from preview_server import _save_config_values
from terminal_state import (
    _MAX_FONT,
    _MIN_FONT,
    _REPO_ROOT,
    _SETTINGS_LIVE,
    _SETTINGS_SCHEMA,
    _SETTINGS_SHELL,
)

logger = logging.getLogger(__name__)


class SettingsMixin:
    """On-display config editor: schema-driven bool/select field editing."""

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
            self._help_active = self._copy_active = False
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
