"""EinkTerminal mixin: scrollback search (Ctrl+F)."""
from __future__ import annotations


class SearchMixin:
    """Scrollback search: build match list, navigate, jump to a match."""

    def _toggle_search(self):
        if self._search_active:
            self._search_active = False
            self._search_query = ''
            self._search_results = []
        else:
            self._search_active = True
            self._search_query = ''
            self._search_results = []
            self._search_idx = 0
            self._palette_active = self._clipboard_active = False
            self._prockill_active = self._svcmgr_active = self._power_active = False
            self._sshpick_active = False
            self._help_active = self._copy_active = False
        self._render()

    def _build_search_results(self) -> list:
        """Scan current visible buffer + pyte history for _search_query matches."""
        if not self._search_query:
            return []
        q = self._search_query.lower()
        results = []
        # History lines (oldest → newest = index 0 → N-1 in history.top)
        if hasattr(self._screen, 'history'):
            try:
                hist_top = list(self._screen.history.top)
                for i, row in enumerate(hist_top):
                    line = ''.join(row[c].data for c in range(self._screen.columns)).rstrip()
                    if q in line.lower() and line.strip():
                        results.append((f'H: {line[:68]}', True, i))
            except (AttributeError, TypeError):
                pass
        # Current visible buffer
        for r in range(self._screen.lines):
            row = self._screen.buffer[r]
            line = ''.join(row[c].data for c in range(self._screen.columns)).rstrip()
            if q in line.lower() and line.strip():
                results.append((f'   {line[:68]}', False, r))
        return results

    def _handle_search_key(self, data: bytes) -> bytes:
        if not self._search_active:
            return data
        if b'\x1b[A' in data:
            self._search_idx = max(0, self._search_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._search_idx = min(max(0, len(self._search_results) - 1), self._search_idx + 1)
            self._render(); return b''
        if b'\r' in data or b'\n' in data:
            self._search_confirm(); return b''
        if b'\x1b' in data:
            self._search_active = False
            self._search_query = ''
            self._search_results = []
            self._render(); return b''
        if data in (b'\x7f', b'\x08'):
            self._search_query = self._search_query[:-1]
            self._search_idx = 0
            self._search_results = self._build_search_results()
            self._render(); return b''
        try:
            ch = data.decode('utf-8', errors='ignore')
            printable = ''.join(c for c in ch if c >= ' ' and c != '\x7f')
            if printable:
                self._search_query += printable
                self._search_idx = 0
                self._search_results = self._build_search_results()
                self._render()
        except Exception:
            pass
        return b''

    def _search_confirm(self):
        """Jump to selected search result and close the overlay."""
        if self._search_results and 0 <= self._search_idx < len(self._search_results):
            _, is_history, idx = self._search_results[self._search_idx]
            if is_history:
                self._search_scroll_to_history(idx)
        self._search_active = False
        self._search_query = ''
        self._search_results = []
        self._render(force_full=True)

    def _search_scroll_to_history(self, history_idx: int):
        """Scroll so that the history line at history_idx becomes visible."""
        if not hasattr(self._screen, 'history') or not hasattr(self._screen, 'prev_page'):
            return
        n_hist = len(self._screen.history.top)
        if n_hist == 0:
            return
        self._snap_to_live()
        scroll_step = max(1, int(self._screen.history.ratio * self._screen.lines))
        # history_idx 0 = oldest (furthest up), n_hist-1 = newest (one scroll up)
        distance = n_hist - 1 - history_idx
        pages = max(1, distance // scroll_step + 1)
        for _ in range(pages):
            if not hasattr(self._screen, 'prev_page'):
                break
            self._screen.prev_page()
            self._scroll_pages += 1
