"""
E-ink display driver wrapper.

On macOS: saves image file only (no hardware access).
On Linux/Pi: drives the Waveshare 7.5" V2 e-ink panel.
"""
import os
import platform
import logging
from PIL import Image

from refresh_tracker import needs_full_refresh, record_full_refresh

# Only import waveshare hardware driver on non-Darwin platforms
if platform.system() != 'Darwin':
    try:
        from waveshare_epd import epd7in5_V2
    except Exception as e:
        logging.warning(f"Could not import waveshare_epd: {e}")
        epd7in5_V2 = None
else:
    epd7in5_V2 = None


def display_image(image_to_display, output_filename='output/terminal.bmp'):
    """
    Full refresh — display an 800x480 PIL image on the e-ink panel.
    On macOS, only saves the file.
    """
    os.makedirs(os.path.dirname(output_filename) or '.', exist_ok=True)
    image_to_display.save(output_filename)
    print(f"Image saved to {output_filename}")
    record_full_refresh()

    if platform.system() == 'Darwin':
        print("macOS: skipping e-ink hardware update")
        return

    if epd7in5_V2 is None:
        print("e-ink module not available")
        return

    try:
        print("Pushing to e-ink display (full refresh)…")
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
