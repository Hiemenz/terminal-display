"""Adaptive refresh cadence by time of day.

Extends the single fixed `update_interval` / `stats_display_interval` into a
per-hour policy: e.g. refresh briskly during the day and slowly overnight to
save the panel and power. Night mode (the full skip-and-sleep window) is still
handled separately in main.py; this just tunes how often we refresh the rest
of the time.

(There is intentionally no "brightness" knob — the Waveshare panel has no
backlight, so cadence is the only lever.)

Config:
    adaptive_refresh: true
    refresh_schedule:
      - { from: 7,  to: 23, update_interval: 30,  stats_display_interval: 120 }
      - { from: 23, to: 7,  update_interval: 120, stats_display_interval: 600 }

`from`/`to` are 24-hour integers; a window wraps midnight when from > to. The
first matching window wins. Any key omitted from a window falls back to the
base top-level value. With adaptive_refresh off (or no matching window), the
base `update_interval` / `stats_display_interval` are used unchanged.
"""
from datetime import datetime


def _hour_in_window(hour: int, start: int, end: int) -> bool:
    if start == end:
        return True               # full-day window
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end   # wraps midnight


def effective_intervals(config: dict, now: datetime = None) -> tuple:
    """Return (update_interval, stats_display_interval) for the current hour."""
    base_update = config.get('update_interval', 30)
    base_stats = config.get('stats_display_interval', 120)

    if not config.get('adaptive_refresh', False):
        return base_update, base_stats

    hour = (now or datetime.now()).hour
    for win in config.get('refresh_schedule', []) or []:
        try:
            start = int(win['from'])
            end = int(win['to'])
        except (KeyError, TypeError, ValueError):
            continue
        if _hour_in_window(hour, start, end):
            return (
                win.get('update_interval', base_update),
                win.get('stats_display_interval', base_stats),
            )

    return base_update, base_stats
