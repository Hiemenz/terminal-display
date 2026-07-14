"""Tests for screensaver robustness and SSH/tmux wake detection."""
import types

import pyte

import eink_terminal_app
import render as render_mod


class _FakeDriver:
    def __init__(self):
        self.calls = []

    def flash_refresh(self, img, *a, **k):
        self.calls.append('flash')

    def sleep(self):
        self.calls.append('sleep')


def _saver_app(make_app):
    app = make_app()
    app._driver = _FakeDriver()
    app._last_image = None
    app._screensaver_is_cycle = False
    app._screensaver_cycle_idx = 0
    app._screensaver_last_cycle = 0.0
    app._screensaver_show_mono = 0.0
    return app


def test_screensaver_shows_then_sleeps_panel(make_app, monkeypatch):
    app = _saver_app(make_app)
    sentinel = object()
    monkeypatch.setattr(render_mod, 'render_screensaver',
                        lambda *a, **k: sentinel)
    app._show_screensaver()
    assert app._driver.calls == ['flash', 'sleep']
    assert app._last_image is sentinel
    assert app._screensaver_show_mono > 0.0


def test_screensaver_failure_still_sleeps_panel(make_app, monkeypatch):
    """A render error must not leave the loop believing the screensaver is up
    while the panel stays awake on a stale frame (regression: an undefined
    variable in the success-path log line aborted the deep-sleep)."""
    app = _saver_app(make_app)

    def _boom(*a, **k):
        raise RuntimeError('render failed')

    monkeypatch.setattr(render_mod, 'render_screensaver', _boom)
    app._show_screensaver()
    assert 'sleep' in app._driver.calls
    assert app._screensaver_show_mono > 0.0


def _tmux_app(make_app):
    app = make_app(terminal_use_tmux=True)
    app._use_tmux = True
    app._wake_on_ssh = True
    app._tmux_activity_seen = 0.0
    app._tmux_poll_mono = 0.0
    app._last_input = 0.0
    return app


def _patch_clients(monkeypatch, stdout):
    monkeypatch.setattr(
        eink_terminal_app.subprocess, 'run',
        lambda *a, **k: types.SimpleNamespace(stdout=stdout, returncode=0))


def test_tmux_input_baseline_then_wake(make_app, monkeypatch):
    app = _tmux_app(make_app)
    _patch_clients(monkeypatch, '50.0\n')
    assert app._tmux_input_seen(100.0) is False     # first poll = baseline
    _patch_clients(monkeypatch, '60.0\n')
    assert app._tmux_input_seen(103.0) is True      # client typed → wake
    assert app._tmux_input_seen(104.0) is False     # throttled (<2 s)
    assert app._tmux_input_seen(106.0) is False     # no new input


def test_tmux_input_skipped_while_local_input_fresh(make_app, monkeypatch):
    app = _tmux_app(make_app)
    app._last_input = 99.5                          # fresh local keystroke
    _patch_clients(monkeypatch, '50.0\n')
    assert app._tmux_input_seen(100.0) is False
    assert app._tmux_poll_mono == 0.0               # didn't even poll


def test_tmux_input_disabled_by_config(make_app, monkeypatch):
    app = _tmux_app(make_app)
    app._wake_on_ssh = False
    _patch_clients(monkeypatch, '60.0\n')
    assert app._tmux_input_seen(100.0) is False


def test_scan_for_url_respects_row_filter(make_app):
    app = make_app()
    screen = pyte.Screen(60, 4)
    stream = pyte.Stream(screen)
    stream.feed('line one\r\nsee http://example.com/page here\r\nlast')
    app._screen = screen
    assert app._scan_for_url() == 'http://example.com/page'
    assert app._scan_for_url(rows={1}) == 'http://example.com/page'
    assert app._scan_for_url(rows={0, 2}) == ''
    assert app._scan_for_url(rows=set()) == ''
