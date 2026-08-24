"""tmux doesn't forward `clear`'s ED 2 to the outer terminal — it scrolls the
whole pane away with SU (CSI Ps S), which pyte doesn't implement. Without the
handling in _ClearTrackingMixin the old lines stayed in the buffer, so the
panel kept showing text the user had just cleared."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from terminal_state import (_TrackedByteStream, _TrackedHistoryScreen,  # noqa: E402
                            _TrackedScreen)

# What `clear` inside tmux actually emits to the outer PTY (captured live).
TMUX_CLEAR = b'\x1b[1;23r\x1b[1;1H\x1b[2;23r\x1b[22S\x1b[1;1H\x1b[K\x1b[1;24r\x1b[1;1H'


def _filled(screen, stream, lines=12):
    for i in range(lines):
        stream.feed(('line%d\r\n' % i).encode())
    screen.full_clear = False


def test_tmux_clear_empties_the_screen():
    screen = _TrackedScreen(80, 24)
    stream = _TrackedByteStream(screen)
    _filled(screen, stream)
    stream.feed(TMUX_CLEAR)
    assert not [ln for ln in screen.display if ln.strip()]


def test_tmux_clear_requests_a_deep_flash():
    screen = _TrackedScreen(80, 24)
    stream = _TrackedByteStream(screen)
    _filled(screen, stream)
    stream.feed(TMUX_CLEAR)
    assert screen.full_clear is True


def test_ed2_still_requests_a_deep_flash():
    screen = _TrackedScreen(80, 24)
    stream = _TrackedByteStream(screen)
    _filled(screen, stream)
    stream.feed(b'\x1b[2J')
    assert screen.full_clear is True


def test_partial_scroll_is_not_a_clear():
    screen = _TrackedScreen(80, 24)
    stream = _TrackedByteStream(screen)
    _filled(screen, stream)
    stream.feed(b'\x1b[3S')
    assert screen.full_clear is False
    assert screen.display[0].strip() == 'line3'


def test_scroll_down_moves_content_back():
    screen = _TrackedScreen(80, 24)
    stream = _TrackedByteStream(screen)
    stream.feed(b'top\r\nsecond\r\n')
    stream.feed(b'\x1b[2T')
    assert screen.display[2].strip() == 'top'
    assert not screen.display[0].strip()


def test_scrollback_survives_the_tmux_clear():
    screen = _TrackedHistoryScreen(80, 24, history=200)
    stream = _TrackedByteStream(screen)
    _filled(screen, stream)
    stream.feed(TMUX_CLEAR)
    assert len(screen.history.top) >= 12
    rows = [''.join(row[x].data for x in sorted(row)).strip()
            for row in screen.history.top]
    # Row 0 sits outside the scroll region tmux sets, so the trailing EL
    # erases it rather than pushing it into history — same as a real terminal.
    assert 'line1' in rows


def test_cursor_is_where_the_shell_left_it():
    screen = _TrackedScreen(80, 24)
    stream = _TrackedByteStream(screen)
    _filled(screen, stream)
    stream.feed(TMUX_CLEAR)
    stream.feed(b'pi@host:~$ ')
    assert screen.display[0].strip() == 'pi@host:~$'
