"""Tests for copy mode (Ctrl+Space) and Alt+1..9 tab jump."""
import json

import pyte

from terminal_state import _ALT_DIGITS, _CTRL_SPACE, _HELP_ITEMS, _Tab

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_tab(title='', text=''):
    screen = pyte.Screen(80, 24)
    stream = pyte.ByteStream(screen)
    if text:
        stream.feed(text.encode())
    return _Tab(screen=screen, stream=stream, pty_master=None, child_pid=None,
                title=title)


def _app_with_tabs(make_app, *titles):
    app = make_app()
    app._tabs = [_make_tab(t) for t in titles]
    app._active_tab = 0
    app._scroll_pages = 0
    app._screen = app._tabs[0].screen
    app._stream = app._tabs[0].stream
    app._help_active = False
    app._copy_active = False
    app._copy_anchor = None
    app._copy_row = 0
    app._copy_col = 0
    app._palette_active = app._clipboard_active = False
    app._prockill_active = app._svcmgr_active = app._power_active = False
    app._sshpick_active = app._search_active = False
    return app


# ── constants ─────────────────────────────────────────────────────────────────

def test_ctrl_space_byte_value():
    assert _CTRL_SPACE == b'\x00'


def test_alt_digits_map_esc_plus_digit_to_int():
    assert _ALT_DIGITS[b'\x1b1'] == 1
    assert _ALT_DIGITS[b'\x1b9'] == 9
    assert len(_ALT_DIGITS) == 9


def test_help_items_cover_copy_mode_and_tab_jump():
    labels = [label for label, _keys in _HELP_ITEMS]
    assert 'Copy Mode' in labels
    assert 'Jump to Tab N' in labels


# ── _handle_hotkeys: Ctrl+Space opens copy mode ───────────────────────────────

def test_ctrl_space_stripped_from_hotkey_data(make_app):
    app = _app_with_tabs(make_app, 'shell')
    result = app._handle_hotkeys(_CTRL_SPACE + b'hello')
    assert app._copy_active is True
    assert _CTRL_SPACE not in result
    assert b'hello' in result


# ── _toggle_copy_mode ──────────────────────────────────────────────────────────

