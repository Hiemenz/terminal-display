"""Tests for Ctrl+T new-tab shortcut and F6-palette tab rename."""
import pyte

from terminal_state import _CTRL_T, _PALETTE_ACTIONS, _RENAME_TAB, _Tab

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_tab(title=''):
    screen = pyte.Screen(80, 24)
    stream = pyte.ByteStream(screen)
    return _Tab(screen=screen, stream=stream, pty_master=None, child_pid=None,
                title=title)


def _app_with_tabs(make_app, *titles):
    """Return an app pre-populated with one _Tab per title string."""
    app = make_app()
    app._tabs = [_make_tab(t) for t in titles]
    app._active_tab = 0
    app._scroll_pages = 0
    # Expose the first tab's state so tests can check it later.
    app._screen = app._tabs[0].screen
    app._stream = app._tabs[0].stream
    return app


# ── _CTRL_T constant and palette presence ─────────────────────────────────────

def test_ctrl_t_byte_value():
    assert _CTRL_T == b'\x14'


def test_rename_tab_in_palette_actions():
    assert _RENAME_TAB in _PALETTE_ACTIONS


# ── Ctrl+T: _handle_hotkeys strips the byte and opens a tab ──────────────────

def test_ctrl_t_stripped_from_hotkey_data(make_app):
    app = _app_with_tabs(make_app, 'shell')
    opened = {'n': 0}
    app._new_tab = lambda *a, **k: opened.__setitem__('n', opened['n'] + 1)

    result = app._handle_hotkeys(_CTRL_T + b'hello')

    assert opened['n'] == 1
    assert _CTRL_T not in result
    assert b'hello' in result


