import os
from datetime import date

import pytest
import yaml

from seasonal import seasonal_screensaver

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_ROOT, 'config', 'config.yaml')) as f:
    SHIPPED = yaml.safe_load(f)


@pytest.mark.parametrize('today, in_season', [
    (date(2026, 11, 19), False),
    (date(2026, 11, 20), True),
    (date(2026, 12, 31), True),
    (date(2027, 1, 1), True),
    (date(2027, 1, 6), True),
    (date(2027, 1, 7), False),
    (date(2027, 6, 15), False),
    (date(2027, 11, 20), True),
])
def test_shipped_christmas_season(today, in_season):
    expected = os.path.join(_ROOT, 'assets', 'christmas.jpg') if in_season else ''
    assert seasonal_screensaver(SHIPPED, today) == expected


def test_range_within_one_year():
    cfg = dict(SHIPPED, screensaver_seasonal_start='10-01', screensaver_seasonal_end='10-31')
    assert seasonal_screensaver(cfg, date(2027, 10, 15))
    assert not seasonal_screensaver(cfg, date(2027, 11, 1))


@pytest.mark.parametrize('override', [
    {'screensaver_seasonal_image_path': ''},
    {'screensaver_seasonal_image_path': 'assets/does-not-exist.jpg'},
    {'screensaver_seasonal_start': 'November'},
    {'screensaver_seasonal_end': '13-40'},
    {'screensaver_seasonal_end': None},
])
def test_bad_or_unset_settings_fall_back(override):
    assert seasonal_screensaver(dict(SHIPPED, **override), date(2026, 12, 25)) == ''


@pytest.mark.parametrize('image, expected', [
    ('assets/christmas.jpg', 'christmas.jpg'),
    ('', 'test.jpg'),
])
def test_terminal_screensaver_draws_the_chosen_picture(make_app, monkeypatch, image, expected):
    import render

    drawn = []
    monkeypatch.setattr(render, 'render_screensaver',
                        lambda path, *a, **k: drawn.append(os.path.basename(path)))
    # A season covering the whole year, so the result doesn't depend on today.
    app = make_app(screensaver_mode='static', screensaver_image_path='assets/test.jpg',
                   screensaver_seasonal_image_path=image,
                   screensaver_seasonal_start='01-01', screensaver_seasonal_end='12-31')
    app._driver = type('Driver', (), {'flash_refresh': lambda *a, **k: None,
                                      'sleep': lambda *a: None})()
    app._claude_usage = lambda: None
    app._screensaver_is_cycle = False
    app._screensaver_last_cycle = 0.0
    app._screensaver_cycle_idx = 0
    app._show_screensaver()
    assert drawn == [expected]
