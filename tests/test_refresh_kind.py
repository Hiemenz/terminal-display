"""Tests for the refresh decision and the deferred (idle-aware) periodic flash."""


def _kind_app(make_app):
    return make_app()


def test_force_full_is_no_flash_full(make_app):
    app = _kind_app(make_app)
    assert app._refresh_kind(True, False, False) == 'full'
    # force_full wins even over a heavy change (clean repaint, no flash).
    assert app._refresh_kind(True, False, True) == 'full'


def test_force_flash_and_heavy_change_flash(make_app):
    app = _kind_app(make_app)
    assert app._refresh_kind(False, True, False) == 'flash'
    assert app._refresh_kind(False, False, True) == 'flash'


def test_default_is_partial(make_app):
    app = _kind_app(make_app)
    assert app._refresh_kind(False, False, False) == 'partial'


def _flash_app(make_app, interval=300, idle_gap=30, last_full=0.0,
               last_activity=0.0, needs=True):
    app = make_app()
    app._full_refresh_interval = interval
    app._flash_idle_gap = idle_gap
    app._last_full_refresh_mono = last_full
    app._last_activity = last_activity
    app._needs_periodic_flash = needs
    return app


def test_not_due_before_interval(make_app):
    app = _flash_app(make_app)
    assert app._periodic_flash_due(now=299.0) is False


def test_due_after_interval_when_quiet(make_app):
    # 5 min elapsed and 30 s since last activity → quiet gap → flash.
    app = _flash_app(make_app, last_activity=0.0)
    assert app._periodic_flash_due(now=301.0) is True


def test_not_due_while_typing_before_2x(make_app):
    # Interval elapsed but user typed recently (not quiet) and < 2× interval.
    app = _flash_app(make_app, last_activity=300.0)
    assert app._periodic_flash_due(now=305.0) is False


def test_forced_at_2x_even_while_typing(make_app):
    # Past 2× the interval → flash even though activity is constant.
    app = _flash_app(make_app, last_activity=600.0)
    assert app._periodic_flash_due(now=601.0) is True


def test_not_due_without_pending_partials(make_app):
    # Idle static screen (no partials since last full) never re-flashes.
    app = _flash_app(make_app, needs=False, last_activity=0.0)
    assert app._periodic_flash_due(now=10_000) is False


def test_interval_zero_disables(make_app):
    app = _flash_app(make_app, interval=0)
    assert app._periodic_flash_due(now=10_000) is False
