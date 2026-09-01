"""
Background speedtest monitor — runs a download/upload measurement every
`interval_minutes` minutes and keeps the last 5 hours in a JSON history
file so the screensaver can plot the trend.

Usage::

    monitor = SpeedtestMonitor(interval_minutes=15, data_dir='data')
    monitor.start()                # non-blocking background thread
    samples = monitor.history()    # [{'ts': epoch, 'down': Mbps, 'up': Mbps}, …]
    monitor.stop()

The history file is also written so results survive a restart.  Readings
older than 5 hours are pruned on every save.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_KEEP_HOURS = 5
_MAX_GAP_MINUTES = 20   # gap wider than this is drawn as a break in the chart


class SpeedtestMonitor:
    def __init__(self, interval_minutes: int = 15, data_dir: str = '.'):
        self._interval   = max(1, interval_minutes) * 60
        self._history_path = os.path.join(data_dir, 'speedtest_history.json')
        self._lock       = threading.Lock()
        self._history: list = self._load()
        self._thread: threading.Thread | None = None
        self._stop       = threading.Event()

    # ── public ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background measurement thread (daemon, safe to call once)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='speedtest',
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def history(self) -> list:
        """Snapshot of recent samples, oldest first."""
        with self._lock:
            return list(self._history)

    def last(self) -> dict | None:
        """Most recent sample, or None if no data."""
        with self._lock:
            return dict(self._history[-1]) if self._history else None

    # ── internals ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        # First test fires right away (with a short startup delay so the app
        # can finish initialising before we consume network + CPU).
        self._stop.wait(5)
        while not self._stop.is_set():
            try:
                self._run_test()
            except Exception as exc:
                logger.debug('Speedtest skipped: %s', exc)
            self._stop.wait(self._interval)

    def _run_test(self) -> None:
        import speedtest as _st
        s = _st.Speedtest(secure=True)
        s.get_best_server()
        down = s.download() / 1e6   # → Mbps
        up   = s.upload()   / 1e6
        entry = {'ts': time.time(), 'down': round(down, 2), 'up': round(up, 2)}
        cutoff = time.time() - _KEEP_HOURS * 3600
        with self._lock:
            self._history.append(entry)
            self._history = [e for e in self._history if e.get('ts', 0) >= cutoff]
            self._save()
        logger.info('Speedtest: ↓ %.1f Mbps  ↑ %.1f Mbps', down, up)

    def _load(self) -> list:
        try:
            with open(self._history_path) as f:
                data = json.load(f)
            cutoff = time.time() - _KEEP_HOURS * 3600
            return [e for e in data
                    if isinstance(e, dict) and e.get('ts', 0) >= cutoff]
        except Exception:
            return []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._history_path) or '.', exist_ok=True)
            with open(self._history_path, 'w') as f:
                json.dump(self._history, f)
        except Exception:
            pass
