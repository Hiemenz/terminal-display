"""
Background parking monitor — polls the Downtown Franklin garage occupancy API
every `interval_minutes` minutes and keeps the last 5 hours in a JSON history
file so the tile screensaver can plot the trend.

Polling is deliberately decoupled from drawing: new samples never trigger a
panel refresh, the chart just shows whatever has accumulated whenever the
screensaver is next drawn.

Usage::

    monitor = ParkingMonitor(interval_minutes=5, data_dir='data')
    monitor.start()
    samples = monitor.history()   # [{'ts': epoch, 'garages': {name: {'total': n, 'occupied': n}}}, …]
    monitor.stop()
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

API_URL = 'https://apps.franklintn.gov/indect/structure'
_KEEP_HOURS = 5


def parse_garages(data: dict) -> dict:
    """{garage name: {'total': bays, 'occupied': bays}} from the API payload."""
    out = {}
    for zone in data.get('Zones', []):
        name = zone.get('Name')
        if not name:
            continue
        out[name] = {'total': int(zone.get('TotalBays', 0) or 0),
                     'occupied': int(zone.get('OccupiedBays', 0) or 0)}
    return out


class ParkingMonitor:
    def __init__(self, interval_minutes: int = 5, data_dir: str = '.',
                 url: str = API_URL):
        self._interval = max(1, interval_minutes) * 60
        self._url = url
        self._history_path = os.path.join(data_dir, 'parking_history.json')
        self._lock = threading.Lock()
        self._history: list = self._load()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── public ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='parking',
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def history(self) -> list:
        """Snapshot of recent samples, oldest first."""
        with self._lock:
            return list(self._history)

    # ── internals ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        self._stop.wait(5)
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception as exc:
                logger.debug('Parking poll skipped: %s', exc)
            self._stop.wait(self._interval)

    def _fetch(self) -> dict:
        req = urllib.request.Request(
            self._url, headers={'User-Agent': 'Mozilla/5.0 (compatible; EinkDisplay/1.0)'})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.load(resp)
        except urllib.error.URLError as exc:
            if not isinstance(exc.reason, ssl.SSLError):
                raise
            # The city's server omits its intermediate cert, so verification
            # fails; this is public, read-only occupancy data.
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                return json.load(resp)

    def _poll(self) -> None:
        garages = parse_garages(self._fetch())
        if not garages:
            return
        entry = {'ts': time.time(), 'garages': garages}
        cutoff = time.time() - _KEEP_HOURS * 3600
        with self._lock:
            self._history.append(entry)
            self._history = [e for e in self._history if e.get('ts', 0) >= cutoff]
            self._save()
        logger.info('Parking: %s', ', '.join(
            f'{n} {g["occupied"]}/{g["total"]}' for n, g in garages.items()))

    def _load(self) -> list:
        try:
            with open(self._history_path) as f:
                data = json.load(f)
            cutoff = time.time() - _KEEP_HOURS * 3600
            return [e for e in data
                    if isinstance(e, dict) and e.get('ts', 0) >= cutoff
                    and isinstance(e.get('garages'), dict)]
        except Exception:
            return []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._history_path) or '.', exist_ok=True)
            with open(self._history_path, 'w') as f:
                json.dump(self._history, f)
        except Exception:
            pass
