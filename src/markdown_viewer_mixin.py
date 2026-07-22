"""EinkTerminal mixin: full-screen paginated Markdown viewer (F6 > "View
notes as Markdown" / Ctrl+/ help). Renders the notes file through
markdown_renderer and lets PgUp/PgDn flip pages, any other key close back to
the terminal.

Unlike the generic list-overlay mechanism (_help_active, _palette_active, ...,
drawn via render_screen's `overlay` tuple) or _show_text_message's one-shot
"any key dismisses" full-screen message, a paginated view needs PgUp/PgDn to
page through content instead of closing it — so this bypasses the terminal's
normal render pipeline entirely and pushes pre-rendered page images straight
to the driver, the same way _show_text_message does.
"""
from __future__ import annotations

import logging
import os

from terminal_state import _PGDN, _PGUP

logger = logging.getLogger(__name__)


class MarkdownViewerMixin:
    """Paginated full-screen Markdown viewer over the notes file."""

    def _show_markdown(self, text: str, label: str):
        from markdown_renderer import render_markdown_pages
        # Overlay self._dark_mode and self._font_path so the Markdown page
        # matches the terminal's live theme (terminal_dark_mode / F7 toggle),
        # not the stats-dashboard's separate dark_mode/font_path keys.
        cfg = dict(self._config)
        cfg['dark_mode'] = self._dark_mode
        cfg['font_path'] = getattr(self, '_font_path', self._config.get('font_path', ''))
        try:
            pages = render_markdown_pages(text, label, cfg)
        except Exception as e:
            logger.warning('Markdown render error: %s', e)
            return
        self._markdown_pages = pages
        self._markdown_page_idx = 0
        self._markdown_active = True
        self._driver.full_refresh(pages[0])
        self._last_image = pages[0]

    def _open_markdown_notes(self):
        """F6 'View notes as Markdown': render the current notes file."""
        path = self._notes_path()
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            text = '*(no notes yet — write something in Notes mode first)*'
        self._show_markdown(text, os.path.basename(path))

    def _close_markdown(self):
        self._markdown_active = False
        self._markdown_pages = []
        self._markdown_page_idx = 0
        self._render(force_full=True)

    def _handle_markdown_key(self, data: bytes) -> bytes:
        if not self._markdown_active:
            return data
        if _PGDN in data:
            data = data.replace(_PGDN, b'')
            if self._markdown_page_idx < len(self._markdown_pages) - 1:
                self._markdown_page_idx += 1
                img = self._markdown_pages[self._markdown_page_idx]
                self._driver.full_refresh(img)
                self._last_image = img
            return data
        if _PGUP in data:
            data = data.replace(_PGUP, b'')
            if self._markdown_page_idx > 0:
                self._markdown_page_idx -= 1
                img = self._markdown_pages[self._markdown_page_idx]
                self._driver.full_refresh(img)
                self._last_image = img
            return data
        # Any other key closes the viewer — mirrors _show_text_message's
        # any-key-to-dismiss convention. Swallowed so it doesn't leak into
        # the shell underneath once the terminal view is back.
        self._close_markdown()
        return b''
