"""Tests for Ctrl+N mode-cycling (terminal / notes / local LLM chat) and the
matching F6 palette entries."""
import os

import pyte

from terminal_state import (
    _CTRL_N,
    _LLM_CHAT_OPEN,
    _MODE_LLM,
    _MODE_NOTES,
    _MODE_TERMINAL,
    _NOTES_OPEN,
    _PALETTE_ACTIONS,
    _REPO_ROOT,
    _RESTART_TERMINAL,
    _Tab,
)

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_tab(mode='', title=''):
    screen = pyte.Screen(80, 24)
    stream = pyte.ByteStream(screen)
    return _Tab(screen=screen, stream=stream, pty_master=None, child_pid=None,
                mode=mode, title=title)


def _app_with_tabs(make_app, *modes):
    app = make_app()
    app._tabs = [_make_tab(m) for m in modes]
    app._active_tab = 0
    app._scroll_pages = 0
    app._screen = app._tabs[0].screen
    app._stream = app._tabs[0].stream

    def _fake_spawn(*a, **k):
        app._pty_master = 10
        app._child_pid = 11

    app._init_screen = lambda: setattr(app, '_screen', pyte.Screen(80, 24)) or \
                               setattr(app, '_stream', pyte.ByteStream(app._screen))
    app._spawn_shell = _fake_spawn
    app._make_tab_logger = lambda: None
    return app


# ── constants / wiring ────────────────────────────────────────────────────────

def test_ctrl_n_byte_value():
    assert _CTRL_N == b'\x0e'


def test_notes_and_llm_in_palette_actions():
    assert _NOTES_OPEN in _PALETTE_ACTIONS
    assert _LLM_CHAT_OPEN in _PALETTE_ACTIONS


def test_ctrl_n_stripped_from_hotkey_data(make_app):
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    called = {'n': 0}
    app._cycle_mode = lambda: called.__setitem__('n', called['n'] + 1)

    result = app._handle_hotkeys(_CTRL_N + b'hello')

    assert called['n'] == 1
    assert _CTRL_N not in result
    assert b'hello' in result


# ── _notes_path ───────────────────────────────────────────────────────────────

def test_notes_path_defaults_under_repo_root(make_app):
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    app._config = dict(app._config)
    app._config.pop('terminal_notes_file', None)

    assert app._notes_path() == os.path.join(_REPO_ROOT, 'data', 'notes.txt')


def test_notes_path_honors_absolute_config_value(make_app):
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    app._config = dict(app._config)
    app._config['terminal_notes_file'] = '/tmp/custom-notes.txt'

    assert app._notes_path() == '/tmp/custom-notes.txt'


# ── _open_notes / _open_llm_chat: open-or-jump ────────────────────────────────

def test_open_notes_creates_tagged_tab(make_app, monkeypatch, tmp_path):
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    app._config = dict(app._config)
    app._config['terminal_notes_file'] = str(tmp_path / 'notes.txt')
    written = {}
    app._new_tab = lambda cmd=None, mode='': written.update(cmd=cmd, mode=mode)

    app._open_notes()

    assert written['mode'] == _MODE_NOTES
    assert 'nano' in written['cmd']
    assert str(tmp_path / 'notes.txt') in written['cmd']


def test_open_notes_jumps_to_existing_tab_instead_of_creating(make_app):
    app = _app_with_tabs(make_app, _MODE_TERMINAL, _MODE_NOTES)
    app._active_tab = 0
    jumped = {'idx': None}
    app._goto_tab = lambda i: jumped.__setitem__('idx', i)
    app._new_tab = lambda *a, **k: (_ for _ in ()).throw(AssertionError('should not create a new tab'))

    app._open_notes()

    assert jumped['idx'] == 1


def test_open_llm_chat_creates_tagged_tab(make_app):
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    written = {}
    app._new_tab = lambda cmd=None, mode='': written.update(cmd=cmd, mode=mode)

    app._open_llm_chat()

    assert written['mode'] == _MODE_LLM
    assert 'llm_chat.py' in written['cmd']
    assert 'poetry run' in written['cmd']


# ── _cycle_mode: terminal -> notes -> llm -> terminal ─────────────────────────

def test_cycle_mode_from_terminal_opens_notes(make_app):
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    opened = {}
    app._open_notes = lambda: opened.setdefault('called', True)
    app._open_llm_chat = lambda: opened.setdefault('llm_called', True)

    app._cycle_mode()

    assert opened.get('called') is True
    assert 'llm_called' not in opened


def test_cycle_mode_from_notes_opens_llm(make_app):
    app = _app_with_tabs(make_app, _MODE_NOTES)
    opened = {}
    app._open_llm_chat = lambda: opened.setdefault('called', True)

    app._cycle_mode()

    assert opened.get('called') is True


def test_cycle_mode_from_llm_returns_to_existing_terminal_tab(make_app):
    app = _app_with_tabs(make_app, _MODE_TERMINAL, _MODE_LLM)
    app._active_tab = 1
    jumped = {'idx': None}
    app._goto_tab = lambda i: jumped.__setitem__('idx', i)

    app._cycle_mode()

    assert jumped['idx'] == 0


def test_cycle_mode_from_llm_creates_terminal_tab_when_none_open(make_app):
    app = _app_with_tabs(make_app, _MODE_LLM)
    created = {'n': 0}
    app._new_tab = lambda *a, **k: created.__setitem__('n', created['n'] + 1)

    app._cycle_mode()

    assert created['n'] == 1


