"""Tests for push notifications, the loop watchdog/spin detector, the web
terminal broadcast, and the shipped notification config defaults."""
import time

import notifier
import watchdog
from config_loader import load_config
from preview_server import TerminalBroadcast


# ─── notifier ──────────────────────────────────────────────────────────────

def test_notifier_disabled_by_default():
    notifier.configure({})
    assert notifier.enabled() is False
    # Must be a no-op (and never raise) when disabled.
    notifier.notify('title', 'body')


def test_notifier_enabled_for_known_providers():
    notifier.configure({'notify_provider': 'ntfy', 'ntfy_topic': 't'})
    assert notifier.enabled() is True
    notifier.configure({'notify_provider': 'pushover'})
    assert notifier.enabled() is True
    notifier.configure({'notify_provider': 'bogus'})
    assert notifier.enabled() is False


def test_notifier_rate_limits_by_key(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, '_send',
                        lambda *a, **k: sent.append(a))
    notifier.configure({'notify_provider': 'ntfy', 'ntfy_topic': 't',
                        'notify_min_interval': 1000})
    # reset any prior throttle state for these keys
    notifier._last_sent.clear()
    notifier.notify('CPU high', key='cpu')
    notifier.notify('CPU high again', key='cpu')   # suppressed (same key)
    notifier.notify('Disk low', key='disk')        # different key → allowed
    # _send runs on a daemon thread; give them a moment.
    time.sleep(0.2)
    assert len(sent) == 2


def test_shipped_config_notify_defaults_off():
    cfg = load_config()
    assert cfg.get('notify_provider', 'none') == 'none'


# ─── watchdog ───────────────────────────────────────────────────────────────

def test_sd_notify_noop_without_socket(monkeypatch):
    monkeypatch.delenv('NOTIFY_SOCKET', raising=False)
    assert watchdog.sd_notify('READY=1') is False


def test_loop_watchdog_detects_spin():
    wd = watchdog.LoopWatchdog(spin_threshold=50, window=0.1)
    # Hammer no-work iterations, then cross the window boundary.
    for _ in range(500):
        wd.tick(did_work=False)
    time.sleep(0.12)
    wd.tick(did_work=False)
    assert wd.spinning is True


def test_loop_watchdog_clear_when_working():
    wd = watchdog.LoopWatchdog(spin_threshold=50, window=0.1)
    for _ in range(500):
        wd.tick(did_work=False)
    time.sleep(0.12)
    wd.tick(did_work=False)
    assert wd.spinning is True
    # A quiet window of real work clears the spin flag.
    wd.tick(did_work=True)
    time.sleep(0.12)
    wd.tick(did_work=True)
    assert wd.spinning is False


# ─── web terminal broadcast ─────────────────────────────────────────────────

def test_broadcast_replays_ring_to_new_subscriber():
    b = TerminalBroadcast()
    b.feed(b'hello ')
    b.feed(b'world')
    q, snapshot = b.subscribe()
    assert snapshot == b'hello world'
    b.feed(b'!')
    assert q.get_nowait() == b'!'
    b.unsubscribe(q)


def test_broadcast_ring_is_bounded():
    b = TerminalBroadcast(ring_bytes=10)
    b.feed(b'0123456789ABCDEF')
    _q, snapshot = b.subscribe()
    assert len(snapshot) == 10
    assert snapshot == b'6789ABCDEF'


def test_broadcast_fans_out_to_multiple_subs():
    b = TerminalBroadcast()
    q1, _ = b.subscribe()
    q2, _ = b.subscribe()
    b.feed(b'x')
    assert q1.get_nowait() == b'x'
    assert q2.get_nowait() == b'x'
