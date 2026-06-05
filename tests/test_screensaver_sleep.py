"""Tests for the configurable screensaver/deep-sleep timeout."""
from eink_terminal_app import _SETTINGS_SCHEMA, _SETTINGS_LIVE
from config_loader import load_config


def test_sleep_minutes_in_on_display_editor():
    keys = {s[0] for s in _SETTINGS_SCHEMA}
    assert 'screensaver_sleep_minutes' in keys
    # It applies live (no restart) when changed in the editor.
    assert 'screensaver_sleep_minutes' in _SETTINGS_LIVE


def test_apply_live_updates_idle_timeout(make_app):
    app = make_app()
    app._idle_timeout = 0
    app._apply_live('screensaver_sleep_minutes', 15)
    assert app._idle_timeout == 15 * 60
    app._apply_live('screensaver_sleep_minutes', 0)   # 0 = never sleep
    assert app._idle_timeout == 0


def test_shipped_config_default_is_15():
    cfg = load_config()
    assert cfg['screensaver_sleep_minutes'] == 15
