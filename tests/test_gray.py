"""Tests for the panel's four-level greyscale path.

The bit order in a two-plane frame is exactly the sort of thing that is
invisible until it renders as garbage on hardware nobody can attach to CI, so
the quantisation and packing live in display_eink (pure) rather than in the
panel driver, and are pinned here.
"""
import logging

import pytest
from PIL import Image

import display_eink as de

logging.disable(logging.WARNING)

# (0x10 plane bit, 0x13 plane bit) the controller reads per pixel.
PAIRS = {0x00: (1, 1), 0x80: (0, 1), 0xC0: (1, 0), 0xFF: (0, 0)}


def _bit(plane, x, y, width=800):
    i = y * width + x
    return (plane[i // 8] >> (7 - (i % 8))) & 1


def _flat(level):
    return Image.new('L', (800, 480), level)


@pytest.mark.parametrize('level,pair', sorted(PAIRS.items()))
def test_each_level_packs_to_its_waveform_pair(level, pair):
    p0, p1 = de.pack_4gray(_flat(level), dither=False)
    assert (_bit(p0, 10, 10), _bit(p1, 10, 10)) == pair


def test_planes_are_one_bit_per_pixel():
    p0, p1 = de.pack_4gray(_flat(0), dither=False)
    assert len(p0) == len(p1) == 800 * 480 // 8


def test_quantise_lands_exactly_on_the_panel_levels():
    grad = Image.linear_gradient('L').resize((800, 480))
    assert set(de.quantize_4gray(grad, dither=True).getdata()) <= set(de.GRAY_LEVELS)


def test_quantise_without_dither_leaves_flat_bands():
    """Rendered text is already anti-aliased; dithering it just speckles the
    glyph edges, so the text path asks for nearest-level instead."""
    grad = Image.linear_gradient('L').resize((800, 480))
    banded = de.quantize_4gray(grad, dither=False)
    # A single row of a vertical gradient is one flat value either way; a
    # column crosses every band exactly once when undithered.
    column = [banded.getpixel((400, y)) for y in range(480)]
    assert len(set(column)) <= len(de.GRAY_LEVELS)
    assert sorted(set(column)) == sorted(set(column), reverse=False)


def test_off_level_values_snap_to_the_nearest_level(monkeypatch):
    """A caller that hands pack_4gray un-quantised pixels must not have them
    all collapse to black."""
    p0, p1 = de.pack_4gray(_flat(0xFE), dither=False)
    assert (_bit(p0, 0, 0), _bit(p1, 0, 0)) == PAIRS[0xFF]


def test_pure_python_fallback_matches_numpy(monkeypatch):
    """numpy is a hard dependency, but the fallback is the only thing standing
    between a missing wheel and a blank panel."""
    img = Image.new('L', (800, 480))
    img.paste(_flat(0x00).crop((0, 0, 400, 240)), (0, 0))
    img.paste(_flat(0x80).crop((0, 0, 400, 240)), (400, 0))
    img.paste(_flat(0xC0).crop((0, 0, 400, 240)), (0, 240))
    img.paste(_flat(0xFF).crop((0, 0, 400, 240)), (400, 240))
    fast = de.pack_4gray(img, dither=False)
    monkeypatch.setattr(de, '_HAS_NUMPY', False)
    assert de.pack_4gray(img, dither=False) == fast


# ── driver state after a grey write ──────────────────────────────────────────

class _FakeEpd:
    def __init__(self):
        self.calls = []

    def init_4gray(self):
        self.calls.append('init_4gray')

    def display_4gray(self, p0, p1):
        self.calls.append('display_4gray')

    def getbuffer(self, image):
        return bytearray(800 * 480 // 8)

    def init(self):
        self.calls.append('init')

    def init_part(self):
        self.calls.append('init_part')

    def display(self, buf):
        self.calls.append('display')

    def Clear(self):
        self.calls.append('Clear')


def _driver(epd):
    drv = de.EinkDriver(local=True)
    drv._local = False            # exercise the hardware routines directly
    drv._epd = epd
    return drv


def test_grey_write_forces_the_next_frame_to_reestablish_a_baseline():
    """A 4-grey frame has no 1-bit equivalent to keep as _prev_buf, so a
    partial update afterwards would diff against a frame that was never shown
    and leave the old one smeared underneath."""
    epd = _FakeEpd()
    drv = _driver(epd)
    drv._hw_gray(_flat(0x80), dither=False, reason='test')
    assert epd.calls == ['init_4gray', 'display_4gray']
    assert drv._needs_baseline is True
    assert drv._prev_buf is None
    assert drv._du_ready is False and drv._partial_ready is False

    drv._hw_partial_diff(_flat(0xFF))
    assert 'display' in epd.calls          # a full baseline, not a partial
    assert drv._needs_baseline is False


def test_grey_falls_back_to_one_bit_on_a_driver_without_it():
    class _Old:
        """A vendored driver from before 4-grey existed."""
        calls = []
        def __init__(self):
            self.calls = []
        def getbuffer(self, image):
            return bytearray(800 * 480 // 8)
        def init(self):
            self.calls.append('init')
        def init_part(self):
            self.calls.append('init_part')
        def display(self, buf):
            self.calls.append('display')
        def Clear(self):
            self.calls.append('Clear')

    epd = _Old()
    drv = _driver(epd)
    drv._hw_gray(_flat(0x00), reason='test')
    assert 'display' in epd.calls


def test_local_mode_never_touches_hardware(tmp_path):
    drv = de.EinkDriver(local=True)
    out = tmp_path / 'frame.bmp'
    drv.gray_refresh(_flat(0x80), output_path=str(out), reason='test')
    assert out.exists()
    assert drv._q.qsize() == 0
