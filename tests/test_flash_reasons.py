"""Every whole-panel flash records why it happened.

"It flashes too much" was previously only diagnosable by correlating journal
timestamps by hand: the counters said how many flashes, never which cause.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from display_eink import EinkDriver  # noqa: E402


def _driver():
    d = EinkDriver.__new__(EinkDriver)
    d._stats = {'partial': 0, 'region': 0, 'full': 0, 'bytes': 0,
                'last_flash_mono': 0.0, 'du_frames': 0, 'last_reason': ''}
    d._flash_reasons = {}
    d._flash_times = []
    return d


def test_reason_is_recorded():
    d = _driver()
    d._note_flash('clear')
    assert d.stats()['last_reason'] == 'clear'
    assert d.stats()['flash_reasons'] == {'clear': 1}


def test_reasons_are_counted_separately():
    d = _driver()
    for reason in ('clear', 'periodic', 'clear'):
        d._note_flash(reason)
    assert d.stats()['flash_reasons'] == {'clear': 2, 'periodic': 1}


def test_unnamed_flash_is_not_silently_dropped():
    d = _driver()
    d._note_flash('')
    assert d.stats()['last_reason'] == 'unknown'


def test_rate_counts_the_last_minute(monkeypatch):
    import display_eink
    now = [1000.0]
    monkeypatch.setattr(display_eink.time, 'monotonic', lambda: now[0])
    d = _driver()
    d._note_flash('clear')
    d._note_flash('clear')
    assert d.stats()['flashes_per_min'] == 2
    now[0] += 61.0                      # both fall out of the window
    assert d.stats()['flashes_per_min'] == 0


def test_old_timestamps_do_not_accumulate(monkeypatch):
    import display_eink
    now = [1000.0]
    monkeypatch.setattr(display_eink.time, 'monotonic', lambda: now[0])
    d = _driver()
    for _ in range(50):
        d._note_flash('periodic')
        now[0] += 5.0
    # The rolling list is pruned on write, not just on read.
    assert len(d._flash_times) <= 13
    assert d.stats()['flash_reasons']['periodic'] == 50
