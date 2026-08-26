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

    def gray_refresh(self, img, *a, **k):
        self.calls.append('gray')

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


# ── Claude activity panel ─────────────────────────────────────────────────────

def test_usage_scan_failure_still_shows_the_screensaver(make_app, monkeypatch):
    """The activity panel is decoration. A transcript scan that blows up must
    not keep the panel awake on a stale frame."""
    app = _saver_app(make_app)
    sentinel = object()
    monkeypatch.setattr(render_mod, 'render_screensaver', lambda *a, **k: sentinel)
    monkeypatch.setattr('claude_usage.collect_usage',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('boom')))
    app._show_screensaver()
    assert app._driver.calls == ['flash', 'sleep']


def test_usage_panel_can_be_turned_off(make_app):
    app = _saver_app(make_app)
    app._config = dict(app._config, screensaver_show_claude_usage=False)
    assert app._claude_usage() == {}


def test_usage_is_cached_between_screensavers(make_app, monkeypatch):
    """Scanning ~130 MB of transcripts takes about a second; it must not run
    every time the screensaver comes up."""
    app = _saver_app(make_app)
    app._config = dict(app._config, screensaver_show_claude_usage=True,
                       terminal_claude_usage_ttl=300)
    scans = []
    monkeypatch.setattr('claude_usage.collect_usage',
                        lambda *a, **k: scans.append(1) or {'5h': {'messages': 1}})
    first = app._claude_usage()
    second = app._claude_usage()
    assert first == second
    assert len(scans) == 1


# ── what the panel sleeps on ──────────────────────────────────────────────────

def _sleep_app(make_app, **config):
    """An app parked right at the early-deep-sleep decision."""
    app = _saver_app(make_app)
    app._config = dict(app._config, **dict({'screensaver_enabled': True}, **config))
    app._sleep_timeout = 300
    app._idle_timeout = 900
    app._in_text_message = False
    app._last_input = 0.0
    app.slept_bare = False
    app._sleep_panel = lambda: setattr(app, 'slept_bare', True)
    app.showed_saver = False
    app._show_screensaver = lambda: setattr(app, 'showed_saver', True)
    return app


def _decide(app, idle):
    """The early-deep-sleep branch, in isolation: what happens at this idle?"""
    if (app._sleep_timeout > 0 and not app._in_text_message
            and idle > app._sleep_timeout
            and not (app._idle_timeout > 0 and idle > app._idle_timeout)):
        if (app._config.get('display_sleep_shows_screensaver', True)
                and app._config.get('screensaver_enabled', True)):
            app._show_screensaver()
        else:
            app._sleep_panel()


def test_panel_sleeps_on_the_lock_screen_not_the_terminal(make_app):
    """E-ink retains the last frame, so sleeping on the terminal leaves your
    session on the glass and looks like a display that never slept."""
    app = _sleep_app(make_app)
    _decide(app, idle=400)
    assert app.showed_saver is True
    assert app.slept_bare is False


def test_old_behaviour_is_still_available(make_app):
    app = _sleep_app(make_app, display_sleep_shows_screensaver=False)
    _decide(app, idle=400)
    assert app.slept_bare is True
    assert app.showed_saver is False


def test_disabled_screensaver_still_sleeps_bare(make_app):
    app = _sleep_app(make_app, screensaver_enabled=False)
    _decide(app, idle=400)
    assert app.slept_bare is True
    assert app.showed_saver is False


def test_nothing_happens_before_the_sleep_window(make_app):
    app = _sleep_app(make_app)
    _decide(app, idle=100)
    assert app.slept_bare is False and app.showed_saver is False
