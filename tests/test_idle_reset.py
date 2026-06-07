"""Tests for idle auto-reset, on-demand clear, and graceful shutdown signals."""
import types

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
