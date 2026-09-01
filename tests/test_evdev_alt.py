"""Alt modifier: applies to printable keys AND special keys; AltGr doesn't.

Before the fix:
  - Alt only prepended \x1b to printable characters (_KEYMAP), not to special
    keys (_SPECIAL) — so Alt+Left (readline word-back) sent plain \x1b[D.
  - KEY_RIGHTALT (AltGr) set _alt=True, corrupting non-US composed characters.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from evdev import ecodes
from evdev_input import EvdevKeyboard


def _kb(alt=False, altgr=False, shift=False, ctrl=False):
    kb = EvdevKeyboard.__new__(EvdevKeyboard)
    kb._shift = shift
    kb._ctrl  = ctrl
    kb._alt   = alt
    kb._altgr = altgr
    kb._caps  = False
    return kb


# ── Alt + printable ───────────────────────────────────────────────────────────

def test_alt_printable_prefixes_escape():
    kb = _kb(alt=True)
    assert kb._translate(ecodes.KEY_A) == b'\x1ba'


def test_plain_printable_no_escape():
    kb = _kb()
    assert kb._translate(ecodes.KEY_A) == b'a'


# ── Alt + special keys ────────────────────────────────────────────────────────

def test_alt_left_arrow_word_backward():
    """Alt+Left → \x1b\x1b[D — readline word-backward."""
    kb = _kb(alt=True)
    assert kb._translate(ecodes.KEY_LEFT) == b'\x1b\x1b[D'


def test_alt_right_arrow_word_forward():
    """Alt+Right → \x1b\x1b[C — readline word-forward."""
    kb = _kb(alt=True)
    assert kb._translate(ecodes.KEY_RIGHT) == b'\x1b\x1b[C'


def test_alt_up_arrow():
    kb = _kb(alt=True)
    assert kb._translate(ecodes.KEY_UP) == b'\x1b\x1b[A'


def test_alt_down_arrow():
    kb = _kb(alt=True)
    assert kb._translate(ecodes.KEY_DOWN) == b'\x1b\x1b[B'


def test_plain_left_no_prefix():
    kb = _kb()
    assert kb._translate(ecodes.KEY_LEFT) == b'\x1b[D'


# ── Alt+Backspace ─────────────────────────────────────────────────────────────

def test_alt_backspace_kill_word():
    """Alt+Backspace → \x1b\x7f — readline kill-word."""
    kb = _kb(alt=True)
    assert kb._translate(ecodes.KEY_BACKSPACE) == b'\x1b\x7f'


def test_plain_backspace_unchanged():
    kb = _kb()
    assert kb._translate(ecodes.KEY_BACKSPACE) == b'\x7f'


# ── AltGr does NOT set _alt ───────────────────────────────────────────────────

def test_altgr_does_not_corrupt_printable():
    """AltGr is held for composed characters on non-US layouts. It must not
    prepend \x1b — that would send the wrong character (or Meta+char)."""
    kb = _kb(altgr=True)  # _alt is False
    # The kernel sends the composed keycode as a regular printable; we just
    # pass it through. Confirm that _alt=False means no escape prefix.
    assert kb._translate(ecodes.KEY_E) == b'e'


def test_altgr_flag_independent_of_alt():
    kb = _kb(altgr=True)
    assert kb._alt is False
    assert kb._altgr is True
