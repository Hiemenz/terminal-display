"""Tests for region-limited (changed-portion) flashing refresh in EinkDriver.

The count-based ghost clear flashes only the rows that changed; the periodic
whole-panel flash is driven by the app layer (terminal_full_refresh_interval).
"""
from display_eink import EinkDriver


class _FakeEpd:
    width = 800
    height = 480


def _driver(region_flash=True, sleeping=False):
    d = EinkDriver(local=True, region_flash=region_flash)   # local → no hw worker
    d._epd = _FakeEpd()
    d._prev_buf = bytearray(d._epd.width * d._epd.height // 8)
    d._hw_sleeping = sleeping
    calls = []
    d._hw_flash_region = lambda img, y0, y1: calls.append(('region', y0, y1))
    d._hw_full = lambda img: calls.append(('full',))
    return d, calls


def test_flashes_exact_changed_rows():
    d, calls = _driver()
    d._region_or_full_flash(None, (96, 128))
    assert calls == [('region', 96, 128)]   # exact band, not snapped to a half


def test_bottom_change_flashes_only_those_rows():
    d, calls = _driver()
    d._region_or_full_flash(None, (400, 480))
    assert calls == [('region', 400, 480)]


def test_no_change_info_flashes_full():
    d, calls = _driver()
    d._region_or_full_flash(None, None)
    assert calls == [('full',)]


def test_empty_span_flashes_full():
    d, calls = _driver()
    d._region_or_full_flash(None, (200, 200))
    assert calls == [('full',)]


def test_region_flash_disabled_always_full():
    d, calls = _driver(region_flash=False)
    d._region_or_full_flash(None, (0, 50))
    assert calls == [('full',)]


def test_sleeping_panel_flashes_full():
    # No reliable prior frame after sleep → can't do a windowed diff.
    d, calls = _driver(sleeping=True)
    d._region_or_full_flash(None, (0, 50))
    assert calls == [('full',)]


def test_change_y_from_bufs_single_row():
    d, _ = _driver()
    row_bytes = d._epd.width // 8        # 100
    n = row_bytes * d._epd.height
    old = bytearray(n)
    new = bytearray(n)
    new[50 * row_bytes + 3] = 0xFF       # change one byte in pixel-row 50
    assert d._change_y_from_bufs(bytes(old), bytes(new)) == (50, 51)


def test_change_y_from_bufs_identical_is_none():
    d, _ = _driver()
    buf = bytes(d._epd.width * d._epd.height // 8)
    assert d._change_y_from_bufs(buf, buf) is None
