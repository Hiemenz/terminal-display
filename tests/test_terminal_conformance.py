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
import re
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


def _replay(fixture, screen=None):
    """Feed a fixture's bytes to a screen, honouring any mid-stream resize.

    A resize marker is what the app does on a font-size change: the grid
    changes under a program that is already drawing.
    """
    if screen is None:
        screen = _TrackedScreen(fixture['cols'], fixture['rows'])
    stream = _TrackedByteStream(screen)
    for chunk in fixture['chunks']:
        if isinstance(chunk, dict):
            cols, rows = chunk['resize']
            screen.resize(rows, cols)
            continue
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


def test_a_resized_fixture_ends_at_its_final_size():
    """Any fixture carrying a resize marker must end on the size tmux captured
    — otherwise the grid we diff isn't the grid tmux described."""
    for path in _fixtures():
        fixture = _load(path)
        markers = [c for c in fixture['chunks'] if isinstance(c, dict)]
        if not markers:
            continue
        screen = _replay(fixture)
        final_cols, final_rows = fixture.get(
            'final_size', [fixture['cols'], fixture['rows']])
        assert (screen.columns, screen.lines) == (final_cols, final_rows), path


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
        if isinstance(chunk, dict):
            cols, rows = chunk['resize']
            screen.resize(rows, cols)
            continue
        stream.feed(base64.b64decode(chunk))
    expected = _normalize(fixture['expected'])
    assert _normalize(screen.display) != expected, (
        'fixture %s no longer exercises %s' % (name, sequence))
    # ...while our screen does match it.
    assert _normalize(_replay(fixture).display) == expected


# Sequences the corpus must keep exercising, and what breaks when it doesn't.
# A fixture can stop covering one silently — a program changes how it draws,
# or someone re-records on a different tmux — and the suite quietly narrows.
COVERAGE = {
    'SU (scroll up)': rb'\x1b\[[0-9;]*S',
    'ED (erase display)': rb'\x1b\[[0-9;]*J',
    'EL (erase line)': rb'\x1b\[[0-9;]*K',
    'CUP (cursor position)': rb'\x1b\[[0-9;]*H',
    'DECSTBM (scroll region)': rb'\x1b\[[0-9;]*r',
    'alternate screen': rb'\x1b\[\?1049[hl]',
    'SGR (colour/attrs)': rb'\x1b\[[0-9;]*m',
}


def _raw_bytes(fixture):
    return b''.join(base64.b64decode(c) for c in fixture['chunks']
                    if not isinstance(c, dict))


def _corpus_bytes():
    return {os.path.basename(p)[:-5]: _raw_bytes(_load(p)) for p in _fixtures()}


@pytest.mark.parametrize('label,pattern', sorted(COVERAGE.items()))
def test_corpus_still_exercises(label, pattern):
    hits = [name for name, raw in _corpus_bytes().items()
            if re.search(pattern, raw)]
    assert hits, 'no fixture exercises %s any more' % label


def test_both_input_paths_are_represented():
    """tmux normalizes what it forwards, so tmux-mode fixtures alone would
    never exercise the escapes a program emits directly."""
    sources = {_load(p).get('source', 'tmux') for p in _fixtures()}
    assert {'tmux', 'raw-pty'} <= sources


def test_a_resize_is_covered():
    resized = [p for p in _fixtures()
               if any(isinstance(c, dict) for c in _load(p)['chunks'])]
    assert resized, 'no fixture resizes mid-stream (what a font-size change does)'
