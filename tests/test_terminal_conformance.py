"""Replay recorded PTY streams and check the screen we build matches tmux's.

Every fixture in tests/fixtures/pty_streams/ holds the exact bytes a real
command wrote to a real PTY, plus what tmux said its pane contained
afterwards. Feeding those bytes to our screen has to reproduce that pane.

This exists because a missing escape sequence fails silently: pyte's CSI
dispatch drops any final byte it doesn't implement, with no exception and no
log line. `clear` inside tmux is spelled SU (`ESC[22S`), which pyte ignores,
so for a long time the panel kept showing text the user had just cleared and
nothing anywhere reported a problem. A grid diff catches that class of bug.

Which sequences a fixture exercises depends on the state tmux was in: with a
short pane it repaints with ED 2, and with a full one it scrolls the region
away with SU. Both are recorded (`clear`, `clear_after_scrolling`), because
both are what a user typing `clear` actually gets.

Record a new fixture with tools/record_pty_stream.py (needs tmux; the
recorded JSON is what CI replays, so CI needs neither tmux nor a terminal).
"""
import base64
import glob
import json
import os
import sys

import pyte
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from terminal_state import _TrackedByteStream, _TrackedScreen  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), 'fixtures', 'pty_streams')

# Fixtures whose divergence is a known, documented gap rather than a
# regression. Each entry is a reason, printed when the test xfails.
KNOWN_GAPS: dict = {}


def _fixtures():
    return sorted(glob.glob(os.path.join(FIXTURE_DIR, '*.json')))


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def _normalize(lines):
    """Compare what's actually on the grid: trailing spaces are invisible, and
    tmux's capture stops at the last row with content."""
    rows = [line.rstrip() for line in lines]
    while rows and not rows[-1]:
        rows.pop()
    return rows


def _replay(fixture):
    screen = _TrackedScreen(fixture['cols'], fixture['rows'])
    stream = _TrackedByteStream(screen)
    for chunk in fixture['chunks']:
        stream.feed(base64.b64decode(chunk))
    return screen


def _diff(expected, actual):
    out = []
    for i in range(max(len(expected), len(actual))):
        want = expected[i] if i < len(expected) else '<missing>'
        got = actual[i] if i < len(actual) else '<missing>'
        if want != got:
            out.append('row %2d\n  tmux: %r\n  ours: %r' % (i, want, got))
    return '\n'.join(out)


def test_fixtures_exist():
    assert _fixtures(), 'no recorded PTY streams — see tools/record_pty_stream.py'


@pytest.mark.parametrize('path', _fixtures(), ids=lambda p: os.path.basename(p)[:-5])
def test_screen_matches_tmux(path):
    fixture = _load(path)
    name = fixture['name']
    expected = _normalize(fixture['expected'])
    actual = _normalize(_replay(fixture).display)
    if name in KNOWN_GAPS and expected != actual:
        pytest.xfail(KNOWN_GAPS[name])
    assert expected == actual, (
        '%s (%s) renders differently than tmux:\n%s'
        % (name, ' '.join(fixture['command']), _diff(expected, actual)))


# Fixtures that stock pyte gets wrong, and the sequence it drops on each.
# These are the proof the suite is sensitive to the bug class it exists for.
SENSITIVE = {
    'scrolling': 'SU (CSI Ps S) — how tmux scrolls a full pane',
    'raw_htop': 'SD (CSI Ps T)',
}


@pytest.mark.parametrize('name,sequence', sorted(SENSITIVE.items()))
def test_harness_catches_a_dropped_escape(name, sequence):
    """A missing escape must show up as a grid diff, not as silence.

    Stock pyte drops the sequence named above, and its CSI dispatch does that
    without an exception or a log line — which is why `clear` was broken for
    so long. Replaying these fixtures through an unextended pyte must NOT
    match the pane. If one ever starts matching, that fixture stopped
    exercising the sequence and the suite has gone quietly blind.
    """
    fixture = _load(os.path.join(FIXTURE_DIR, name + '.json'))
    screen = pyte.Screen(fixture['cols'], fixture['rows'])
    stream = pyte.ByteStream(screen)
    for chunk in fixture['chunks']:
        stream.feed(base64.b64decode(chunk))
    expected = _normalize(fixture['expected'])
    assert _normalize(screen.display) != expected, (
        'fixture %s no longer exercises %s' % (name, sequence))
    # ...while our screen does match it.
    assert _normalize(_replay(fixture).display) == expected
