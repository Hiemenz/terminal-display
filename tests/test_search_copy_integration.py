"""Tests for the search → copy-mode handoff.

Ctrl+F finds a match; Enter closes the overlay and opens copy mode with the
matched text pre-selected so the user can yank immediately.
"""
import pyte

from terminal_state import _Tab

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_tab(text=''):
    screen = pyte.Screen(80, 24)
    stream = pyte.ByteStream(screen)
    if text:
        stream.feed(text.encode())
    return _Tab(screen=screen, stream=stream, pty_master=None, child_pid=None,
                title='shell')


def _app(make_app, text=''):
    app = make_app()
    tab = _make_tab(text)
    app._tabs = [tab]
    app._active_tab = 0
    app._scroll_pages = 0
    app._screen = tab.screen
    app._stream = tab.stream
    # search state
    app._search_active = False
    app._search_query = ''
    app._search_results = []
    app._search_idx = 0
    # copy state
    app._copy_active = False
    app._copy_anchor = None
    app._copy_row = 0
    app._copy_col = 0
    # other overlays
    app._palette_active = app._clipboard_active = False
    app._prockill_active = app._svcmgr_active = app._power_active = False
    app._sshpick_active = app._help_active = False
    return app


# ── _find_query_in_viewport ───────────────────────────────────────────────────

def test_find_query_finds_text_on_correct_row(make_app):
    app = _app(make_app, 'first line\r\nsecond line\r\n')
    result = app._find_query_in_viewport('second')
    assert result is not None
    row, col_start, col_end = result
    assert row == 1
    assert col_start == 0
    assert col_end == 5   # len('second') - 1


def test_find_query_returns_none_when_absent(make_app):
    app = _app(make_app, 'hello world\r\n')
    assert app._find_query_in_viewport('nothere') is None


def test_find_query_is_case_insensitive(make_app):
    app = _app(make_app, 'Hello World\r\n')
    result = app._find_query_in_viewport('hello')
    assert result is not None
    assert result[1] == 0


def test_find_query_returns_first_occurrence(make_app):
    app = _app(make_app, 'foo bar\r\nfoo baz\r\n')
    row, col_start, _ = app._find_query_in_viewport('foo')
    assert row == 0


def test_find_query_col_end_matches_query_length(make_app):
    app = _app(make_app, 'abcdef\r\n')
    _, col_start, col_end = app._find_query_in_viewport('cde')
    assert col_start == 2
    assert col_end == 4   # col_start + len('cde') - 1


# ── _enter_copy_mode ──────────────────────────────────────────────────────────

def test_enter_copy_mode_sets_position(make_app):
    app = _app(make_app)
    app._enter_copy_mode(3, 7)
    assert app._copy_active is True
    assert app._copy_row == 3
    assert app._copy_col == 7
    assert app._copy_anchor is None


def test_enter_copy_mode_presets_anchor(make_app):
    app = _app(make_app)
    app._enter_copy_mode(2, 9, anchor_row=2, anchor_col=4)
    assert app._copy_anchor == (2, 4)
    assert app._copy_row == 2
    assert app._copy_col == 9


def test_enter_copy_mode_clamps_row_to_screen(make_app):
    app = _app(make_app)
    app._enter_copy_mode(999, 999)
    assert app._copy_row == app._screen.lines - 1
    assert app._copy_col == app._screen.columns - 1


def test_enter_copy_mode_clamps_negative(make_app):
    app = _app(make_app)
    app._enter_copy_mode(-1, -5)
    assert app._copy_row == 0
    assert app._copy_col == 0


def test_enter_copy_mode_clears_other_overlays(make_app):
    app = _app(make_app)
    app._palette_active = True
    app._clipboard_active = True
    app._help_active = True
    app._search_active = True
    app._enter_copy_mode(0, 0)
    assert app._palette_active is False
    assert app._clipboard_active is False
    assert app._help_active is False
    assert app._search_active is False


# ── _toggle_copy_mode still works ─────────────────────────────────────────────

def test_toggle_copy_mode_opens_at_cursor(make_app):
    app = _app(make_app)
    app._screen.cursor.y, app._screen.cursor.x = 4, 6
    app._toggle_copy_mode()
    assert app._copy_active is True
    assert app._copy_row == 4
    assert app._copy_col == 6
    assert app._copy_anchor is None


def test_toggle_copy_mode_closes_when_open(make_app):
    app = _app(make_app)
    app._copy_active = True
    app._toggle_copy_mode()
    assert app._copy_active is False


# ── _search_confirm → copy mode ───────────────────────────────────────────────

def test_search_confirm_enters_copy_mode_at_screen_match(make_app):
    app = _app(make_app, 'hello world\r\n')
    app._search_active = True
    app._search_query = 'world'
    app._search_results = [('   hello world', False, 0)]
    app._search_idx = 0

    app._search_confirm()

    assert app._search_active is False
    assert app._copy_active is True


def test_search_confirm_preselects_exact_match_text(make_app):
    app = _app(make_app, 'hello world\r\n')
    app._search_active = True
    app._search_query = 'world'
    # 'world' starts at col 6 in 'hello world'
    app._search_results = [('   hello world', False, 0)]
    app._search_idx = 0

    app._search_confirm()

    assert app._copy_anchor == (0, 6)   # anchor at start of 'world'
    assert app._copy_col == 10          # cursor at end of 'world' (col 10)
    assert app._copy_row == 0


def test_search_confirm_no_results_closes_cleanly(make_app):
    app = _app(make_app)
    app._search_active = True
    app._search_query = 'xyz'
    app._search_results = []
    app._search_idx = 0

    app._search_confirm()

    assert app._search_active is False
    assert app._copy_active is False


def test_search_confirm_clears_search_state(make_app):
    app = _app(make_app, 'needle\r\n')
    app._search_active = True
    app._search_query = 'needle'
    app._search_results = [('   needle', False, 0)]
    app._search_idx = 0

    app._search_confirm()

    assert app._search_query == ''
    assert app._search_results == []


def test_search_confirm_history_match_enters_copy_mode(make_app):
    app = _app(make_app, 'visible needle here\r\n')
    scrolled = {}
    app._search_scroll_to_history = lambda idx: scrolled.setdefault('idx', idx)
    app._search_active = True
    app._search_query = 'needle'
    app._search_results = [('H: some needle line', True, 3)]
    app._search_idx = 0

    app._search_confirm()

    assert scrolled['idx'] == 3
    assert app._copy_active is True
    # The visible screen has 'needle' at col 8 of row 0 after the scroll mock
    assert app._copy_anchor is not None


def test_search_confirm_fallback_when_match_not_in_viewport(make_app):
    app = _app(make_app, 'no match here\r\n')
    app._search_scroll_to_history = lambda idx: None
    app._search_active = True
    app._search_query = 'invisible'
    app._search_results = [('H: invisible', True, 0)]
    app._search_idx = 0

    app._search_confirm()

    # Fallback: copy mode at (0, 0), no pre-selection
    assert app._copy_active is True
    assert app._copy_row == 0
    assert app._copy_col == 0
    assert app._copy_anchor is None
