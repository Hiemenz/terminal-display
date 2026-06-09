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
import os
import socket
import time


class Watchdog:
    """Sends sd_notify keep-alive pings to systemd, throttled automatically."""

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
        restarts the service — which is the whole point of the watchdog.
        """
        if self._sock is None:
            return
        now = time.monotonic() if now is None else now
        if now - self._last_ping >= self._interval:
            self._last_ping = now
            self._send('WATCHDOG=1')
