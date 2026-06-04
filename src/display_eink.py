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

try:
    import numpy as _np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

from refresh_tracker import needs_full_refresh, record_full_refresh, record_partial_refresh

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

    # Above this many disjoint changed regions in one frame, do a single full
    # refresh instead of many small partial flashes.
    _MAX_PARTIAL_SPANS = 7

    def __init__(self, local: bool = False, partial_refresh_limit: int = 30):
        self._local = local or _IS_MAC
        self._partial_refresh_limit = partial_refresh_limit

        # Character cell dimensions for aligned partial-refresh rectangles.
        # Set by EinkTerminal via set_cell_size() whenever the font changes.
        self._cell_w = 0
        self._cell_h = 0

        # Hardware state — owned exclusively by the worker thread (no locks).
        self._epd          = None
        self._partial_ready = False
        self._prev_buf      = None
        self._hw_sleeping   = True    # assume display is sleeping on startup; forces full init()

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

    def set_cell_size(self, cell_w: int, cell_h: int):
        """Set the terminal font's character cell dimensions.

        When set, partial-refresh rectangles are snapped to whole character
        cells so the flash box always aligns cleanly with character boundaries
        instead of cutting through the middle of a glyph or background row."""
        self._cell_w = max(0, cell_w)
        self._cell_h = max(0, cell_h)

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
            self._hw_sleeping  = True
            self._partial_ready = False

    def _hw_partial_diff(self, image: Image.Image):
        """Diff against previous frame; refresh minimal per-band bounding boxes."""
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

            # Diff against previous frame to find changed bytes.
            row_x = {}  # pixel_row -> [x_lo_px, x_hi_px]
            if _HAS_NUMPY:
                arr  = _np.frombuffer(buf, dtype=_np.uint8)
                prev = _np.frombuffer(self._prev_buf, dtype=_np.uint8)
                changed = _np.where(arr != prev)[0]
                self._prev_buf[:] = buf  # in-place: reuse allocation
                if len(changed) == 0:
                    return
                rows = changed // row_bytes
                cols = changed % row_bytes
                for r in _np.unique(rows):
                    mask     = rows == r
                    row_cols = cols[mask]
                    row_x[int(r)] = [int(row_cols.min()) * 8,
                                     int(row_cols.max()) * 8 + 7]
            else:
                for idx in range(len(buf)):
                    if buf[idx] != self._prev_buf[idx]:
                        prow = idx // row_bytes
                        pcol = idx % row_bytes
                        x_lo = pcol * 8
                        x_hi = x_lo + 7
                        if prow in row_x:
                            if x_lo < row_x[prow][0]: row_x[prow][0] = x_lo
                            if x_hi > row_x[prow][1]: row_x[prow][1] = x_hi
                        else:
                            row_x[prow] = [x_lo, x_hi]
                self._prev_buf[:] = buf  # in-place: reuse allocation
                if not row_x:
                    return

            # Map pixel rows to cell rows, accumulating x range per cell row.
            cell_h = self._cell_h if self._cell_h > 0 else 1
            snap_y = self._cell_h > 0
            snap_x = self._cell_w > 0

            cell_row_x = {}  # cell_row -> [x_lo, x_hi]
            for prow, (x_lo, x_hi) in row_x.items():
                cr = prow // cell_h if snap_y else prow
                if cr in cell_row_x:
                    if x_lo < cell_row_x[cr][0]: cell_row_x[cr][0] = x_lo
                    if x_hi > cell_row_x[cr][1]: cell_row_x[cr][1] = x_hi
                else:
                    cell_row_x[cr] = [x_lo, x_hi]

            # Group contiguous cell rows into bands.
            sorted_crows = sorted(cell_row_x)
            bands = []  # (cell_row_start, cell_row_end, x_lo, x_hi)
            b_start = sorted_crows[0]
            b_end   = b_start
            b_xlo, b_xhi = cell_row_x[b_start]
            for cr in sorted_crows[1:]:
                if cr == b_end + 1:
                    b_end = cr
                    if cell_row_x[cr][0] < b_xlo: b_xlo = cell_row_x[cr][0]
                    if cell_row_x[cr][1] > b_xhi: b_xhi = cell_row_x[cr][1]
                else:
                    bands.append((b_start, b_end, b_xlo, b_xhi))
                    b_start = b_end = cr
                    b_xlo, b_xhi = cell_row_x[cr]
            bands.append((b_start, b_end, b_xlo, b_xhi))

            # Fall back to full refresh when total changed height or band count is large.
            total_h = sum(min(epd.height, (be + 1) * cell_h) - bs * cell_h
                          for bs, be, _, _ in bands)
            if total_h > epd.height * 0.6 or len(bands) > self._MAX_PARTIAL_SPANS:
                self._hw_full(image)
                return

            # Collect all band patches, then flush with a single panel refresh.
            patches = []
            for b_start, b_end, b_xlo, b_xhi in bands:
                y_min = b_start * cell_h
                y_max = min(epd.height, (b_end + 1) * cell_h)

                if snap_x:
                    cw = self._cell_w
                    x_start = (b_xlo // cw) * cw
                    x_end   = ((b_xhi // cw) + 1) * cw
                    x_start = (x_start // 8) * 8
                    x_end   = min(epd.width, ((x_end + 7) // 8) * 8)
                else:
                    x_start = b_xlo          # already byte-aligned (pcol * 8)
                    x_end   = min(epd.width, b_xhi + 1)  # b_xhi+1 is byte-aligned

                # Slice band rows directly from the already-converted buffer —
                # avoids PIL crop + convert('1') + inversion per band.
                x_b0 = x_start // 8
                band_w = x_end // 8 - x_b0
                band = bytearray(band_w * (y_max - y_min))
                out = 0
                for row in range(y_min, y_max):
                    src = row * row_bytes + x_b0
                    band[out:out + band_w] = buf[src:src + band_w]
                    out += band_w
                patches.append((band, x_start, y_min, x_end, y_max))

            epd.display_Partial_multi(patches)

            if record_partial_refresh(self._partial_refresh_limit):
                self._hw_full(image)
                record_full_refresh()

        except IOError as e:
            logging.error('E-ink partial refresh error: %s', e)
            self._partial_ready = False
            self._prev_buf      = None

    def _hw_sleep(self):
        epd = self._epd_instance()
        if epd is not None:
            try:
                epd.sleep()
            except Exception:
                pass
        self._partial_ready = False
        self._hw_sleeping = True
