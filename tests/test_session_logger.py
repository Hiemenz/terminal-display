"""Tests for TabLogger (src/session_logger.py) and its wiring into tabs."""
import glob
import os
import time

import pyte

from eink_terminal_app import _Tab
from session_logger import TabLogger, _AnsiStripper, _safe_label

# ── _AnsiStripper ────────────────────────────────────────────────────────────

def test_strips_csi_color_codes():
    s = _AnsiStripper()
    out = s.feed(b'\x1b[31mred\x1b[0m plain')
    assert out == b'red plain'


def test_strips_osc_title_sequence():
    s = _AnsiStripper()
    out = s.feed(b'\x1b]0;my title\x07rest')
    assert out == b'rest'


def test_plain_text_passes_through_unchanged():
    s = _AnsiStripper()
    assert s.feed(b'hello world\r\n') == b'hello world\r\n'


def test_escape_split_across_chunks_is_not_leaked():
    s = _AnsiStripper()
    first = s.feed(b'abc\x1b[3')
    second = s.feed(b'1mred\x1b[0mdef')
    assert first == b'abc'
    assert second == b'reddef'


def test_carry_flushed_if_never_completed():
    # An escape that never terminates shouldn't be held forever; a
    # subsequent unrelated feed should still make forward progress on
    # the plain text around it.
    s = _AnsiStripper()
    s.feed(b'\x1b[')
    out = s.feed(b'not a real terminator but eventually plain text')
    assert b'plain text' in out


# ── _safe_label ───────────────────────────────────────────────────────────────

def test_safe_label_sanitizes_unsafe_chars():
    assert _safe_label('../../etc/passwd') == 'etc_passwd'


def test_safe_label_empty_falls_back():
    assert _safe_label('') == 'tab'


def test_safe_label_truncates_long_input():
    assert len(_safe_label('x' * 200)) <= 40


# ── TabLogger: writing + rotation + pruning ──────────────────────────────────

def test_write_creates_log_file_with_plain_text(tmp_path):
    log = TabLogger(str(tmp_path), 'tab1')
    log.write(b'\x1b[32mhello\x1b[0m\n')
    log.close()

    files = glob.glob(os.path.join(str(tmp_path), 'tab1_*.log'))
    assert len(files) == 1
    with open(files[0], 'rb') as f:
        assert f.read() == b'hello\n'


def test_rotation_starts_new_file_past_max_bytes(tmp_path):
    log = TabLogger(str(tmp_path), 'tab1', max_bytes=10, max_files=10)
    log.write(b'0123456789')   # hits the threshold, rotates
    log.write(b'more')
    log.close()

    files = sorted(glob.glob(os.path.join(str(tmp_path), 'tab1_*.log')))
    assert len(files) == 2
    with open(files[0], 'rb') as f:
        assert f.read() == b'0123456789'
    with open(files[1], 'rb') as f:
        assert f.read() == b'more'


def test_prune_keeps_only_max_files(tmp_path):
    # Pre-populate more *.log files than max_files allows.
    for i in range(5):
        p = tmp_path / f'old{i}.log'
        p.write_bytes(b'x')
        # Ensure distinct, increasing mtimes so pruning order is deterministic.
        os.utime(p, (time.time() - (10 - i), time.time() - (10 - i)))

    log = TabLogger(str(tmp_path), 'new', max_bytes=1_000_000, max_files=3)
    log.write(b'data')
    log.close()

    remaining = sorted(os.listdir(str(tmp_path)))
    assert len(remaining) == 3
    # The newest file (just written) must survive pruning.
    assert any(f.startswith('new_') for f in remaining)


def test_write_empty_chunk_is_noop(tmp_path):
    log = TabLogger(str(tmp_path), 'tab1')
    log.write(b'')
    log.close()
    files = glob.glob(os.path.join(str(tmp_path), 'tab1_*.log'))
    assert len(files) == 1
    with open(files[0], 'rb') as f:
        assert f.read() == b''


# ── Wiring: _Tab carries an optional logger ──────────────────────────────────

def test_tab_logger_field_defaults_to_none():
    screen = pyte.Screen(80, 24)
    stream = pyte.ByteStream(screen)
    tab = _Tab(screen=screen, stream=stream, pty_master=None, child_pid=None)
    assert tab.logger is None


def test_make_tab_logger_returns_none_when_disabled(make_app):
    app = make_app()
    app._log_enabled = False
    assert app._make_tab_logger() is None


def test_make_tab_logger_creates_logger_when_enabled(make_app, tmp_path):
    app = make_app()
    app._log_enabled = True
    app._log_dir = str(tmp_path)
    app._log_max_bytes = 1_000_000
    app._log_max_files = 10
    app._tab_log_seq = 0

    log = app._make_tab_logger()
    try:
        assert isinstance(log, TabLogger)
        assert log._label == 'tab1'
    finally:
        log.close()


def test_make_tab_logger_increments_sequence(make_app, tmp_path):
    app = make_app()
    app._log_enabled = True
    app._log_dir = str(tmp_path)
    app._log_max_bytes = 1_000_000
    app._log_max_files = 10
    app._tab_log_seq = 0

    log1 = app._make_tab_logger()
    log2 = app._make_tab_logger()
    try:
        assert log1._label == 'tab1'
        assert log2._label == 'tab2'
    finally:
        log1.close(); log2.close()
