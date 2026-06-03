"""
Raw evdev keyboard reader — reads directly from /dev/input/eventX.

Bypasses X11/Wayland so input works even when a desktop is running.
The device is grabbed exclusively so keystrokes don't also go to the desktop.
"""
import logging
import os
import select

logger = logging.getLogger(__name__)

try:
    import evdev
    from evdev import ecodes
    _EVDEV_OK = True
except ImportError:
    _EVDEV_OK = False

# US QWERTY: keycode → (unshifted_byte, shifted_byte)
_KEYMAP: dict[int, tuple[bytes, bytes]] = {}

def _build_keymap():
    if not _EVDEV_OK:
        return
    pairs = [
        (ecodes.KEY_GRAVE,      b'`',  b'~'),
        (ecodes.KEY_1,          b'1',  b'!'),
        (ecodes.KEY_2,          b'2',  b'@'),
        (ecodes.KEY_3,          b'3',  b'#'),
        (ecodes.KEY_4,          b'4',  b'$'),
        (ecodes.KEY_5,          b'5',  b'%'),
        (ecodes.KEY_6,          b'6',  b'^'),
        (ecodes.KEY_7,          b'7',  b'&'),
        (ecodes.KEY_8,          b'8',  b'*'),
        (ecodes.KEY_9,          b'9',  b'('),
        (ecodes.KEY_0,          b'0',  b')'),
        (ecodes.KEY_MINUS,      b'-',  b'_'),
        (ecodes.KEY_EQUAL,      b'=',  b'+'),
        (ecodes.KEY_Q,          b'q',  b'Q'),
        (ecodes.KEY_W,          b'w',  b'W'),
        (ecodes.KEY_E,          b'e',  b'E'),
        (ecodes.KEY_R,          b'r',  b'R'),
        (ecodes.KEY_T,          b't',  b'T'),
        (ecodes.KEY_Y,          b'y',  b'Y'),
        (ecodes.KEY_U,          b'u',  b'U'),
        (ecodes.KEY_I,          b'i',  b'I'),
        (ecodes.KEY_O,          b'o',  b'O'),
        (ecodes.KEY_P,          b'p',  b'P'),
        (ecodes.KEY_LEFTBRACE,  b'[',  b'{'),
        (ecodes.KEY_RIGHTBRACE, b']',  b'}'),
        (ecodes.KEY_BACKSLASH,  b'\\', b'|'),
        (ecodes.KEY_A,          b'a',  b'A'),
        (ecodes.KEY_S,          b's',  b'S'),
        (ecodes.KEY_D,          b'd',  b'D'),
        (ecodes.KEY_F,          b'f',  b'F'),
        (ecodes.KEY_G,          b'g',  b'G'),
        (ecodes.KEY_H,          b'h',  b'H'),
        (ecodes.KEY_J,          b'j',  b'J'),
        (ecodes.KEY_K,          b'k',  b'K'),
        (ecodes.KEY_L,          b'l',  b'L'),
        (ecodes.KEY_SEMICOLON,  b';',  b':'),
        (ecodes.KEY_APOSTROPHE, b"'",  b'"'),
        (ecodes.KEY_Z,          b'z',  b'Z'),
        (ecodes.KEY_X,          b'x',  b'X'),
        (ecodes.KEY_C,          b'c',  b'C'),
        (ecodes.KEY_V,          b'v',  b'V'),
        (ecodes.KEY_B,          b'b',  b'B'),
        (ecodes.KEY_N,          b'n',  b'N'),
        (ecodes.KEY_M,          b'm',  b'M'),
        (ecodes.KEY_COMMA,      b',',  b'<'),
        (ecodes.KEY_DOT,        b'.',  b'>'),
        (ecodes.KEY_SLASH,      b'/',  b'?'),
        (ecodes.KEY_SPACE,      b' ',  b' '),
        # Numpad
        (ecodes.KEY_KP0,        b'0',  b'0'),
        (ecodes.KEY_KP1,        b'1',  b'1'),
        (ecodes.KEY_KP2,        b'2',  b'2'),
        (ecodes.KEY_KP3,        b'3',  b'3'),
        (ecodes.KEY_KP4,        b'4',  b'4'),
        (ecodes.KEY_KP5,        b'5',  b'5'),
        (ecodes.KEY_KP6,        b'6',  b'6'),
        (ecodes.KEY_KP7,        b'7',  b'7'),
        (ecodes.KEY_KP8,        b'8',  b'8'),
        (ecodes.KEY_KP9,        b'9',  b'9'),
        (ecodes.KEY_KPDOT,      b'.',  b'.'),
        (ecodes.KEY_KPPLUS,     b'+',  b'+'),
        (ecodes.KEY_KPMINUS,    b'-',  b'-'),
        (ecodes.KEY_KPASTERISK, b'*',  b'*'),
        (ecodes.KEY_KPSLASH,    b'/',  b'/'),
    ]
    for code, unshift, shift in pairs:
        _KEYMAP[code] = (unshift, shift)

_build_keymap()

# Special (non-printable) keys → escape sequences
_SPECIAL: dict = {}

