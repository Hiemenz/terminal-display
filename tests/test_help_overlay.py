"""Tests for the Ctrl+/ help overlay (lists every hotkey; Enter runs it)."""
import pyte
import pytest

from terminal_state import _CTRL_SLASH, _HELP_ITEMS, _Tab

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_tab(title=''):
    screen = pyte.Screen(80, 24)
    stream = pyte.ByteStream(screen)
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
    app._help_idx = 0
    app._palette_active = app._clipboard_active = False
    app._prockill_active = app._svcmgr_active = app._power_active = False
    app._sshpick_active = app._search_active = False
    return app


# ── _CTRL_SLASH constant ───────────────────────────────────────────────────────

def test_ctrl_slash_byte_value():
    assert _CTRL_SLASH == b'\x1f'


def test_help_items_cover_tab_and_split_actions():
    labels = [label for label, _keys in _HELP_ITEMS]
    for expected in ('New Tab', 'Close Tab', 'Next Tab', 'Prev Tab',
                      'Toggle Split Pane', 'Swap Split Focus'):
        assert expected in labels


# ── _handle_hotkeys: Ctrl+/ opens the overlay ─────────────────────────────────

def test_ctrl_slash_stripped_from_hotkey_data(make_app):
    app = _app_with_tabs(make_app, 'shell')
    result = app._handle_hotkeys(_CTRL_SLASH + b'hello')
    assert app._help_active is True
    assert _CTRL_SLASH not in result
    assert b'hello' in result


def test_ctrl_slash_alone_is_fully_consumed(make_app):
    app = _app_with_tabs(make_app, 'shell')
    result = app._handle_hotkeys(_CTRL_SLASH)
    assert result == b''


# ── _toggle_help ───────────────────────────────────────────────────────────────

def test_toggle_help_opens_overlay(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._toggle_help()
    assert app._help_active is True
    assert app._help_idx == 0


def test_toggle_help_closes_when_already_open(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._help_active = True
    app._toggle_help()
    assert app._help_active is False


def test_toggle_help_closes_other_overlays(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._palette_active = True
    app._clipboard_active = True
    app._prockill_active = True
    app._svcmgr_active = True
    app._power_active = True
    app._sshpick_active = True
    app._search_active = True

    app._toggle_help()

    assert app._palette_active is False
    assert app._clipboard_active is False
    assert app._prockill_active is False
    assert app._svcmgr_active is False
    assert app._power_active is False
    assert app._sshpick_active is False
    assert app._search_active is False


def test_opening_another_overlay_closes_help(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._help_active = True
    app._toggle_palette()
    assert app._help_active is False


# ── _handle_help_key: navigation ──────────────────────────────────────────────

def test_help_key_down_moves_index(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._help_active = True
    app._help_idx = 0
    app._handle_help_key(b'\x1b[B')
    assert app._help_idx == 1


def test_help_key_up_moves_index(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._help_active = True
    app._help_idx = 2
    app._handle_help_key(b'\x1b[A')
    assert app._help_idx == 1


def test_help_key_up_clamps_at_zero(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._help_active = True
    app._help_idx = 0
    app._handle_help_key(b'\x1b[A')
    assert app._help_idx == 0


def test_help_key_down_clamps_at_last_item(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._help_active = True
    app._help_idx = len(_HELP_ITEMS) - 1
    app._handle_help_key(b'\x1b[B')
    assert app._help_idx == len(_HELP_ITEMS) - 1


def test_help_key_escape_closes_without_action(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._help_active = True
    app._new_tab = lambda *a, **k: pytest.fail('should not run an action on Esc')
    app._handle_help_key(b'\x1b')
    assert app._help_active is False


def test_help_key_passthrough_when_inactive(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._help_active = False
    result = app._handle_help_key(b'hello')
    assert result == b'hello'


def test_help_key_consumes_all_input_while_active(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._help_active = True
    result = app._handle_help_key(b'x')
    assert result == b''


# ── _handle_help_key: Enter runs the selected action ──────────────────────────

def test_help_key_enter_runs_selected_action_and_closes(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._help_active = True
    app._help_idx = [label for label, _ in _HELP_ITEMS].index('New Tab')
    called = {'n': 0}
    app._new_tab = lambda *a, **k: called.__setitem__('n', called['n'] + 1)

    app._handle_help_key(b'\r')

    assert called['n'] == 1
    assert app._help_active is False


# ── _run_help_action: label → method dispatch ─────────────────────────────────

def test_run_help_action_new_tab(make_app):
    app = _app_with_tabs(make_app, 'shell')
    called = {'n': 0}
    app._new_tab = lambda *a, **k: called.__setitem__('n', called['n'] + 1)
    app._run_help_action('New Tab')
    assert called['n'] == 1


def test_run_help_action_close_tab(make_app):
    app = _app_with_tabs(make_app, 'a', 'b')
    for t in app._tabs:
        t.child_pid = 999999   # nonexistent pid: os.kill(SIGWINCH) -> caught OSError
    app._active_tab = 1
    app._sync_pty_winsize = lambda: None
    app._run_help_action('Close Tab')
    assert len(app._tabs) == 1


def test_run_help_action_next_tab(make_app):
    app = _app_with_tabs(make_app, 'a', 'b')
    for t in app._tabs:
        t.child_pid = 999999   # nonexistent pid: os.kill(SIGWINCH) -> caught OSError
    app._active_tab = 0
    app._pty_master = None
    app._child_pid = None
    app._sync_pty_winsize = lambda: None
    app._run_help_action('Next Tab')
    assert app._active_tab == 1


def test_run_help_action_prev_tab(make_app):
    app = _app_with_tabs(make_app, 'a', 'b')
    for t in app._tabs:
        t.child_pid = 999999
    app._active_tab = 1
    app._pty_master = None
    app._child_pid = None
    app._sync_pty_winsize = lambda: None
    app._run_help_action('Prev Tab')
    assert app._active_tab == 0


def test_run_help_action_toggle_split_pane(make_app):
    app = _app_with_tabs(make_app, 'shell')
    called = {'n': 0}
    app._toggle_split_pane = lambda *a, **k: called.__setitem__('n', called['n'] + 1)
    app._run_help_action('Toggle Split Pane')
    assert called['n'] == 1


def test_run_help_action_swap_split_focus(make_app):
    app = _app_with_tabs(make_app, 'shell')
    called = {'n': 0}
    app._swap_pane_focus = lambda *a, **k: called.__setitem__('n', called['n'] + 1)
    app._run_help_action('Swap Split Focus')
    assert called['n'] == 1


def test_run_help_action_full_refresh_rerenders_instead_of_flashing_stale_image(make_app):
    # Full Refresh must not call _force_full_refresh() directly: that flashes
    # self._last_image, which still has the help overlay baked in at this point.
    app = _app_with_tabs(make_app, 'shell')
    rendered = {'force_full': None}
    app._render = lambda *a, **k: rendered.__setitem__('force_full', k.get('force_full'))
    app._force_full_refresh = lambda: pytest.fail('must not flash the stale cached frame')
    app._run_help_action('Full Refresh')
    assert rendered['force_full'] is True


def test_run_help_action_unknown_label_is_a_noop(make_app):
    app = _app_with_tabs(make_app, 'shell')
    app._run_help_action('Nonexistent Action')  # must not raise
