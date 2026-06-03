"""
E-ink display driver wrapper.

On macOS: saves image file only (no hardware access).
On Linux/Pi: drives the Waveshare 7.5" V2 e-ink panel.

Two usage modes:
  display_image(img)         — legacy one-shot full refresh (stats dashboard)
  EinkDriver(local)          — persistent driver for terminal mode with partial refresh
"""
import os
import platform
import logging
import queue as _queue
import threading
from PIL import Image

from refresh_tracker import needs_full_refresh, record_full_refresh

_IS_MAC = platform.system() == 'Darwin'

if not _IS_MAC:
    try:
        from waveshare_epd import epd7in5_V2
    except Exception as e:
        logging.warning('Could not import waveshare_epd: %s', e)
        epd7in5_V2 = None
else:
    epd7in5_V2 = None

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_OUTPUT = os.path.join(_REPO_ROOT, 'output', 'terminal.bmp')


# ── Legacy one-shot function (used by stats dashboard) ───────────────────────

def display_image(image_to_display, output_filename=None):
    """Full refresh — display an 800×480 PIL image on the e-ink panel."""
    if output_filename is None:
        output_filename = _DEFAULT_OUTPUT
    os.makedirs(os.path.dirname(output_filename) or '.', exist_ok=True)
    image_to_display.save(output_filename)
    print(f'Image saved to {output_filename}')
    record_full_refresh()

    if _IS_MAC:
        print('macOS: skipping e-ink hardware update')
        return

    if epd7in5_V2 is None:
        print('e-ink module not available')
        return

    try:
        print('Pushing to e-ink display (full refresh)…')
        epd = epd7in5_V2.EPD()
        epd.init()
        epd.display(epd.getbuffer(image_to_display))
    except IOError as e:
        logging.error(e)
    except KeyboardInterrupt:
        epd7in5_V2.epdconfig.module_exit()
        raise
    finally:
        try:
            epd.sleep()
        except Exception:
            pass


def _save(image: Image.Image, output_path: str = None):
    if output_path is None:
        output_path = _DEFAULT_OUTPUT
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    image.save(output_path)


# ── Persistent driver for terminal mode ──────────────────────────────────────

