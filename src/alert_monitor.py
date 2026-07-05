"""
Lightweight alert monitor — polls for system conditions and accumulates
short-lived alert messages shown in the terminal status bar.

Designed to be called synchronously from the terminal loop via tick().
No threads — just fast, cached checks.
"""
import shutil
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
        # Heavier checks (subprocess/network calls) run on their own slower
        # timer instead of every terminal_alert_check_interval tick, so a
        # blocking ping or vcgencmd call can't add input lag every 10s.
        self._last_health_check: float = 0.0
        self._network_fail_streak: int = 0

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

        health_interval = self._config.get('terminal_alert_health_interval', 30.0)
        if now - self._last_health_check >= health_interval:
            self._last_health_check = now
            changed |= self._check_throttle()
            changed |= self._check_failed_units()
            changed |= self._check_storage_health()
            changed |= self._check_network()
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

    def _check_throttle(self) -> bool:
        """Raspberry Pi under-voltage / thermal throttle, via vcgencmd.
        Only the *currently active* bits (0-3) are alerted on — bits 16-19
        are historical ('has occurred since boot') and would otherwise nag
        forever after a single power blip."""
        if not self._config.get('terminal_alert_throttle', True):
            return False
        if not shutil.which('vcgencmd'):
            return False
        try:
            r = subprocess.run(['vcgencmd', 'get_throttled'],
                               capture_output=True, text=True, timeout=2)
            raw = r.stdout.strip().split('=')[-1]
            bits = int(raw, 16)
        except Exception:
            return False
        labels = []
        if bits & 0x1:
            labels.append('UNDER-VOLTAGE')
        if bits & 0x2:
            labels.append('FREQ CAPPED')
        if bits & 0x4:
            labels.append('THROTTLED')
        if bits & 0x8:
            labels.append('TEMP LIMIT')
        if labels:
            self._push(' + '.join(labels))
            return True
        return False

    def _check_failed_units(self) -> bool:
        if not self._config.get('terminal_alert_failed_units', True):
            return False
        try:
            r = subprocess.run(
                ['systemctl', '--failed', '--no-legend', '--plain'],
                capture_output=True, text=True, timeout=3,
            )
            units = []
            for ln in r.stdout.splitlines():
                parts = ln.split()
                if not parts:
                    continue
                # A real terminal prefixes each row with a status glyph
                # (see `man systemctl`); piped output usually omits it, but
                # don't rely on that — unit names always contain a dot.
                tok = parts[0] if '.' in parts[0] else (
                    parts[1] if len(parts) > 1 else parts[0])
                units.append(tok)
        except Exception:
            return False
        if units:
            self._push(f"FAILED UNIT: {units[0]}" if len(units) == 1
                       else f"{len(units)} FAILED UNITS: {', '.join(units[:3])}")
            return True
        return False

    def _check_storage_health(self) -> bool:
        """The classic SD-card-dying symptom: the kernel remounts a failing
        filesystem read-only rather than crashing. Catch it via the actual
        mount options instead of dmesg, so it doesn't depend on log buffer
        size or verbosity."""
        if not self._config.get('terminal_alert_storage_health', True):
            return False
        try:
            with open('/proc/mounts') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 4 and parts[1] == '/':
                        opts = parts[3].split(',')
                        if 'ro' in opts:
                            self._push('ROOT FS READ-ONLY (SD card failing?)')
                            return True
                        break
        except Exception:
            pass
        return False

    def _check_network(self) -> bool:
        """Ping the default gateway (or a configured host) to detect a dead
        network — the one outage this device has no other way to report.
        Requires terminal_alert_network_fails consecutive misses before
        alerting, so a single dropped packet doesn't cry wolf."""
        if not self._config.get('terminal_alert_network', True):
            return False
        host = self._config.get('terminal_alert_network_host', '') or self._default_gateway()
        if not host:
            return False
        try:
            r = subprocess.run(['ping', '-c', '1', '-W', '1', host],
                               capture_output=True, timeout=2)
            ok = r.returncode == 0
        except Exception:
            ok = False
        if ok:
            self._network_fail_streak = 0
            return False
        self._network_fail_streak += 1
        threshold = self._config.get('terminal_alert_network_fails', 3)
        if self._network_fail_streak >= threshold:
            self._push(f'NETWORK DOWN (no reply from {host})')
            return True
        return False

    @staticmethod
    def _default_gateway() -> str:
        try:
            r = subprocess.run(['ip', 'route', 'show', 'default'],
                               capture_output=True, text=True, timeout=2)
            parts = r.stdout.split()
            if 'via' in parts:
                return parts[parts.index('via') + 1]
        except Exception:
            pass
        return '1.1.1.1'
