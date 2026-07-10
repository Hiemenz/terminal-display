"""Ctrl+punctuation from a directly-attached (evdev) keyboard.

Before this fix, EvdevKeyboard._translate only special-cased Ctrl+letter and
Ctrl+space: Ctrl+\\ and Ctrl+] (the split-pane hotkeys) and the new Ctrl+/
(help overlay) fell through and sent the plain character instead of the
control byte the app's _handle_hotkeys expects. This only affects the
keyboard wired directly into the Pi — SSH clients translate Ctrl+key locally
before the byte ever reaches the app, which is why the bug was easy to miss.
"""
from evdev import ecodes

from evdev_input import EvdevKeyboard


def _translator(ctrl=True, shift=False, alt=False):
    kb = EvdevKeyboard.__new__(EvdevKeyboard)
    kb._shift = shift
    kb._ctrl = ctrl
    kb._alt = alt
    kb._caps = False
    return kb


def test_ctrl_backslash_matches_toggle_split_pane_hotkey():
    kb = _translator()
    assert kb._translate(ecodes.KEY_BACKSLASH) == b'\x1c'


def test_ctrl_right_bracket_matches_swap_focus_hotkey():
    kb = _translator()
    assert kb._translate(ecodes.KEY_RIGHTBRACE) == b'\x1d'


def test_ctrl_slash_matches_help_overlay_hotkey():
    kb = _translator()
    assert kb._translate(ecodes.KEY_SLASH) == b'\x1f'


def test_ctrl_left_bracket():
    kb = _translator()
    assert kb._translate(ecodes.KEY_LEFTBRACE) == bytes([ord('[') & 0x1f])


def test_without_ctrl_backslash_is_unaffected():
    kb = _translator(ctrl=False)
    assert kb._translate(ecodes.KEY_BACKSLASH) == b'\\'


def test_without_ctrl_slash_is_unaffected():
    kb = _translator(ctrl=False)
    assert kb._translate(ecodes.KEY_SLASH) == b'/'


def test_ctrl_letter_still_works():
    kb = _translator()
    assert kb._translate(ecodes.KEY_T) == b'\x14'   # Ctrl+T: new tab