def _build_special():
    if not _EVDEV_OK:
        return
    _SPECIAL.update({
        ecodes.KEY_ENTER:     b'\r',
        ecodes.KEY_KPENTER:   b'\r',
        ecodes.KEY_BACKSPACE: b'\x7f',
        ecodes.KEY_TAB:       b'\t',
        ecodes.KEY_ESC:       b'\x1b',
        ecodes.KEY_UP:        b'\x1b[A',
        ecodes.KEY_DOWN:      b'\x1b[B',
        ecodes.KEY_RIGHT:     b'\x1b[C',
        ecodes.KEY_LEFT:      b'\x1b[D',
        ecodes.KEY_HOME:      b'\x1b[H',
        ecodes.KEY_END:       b'\x1b[F',
        ecodes.KEY_INSERT:    b'\x1b[2~',
        ecodes.KEY_DELETE:    b'\x1b[3~',
        ecodes.KEY_PAGEUP:    b'\x1b[5~',
        ecodes.KEY_PAGEDOWN:  b'\x1b[6~',
        ecodes.KEY_F1:        b'\x1bOP',
        ecodes.KEY_F2:        b'\x1bOQ',
        ecodes.KEY_F3:        b'\x1bOR',
        ecodes.KEY_F4:        b'\x1bOS',
        ecodes.KEY_F5:        b'\x1b[15~',
        ecodes.KEY_F6:        b'\x1b[17~',
        ecodes.KEY_F7:        b'\x1b[18~',
        ecodes.KEY_F8:        b'\x1b[19~',
        ecodes.KEY_F9:        b'\x1b[20~',
        ecodes.KEY_F10:       b'\x1b[21~',
        ecodes.KEY_F11:       b'\x1b[23~',
        ecodes.KEY_F12:       b'\x1b[24~',
    })
    # Ctrl+arrow
    _SPECIAL['ctrl_up']    = b'\x1b[1;5A'
    _SPECIAL['ctrl_down']  = b'\x1b[1;5B'
    _SPECIAL['ctrl_right'] = b'\x1b[1;5C'
    _SPECIAL['ctrl_left']  = b'\x1b[1;5D'

_build_special()

_MODIFIER_KEYS = set()

def _build_modifier_set():
    if not _EVDEV_OK:
        return
    _MODIFIER_KEYS.update({
        ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT,
        ecodes.KEY_LEFTCTRL,  ecodes.KEY_RIGHTCTRL,
        ecodes.KEY_LEFTALT,   ecodes.KEY_RIGHTALT,
        ecodes.KEY_LEFTMETA,  ecodes.KEY_RIGHTMETA,
        ecodes.KEY_CAPSLOCK,
    })

_build_modifier_set()


def find_keyboard(prefer_path: str = '') -> 'evdev.InputDevice | None':
    """Return the first keyboard-like input device, or None."""
    if not _EVDEV_OK:
        return None

    if prefer_path:
        try:
            return evdev.InputDevice(prefer_path)
        except Exception as e:
            logger.warning('evdev: could not open %s: %s', prefer_path, e)

    for path in sorted(evdev.list_devices()):
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities()
            keys = caps.get(ecodes.EV_KEY, [])
            if ecodes.KEY_A in keys and ecodes.KEY_ENTER in keys:
                logger.info('evdev: auto-selected keyboard %s (%s)', dev.name, path)
                return dev
        except Exception:
            continue
    return None


class EvdevKeyboard:
    """
    Reads raw keyboard events from an evdev device and converts them
    to terminal byte sequences.

    Usage:
        kb = EvdevKeyboard(device)
        kb.grab()          # exclusive — desktop won't also receive keys
        ...
        data = kb.read()   # returns bytes or b'' on no input
        kb.ungrab()
    """

    def __init__(self, device: 'evdev.InputDevice'):
        self._dev = device
        self._shift = False
        self._ctrl  = False
        self._alt   = False
        self._caps   = False
        self._buf: list[bytes] = []

    def grab(self):
        try:
            self._dev.grab()
            logger.info('evdev: grabbed %s', self._dev.path)
        except Exception as e:
            logger.warning('evdev: grab failed: %s', e)

    def ungrab(self):
        try:
            self._dev.ungrab()
        except Exception:
            pass

    def fileno(self) -> int:
        return self._dev.fileno()

    def read(self) -> bytes:
        """Read all pending events and return resulting terminal bytes."""
        out = bytearray()
        try:
            for event in self._dev.read():
                chunk = self._handle(event)
                if chunk:
                    out.extend(chunk)
        except BlockingIOError:
            pass
        except Exception as e:
            logger.debug('evdev read error: %s', e)
        return bytes(out)

    def _handle(self, event) -> bytes:
        if event.type != ecodes.EV_KEY:
            return b''

        key = event.code
        val = event.value  # 0=up 1=down 2=repeat

        # Update modifier state
        if key in (ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT):
            self._shift = val != 0
            return b''
        if key in (ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL):
            self._ctrl = val != 0
            return b''
        if key in (ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT):
            self._alt = val != 0
            return b''
        if key == ecodes.KEY_CAPSLOCK and val == 1:
            self._caps = not self._caps
            return b''
        if key in _MODIFIER_KEYS:
            return b''

        if val == 0:  # key-up, ignore
            return b''

        return self._translate(key)

    def _translate(self, key: int) -> bytes:
        # Ctrl+arrow
        if self._ctrl:
            if key == ecodes.KEY_UP:    return _SPECIAL['ctrl_up']
            if key == ecodes.KEY_DOWN:  return _SPECIAL['ctrl_down']
            if key == ecodes.KEY_RIGHT: return _SPECIAL['ctrl_right']
            if key == ecodes.KEY_LEFT:  return _SPECIAL['ctrl_left']

        # Non-printable specials
        if key in _SPECIAL:
            return _SPECIAL[key]

        # Printable
        if key in _KEYMAP:
            unshift, shift = _KEYMAP[key]
            effective_shift = self._shift ^ self._caps if unshift.isalpha() else self._shift
            char = shift if effective_shift else unshift

            if self._ctrl and len(char) == 1:
                c = char[0]
                if ord('a') <= c <= ord('z'):
                    return bytes([c - ord('a') + 1])
                if ord('A') <= c <= ord('Z'):
                    return bytes([c - ord('A') + 1])
                if c == ord(' '):
                    return b'\x00'
            if self._alt:
                return b'\x1b' + char
            return char

        return b''
