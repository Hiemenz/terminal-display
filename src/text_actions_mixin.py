"""EinkTerminal mixin: tab rename, big-text read mode, beam-to-phone, copy
mode, and the clipboard overlay (F8) — the screen-text capture/output actions."""
from __future__ import annotations

import json
import os
import time

from terminal_state import _MAX_FONT, _get_local_ip


class TextActionsMixin:
    """Tab rename, big text, beam-to-phone, copy mode, and clipboard."""

    def _start_rename(self):
        tab = self._current_tab()
        if tab is None:
            return
        self._rename_query = tab.title or ''
        self._rename_active = True
        self._palette_active = self._clipboard_active = False
        self._prockill_active = self._svcmgr_active = self._power_active = False
        self._sshpick_active = self._search_active = self._copy_active = False
        self._help_active = False
        self._render()

    def _handle_rename_key(self, data: bytes) -> bytes:
        if not self._rename_active:
            return data
        if b'\r' in data or b'\n' in data:
            tab = self._current_tab()
            if tab is not None:
                tab.title = self._rename_query.strip()
            self._rename_active = False
            self._rename_query = ''
            self._render(force_full=True)
            return b''
        if b'\x1b' in data:
            self._rename_active = False
            self._rename_query = ''
            self._render()
            return b''
        if data in (b'\x7f', b'\x08'):
            self._rename_query = self._rename_query[:-1]
            self._render()
            return b''
        try:
            ch = data.decode('utf-8', errors='ignore')
            printable = ''.join(c for c in ch if c >= ' ' and c != '\x7f')
            if printable:
                self._rename_query += printable
                self._render()
        except Exception:
            pass
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

    def _beam_to_phone(self, text: str = None):
        """Hand text to the preview server and pin a QR linking to the page
        that shows it (copyable on a phone). Defaults to the whole visible
        screen; copy mode passes just the selected range."""
        text = self._screen_text() if text is None else text
        ip = _get_local_ip()
        port = self._config.get('preview_server_port', 8080)
        server = getattr(self, '_preview_server', None)
        if server is not None and ip:
            server.set_beam_text(text)
            self._beam_url = f'http://{ip}:{port}/beam'
            self._beam_until_mono = time.monotonic() + 120  # show QR for 2 min
        self._render(force_full=True)

    # ─── Copy mode (Ctrl+Space) ────────────────────────────────────────────────

    def _toggle_copy_mode(self):
        if self._copy_active:
            self._copy_active = False
            self._render()
            return
        self._copy_row = min(self._screen.cursor.y, self._screen.lines - 1)
        self._copy_col = min(self._screen.cursor.x, self._screen.columns - 1)
        self._copy_anchor = None
        self._copy_active = True
        self._palette_active = self._clipboard_active = False
        self._prockill_active = self._svcmgr_active = self._power_active = False
        self._sshpick_active = self._search_active = False
        self._help_active = False
        self._render()

    def _copy_render_range(self) -> tuple:
        """(r1, c1, r2, c2) — reading-order-normalized highlight range for
        rendering. A single cell (the cursor) when no anchor is set yet."""
        r2, c2 = self._copy_row, self._copy_col
        r1, c1 = self._copy_anchor if self._copy_anchor is not None else (r2, c2)
        if (r1, c1) > (r2, c2):
            (r1, c1), (r2, c2) = (r2, c2), (r1, c1)
        return (r1, c1, r2, c2)

    def _handle_copy_key(self, data: bytes) -> bytes:
        if not self._copy_active:
            return data
        max_row = self._screen.lines - 1
        max_col = self._screen.columns - 1
        if b'\x1b[A' in data:
            self._copy_row = max(0, self._copy_row - 1)
            self._render(force_full=True); return b''
        if b'\x1b[B' in data:
            self._copy_row = min(max_row, self._copy_row + 1)
            self._render(force_full=True); return b''
        if b'\x1b[D' in data:
            self._copy_col = max(0, self._copy_col - 1)
            self._render(force_full=True); return b''
        if b'\x1b[C' in data:
            self._copy_col = min(max_col, self._copy_col + 1)
            self._render(force_full=True); return b''
        if b'\x1b' in data:   # Esc — exit entirely, discarding any selection
            self._copy_active = False
            self._render(force_full=True); return b''
        if data == b' ':
            self._copy_anchor = (
                None if self._copy_anchor is not None else (self._copy_row, self._copy_col)
            )
            self._render(force_full=True); return b''
        if b'\r' in data or b'\n' in data:
            self._copy_confirm()
            return b''
        return b''   # swallow everything else while navigating

    def _copy_confirm(self):
        """Yank the current selection (or, with no anchor, the whole line
        under the cursor) into the on-device clipboard and beam it to a QR."""
        if self._copy_anchor is None:
            row = self._screen.buffer[self._copy_row]
            text = ''.join(row[c].data for c in range(self._screen.columns)).rstrip()
        else:
            r1, c1, r2, c2 = self._copy_render_range()
            lines = []
            for r in range(r1, r2 + 1):
                row = self._screen.buffer[r]
                c_start = c1 if r == r1 else 0
                c_end = c2 if r == r2 else self._screen.columns - 1
                lines.append(''.join(row[c].data for c in range(c_start, c_end + 1)).rstrip())
            text = '\n'.join(lines)
        self._copy_active = False
        self._copy_anchor = None
        if text:
            self._add_clipboard_entry(text)
            self._beam_to_phone(text)
        else:
            self._render(force_full=True)

    def _add_clipboard_entry(self, text: str):
        """Push a yanked selection onto the front of the on-device clipboard
        (same store F8 reads from), capped like the web editor's copy."""
        first_line = text.split('\n', 1)[0]
        label = first_line if len(first_line) <= 40 else first_line[:39] + '…'
        self._clipboard.insert(0, {'text': text, 'label': label})
        self._clipboard = self._clipboard[:20]
        try:
            os.makedirs(os.path.dirname(self._clipboard_path), exist_ok=True)
            with open(self._clipboard_path, 'w') as f:
                json.dump(self._clipboard, f)
        except OSError:
            pass
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
            self._help_active = self._copy_active = False
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