def test_toggle_copy_mode_opens_at_cursor(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._screen.cursor.y, app._screen.cursor.x = 3, 5
    app._toggle_copy_mode()
    assert app._copy_active is True
    assert (app._copy_row, app._copy_col) == (3, 5)
    assert app._copy_anchor is None


def test_toggle_copy_mode_closes_when_already_open(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._copy_active = True
    app._toggle_copy_mode()
    assert app._copy_active is False


def test_toggle_copy_mode_closes_other_overlays(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._palette_active = True
    app._clipboard_active = True
    app._help_active = True
    app._toggle_copy_mode()
    assert app._palette_active is False
    assert app._clipboard_active is False
    assert app._help_active is False


# ── _handle_copy_key: movement, anchor, escape ────────────────────────────────

def test_copy_key_arrows_move_cursor_clamped(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._copy_active = True
    app._copy_row, app._copy_col = 0, 0
    app._handle_copy_key(b'\x1b[B')   # down
    assert app._copy_row == 1
    app._handle_copy_key(b'\x1b[A')   # up
    app._handle_copy_key(b'\x1b[A')   # up again — clamp at 0
    assert app._copy_row == 0
    app._handle_copy_key(b'\x1b[C')   # right
    assert app._copy_col == 1
    app._handle_copy_key(b'\x1b[D')   # left
    app._handle_copy_key(b'\x1b[D')   # left again — clamp at 0
    assert app._copy_col == 0


def test_copy_key_space_sets_and_clears_anchor(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._copy_active = True
    app._copy_row, app._copy_col = 2, 4
    app._handle_copy_key(b' ')
    assert app._copy_anchor == (2, 4)
    app._handle_copy_key(b' ')
    assert app._copy_anchor is None


def test_copy_key_esc_exits_mode(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._copy_active = True
    app._copy_anchor = (0, 0)
    app._handle_copy_key(b'\x1b')
    assert app._copy_active is False


def test_copy_key_swallows_input_while_active(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._copy_active = True
    assert app._handle_copy_key(b'x') == b''


def test_copy_key_passthrough_when_inactive(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._copy_active = False
    assert app._handle_copy_key(b'x') == b'x'


# ── _copy_render_range: normalization ─────────────────────────────────────────

def test_copy_render_range_single_cell_without_anchor(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._copy_row, app._copy_col = 3, 5
    app._copy_anchor = None
    assert app._copy_render_range() == (3, 5, 3, 5)


def test_copy_render_range_normalizes_reverse_selection(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._copy_anchor = (5, 10)
    app._copy_row, app._copy_col = 2, 1
    assert app._copy_render_range() == (2, 1, 5, 10)


# ── _copy_confirm: extraction + clipboard + beam ──────────────────────────────

def test_copy_confirm_no_anchor_yanks_whole_line(make_app, tmp_path):
    app = _app_with_tabs(make_app, 'shell')
    app._screen.buffer[0]  # touch to ensure row exists
    app._stream.feed(b'hello world\r\n')
    app._clipboard_path = str(tmp_path / 'clipboard.json')
    app._clipboard = []
    app._copy_active = True
    app._copy_anchor = None
    app._copy_row, app._copy_col = 0, 0
    app._beam_to_phone = lambda text=None: None

    app._copy_confirm()

    assert app._copy_active is False
    assert app._clipboard[0]['text'] == 'hello world'
    saved = json.loads((tmp_path / 'clipboard.json').read_text())
    assert saved[0]['text'] == 'hello world'


def test_copy_confirm_with_anchor_yanks_char_range(make_app, tmp_path):
    app = _app_with_tabs(make_app, 'shell')
    app._stream.feed(b'hello world\r\nsecond line\r\n')
    app._clipboard_path = str(tmp_path / 'clipboard.json')
    app._clipboard = []
    app._copy_active = True
    app._copy_anchor = (0, 6)     # start at 'w' in "world"
    app._copy_row, app._copy_col = 1, 5   # end at 'd' in "second"
    beamed = {}
    app._beam_to_phone = lambda text=None: beamed.setdefault('text', text)

    app._copy_confirm()

    assert app._clipboard[0]['text'] == 'world\nsecond'
    assert beamed['text'] == 'world\nsecond'


def test_copy_confirm_caps_clipboard_at_twenty(make_app, tmp_path):
    app = _app_with_tabs(make_app, 'shell')
    app._stream.feed(b'x\r\n')
    app._clipboard_path = str(tmp_path / 'clipboard.json')
    app._clipboard = [{'text': f't{i}', 'label': f't{i}'} for i in range(20)]
    app._copy_active = True
    app._copy_anchor = None
    app._copy_row, app._copy_col = 0, 0
    app._beam_to_phone = lambda text=None: None

    app._copy_confirm()

    assert len(app._clipboard) == 20
    assert app._clipboard[0]['text'] == 'x'


# ── Alt+1..9: jump straight to tab N ──────────────────────────────────────────

def test_alt_digit_jumps_to_tab(make_app):
    app = _app_with_tabs(make_app, 'one', 'two', 'three')
    jumped = {}
    app._goto_tab = lambda idx: jumped.setdefault('idx', idx)

    result = app._handle_hotkeys(b'\x1b2hello')

    assert jumped['idx'] == 1   # Alt+2 -> tab index 1
    assert b'\x1b2' not in result
    assert b'hello' in result


def test_alt_digit_beyond_tab_count_is_noop(make_app):
    app = _app_with_tabs(make_app, 'one')
    jumped = {'called': False}
    app._goto_tab = lambda idx: jumped.__setitem__('called', True)

    app._handle_hotkeys(b'\x1b5')

    assert jumped['called'] is False


# ── _tab_indicator: background activity bullet ────────────────────────────────

def test_tab_indicator_shows_activity_bullet(make_app):
    app = _app_with_tabs(make_app, 'one', 'two', 'three')
    app._active_tab = 0
    app._tabs[2].activity = True
    assert app._tab_indicator() == '[1/3 one] •3'


def test_tab_indicator_no_bullet_when_no_activity(make_app):
    app = _app_with_tabs(make_app, 'one', 'two')
    assert '•' not in app._tab_indicator()
