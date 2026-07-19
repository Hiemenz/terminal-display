"""Tests for llm_chat.py's /menu picker: the shared command list and the
raw-mode key decoder that distinguishes arrow keys from a bare Esc press."""
import os

from llm_chat import _command_rows, _read_menu_key


def test_command_rows_includes_every_slash_command():
    labels = [label for label, _ in _command_rows()]
    assert labels == ['/help', '/reset', '/notes', '/terminal', '/exit']


def _pipe_with(data: bytes):
    r, w = os.pipe()
    os.write(w, data)
    return r, w


def test_read_menu_key_enter():
    r, w = _pipe_with(b'\r')
    try:
        assert _read_menu_key(r) == 'ENTER'
    finally:
        os.close(r); os.close(w)


def test_read_menu_key_up_arrow():
    r, w = _pipe_with(b'\x1b[A')
    try:
        assert _read_menu_key(r) == 'UP'
    finally:
        os.close(r); os.close(w)


def test_read_menu_key_down_arrow():
    r, w = _pipe_with(b'\x1b[B')
    try:
        assert _read_menu_key(r) == 'DOWN'
    finally:
        os.close(r); os.close(w)


def test_read_menu_key_bare_escape_is_not_mistaken_for_arrow():
    r, w = _pipe_with(b'\x1b')
    try:
        assert _read_menu_key(r) == 'ESC'
    finally:
        os.close(r); os.close(w)


def test_read_menu_key_ctrl_c_cancels():
    r, w = _pipe_with(b'\x03')
    try:
        assert _read_menu_key(r) == 'ESC'
    finally:
        os.close(r); os.close(w)


def test_read_menu_key_unrecognized_escape_sequence():
    r, w = _pipe_with(b'\x1b[Z')
    try:
        assert _read_menu_key(r) == ''
    finally:
        os.close(r); os.close(w)