class EinkDriver:
    """
    Persistent e-ink driver with a background hardware-write thread.

    Public methods return immediately; the actual SPI writes happen on a
    dedicated worker thread.  For partial refreshes the worker always uses
    the *latest* enqueued frame (stale intermediate frames are dropped), so
    the main loop is never blocked waiting for the display to finish.

    Sequence:
      partial_refresh_diff()  → async, latest-frame-wins
      full_refresh()          → async, queued in order
      sleep()                 → synchronous (blocks until hardware sleeps)
    """

    # ── task kinds sent to the worker ─────────────────────────────────────────
    _PARTIAL = 'partial'
    _FULL    = 'full'
    _SLEEP   = 'sleep'

    def __init__(self, local: bool = False):
        self._local = local or _IS_MAC

        # Hardware state — owned exclusively by the worker thread (no locks).
        self._epd          = None
        self._partial_ready = False
        self._prev_buf      = None
        self._hw_sleeping   = False   # True after sleep(); full init() needed to wake

        # Worker communication.
        self._q            = _queue.Queue()       # ordered task queue
        self._partial_lock = threading.Lock()
        self._pending_partial = None              # latest partial frame (replace-on-write)

        if not (local or _IS_MAC):
            threading.Thread(target=self._worker, daemon=True, name='eink-hw').start()

    # ── worker loop ───────────────────────────────────────────────────────────

    def _worker(self):
        while True:
            item = self._q.get()
            kind = item[0]
            if kind == self._PARTIAL:
                with self._partial_lock:
                    img = self._pending_partial
                    self._pending_partial = None
                if img is not None:
                    self._hw_partial_diff(img)
            elif kind == self._FULL:
                self._hw_full(item[1])
            elif kind == self._SLEEP:
                self._hw_sleep()
                item[1].set()   # unblock sleep() caller

    # ── public API ────────────────────────────────────────────────────────────

    def full_refresh(self, image: Image.Image, output_path: str = None):
        """Full refresh with flash. Async — enqueued behind any pending work."""
        _save(image, output_path)
        record_full_refresh()
        if self._local:
            return
        self._q.put((self._FULL, image))

    def partial_refresh_diff(self, image: Image.Image, output_path: str = None):
        """Async partial refresh. Returns immediately; hardware write runs in background.
        If the worker is still busy, the previous pending frame is replaced by this one."""
        _save(image, output_path)
        if self._local:
            return
        with self._partial_lock:
            already_queued = self._pending_partial is not None
            self._pending_partial = image
        if not already_queued:
            self._q.put((self._PARTIAL,))

    def partial_refresh(self, image: Image.Image, output_path: str = None):
        self.partial_refresh_diff(image, output_path)

    def sleep(self):
        """Wait for all pending writes to finish, then hardware-sleep the display."""
        if self._local:
            return
        # Cancel any queued partial that will never be shown.
        with self._partial_lock:
            self._pending_partial = None
        done = threading.Event()
        self._q.put((self._SLEEP, done))
        done.wait()

    # ── hardware routines (worker thread only) ────────────────────────────────

    def _epd_instance(self):
        if self._epd is None and epd7in5_V2 is not None:
            self._epd = epd7in5_V2.EPD()
        return self._epd

    def _hw_full(self, image: Image.Image):
        epd = self._epd_instance()
        if epd is None:
            return
        try:
            buf = epd.getbuffer(image)
            # After deep sleep use full init() to restore power rails.
            if self._hw_sleeping:
                epd.init()
                self._hw_sleeping = False
            else:
                epd.init_fast()
            epd.display(buf)
            # Stay in partial mode so the next partial write needs no re-init.
            epd.init_part()
            self._partial_ready = True
            self._prev_buf = bytearray(buf)
        except IOError as e:
            logging.error('E-ink full refresh error: %s', e)

    def _hw_partial_diff(self, image: Image.Image):
        """Diff against previous frame; update only changed pixel rectangles."""
        epd = self._epd_instance()
        if epd is None:
            return
        try:
            if not self._partial_ready:
                epd.init_part()
                self._partial_ready = True

            buf = epd.getbuffer(image)
            row_bytes = epd.width // 8  # 100 bytes per row for 800-wide display

            if self._prev_buf is None or len(self._prev_buf) != len(buf):
                epd.display_Partial(buf, 0, 0, epd.width, epd.height)
                self._prev_buf = bytearray(buf)
                return

            # Find dirty byte positions and record per-row X extents.
            dirty: dict = {}  # pixel-row → (xmin_byte, xmax_byte)
            for idx in range(len(buf)):
                if buf[idx] != self._prev_buf[idx]:
                    row, col = divmod(idx, row_bytes)
                    if row in dirty:
                        xn, xx = dirty[row]
                        dirty[row] = (min(xn, col), max(xx, col))
                    else:
                        dirty[row] = (col, col)

            self._prev_buf = bytearray(buf)

            if not dirty:
                return

            # Too many dirty rows — a full refresh is faster and cleaner.
            if len(dirty) > epd.height * 0.4:
                self._hw_full(image)
                return

            # Group nearby dirty rows (within 2 rows) into a single span to
            # reduce the number of display_Partial SPI calls.
            _GAP = 2
            rows = sorted(dirty)
            ys, ye = rows[0], rows[0] + 1
            xn, xx = dirty[rows[0]]
            spans = []
            for row in rows[1:]:
                rn, rx = dirty[row]
                if row <= ye + _GAP:
                    ye = max(ye, row + 1)
                    xn = min(xn, rn)
                    xx = max(xx, rx)
                else:
                    spans.append((ys, ye, xn, xx))
                    ys, ye, xn, xx = row, row + 1, rn, rx
            spans.append((ys, ye, xn, xx))

            # Single display_Partial call over the bounding box of all spans —
            # avoids a ReadBusy() wait per span.
            y0  = spans[0][0]
            y1  = spans[-1][1]
            xb0 = min(s[2] for s in spans)
            xb1 = max(s[3] for s in spans)
            # Crop the PIL image to the dirty bounding box and convert to
            # e-paper format (same polarity as getbuffer output).
            crop  = image.crop((xb0 * 8, y0, (xb1 + 1) * 8, y1))
            patch = epd.getbuffer_partial(crop)
            epd.display_Partial(patch, xb0 * 8, y0, (xb1 + 1) * 8, y1)

        except IOError as e:
            logging.error('E-ink partial refresh error: %s', e)

    def _hw_sleep(self):
        epd = self._epd_instance()
        if epd is not None:
            try:
                epd.sleep()
            except Exception:
                pass
        self._partial_ready = False
        self._hw_sleeping = True
