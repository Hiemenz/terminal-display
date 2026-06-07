"""
Health watchdog: systemd sd_notify + a busy-loop ("spin") detector.

Two independent safety nets:

* sd_notify  — talk to systemd's $NOTIFY_SOCKET (set when the unit is
  Type=notify and/or has WatchdogSec=). READY=1 on startup, periodic
  WATCHDOG=1 pings afterwards. If the process *hangs* (stops pinging),
  systemd restarts it. No-op when not run under systemd. Stdlib only.

* LoopWatchdog — counts loop iterations that did **no work** (no bytes
  consumed). A healthy idle loop blocks on select() and ticks a few times a
  second; a busy-spin (e.g. a fd stuck at EOF) ticks thousands of times a
  second doing nothing. That pathology does NOT trip the sd watchdog — a
  spinning loop keeps pinging happily — so we detect it here and raise a
  push notification. This is the exact failure class that once pegged the
  panel at ~74% CPU and stopped it sleeping.
"""
import logging
import os
import socket
import time

logger = logging.getLogger(__name__)

try:
    import notifier
except ImportError:
    notifier = None


def sd_notify(state: str) -> bool:
    """Send a datagram to systemd's notify socket. Returns False if not under
    systemd (no $NOTIFY_SOCKET) or on any socket error."""
    addr = os.environ.get('NOTIFY_SOCKET')
    if not addr:
        return False
    if addr[0] == '@':              # abstract namespace
        addr = '\0' + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode('utf-8'))
        return True
    except OSError as e:
        logger.debug('sd_notify(%r) failed: %s', state, e)
        return False


def notify_ready():
    sd_notify('READY=1')


def _watchdog_period() -> float:
    """Half of WATCHDOG_USEC (systemd's recommended ping cadence), or 0 if the
    watchdog isn't enabled for this unit."""
    usec = os.environ.get('WATCHDOG_USEC')
    if not usec:
        return 0.0
    try:
        return (int(usec) / 1_000_000.0) / 2.0
    except ValueError:
        return 0.0


class LoopWatchdog:
    """Drive once per main-loop iteration via tick(did_work)."""

    def __init__(self, spin_threshold: float = 300.0, window: float = 5.0,
                 notify_key: str = 'loopspin'):
        self._spin_threshold = spin_threshold   # no-work iters/sec → "spinning"
        self._window = window
        self._notify_key = notify_key
        self._noop_count = 0
        self._window_start = time.monotonic()
        self._wd_period = _watchdog_period()
        self._last_ping = 0.0
        self._spinning = False
        if self._wd_period:
            logger.info('sd watchdog active: ping every %.1fs', self._wd_period)

    def tick(self, did_work: bool):
        now = time.monotonic()

        # ── systemd liveness ping ────────────────────────────────────────────
        if self._wd_period and (now - self._last_ping) >= self._wd_period:
            self._last_ping = now
            sd_notify('WATCHDOG=1')

        # ── spin detection (count only no-work iterations) ───────────────────
        if not did_work:
            self._noop_count += 1
        elapsed = now - self._window_start
        if elapsed >= self._window:
            rate = self._noop_count / elapsed
            if rate >= self._spin_threshold:
                if not self._spinning:          # edge-trigger: warn once
                    self._spinning = True
                    logger.error(
                        'busy-loop detected: %.0f no-work iters/s — a fd is '
                        'likely stuck readable at EOF', rate)
                    if notifier is not None:
                        notifier.notify(
                            'e-ink busy-loop',
                            f'{rate:.0f} idle iters/s — input fd stuck at EOF?',
                            priority='high', tags='warning', key=self._notify_key)
            else:
                self._spinning = False
            self._noop_count = 0
            self._window_start = now

    @property
    def spinning(self) -> bool:
        return self._spinning
