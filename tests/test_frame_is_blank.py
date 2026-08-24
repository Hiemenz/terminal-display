"""A deep flash runs epd.Clear() *and* epd.display(). For an already-blank
frame those write identical bytes to identical registers, so the panel flashes
twice for one wipe — _frame_is_blank lets the driver skip the redundant one."""
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import display_eink  # noqa: E402
from display_eink import _frame_is_blank  # noqa: E402


def _buf(img):
    """Same polarity as epd.getbuffer(): a 1 bit is a black pixel."""
    return bytearray(b ^ 0xFF for b in img.convert('1').tobytes('raw'))


def _cleared_screen():
    img = Image.new('1', (800, 480), 1)
    draw = ImageDraw.Draw(img)
    draw.text((4, 4), 'pi@hiemenzZero:~$', fill=0)
    draw.rectangle([0, 458, 799, 479], fill=0)   # status bar
    return img


def _screenful_of_text():
    img = Image.new('1', (800, 480), 1)
    draw = ImageDraw.Draw(img)
    for y in range(0, 456, 16):
        draw.text((4, y), 'some terminal output line %d' % y, fill=0)
    draw.rectangle([0, 458, 799, 479], fill=0)
    return img


def test_cleared_screen_is_blank():
    assert _frame_is_blank(_buf(_cleared_screen())) is True


def test_screenful_of_text_is_not_blank():
    assert _frame_is_blank(_buf(_screenful_of_text())) is False


def test_dark_mode_is_not_blank():
    assert _frame_is_blank(_buf(Image.new('1', (800, 480), 0))) is False


def test_empty_buffer_is_not_blank():
    assert _frame_is_blank(bytearray()) is False


def test_numpy_and_pure_python_paths_agree(monkeypatch):
    frames = [_buf(_cleared_screen()), _buf(_screenful_of_text()),
              _buf(Image.new('1', (800, 480), 0))]
    with_numpy = [_frame_is_blank(f) for f in frames]
    monkeypatch.setattr(display_eink, '_HAS_NUMPY', False)
    assert [_frame_is_blank(f) for f in frames] == with_numpy
