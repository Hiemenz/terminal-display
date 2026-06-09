"""systemd watchdog + readiness notifications (pure stdlib, no dependencies).

The eink-display.service unit sets ``WatchdogSec=60``. That arms systemd's
hardware-style watchdog and exports ``NOTIFY_SOCKET`` / ``WATCHDOG_USEC`` into
the service environment, expecting the app to send ``WATCHDOG=1`` keep-alive
pings. If nothing ever pings, systemd assumes the app hung and SIGABRTs it about
once every ``WatchdogSec`` — which restarts the app, triggers a full-panel
refresh on every restart, and resets the idle timer so the screensaver never
fires. This module sends those pings.

It no-ops cleanly when ``NOTIFY_SOCKET`` is unset (dev machines, ``--local``,
anything not launched under systemd), so it is always safe to construct and call.
"""
import logging
import os
import socket
import time

logger = logging.getLogger(__name__)

# Spin detection: ping() is called once per main-loop iteration. A healthy idle
# loop blocks on select() each turn (tens of iterations/sec); a runaway busy-loop
# (e.g. a perpetually-readable fd we never drain) iterates far faster. If the
# rate stays above this for a full window, we treat it as a hang-equivalent and
# stop pinging so systemd's watchdog restarts us. Set generously above the
# healthy idle rate (~50 Hz) to avoid false positives during bursty I/O.
_SPIN_THRESHOLD_HZ = 2000.0
_SPIN_WINDOW_SEC = 10.0


class Watchdog:
    """Sends sd_notify keep-alive pings to systemd, throttled automatically.

    Also detects a busy-spinning main loop: because pings come from the loop, a
    spin would otherwise keep the watchdog satisfied forever. On spin we withhold
    pings so systemd restarts the service (matching the unit's documented intent).
    """

    def __init__(self):
        addr = os.environ.get('NOTIFY_SOCKET', '')
        # Abstract-namespace sockets are reported with a leading '@'.
        if addr.startswith('@'):
            addr = '\0' + addr[1:]
        self._addr = addr
        self._sock = None
        if addr:
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            except OSError:
                self._sock = None

        # systemd recommends pinging at half the watchdog interval. WATCHDOG_USEC
        # is in microseconds; fall back to 30s if it is missing.
        usec = int(os.environ.get('WATCHDOG_USEC', '0') or 0)
        self._interval = (usec / 1_000_000 / 2) if usec > 0 else 30.0
        self._last_ping = 0.0

        # Spin-detection window state.
        self._win_start = time.monotonic()
        self._win_count = 0
        self._spinning = False

    @property
    def enabled(self) -> bool:
        return self._sock is not None

    def _send(self, msg: str) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendto(msg.encode('utf-8'), self._addr)
        except OSError:
            pass

    def ready(self) -> None:
        """Signal start-up is complete and prime the first watchdog ping.

        ``READY=1`` is harmless under ``Type=simple`` (systemd ignores it there)
        and correct should the unit ever switch to ``Type=notify``.
        """
        self._send('READY=1')
        self._last_ping = time.monotonic()
        self._send('WATCHDOG=1')

    def ping(self, now: float | None = None) -> None:
        """Send ``WATCHDOG=1`` if half the interval has elapsed.

        Cheap to call every loop iteration: it only touches the socket once per
        half-interval. If the caller's loop hangs, pings stop and systemd
        restarts the service — which is the whole point of the watchdog. Also
        runs spin detection (see module docstring): once a spin is detected we
        stop pinging so systemd restarts us.
        """
        now = time.monotonic() if now is None else now

        # ── Spin detection ────────────────────────────────────────────────
        self._win_count += 1
        elapsed = now - self._win_start
        if elapsed >= _SPIN_WINDOW_SEC:
            rate = self._win_count / elapsed
            if rate > _SPIN_THRESHOLD_HZ and not self._spinning:
                self._spinning = True
                logger.error(
                    'Main loop spinning at %.0f Hz over %.0fs — withholding '
                    'watchdog pings so systemd restarts the service', rate, elapsed)
            self._win_start = now
            self._win_count = 0
        if self._spinning:
            return  # stop pinging → systemd watchdog fires and restarts us

        if self._sock is None:
            return
        if now - self._last_ping >= self._interval:
            self._last_ping = now
            self._send('WATCHDOG=1')
