"""AlertMonitor: unit tests for alert logic and the SSH startup fix.

AlertMonitor has no threads and all subprocess calls are mockable, so these
run without a real Pi or system state. The tests focus on:
  - startup baseline: no false positives on first check
  - deduplication: the same alert doesn't stack while still active
  - SSH diffing: new sessions fire, departures are silent
  - disabled checks: returning False immediately when the config flag is off
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from alert_monitor import AlertMonitor  # noqa: E402


def _monitor(config=None):
    return AlertMonitor(config or {})


# ── startup baseline ──────────────────────────────────────────────────────────

def test_ssh_no_false_positive_at_startup(monkeypatch):
    """The baseline is seeded from who(1) at __init__ time, so existing
    sessions never trigger a login alert on the first check."""
    existing = {'pi pts/0 2026-01-01 10:00 (192.168.1.2)'}
    monkeypatch.setattr(
        'alert_monitor.AlertMonitor._current_who',
        staticmethod(lambda: existing),
    )
    m = _monitor({'terminal_alert_ssh_logins': True})
    # First tick — same sessions as startup baseline, should be silent
    changed = m._check_ssh()
    assert changed is False
    assert m.active() == []


def test_ssh_new_session_fires_alert(monkeypatch):
    """A session that wasn't there at startup → alert."""
    existing = {'pi pts/0 2026-01-01 10:00 (192.168.1.2)'}
    call_count = {'n': 0}

    def _who():
        call_count['n'] += 1
        if call_count['n'] == 1:
            return existing  # startup baseline
        return existing | {'alice pts/1 2026-01-01 11:00 (10.0.0.5)'}

    monkeypatch.setattr('alert_monitor.AlertMonitor._current_who', staticmethod(_who))
    m = _monitor({'terminal_alert_ssh_logins': True})
    changed = m._check_ssh()
    assert changed is True
    alerts = m.active()
    assert len(alerts) == 1
    assert 'alice' in alerts[0]


def test_ssh_departure_is_silent(monkeypatch):
    """A session that disappears should not fire any alert."""
    existing = {
        'pi pts/0 2026-01-01 10:00 (192.168.1.2)',
        'alice pts/1 2026-01-01 10:30 (10.0.0.5)',
    }
    call_count = {'n': 0}

    def _who():
        call_count['n'] += 1
        if call_count['n'] == 1:
            return existing
        return {'pi pts/0 2026-01-01 10:00 (192.168.1.2)'}  # alice left

    monkeypatch.setattr('alert_monitor.AlertMonitor._current_who', staticmethod(_who))
    m = _monitor({'terminal_alert_ssh_logins': True})
    changed = m._check_ssh()
    assert changed is False
    assert m.active() == []


def test_ssh_alert_disabled_by_config(monkeypatch):
    """No subprocess call and no alert when terminal_alert_ssh_logins is False."""
    called = {'n': 0}
    def _who():
        called['n'] += 1
        return set()
    monkeypatch.setattr('alert_monitor.AlertMonitor._current_who', staticmethod(_who))
    m = _monitor({'terminal_alert_ssh_logins': False})
    assert m._check_ssh() is False
    assert called['n'] == 1  # only the __init__ baseline call; check_ssh returns early


# ── deduplication ─────────────────────────────────────────────────────────────

def test_same_alert_not_stacked():
    m = _monitor()
    m._push('TEST')
    m._push('TEST')
    assert len(m.active()) == 1


def test_expired_alert_replaced():
    m = _monitor()
    m._alerts.append(('OLD', time.monotonic() - 1))  # already expired
    m._push('OLD')
    assert len(m.active()) == 1


# ── expiry ────────────────────────────────────────────────────────────────────

def test_expired_alerts_are_pruned():
    m = _monitor()
    m._alerts.append(('GONE', time.monotonic() - 1))
    m._alerts.append(('LIVE', time.monotonic() + 60))
    changed = m._expire(time.monotonic())
    assert changed is True
    assert m.active() == ['LIVE']


# ── disabled checks ───────────────────────────────────────────────────────────

def test_cpu_check_disabled_at_zero():
    m = _monitor({'terminal_alert_cpu_threshold': 0})
    assert m._check_cpu() is False


def test_disk_check_disabled_at_zero():
    m = _monitor({'terminal_alert_disk_free_threshold': 0})
    assert m._check_disk() is False


def test_throttle_check_respects_config():
    m = _monitor({'terminal_alert_throttle': False})
    assert m._check_throttle() is False


def test_failed_units_check_respects_config():
    m = _monitor({'terminal_alert_failed_units': False})
    assert m._check_failed_units() is False


def test_storage_health_check_respects_config():
    m = _monitor({'terminal_alert_storage_health': False})
    assert m._check_storage_health() is False


def test_network_check_respects_config():
    m = _monitor({'terminal_alert_network': False})
    assert m._check_network() is False


# ── network fail streak ───────────────────────────────────────────────────────

def test_network_single_failure_does_not_alert(monkeypatch):
    """One missed ping isn't an alert — only consecutive failures are."""
    import subprocess as sp
    monkeypatch.setattr(sp, 'run', lambda *a, **kw: type('R', (), {'returncode': 1})())
    m = _monitor({'terminal_alert_network': True,
                  'terminal_alert_network_host': '1.2.3.4',
                  'terminal_alert_network_fails': 3})
    assert m._check_network() is False
    assert m._network_fail_streak == 1


def test_network_streak_reaches_threshold(monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(sp, 'run', lambda *a, **kw: type('R', (), {'returncode': 1})())
    m = _monitor({'terminal_alert_network': True,
                  'terminal_alert_network_host': '1.2.3.4',
                  'terminal_alert_network_fails': 3})
    m._network_fail_streak = 2
    assert m._check_network() is True
    assert 'NETWORK DOWN' in m.active()[0]