# ── F6 palette dispatch ────────────────────────────────────────────────────────

def test_palette_action_notes_calls_open_notes(make_app):
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    called = {'n': 0}
    app._open_notes = lambda: called.__setitem__('n', called['n'] + 1)

    app._run_palette_action(_NOTES_OPEN)

    assert called['n'] == 1


def test_palette_action_llm_calls_open_llm_chat(make_app):
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    called = {'n': 0}
    app._open_llm_chat = lambda: called.__setitem__('n', called['n'] + 1)

    app._run_palette_action(_LLM_CHAT_OPEN)

    assert called['n'] == 1


# ── _open_terminal (used by _cycle_mode and the `terminal` shell command) ────

def test_open_terminal_jumps_to_existing_tab(make_app):
    app = _app_with_tabs(make_app, _MODE_LLM, _MODE_TERMINAL)
    app._active_tab = 0
    jumped = {'idx': None}
    app._goto_tab = lambda i: jumped.__setitem__('idx', i)
    app._new_tab = lambda *a, **k: (_ for _ in ()).throw(AssertionError('should not create a new tab'))

    app._open_terminal()

    assert jumped['idx'] == 1


def test_open_terminal_creates_tab_when_none_open(make_app):
    app = _app_with_tabs(make_app, _MODE_LLM)
    created = {'n': 0}
    app._new_tab = lambda *a, **k: created.__setitem__('n', created['n'] + 1)

    app._open_terminal()

    assert created['n'] == 1


# ── notes/llmchat/terminal shell-command signals (SIGRTMIN+1/+2/+3) ──────────
# Same pattern as test_idle_reset.py's SIGUSR2/_on_clear_signal test: the
# handler only flips a flag (signal-safe); dispatch happens in the main loop.

def test_on_notes_signal_sets_flag(make_app):
    app = make_app()
    app._notes_requested = False
    app._on_notes_signal(0, None)
    assert app._notes_requested is True


def test_on_llm_chat_signal_sets_flag(make_app):
    app = make_app()
    app._llm_chat_requested = False
    app._on_llm_chat_signal(0, None)
    assert app._llm_chat_requested is True


def test_on_terminal_signal_sets_flag(make_app):
    app = make_app()
    app._terminal_requested = False
    app._on_terminal_signal(0, None)
    assert app._terminal_requested is True


# ── Restart Terminal (F6 palette): backs up notes, then resets all tabs ──────

def test_restart_terminal_in_palette_actions():
    assert _RESTART_TERMINAL in _PALETTE_ACTIONS


def test_palette_action_restart_terminal_calls_restart(make_app):
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    called = {'n': 0}
    app._restart_terminal = lambda: called.__setitem__('n', called['n'] + 1)

    app._run_palette_action(_RESTART_TERMINAL)

    assert called['n'] == 1


def test_restart_terminal_backs_up_notes_before_resetting(make_app):
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    order = []
    app._backup_notes = lambda: order.append('backup')
    app._reset_session = lambda: order.append('reset')

    app._restart_terminal()

    assert order == ['backup', 'reset']


def test_backup_notes_copies_file_to_snapshot_dir(make_app, tmp_path, monkeypatch):
    import tabs_mixin
    monkeypatch.setattr(tabs_mixin, '_REPO_ROOT', str(tmp_path))
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    app._config = dict(app._config)
    notes_file = tmp_path / 'data' / 'notes.txt'
    notes_file.parent.mkdir(parents=True)
    notes_file.write_text('remember the milk')
    app._config['terminal_notes_file'] = str(notes_file)

    app._backup_notes()

    snap_dir = tmp_path / 'data' / 'notes_snapshots'
    snaps = list(snap_dir.glob('notes-*.txt'))
    assert len(snaps) == 1
    assert snaps[0].read_text() == 'remember the milk'


def test_backup_notes_missing_file_is_a_noop(make_app, tmp_path, monkeypatch):
    import tabs_mixin
    monkeypatch.setattr(tabs_mixin, '_REPO_ROOT', str(tmp_path))
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    app._config = dict(app._config)
    app._config['terminal_notes_file'] = str(tmp_path / 'data' / 'nope.txt')

    app._backup_notes()   # must not raise

    assert not (tmp_path / 'data' / 'notes_snapshots').exists()


def test_backup_notes_prunes_to_keep_limit(make_app, tmp_path, monkeypatch):
    import tabs_mixin
    monkeypatch.setattr(tabs_mixin, '_REPO_ROOT', str(tmp_path))
    app = _app_with_tabs(make_app, _MODE_TERMINAL)
    app._config = dict(app._config)
    notes_file = tmp_path / 'data' / 'notes.txt'
    notes_file.parent.mkdir(parents=True)
    notes_file.write_text('x')
    app._config['terminal_notes_file'] = str(notes_file)
    snap_dir = tmp_path / 'data' / 'notes_snapshots'
    snap_dir.mkdir(parents=True)
    for i in range(app._NOTES_SNAPSHOT_KEEP):
        (snap_dir / f'notes-2020010{i}-000000.txt').write_text('old')

    app._backup_notes()

    assert len(list(snap_dir.glob('notes-*.txt'))) == app._NOTES_SNAPSHOT_KEEP