def test_ctrl_t_alone_is_fully_consumed(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._new_tab = lambda *a, **k: None

    result = app._handle_hotkeys(_CTRL_T)

    assert result == b''


def test_ctrl_t_calls_new_tab_once_per_byte(make_app):
    app = _app_with_tabs(make_app, 'shell')
    opened = {'n': 0}
    app._new_tab = lambda *a, **k: opened.__setitem__('n', opened['n'] + 1)

    app._handle_hotkeys(_CTRL_T)
    assert opened['n'] == 1


# ── _new_tab: tab list grows ──────────────────────────────────────────────────

def test_new_tab_increments_tab_count(make_app):
    app = _app_with_tabs(make_app, 'shell')
    spawned = {'n': 0}

    def _fake_spawn(*a, **k):
        spawned['n'] += 1
        app._pty_master = 42
        app._child_pid = 999

    app._init_screen = lambda: setattr(app, '_screen', pyte.Screen(80, 24)) or \
                               setattr(app, '_stream', pyte.ByteStream(app._screen))
    app._spawn_shell = _fake_spawn

    app._new_tab()

    assert spawned['n'] == 1
    assert len(app._tabs) == 2
    assert app._active_tab == 1


def test_new_tab_becomes_active(make_app):
    app = _app_with_tabs(make_app, 'a', 'b')
    app._active_tab = 0

    def _fake_spawn(*a, **k):
        app._pty_master = 10
        app._child_pid = 11

    app._init_screen = lambda: setattr(app, '_screen', pyte.Screen(80, 24)) or \
                               setattr(app, '_stream', pyte.ByteStream(app._screen))
    app._spawn_shell = _fake_spawn

    app._new_tab()

    assert app._active_tab == len(app._tabs) - 1


# ── _start_rename ─────────────────────────────────────────────────────────────

def test_start_rename_opens_overlay(make_app):
    app = _app_with_tabs(make_app, 'myproject')
    app._rename_active = False
    app._palette_active = False
    app._clipboard_active = False
    app._prockill_active = False
    app._svcmgr_active = False
    app._power_active = False
    app._sshpick_active = False
    app._search_active = False

    app._start_rename()

    assert app._rename_active is True


def test_start_rename_prefills_existing_title(make_app):
    app = _app_with_tabs(make_app, 'work')
    app._rename_active = False
    app._palette_active = app._clipboard_active = False
    app._prockill_active = app._svcmgr_active = False
    app._power_active = app._sshpick_active = app._search_active = False

    app._start_rename()

    assert app._rename_query == 'work'


def test_start_rename_empty_when_no_custom_title(make_app):
    app = _app_with_tabs(make_app, '')   # no title set
    app._rename_active = False
    app._palette_active = app._clipboard_active = False
    app._prockill_active = app._svcmgr_active = False
    app._power_active = app._sshpick_active = app._search_active = False

    app._start_rename()

    assert app._rename_query == ''


def test_start_rename_closes_other_overlays(make_app):
    app = _app_with_tabs(make_app, '')
    app._rename_active = False
    app._palette_active = True
    app._clipboard_active = True
    app._prockill_active = True
    app._svcmgr_active = True
    app._power_active = True
    app._sshpick_active = True
    app._search_active = True

    app._start_rename()

    assert app._palette_active is False
    assert app._clipboard_active is False
    assert app._prockill_active is False
    assert app._svcmgr_active is False
    assert app._power_active is False
    assert app._sshpick_active is False
    assert app._search_active is False


# ── _handle_rename_key ────────────────────────────────────────────────────────

def _rename_app(make_app, existing_title=''):
    app = _app_with_tabs(make_app, existing_title)
    app._rename_active = True
    app._rename_query = existing_title
    return app


def test_rename_key_appends_characters(make_app):
    app = _rename_app(make_app, '')
    app._handle_rename_key(b'foo')
    assert app._rename_query == 'foo'


def test_rename_key_appends_to_existing_query(make_app):
    app = _rename_app(make_app, 'my')
    app._handle_rename_key(b'proj')
    assert app._rename_query == 'myproj'


def test_rename_key_backspace_removes_last_char(make_app):
    app = _rename_app(make_app, 'abc')
    app._handle_rename_key(b'\x7f')
    assert app._rename_query == 'ab'


def test_rename_key_backspace_on_empty_is_safe(make_app):
    app = _rename_app(make_app, '')
    app._handle_rename_key(b'\x7f')
    assert app._rename_query == ''


def test_rename_key_ctrl_h_also_deletes(make_app):
    app = _rename_app(make_app, 'xyz')
    app._handle_rename_key(b'\x08')
    assert app._rename_query == 'xy'


def test_rename_key_enter_confirms_and_sets_title(make_app):
    app = _rename_app(make_app, '')
    app._rename_query = 'newname'

    app._handle_rename_key(b'\r')

    assert app._tabs[0].title == 'newname'
    assert app._rename_active is False
    assert app._rename_query == ''


def test_rename_key_enter_strips_whitespace(make_app):
    app = _rename_app(make_app, '')
    app._rename_query = '  trimmed  '

    app._handle_rename_key(b'\r')

    assert app._tabs[0].title == 'trimmed'


def test_rename_key_newline_also_confirms(make_app):
    app = _rename_app(make_app, '')
    app._rename_query = 'test'

    app._handle_rename_key(b'\n')

    assert app._tabs[0].title == 'test'
    assert app._rename_active is False


def test_rename_key_escape_cancels_without_mutation(make_app):
    app = _rename_app(make_app, 'original')
    app._rename_query = 'edited but not saved'

    app._handle_rename_key(b'\x1b')

    assert app._tabs[0].title == 'original'   # unchanged
    assert app._rename_active is False
    assert app._rename_query == ''


def test_rename_key_passthrough_when_inactive(make_app):
    app = _rename_app(make_app, '')
    app._rename_active = False

    result = app._handle_rename_key(b'hello')

    assert result == b'hello'
    assert app._rename_query == ''   # untouched


def test_rename_key_consumes_all_input_while_active(make_app):
    app = _rename_app(make_app, '')

    result = app._handle_rename_key(b'a')

    assert result == b''


# ── _run_palette_action wires up rename ───────────────────────────────────────

def test_palette_action_rename_tab_calls_start_rename(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._rename_active = False
    app._rename_query = ''
    app._palette_active = app._clipboard_active = False
    app._prockill_active = app._svcmgr_active = False
    app._power_active = app._sshpick_active = app._search_active = False

    app._run_palette_action(_RENAME_TAB)

    assert app._rename_active is True


# ── tab indicator reflects custom title after rename ──────────────────────────

def test_tab_indicator_uses_custom_title_after_rename(make_app):
    app = _app_with_tabs(make_app, '', '')
    app._active_tab = 0
    app._rename_active = True
    app._rename_query = 'claude'

    app._handle_rename_key(b'\r')

    app._active_tab = 0
    indicator = app._tab_indicator()
    assert 'claude' in indicator
