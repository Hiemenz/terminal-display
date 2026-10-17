import time

from PIL import Image

from parking_monitor import ParkingMonitor, parse_garages
from render import _DEFAULT_TILES, render_tile_screensaver

PAYLOAD = {'Name': 'City of Franklin', 'Zones': [
    {'Name': 'Second Avenue', 'TotalBays': 257, 'OccupiedBays': 52, 'Zones': []},
    {'Name': 'Fourth Avenue', 'TotalBays': 333, 'OccupiedBays': 83},
]}


def test_parse_garages_takes_top_level_zones():
    assert parse_garages(PAYLOAD) == {
        'Second Avenue': {'total': 257, 'occupied': 52},
        'Fourth Avenue': {'total': 333, 'occupied': 83},
    }


def test_monitor_polls_prunes_and_persists(tmp_path):
    m = ParkingMonitor(data_dir=str(tmp_path))
    m._fetch = lambda: PAYLOAD
    m._history = [{'ts': time.time() - 6 * 3600, 'garages': parse_garages(PAYLOAD)}]
    m._poll()
    assert len(m.history()) == 1          # stale sample pruned
    assert len(ParkingMonitor(data_dir=str(tmp_path)).history()) == 1


def test_tile_renders_two_lines():
    now = time.time()
    hist = [{'ts': now - (60 - i) * 300,
             'garages': {'Second Avenue': {'total': 257, 'occupied': 20 + i},
                         'Fourth Avenue': {'total': 333, 'occupied': 200 - i}}}
            for i in range(60)]
    assert 'parking' in _DEFAULT_TILES and 'speedtest' not in _DEFAULT_TILES
    cfg = {'dark_mode': False, 'screensaver_tiles': ['parking']}
    with_data = render_tile_screensaver(cfg, tile_data={'parking_history': hist})
    empty = render_tile_screensaver(cfg, tile_data={})
    assert isinstance(with_data, Image.Image) and with_data.tobytes() != empty.tobytes()


def test_photo_screensaver_shows_parking_chart():
    from render import render_screensaver
    now = time.time()
    hist = [{'ts': now - (20 - i) * 300,
             'garages': {'Second Avenue': {'total': 257, 'occupied': 50 + i},
                         'Fourth Avenue': {'total': 333, 'occupied': 90 - i}}}
            for i in range(20)]
    cfg = {'dark_mode': False}
    a = render_screensaver('assets/test.jpg', '', cfg, parking_history=hist)
    b = render_screensaver('assets/test.jpg', '', cfg)
    assert a.tobytes() != b.tobytes()
