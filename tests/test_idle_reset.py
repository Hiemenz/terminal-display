"""Tests for idle auto-reset, on-demand clear, graceful shutdown, and shell-exit recovery."""
import io
import select
import types

import pytest

from eink_terminal_app import _Tab
from config_loader import load_config


def test_shipped_config_default_reset_is_60():
    cfg = load_config()
    assert cfg['terminal_reset_minutes'] == 60


def test_shutdown_signal_requests_stop(make_app):
    app = make_app()
    app._running = True
    app._on_shutdown_signal(2, None)   # SIGINT
    assert app._running is False


def test_clear_signal_sets_flag(make_app):
    app = make_app()
    app._clear_requested = False
    app._on_clear_signal(12, None)     # SIGUSR2
    assert app._clear_requested is True


def test_clear_screen_resets_screen_and_keeps_shell(make_app):
    app = make_app()
    reset_called = {'n': 0}
    app._screen = types.SimpleNamespace(reset=lambda: reset_called.__setitem__('n', reset_called['n'] + 1))
    app._scroll_pages = 3
    app._pty_master = None          # skip the Ctrl-L write
    app._last_image = None          # skip the hardware flash
    app._last_full_refresh_mono = 0.0
    app._clear_screen()
    assert reset_called['n'] == 1
    assert app._scroll_pages == 0


def test_reset_session_collapses_to_single_fresh_tab(make_app):
    app = make_app(terminal_use_tmux=False)
    # Two stale tabs with no live children/PTYs (nothing to actually kill).
    app._tabs = [
        _Tab(screen=None, stream=None, pty_master=None, child_pid=None),
        _Tab(screen=None, stream=None, pty_master=None, child_pid=None),
    ]
    app._active_tab = 1
    app._scroll_pages = 5

    spawned = {'n': 0}

    def _fake_spawn(*a, **k):
        spawned['n'] += 1
        app._pty_master = 99
        app._child_pid = 1234

    app._init_screen = lambda: setattr(app, '_screen', 'fresh') or setattr(app, '_stream', 'fresh')
    app._spawn_shell = _fake_spawn

    app._reset_session(render=False)

    assert spawned['n'] == 1
    assert len(app._tabs) == 1
    assert app._active_tab == 0
    assert app._scroll_pages == 0
    assert app._tabs[0].pty_master == 99


# ── _shell_exited_handler ─────────────────────────────────────────────────────

def _stub_shell_exit_app(make_app):
    """Return an app wired up for _shell_exited_handler testing."""
    import pyte
    app = make_app(terminal_use_tmux=False)
    app._screen = pyte.Screen(80, 24)
    app._stream = pyte.ByteStream(app._screen)
    app._pty_master = 99
    app._child_pid = 1234
    app._tabs = [_Tab(screen=app._screen, stream=app._stream,
                      pty_master=99, child_pid=1234)]
    app._active_tab = 0
    app._evdev_kb = None
    spawned = {'n': 0}
    def _fake_spawn(*a, **k):
        spawned['n'] += 1
        app._pty_master = 99
        app._child_pid = 1234
    app._init_screen = lambda: None
    app._spawn_shell = _fake_spawn
    app._watchdog = types.SimpleNamespace(ping=lambda: None)
    return app, spawned


def test_shell_exited_handler_auto_restarts_on_stdin_eof(make_app, monkeypatch):
    """stdin returning b'' (e.g. /dev/null) should trigger an immediate auto-restart."""
    import os as _os
    app, spawned = _stub_shell_exit_app(make_app)

    # Fake stdin fd whose os.read always returns b'' (EOF).
    r_fd, w_fd = _os.pipe()
    _os.close(w_fd)   # close write-end so read-end is permanently EOF
    app._stdin_fd = r_fd

    try:
        result = app._shell_exited_handler()
    finally:
        try: _os.close(r_fd)
        except OSError: pass

    assert result is True
    assert spawned['n'] == 1


def test_shell_exited_handler_auto_restarts_on_stdin_oserror(make_app, monkeypatch):
    """OSError on stdin read (closed fd) should trigger auto-restart, not crash."""
    import os as _os
    app, spawned = _stub_shell_exit_app(make_app)

    # Closed fd so os.read raises OSError.
    r_fd, w_fd = _os.pipe()
    _os.close(r_fd)
    _os.close(w_fd)
    app._stdin_fd = r_fd   # already closed — os.read will OSError

    result = app._shell_exited_handler()

    assert result is True
    assert spawned['n'] == 1


def test_shell_exited_handler_evdev_enter_restarts(make_app, monkeypatch):
    """With an evdev keyboard, Enter (b'\\r' from evdev.read()) restarts the shell."""
    import os as _os
    app, spawned = _stub_shell_exit_app(make_app)

    r_fd, w_fd = _os.pipe()
    _os.write(w_fd, b'\r')   # simulate Enter
    _os.close(w_fd)

    class _FakeEvdev:
        def fileno(self): return r_fd
        def read(self): return _os.read(r_fd, 10)

    app._evdev_kb = _FakeEvdev()

    try:
        result = app._shell_exited_handler()
    finally:
        try: _os.close(r_fd)
        except OSError: pass

    assert result is True
    assert spawned['n'] == 1


def test_shell_exited_handler_ctrl_c_stops(make_app, monkeypatch):
    """Ctrl+C on the shell-exited prompt sets _running=False and returns False."""
    import os as _os
    app, spawned = _stub_shell_exit_app(make_app)

    r_fd, w_fd = _os.pipe()
    _os.write(w_fd, b'\x03')   # Ctrl+C
    _os.close(w_fd)
    app._stdin_fd = r_fd

    try:
        result = app._shell_exited_handler()
    finally:
        try: _os.close(r_fd)
        except OSError: pass

    assert result is False
    assert app._running is False
    assert spawned['n'] == 0   # no respawn


# ── Idle-reset exception safety ───────────────────────────────────────────────

def test_reset_session_exception_does_not_propagate(make_app):
    """A failure inside _reset_session must be caught by the caller, not crash it."""
    app = make_app(terminal_use_tmux=False)
    app._tabs = [_Tab(screen=None, stream=None, pty_master=None, child_pid=None)]
    app._active_tab = 0
    app._scroll_pages = 0

    def _bad_spawn(*a, **k):
        raise OSError('pty.fork() resource limit')

    app._init_screen = lambda: None
    app._spawn_shell = _bad_spawn

    # The caller in the main loop wraps _reset_session in try/except.
    # Verify _reset_session itself raises so the wrapper can catch it.
    with pytest.raises(OSError):
        app._reset_session(render=False)
