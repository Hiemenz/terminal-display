"""
E-ink display driver wrapper.

On macOS: saves image file only (no hardware access).
On Linux/Pi: drives the Waveshare 7.5" V2 e-ink panel.

Two usage modes:
  display_image(img)         — legacy one-shot full refresh (stats dashboard)
  EinkDriver(local)          — persistent driver for terminal mode with partial refresh
"""
import os
import time
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

    def __init__(self, local: bool = False, partial_refresh_limit: int = 30,
                 flicker_free: bool = False, region_flash: bool = True,
                 du_adaptive: bool = True, du_frames_text: int = 0x14,
                 du_frames_heavy: int = 0x1A, du_heavy_threshold: float = 0.22):
        self._local = local or _IS_MAC
        self._partial_refresh_limit = partial_refresh_limit

        # Content-adaptive DU waveform. Heavy/inverse content (TUIs like htop/vim
        # — lots of solid black) needs more drive frames to fully transition and
        # not ghost; light text needs fewer (crisper + faster). We pick the frame
        # count per refresh from the black-pixel density and re-load the DU LUT
        # only when it crosses the threshold.
        self._du_adaptive       = du_adaptive
        self._du_frames_text    = int(du_frames_text)
        self._du_frames_heavy   = int(du_frames_heavy)
        self._du_heavy_threshold = du_heavy_threshold
        self._du_frames_loaded  = None   # frame count currently in the DU LUT

        # Live refresh counters for the debug HUD (read by the app via stats()).
        self._stats = {'partial': 0, 'region': 0, 'full': 0,
                       'bytes': 0, 'last_flash_mono': 0.0, 'du_frames': 0}

        # Ghost clearing: once `partial_refresh_limit` partials have stacked up,
        # flash just the rows that changed (the portion that changed) rather than
        # the whole panel — far less jarring on e-ink. A periodic whole-panel
        # flash (every terminal_full_refresh_interval seconds of activity) is
        # driven separately by the app layer. Set region_flash False to fall back
        # to a whole-panel flash for the count-based clear too.
        self._region_flash = region_flash

        # Flash-free direct-update partial mode (custom register LUT). When on,
        # partial updates rewrite the whole frame through a DU waveform that
        # moves only changed pixels, so typed characters appear without the
        # per-cell flash of the stock OTP partial waveform.
        self._flicker_free = flicker_free
        self._du_ready     = False   # panel currently in DU register-LUT mode

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

    def stats(self) -> dict:
        """Snapshot of the live refresh counters (for the debug HUD)."""
        s = dict(self._stats)
        s['last_flash_age'] = (time.monotonic() - s['last_flash_mono']
                               if s['last_flash_mono'] else None)
        return s

    def _du_frames_for(self, buf) -> int:
        """Pick the DU drive-frame count from the frame's black-pixel density:
        heavy/inverse content gets more frames, light text fewer."""
        if not self._du_adaptive or not _HAS_NUMPY:
            return self._du_frames_text
        arr = _np.frombuffer(buf, dtype=_np.uint8)
        # bit=1 → black; mean of all bits = black fraction of the frame.
        black_frac = float(_np.unpackbits(arr).mean())
        return (self._du_frames_heavy if black_frac >= self._du_heavy_threshold
                else self._du_frames_text)

    def full_refresh(self, image: Image.Image, output_path: str = None, flash: bool = False):
        """Update the full screen. By default uses partial mode (no flash).
        Pass flash=True to force a full black/white clear (clears ghosting but visible flash)."""
        _save(image, output_path)
        if self._local:
            return
        if flash:
            record_full_refresh()
            self._q.put((self._FULL, image))
        else:
            with self._partial_lock:
                already_queued = self._pending_partial is not None
                self._pending_partial = image
            if not already_queued:
                self._q.put((self._PARTIAL,))

    def flash_refresh(self, image: Image.Image, output_path: str = None):
        """Force a full flash refresh (black/white clear) — use for anti-ghosting or F10."""
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
        logging.info('E-ink full flash refresh (hw_sleeping=%s)', self._hw_sleeping)
        try:
            buf = epd.getbuffer(image)
            # After deep sleep use full init() to restore power rails.
            if self._hw_sleeping:
                epd.init()
                self._hw_sleeping = False
            else:
                epd.init_fast()
            epd.display(buf)
            self._stats['full'] += 1
            self._stats['last_flash_mono'] = time.monotonic()
            if self._flicker_free:
                # The flash above left the panel in OTP-LUT mode; force a DU
                # re-init before the next partial so flash-free updates resume.
                self._du_ready = False
                self._du_frames_loaded = None
            else:
                # Stay in partial mode so the next partial write needs no re-init.
                epd.init_part()
                self._partial_ready = True
            self._prev_buf = bytearray(buf)
        except IOError as e:
            logging.error('E-ink full refresh error: %s', e)
            self._hw_sleeping  = True
            self._partial_ready = False
            self._du_ready      = False

    def _hw_partial_du(self, image: Image.Image):
        """Flash-free refresh via the DU register-LUT waveform.

        Rewrites the whole frame, but the DU LUT gives unchanged pixels zero
        voltage so only changed pixels move — no per-cell flash. Supplies the
        prior frame as the controller's reference so the diff is correct even
        on the first update after switching into DU mode."""
        epd = self._epd_instance()
        if epd is None:
            return
        try:
            # No known prior frame (cold start / post-sleep): establish a clean
            # baseline with one full refresh, then DU-update from there on.
            if self._hw_sleeping and self._prev_buf is None:
                self._hw_full(image)
                return

            buf = epd.getbuffer(image)
            # Content-adaptive DU frame count; reload the LUT only when it changes.
            frames = self._du_frames_for(buf)
            if not self._du_ready or frames != self._du_frames_loaded:
                epd.init_du(frames)
                self._du_ready = True
                self._du_frames_loaded = frames
                self._partial_ready = False
                self._stats['du_frames'] = frames

            old = (bytes(self._prev_buf)
                   if self._prev_buf is not None and len(self._prev_buf) == len(buf)
                   else None)
            change_y = self._change_y_from_bufs(old, buf)
            if old is not None and _HAS_NUMPY:
                self._stats['bytes'] = int((_np.frombuffer(buf, dtype=_np.uint8)
                                            != _np.frombuffer(old, dtype=_np.uint8)).sum())
            self._stats['partial'] += 1
            epd.display_du(buf, old)
            self._prev_buf = bytearray(buf)
            self._hw_sleeping = False

            if record_partial_refresh(self._partial_refresh_limit):
                # Ghost-clearing flash is due — flash just the changed rows.
                if self._region_or_full_flash(image, change_y):
                    # The flash left the panel in partial/OTP-LUT mode; force a
                    # DU re-init before the next flash-free partial.
                    self._du_ready = False
                record_full_refresh()
        except IOError as e:
            logging.error('E-ink DU partial refresh error: %s', e)
            self._du_ready    = False
            self._hw_sleeping = True

    def _hw_partial_diff(self, image: Image.Image):
        """Diff against previous frame; refresh minimal per-band bounding boxes."""
        if self._flicker_free:
            self._hw_partial_du(image)
            return
        epd = self._epd_instance()
        if epd is None:
            return
        try:
            # If we just woke from a hardware sleep, the panel has lost the
            # 0x10 back-buffer (the "what's on screen" reference the partial
            # diff needs). With no known prior frame, do one clean full refresh
            # to re-establish it; otherwise re-prime 0x10 from _prev_buf below.
            if self._hw_sleeping and self._prev_buf is None:
                self._hw_full(image)
                return
            # Always re-prime the panel's 0x10 back-buffer with the frame that is
            # currently on screen (_prev_buf). The panel computes a partial update
            # as the diff between 0x10 and the new 0x13 data and only physically
            # drives pixels that differ. If 0x10 is stale, it drives the whole band
            # — that is the "flash" of an updated line. Supplying the true prior
            # frame every time keeps updates limited to genuinely-changed pixels.
            old_prev = bytes(self._prev_buf) if self._prev_buf is not None else None

            if not self._partial_ready:
                epd.init_part()
                self._partial_ready = True

            buf = epd.getbuffer(image)
            row_bytes = epd.width // 8  # 100 bytes per row for 800-wide display

            if self._prev_buf is None or len(self._prev_buf) != len(buf):
                epd.display_Partial(buf, 0, 0, epd.width, epd.height)
                self._prev_buf = bytearray(buf)
                self._hw_sleeping = False
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
                self._stats['bytes'] = int(len(changed))
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

            # Fall back to full-screen partial when too many bands change.
            # Using display_Partial for the whole screen avoids the flash that
            # epd.display() causes while still updating every pixel.
            total_h = sum(min(epd.height, (be + 1) * cell_h) - bs * cell_h
                          for bs, be, _, _ in bands)
            if total_h > epd.height * 0.6 or len(bands) > self._MAX_PARTIAL_SPANS:
                epd.display_Partial(buf, 0, 0, epd.width, epd.height,
                                    old_image=old_prev)
                self._prev_buf = bytearray(buf)
                self._hw_sleeping = False
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
                old_band = bytearray(band_w * (y_max - y_min)) if old_prev else None
                out = 0
                for row in range(y_min, y_max):
                    src = row * row_bytes + x_b0
                    band[out:out + band_w] = buf[src:src + band_w]
                    if old_band is not None:
                        old_band[out:out + band_w] = old_prev[src:src + band_w]
                    out += band_w
                patches.append((band, old_band, x_start, y_min, x_end, y_max))

            epd.display_Partial_multi(patches)
            self._stats['partial'] += 1
            self._hw_sleeping = False

            if record_partial_refresh(self._partial_refresh_limit):
                # Ghost-clearing flash is due — flash just the changed rows
                # (the portion that changed), not the whole panel.
                cy0 = min(bs for bs, _, _, _ in bands) * cell_h
                cy1 = min(epd.height, (max(be for _, be, _, _ in bands) + 1) * cell_h)
                self._region_or_full_flash(image, (cy0, cy1))
                record_full_refresh()               # reset the partial counter

        except IOError as e:
            logging.error('E-ink partial refresh error: %s', e)
            self._partial_ready = False
            self._prev_buf      = None

    def _change_y_from_bufs(self, old, new) -> tuple:
        """Pixel-row span (y_min, y_max) of bytes that differ between two frame
        buffers, or None if identical / not comparable."""
        epd = self._epd
        if old is None or epd is None or len(old) != len(new):
            return None
        row_bytes = epd.width // 8
        if _HAS_NUMPY:
            a = _np.frombuffer(new, dtype=_np.uint8)
            b = _np.frombuffer(old, dtype=_np.uint8)
            idx = _np.where(a != b)[0]
            if len(idx) == 0:
                return None
            rows = idx // row_bytes
            return (int(rows.min()), int(rows.max()) + 1)
        rows = [i // row_bytes for i in range(len(new)) if new[i] != old[i]]
        if not rows:
            return None
        return (min(rows), max(rows) + 1)

    def _region_or_full_flash(self, image: Image.Image, change_y: tuple = None) -> bool:
        """Ghost-clearing flash for the count-based trigger. Flashes only the
        changed rows (change_y = (y_min, y_max) in pixels) when region flashing is
        on and the prior frame is known; otherwise flashes the whole panel.
        Returns True (a hardware clear always happens here)."""
        epd = self._epd_instance()
        if epd is None:
            return False
        if (self._region_flash and change_y is not None
                and change_y[1] > change_y[0]
                and self._prev_buf is not None and not self._hw_sleeping):
            self._hw_flash_region(image, change_y[0], change_y[1])
        else:
            self._hw_full(image)
        return True

    def _hw_flash_region(self, image: Image.Image, y0: int, y1: int):
        """Clear ghosting in the horizontal band [y0, y1) only: drive the band to
        white, then repaint the image into it — a localized flash over the full
        width. Reuses display_Partial (no new waveforms) so it's safe on the V2.

        Falls back to a whole-panel flash if the prior frame isn't known (the
        windowed diff needs it) — e.g. straight after a hardware sleep."""
        epd = self._epd_instance()
        if epd is None:
            return
        row_bytes = epd.width // 8
        y0 = max(0, min(epd.height, int(y0)))
        y1 = max(y0, min(epd.height, int(y1)))
        try:
            buf = epd.getbuffer(image)
            if (self._prev_buf is None or len(self._prev_buf) != len(buf)
                    or self._hw_sleeping):
                self._hw_full(image); return
            if not self._partial_ready:
                epd.init_part()
                self._partial_ready = True

            region = bytes(buf[y0 * row_bytes:y1 * row_bytes])
            n = len(region)
            white = b'\xff' * n   # getbuffer white = 0xFF; display_Partial inverts
            black = b'\x00' * n
            # Drive every pixel in the band to white (old=black ⇒ all pixels move),
            # then paint the real image over it — a clean B/W refresh of the band.
            epd.display_Partial(white, 0, y0, epd.width, y1, old_image=black)
            epd.display_Partial(region, 0, y0, epd.width, y1, old_image=white)

            self._stats['region'] += 1
            self._stats['last_flash_mono'] = time.monotonic()
            self._prev_buf[:] = buf
            self._hw_sleeping = False
        except IOError as e:
            logging.error('E-ink region flash error: %s', e)
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
        # Deep sleep clears the panel's 0x10 back-buffer, so the partial-diff
        # reference is no longer valid. Drop it: the first refresh after wake
        # then takes the full-refresh path (which re-inits) instead of pushing
        # a partial against a frame the panel no longer holds.
        self._prev_buf = None
