"""
Lightweight alert monitor — polls for system conditions and accumulates
short-lived alert messages shown in the terminal status bar.

Designed to be called synchronously from the terminal loop via tick().
No threads — just fast, cached checks.
"""
import subprocess
import time

import psutil

_ALERT_DURATION = 10.0  # seconds each alert is shown


class AlertMonitor:
    def __init__(self, config: dict):
        self._config = config
        self._alerts: list = []   # [(message: str, expiry: float)]
        self._last_who: set = set()
        self._last_check: float = 0.0

    def tick(self) -> bool:
        """
        Run pending checks if enough time has passed.
        Returns True if the alert list changed (so the caller knows to re-render).
        """
        now = time.monotonic()
        interval = self._config.get('terminal_alert_check_interval', 10.0)
        changed = self._expire(now)
        if now - self._last_check >= interval:
            self._last_check = now
            changed |= self._check_cpu()
            changed |= self._check_disk()
            changed |= self._check_ssh()
        return changed

    def active(self) -> list:
        """Return messages for currently-active (non-expired) alerts."""
        now = time.monotonic()
        return [msg for msg, exp in self._alerts if exp > now]

    # ─── private ──────────────────────────────────────────────────────────────

    def _push(self, msg: str):
        # Avoid duplicating the same message while it is still active
        now = time.monotonic()
        for m, exp in self._alerts:
            if m == msg and exp > now:
                return
        self._alerts.append((msg, now + _ALERT_DURATION))

    def _expire(self, now: float) -> bool:
        before = len(self._alerts)
        self._alerts = [(m, e) for m, e in self._alerts if e > now]
        return len(self._alerts) != before

    def _check_cpu(self) -> bool:
        threshold = self._config.get('terminal_alert_cpu_threshold', 0)
        if threshold <= 0:
            return False
        pct = psutil.cpu_percent()  # uses cached value, no blocking interval
        if pct >= threshold:
            self._push(f'HIGH CPU {pct:.0f}%')
            return True
        return False

    def _check_disk(self) -> bool:
        threshold = self._config.get('terminal_alert_disk_free_threshold', 0)
        if threshold <= 0:
            return False
        path = self._config.get('disk_path', '/')
        try:
            usage = psutil.disk_usage(path)
            free_pct = 100.0 - usage.percent
            if free_pct <= threshold:
                self._push(f'LOW DISK {free_pct:.0f}% free on {path}')
                return True
        except Exception:
            pass
        return False

    def _check_ssh(self) -> bool:
        if not self._config.get('terminal_alert_ssh_logins', False):
            return False
        try:
            r = subprocess.run(
                ['who'], capture_output=True, text=True, timeout=1
            )
            sessions = set(r.stdout.strip().splitlines()) if r.returncode == 0 else set()
            new = sessions - self._last_who
            self._last_who = sessions
            for s in new:
                user = s.split()[0] if s.split() else '?'
                self._push(f'SSH LOGIN: {user}')
            return bool(new)
        except Exception:
            return False
