"""Shift+Enter sends a bare LF (distinct from plain Enter's CR) so raw-mode
readers like llm_chat.py's composer can tell 'insert a newline' apart from
'submit'. See _read_composer in llm_chat.py."""
from evdev import ecodes

from evdev_input import EvdevKeyboard


def _translator(shift=False):
    kb = EvdevKeyboard.__new__(EvdevKeyboard)
    kb._shift = shift
    kb._ctrl = False
    kb._alt = False
    kb._caps = False
    return kb


def test_shift_enter_sends_bare_lf():
    kb = _translator(shift=True)
    assert kb._translate(ecodes.KEY_ENTER) == b'\n'


def test_plain_enter_still_sends_cr():
    kb = _translator(shift=False)
    assert kb._translate(ecodes.KEY_ENTER) == b'\r'


def test_shift_kpenter_sends_bare_lf():
    kb = _translator(shift=True)
    assert kb._translate(ecodes.KEY_KPENTER) == b'\n'


def test_plain_kpenter_still_sends_cr():
    kb = _translator(shift=False)
    assert kb._translate(ecodes.KEY_KPENTER) == b'\r'
