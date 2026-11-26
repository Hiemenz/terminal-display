"""Date-based screensaver picture (e.g. a Christmas one every year).

Decided each time the screensaver is drawn, so nothing has to edit the config
or restart the app when the season starts or ends.
"""
from __future__ import annotations

import os
from datetime import date

from util import _REPO_ROOT


def _month_day(value) -> tuple:
    month, day = str(value).split('-')
    # 2000 is a leap year, so 02-29 is accepted.
    parsed = date(2000, int(month), int(day))
    return parsed.month, parsed.day


def seasonal_screensaver(config: dict, today: date | None = None) -> str:
    """Path of the seasonal picture if it applies today, else ''.

    The start and end days are both included, and the range may wrap past
    New Year. Unset, malformed or missing-file settings give '', which leaves
    the normal screensaver in place.
    """
    path = config.get('screensaver_seasonal_image_path') or ''
    if not path:
        return ''
    try:
        start = _month_day(config.get('screensaver_seasonal_start'))
        end = _month_day(config.get('screensaver_seasonal_end'))
    except ValueError:
        return ''
    today = today or date.today()
    now = (today.month, today.day)
    if start <= end:
        in_season = start <= now <= end
    else:
        in_season = now >= start or now <= end
    if not in_season:
        return ''
    if not os.path.isabs(path):
        path = os.path.join(_REPO_ROOT, path)
    return path if os.path.isfile(path) else ''
