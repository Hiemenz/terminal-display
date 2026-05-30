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


def _group_rows(rows: set) -> list:
    """Convert a set of row indices into sorted contiguous spans [(ystart, yend), ...]."""
    if not rows:
        return []
    sorted_r = sorted(rows)
    groups = []
    start = prev = sorted_r[0]
    for r in sorted_r[1:]:
        if r == prev + 1:
            prev = r
        else:
            groups.append((start, prev + 1))
            start = prev = r
    groups.append((start, prev + 1))
    return groups


# ── Persistent driver for terminal mode ──────────────────────────────────────

class EinkDriver:
    """
    Persistent e-ink driver that keeps the display "warm" between frames.

    Refresh modes:
      partial_refresh — fast (~0.3 s), no flash.  Use for every terminal update.
      full_refresh    — slow (~2 s), visible flash. Use periodically to clear ghosting.

    Sequence for terminal:
      1. partial_refresh() × N          → automatic init_part on first call
      2. full_refresh()                 → init_fast → display → init_part (stays in partial mode)
      3. repeat
      4. sleep()                        → only on app exit
    """

    def __init__(self, local: bool = False):
        self._local = local or _IS_MAC
        self._epd = None
        self._partial_ready = False  # True after init_part() has been called

    def _epd_instance(self):
        if self._epd is None and epd7in5_V2 is not None:
            self._epd = epd7in5_V2.EPD()
        return self._epd

    def full_refresh(self, image: Image.Image, output_path: str = None):
        """Full refresh with flash. Clears ghosting. Re-enters partial mode afterwards."""
        if output_path is None:
            output_path = _DEFAULT_OUTPUT
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        image.save(output_path)
        record_full_refresh()

        if self._local:
            return

        epd = self._epd_instance()
        if epd is None:
            return

        try:
            # init_fast is faster than init for a periodic ghost-clear refresh
            epd.init_fast()
            epd.display(epd.getbuffer(image))
            # Re-enter partial mode so the next partial_refresh() needs no re-init
            epd.init_part()
            self._partial_ready = True
        except IOError as e:
            logging.error('E-ink full refresh error: %s', e)

    def partial_refresh(self, image: Image.Image, output_path: str = None):
        """Fast partial refresh of the full screen — no flash."""
        self.partial_refresh_rows(image, None, output_path)

    def partial_refresh_rows(
        self,
        image: Image.Image,
        dirty_pixel_rows: set,
        output_path: str = None,
    ):
        """
        Refresh only the dirty pixel rows — the fastest possible update.

        dirty_pixel_rows: set of pixel-row indices that changed.
                          Pass None to refresh the entire screen.

        How it works: display_Partial(buf, 0, ystart, 800, yend) accepts a
        buffer offset so we slice buf[ystart * row_bytes:] which makes the
        function read the correct rows from the full-image buffer.
        """
        if output_path is None:
            output_path = _DEFAULT_OUTPUT
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        image.save(output_path)

        if self._local:
            return

        epd = self._epd_instance()
        if epd is None:
            return

        try:
            if not self._partial_ready:
                epd.init_part()
                self._partial_ready = True

            buf = epd.getbuffer(image)
            row_bytes = epd.width // 8  # 100 for 800-px-wide display

            if dirty_pixel_rows is None:
                # Full-screen partial (no flash, updates everything)
                epd.display_Partial(buf, 0, 0, epd.width, epd.height)
                return

            for ystart, yend in _group_rows(dirty_pixel_rows):
                # Slice the buffer so display_Partial reads the correct rows.
                # display_Partial indexes Image[0..Width*Height-1], which maps
                # to buf[ystart*row_bytes .. yend*row_bytes-1] via this slice.
                epd.display_Partial(buf[ystart * row_bytes:], 0, ystart, epd.width, yend)

        except IOError as e:
            logging.error('E-ink partial refresh error: %s', e)

    def sleep(self):
        """Put the display to sleep. Call only when the application exits."""
        if self._local:
            return
        epd = self._epd_instance()
        if epd is not None:
            try:
                epd.sleep()
            except Exception:
                pass
        self._partial_ready = False
