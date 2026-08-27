"""Tests for the status bar's condition slot and the idle grey re-render.

An alert is an *event*: it fires once and ages out of the rotation. "claude is
waiting" is a *state* that stays true until someone answers it, and routing it
through the alert channel meant the notice scrolled away while the session was
still stopped.
"""
import pyte

import terminal_renderer as tr
from claude_attention import APPROVAL, WAITING, WORKING, AttentionWatcher


def _worked_then_stopped(watcher, key, label, state=WAITING, t0=0.0):
    """Drive a pane through the transition that announces itself."""
    watcher.update(key, WORKING, label, t0)
    return watcher.update(key, state, label, t0 + 60)


# ── the condition itself ─────────────────────────────────────────────────────

def test_no_condition_until_a_session_stops():
    w = AttentionWatcher(min_seconds=30)
    assert w.condition() == ''
    w.update('%1', WORKING, 'build', 0.0)
    assert w.condition() == ''


def test_a_stopped_session_becomes_a_standing_condition():
    w = AttentionWatcher(min_seconds=30)
    assert _worked_then_stopped(w, '%1', 'MlbDisplay')
    assert w.condition() == 'claude waiting (MlbDisplay)'


def test_an_approval_outranks_a_plain_wait():
    w = AttentionWatcher(min_seconds=30)
    _worked_then_stopped(w, '%1', 'a')
    _worked_then_stopped(w, '%2', 'b', state=APPROVAL)
    assert w.condition() == 'claude ×2 needs you'


def test_condition_clears_when_the_session_goes_back_to_work():
    """Answering it is what makes it stop being true — not time passing, and
    not the user typing somewhere else."""
    w = AttentionWatcher(min_seconds=30)
    _worked_then_stopped(w, '%1', 'MlbDisplay')
    w.update('%1', WORKING, 'MlbDisplay', 200.0)
    assert w.condition() == ''


def test_a_closed_pane_stops_counting():
    w = AttentionWatcher(min_seconds=30)
    _worked_then_stopped(w, '%1', 'MlbDisplay')
    w.forget('%1')
    assert w.condition() == ''


def test_a_short_turn_never_becomes_a_condition():
    """min_seconds keeps it quiet, and the standing line has to honour that too
    or it would say what the alert deliberately did not."""
    w = AttentionWatcher(min_seconds=30)
    w.update('%1', WORKING, 'x', 0.0)
    assert w.update('%1', WAITING, 'x', 4.0) is None
    assert w.condition() == ''


# ── how it lands on the panel ────────────────────────────────────────────────

def _render(**kw):
    screen = pyte.Screen(40, 4)
    pyte.Stream(screen).feed('hello')
    return tr.render_screen(screen, 14, dark_mode=False, hq=False, **kw)


def _bar_ink(img):
    bar = img.crop((0, tr.TERMINAL_H, 800, 480))
    # The bar is inverse video, so its text is the *light* pixels.
    return sum(1 for p in bar.getdata() if p > 128)


def test_the_condition_is_drawn_in_the_status_bar():
    assert _bar_ink(_render(conditions='claude waiting (x)')) > _bar_ink(_render())


def test_the_condition_survives_an_active_alert():
    """Alerts take over the left of the bar; the condition slot is separate
    precisely so a passing alert cannot hide a standing one."""
    with_both = _bar_ink(_render(alerts=['disk 95% full'],
                                 conditions='claude waiting (x)'))
    alert_only = _bar_ink(_render(alerts=['disk 95% full']))
    assert with_both > alert_only


def test_long_left_hand_text_is_trimmed_rather_than_overrunning():
    """Without this the two halves overprint into an unreadable smear."""
    long_bar = {'host': 'a-very-long-hostname-indeed' * 4, 'show_host': True}
    img = _render(bar_config=long_bar, conditions='claude ×3 needs you')
    plain = _render(bar_config=long_bar)
    # Same bar, but the right-hand slot is now reserved, so total lit pixels
    # cannot simply be the sum of both — the left text gives way.
    assert _bar_ink(img) < _bar_ink(plain) + _bar_ink(_render(
        conditions='claude ×3 needs you'))


# ── the idle grey re-render ──────────────────────────────────────────────────

class _RecordingDriver:
    def __init__(self):
        self.calls = []

    def gray_refresh(self, img, output_path=None, dither=True, reason=''):
        self.calls.append(('gray', dither, reason))

    def flash_refresh(self, img, *a, **k):
        self.calls.append(('flash', None, k.get('reason', '')))

    def full_refresh(self, img, *a, **k):
        self.calls.append(('full', None, k.get('reason', '')))


def _idle_app(make_app):
    app = make_app()
    app._driver = _RecordingDriver()
    app._screen = pyte.Screen(80, 20)
    pyte.Stream(app._screen).feed('$ make build\r\nok\r\n')
    app._net_stats = None
    app._bar_config = {}
    app._get_status_info = lambda: ('12:00', '~', '', '', '')
    app._img_cache = object()
    return app


def test_grey_rerender_pushes_an_undithered_grey_frame(make_app):
    app = _idle_app(make_app)
    assert app._gray_rerender() is True
    assert app._driver.calls == [('gray', False, 'gray-idle')]


def test_grey_rerender_drops_the_incremental_cache(make_app):
    """The cached 1-bit frame no longer matches the glass, so the next repaint
    has to be a full one rather than a diff against a frame never shown."""
    app = _idle_app(make_app)
    app._gray_rerender()
    assert app._img_cache is None


def test_grey_rerender_reports_failure_instead_of_raising(make_app):
    """Nothing about this is worth taking the terminal down for."""
    app = _idle_app(make_app)
    app._get_status_info = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
    assert app._gray_rerender() is False
    assert app._driver.calls == []


def _gray_guard(app) -> bool:
    """The grey re-render gate, mirroring the loop condition in eink_terminal_app."""
    return (app._gray_idle > 0
            and not getattr(app, '_in_text_message', False)
            and not getattr(app, '_markdown_active', False)
            and not getattr(app, '_big_text_active', False)
            and not getattr(app, '_help_sheet_active', False))


def _overlay_app(make_app):
    app = _idle_app(make_app)
    app._gray_idle = 20.0
    app._in_text_message = False
    app._help_sheet_active = False
    return app


def test_grey_rerender_skipped_while_help_sheet_active(make_app):
    """Outboard input that resets _last_input must not let the grey re-render
    overwrite the help sheet with the terminal 20 s later."""
    app = _overlay_app(make_app)
    app._help_sheet_active = True
    assert _gray_guard(app) is False


def test_grey_rerender_skipped_while_big_text_active(make_app):
    app = _overlay_app(make_app)
    app._big_text_active = True
    assert _gray_guard(app) is False


def test_grey_rerender_allowed_when_no_overlay(make_app):
    app = _overlay_app(make_app)
    assert _gray_guard(app) is True
