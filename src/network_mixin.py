"""EinkTerminal mixin: the ambient URL QR overlay and the background
network-speed monitor thread."""
from __future__ import annotations

import logging
import subprocess
import threading
import time

from terminal_state import _URL_RE, _fmt_speed, _get_local_ip, _read_wifi_signal

logger = logging.getLogger(__name__)


class NetworkMixin:
    """URL-scan QR overlay and the periodic network-speed sampling thread."""

    def _toggle_url_qr(self):
        self._show_url_qr = not self._show_url_qr
        if not self._show_url_qr:
            self._last_url = ''
        self._render(force_full=False)

    def _scan_for_url(self, rows=None) -> str:
        """Scan visible screen buffer bottom-to-top, return first URL found.
        `rows` limits the scan to those row indices (e.g. pyte's dirty set)."""
        if rows is not None:
            for row_idx in sorted((r for r in rows if 0 <= r < self._screen.lines),
                                  reverse=True):
                row = self._screen.buffer[row_idx]
                line = ''.join(row[c].data for c in range(self._screen.columns))
                m = _URL_RE.search(line)
                if m:
                    return m.group(0)
            return ''
        for row_idx in range(self._screen.lines - 1, -1, -1):
            row = self._screen.buffer[row_idx]
            line = ''.join(row[c].data for c in range(self._screen.columns))
            m = _URL_RE.search(line)
            if m:
                return m.group(0)
        return ''

    def _start_network_monitor_thread(self):
        """Run speedtest.net every speedtest_interval seconds (default 20 min).
        Falls back to a 5-second local throughput sample if speedtest is unavailable."""
        iface    = self._config.get('network_interface', '') or 'wlan0'
        interval = self._config.get('speedtest_interval', 1200)

        def _run_speedtest() -> tuple:
            """Returns (up_bps, down_bps). Tries speedtest-cli, then local sample."""
            # Try speedtest-cli Python package
            try:
                import speedtest as _st
                s = _st.Speedtest(secure=True)
                s.get_best_server()
                down_bps = s.download()
                up_bps   = s.upload()
                logger.info('Speedtest: ↓%.1f Mbps ↑%.1f Mbps',
                            down_bps / 1e6, up_bps / 1e6)
                return up_bps, down_bps
            except Exception:
                pass
            # Try speedtest-cli subprocess
            try:
                import json as _json
                r = subprocess.run(
                    ['speedtest-cli', '--json', '--secure'],
                    capture_output=True, text=True, timeout=60,
                )
                if r.returncode == 0:
                    d = _json.loads(r.stdout)
                    return d.get('upload', 0), d.get('download', 0)
            except Exception:
                pass
            # Fall back to 5-second local throughput sample
            try:
                import psutil as _psutil
                per = _psutil.net_io_counters(pernic=True)
                c0  = per.get(iface) or _psutil.net_io_counters()
                time.sleep(5)
                per = _psutil.net_io_counters(pernic=True)
                c1  = per.get(iface) or _psutil.net_io_counters()
                up   = (c1.bytes_sent - c0.bytes_sent) / 5.0 * 8
                down = (c1.bytes_recv - c0.bytes_recv) / 5.0 * 8
                return up, down
            except Exception:
                return 0.0, 0.0

        def _loop():
            while self._running:
                up_bps, down_bps = _run_speedtest()
                ip = _get_local_ip()
                with self._net_stats_lock:
                    self._net_stats = {
                        'ip':          ip,
                        'up':          _fmt_speed(up_bps),
                        'down':        _fmt_speed(down_bps),
                        'dirty':       True,
                        'wifi_signal': _read_wifi_signal(),
                    }
                if interval <= 0:
                    break
                deadline = time.monotonic() + interval
                while self._running and time.monotonic() < deadline:
                    time.sleep(30)

        threading.Thread(target=_loop, daemon=True, name='net-monitor').start()
