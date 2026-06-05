"""Tests for content-adaptive DU frame selection and refresh stats counters."""
from display_eink import EinkDriver


def _driver(**kw):
    return EinkDriver(local=True, **kw)


def test_light_content_uses_text_frames():
    d = _driver(du_adaptive=True, du_frames_text=20, du_frames_heavy=26,
               du_heavy_threshold=0.22)
    white = b'\x00' * (800 * 480 // 8)   # bit=1 → black; all-zero = all white
    assert d._du_frames_for(white) == 20


def test_heavy_content_uses_heavy_frames():
    d = _driver(du_adaptive=True, du_frames_text=20, du_frames_heavy=26,
               du_heavy_threshold=0.22)
    black = b'\xff' * (800 * 480 // 8)   # all black → density 1.0 ≥ threshold
    assert d._du_frames_for(black) == 26


def test_threshold_boundary():
    # ~25% black with a 0.22 threshold → heavy.
    d = _driver(du_frames_text=20, du_frames_heavy=26, du_heavy_threshold=0.22)
    n = 800 * 480 // 8
    buf = bytes([0xFF]) * (n // 4) + bytes([0x00]) * (n - n // 4)
    assert d._du_frames_for(buf) == 26


def test_adaptive_disabled_always_text():
    d = _driver(du_adaptive=False, du_frames_text=20, du_frames_heavy=26)
    black = b'\xff' * (800 * 480 // 8)
    assert d._du_frames_for(black) == 20


def test_stats_shape():
    d = _driver()
    s = d.stats()
    for k in ('partial', 'region', 'full', 'bytes', 'du_frames'):
        assert k in s
    assert s['last_flash_age'] is None   # nothing flashed yet
