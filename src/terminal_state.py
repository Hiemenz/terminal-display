"""Shared state for the terminal emulator: the per-tab dataclass, hotkey byte
constants, overlay content/labels, and small pure helpers used by EinkTerminal
and its mixins (see eink_terminal_app.py)."""
from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from typing import Optional

import pyte

from session_logger import TabLogger


@dataclass
class _Tab:
    screen: 'pyte.Screen'
    stream: 'pyte.ByteStream'
    pty_master: int
    child_pid: int
    title: str = ''
    scroll_pages: int = 0
    tmux_session: str = ''
    # Split pane (left/right, separate PTY)
    pane2_screen: Optional['pyte.Screen'] = None
    pane2_stream: Optional['pyte.ByteStream'] = None
    pane2_master: int = -1
    pane2_pid: int = 0
    pane2_tmux: str = ''
    pane_focus: int = 0    # 0 = primary (left), 1 = secondary (right)
    split_dir: str = ''    # 'h' = left/right split; '' = no split
    activity: bool = False  # produced output while in the background, unseen
    logger: Optional['TabLogger'] = None  # optional rotating on-disk session log


_RENDER_DEBOUNCE  = 0.02   # seconds — lower now that hw writes are async
_STATUS_CACHE_TTL = 5.0    # seconds between CWD/branch re-reads
_STATS_UPDATE_SEC = 30     # seconds between split-view stats refreshes
_MIN_FONT = 8
_MAX_FONT = 32

_URL_RE = re.compile(r'https?://[^\s\x00-\x1f"\'<>]{6,}')


def _fmt_speed(bps: float) -> str:
    mbps = bps * 8 / 1_000_000
    if mbps < 0.1:
        return '<0.1 Mbps'
    elif mbps < 10.0:
        return f'{mbps:.1f} Mbps'
    return f'{mbps:.0f} Mbps'


def _get_uptime() -> str:
    """System uptime as 'Xd Yh Zm' (drops leading zero units). Cheap: reads /proc."""
    try:
        with open('/proc/uptime') as f:
            secs = int(float(f.read().split()[0]))
    except Exception:
        return ''
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f'{days}d {hours}h {mins}m'
    if hours:
        return f'{hours}h {mins}m'
    return f'{mins}m'


def _filter_pty_output(data: bytes, pty_master_fd) -> bytes:
    """
    Strip DCS escape sequences before feeding output to pyte.

    pyte doesn't fully handle Device Control String (DCS) sequences; instead
    it renders their content as visible garbage text.  Fish shell sends two
    DCS-based probes on startup:
      - XTGETTCAP  (\\x1bP+q<hex-caps>\\x1b\\) — capability queries
      - Primary/Secondary/Tertiary DA (\\x1b[c, \\x1b[>c, \\x1b[=c)

    We strip DCS sequences entirely and write the expected (negative) responses
    back to the PTY so the shell gets an answer immediately instead of printing
    a timeout warning.
    """
    out = bytearray()
    i, n = 0, len(data)

    while i < n:
        c = data[i]
        nxt = data[i + 1] if i + 1 < n else -1

        # ── DCS  ESC P ... ST  (ST = ESC \  or  C1 0x9C) ────────────────────
        if c == 0x1B and nxt == 0x50:
            end = -1
            content_end = -1
            for k in range(i + 2, n):
                if data[k] == 0x9C:                                 # C1 ST
                    content_end = k
                    end = k + 1
                    break
                if data[k] == 0x1B and k + 1 < n and data[k + 1] == 0x5C:  # ESC \
                    content_end = k
                    end = k + 2
                    break
            if end < 0:
                # Incomplete DCS at end of chunk — discard remainder
                break
            content = data[i + 2:content_end]
            if content.startswith(b'+q') and pty_master_fd is not None:
                # XTGETTCAP: respond "not supported" for every capability queried
                try:
                    os.write(pty_master_fd, b'\x1bP0+r\x1b\\')
                except OSError:
                    pass
            i = end  # skip the entire DCS sequence

        # ── CSI c  (Device Attributes request) ───────────────────────────────
        elif c == 0x1B and nxt == 0x5B:   # ESC [
            # Scan for CSI final byte (0x40–0x7E)
            j = i + 2
            while j < n and (0x20 <= data[j] <= 0x3F):
                j += 1
            if j < n and data[j] == 0x63:  # final byte 'c'
                params = data[i + 2:j]
                try:
                    if params in (b'', b'0') and pty_master_fd is not None:
                        os.write(pty_master_fd, b'\x1b[?62;c')      # Primary DA
                    elif params == b'>' and pty_master_fd is not None:
                        os.write(pty_master_fd, b'\x1b[>0;10;1c')   # Secondary DA
                    elif params == b'=' and pty_master_fd is not None:
                        os.write(pty_master_fd, b'\x1bP!|00000000\x1b\\')  # Tertiary DA
                except OSError:
                    pass
                # Don't pass DA requests to pyte — they're queries, not display content
                i = j + 1
            else:
                # Normal CSI — pass through to pyte unchanged
                out.append(c)
                i += 1

        else:
            out.append(c)
            i += 1

    return bytes(out)

# Function key escape sequences (xterm/VT220)
_F7   = b'\x1b[18~'   # dark/light mode toggle
_F8   = b'\x1b[19~'   # paste from file
_F9   = b'\x1b[20~'
_F10  = b'\x1b[21~'
_F1   = b'\x1bOP'     # new tab / SSH picker
_F2   = b'\x1bOQ'     # close tab
_F3   = b'\x1bOR'     # process kill overlay
_F4   = b'\x1bOS'     # service manager overlay
_F5   = b'\x1b[15~'  # power menu
_F6   = b'\x1b[17~'   # command palette
_F11  = b'\x1b[23~'

_POWER_ITEMS = ['  Shutdown', '  Reboot', '  Cancel']

# On-display config editor (opened from the F6 command palette). Each entry is
# (config_key, type, label, options). Only bool/select are editable here so the
# whole thing is keyboard-navigable on the e-ink panel without text entry.
# Anything more exotic stays in the web editor at /config.
_SETTINGS_SCHEMA = [
    ('terminal_show_qr',              'bool',   'QR Code',          None),
    ('terminal_cursor_style',         'select', 'Cursor',          ['block', 'underline']),
    ('terminal_dark_mode',            'bool',   'Dark Mode',        None),
    ('terminal_font_size',            'select', 'Font Size',        [8, 10, 12, 14, 16, 18, 20]),
    ('terminal_font_path',            'select', 'Font',             '__FONTS__'),
    ('terminal_split_view',           'bool',   'Split View',       None),
    ('display_sleep_minutes',         'select', 'Panel Sleep (min)', [0, 2, 5, 10, 15, 30]),
    ('screensaver_sleep_minutes',     'select', 'Screensaver (min)', [0, 5, 10, 15, 30, 60]),
    ('terminal_start_dir',            'select', 'Start Dir',        ['home', 'last', 'root']),
    ('terminal_prompt_custom',        'bool',   'Custom Prompt',    None),
    ('terminal_prompt_show_user',     'bool',   'Prompt: User',     None),
    ('terminal_prompt_show_host',     'bool',   'Prompt: Host',     None),
    ('terminal_prompt_show_cwd',      'bool',   'Prompt: Dir',      None),
    ('terminal_prompt_show_git',      'bool',   'Prompt: Git',      None),
]

# Display-level settings take effect instantly on Save (no service restart).
_SETTINGS_LIVE = {
    'terminal_cursor_style', 'terminal_show_qr', 'terminal_dark_mode',
    'terminal_font_size', 'terminal_font_path', 'terminal_split_view',
    'display_sleep_minutes', 'screensaver_sleep_minutes',
}
# Shell-level settings only affect newly-spawned shells, so saving them
# restarts the service to respawn. (Everything not in _SETTINGS_LIVE.)
_SETTINGS_SHELL = {
    'terminal_start_dir', 'terminal_prompt_custom', 'terminal_prompt_show_user',
    'terminal_prompt_show_host', 'terminal_prompt_show_cwd',
    'terminal_prompt_show_git', 'terminal_prompt_symbol',
}
_SETTINGS_OPEN = '⚙ Settings (e-ink config)'
_SNIPPETS_OPEN = '✎ Snippets (saved commands)'
_BIGTEXT_OPEN  = '🔍 Big text (read mode)'
_BEAM_OPEN     = '📱 Beam screen to phone'
_HUD_TOGGLE    = '📊 Toggle refresh stats HUD'
_RENAME_TAB    = '✏ Rename tab'
# Palette actions that open an overlay / run in-app instead of typing a command.
_PALETTE_ACTIONS = (_SETTINGS_OPEN, _SNIPPETS_OPEN, _BIGTEXT_OPEN, _BEAM_OPEN, _HUD_TOGGLE, _RENAME_TAB)
_F12  = b'\x1b[24~'
_CTRL_LEFT  = b'\x1b[1;5D'   # cycle tabs
_CTRL_RIGHT = b'\x1b[1;5C'
_PGUP = b'\x1b[5~'
_PGDN = b'\x1b[6~'
_CTRL_F            = b'\x06'   # scrollback search
_CTRL_T            = b'\x14'   # new tab
_CTRL_BACKSLASH    = b'\x1c'   # toggle left/right split pane
_CTRL_BRACKETRIGHT = b'\x1d'   # swap split pane focus
_CTRL_SLASH        = b'\x1f'   # help overlay (lists every hotkey, Enter runs it)
_CTRL_SPACE        = b'\x00'   # copy mode: select on-screen text, yank to clipboard/beam

# Alt+1..Alt+9 → jump straight to tab N. evdev sends Meta as ESC+char (same
# convention every terminal emulator uses), so this needs no evdev changes.
_ALT_DIGITS = {('\x1b' + d).encode(): int(d) for d in '123456789'}

# Help overlay (Ctrl+/): every hotkey with a one-line label. Enter runs the
# selected item; see _run_help_action for the label → method mapping. Ordered
# with tab/split management first since that's what people ask about most.
_HELP_ITEMS = [
    ('New Tab',             'Ctrl+T'),
    ('Close Tab',           'F2'),
    ('Next Tab',            'Ctrl+Right'),
    ('Prev Tab',            'Ctrl+Left'),
    ('Jump to Tab N',       'Alt+1..9'),
    ('Toggle Split Pane',   'Ctrl+\\'),
    ('Swap Split Focus',    'Ctrl+]'),
    ('Rename Tab',          'F6 > Rename'),
    ('SSH Picker',          'F1'),
    ('Command Palette',     'F6'),
    ('Kill Process',        'F3'),
    ('Service Manager',     'F4'),
    ('Power Menu',          'F5'),
    ('Dark Mode',           'F7'),
    ('Clipboard',           'F8'),
    ('Copy Mode',           'Ctrl+Space'),
    ('Font Smaller',        'F9'),
    ('Font Larger',         'F12'),
    ('Full Refresh',        'F10'),
    ('Switch to Dashboard', 'F11'),
    ('Scrollback Search',   'Ctrl+F'),
    ('Scroll Up',           'PgUp'),
    ('Scroll Down',         'PgDn'),
]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_local_ip() -> str:
    """Return the Pi's primary LAN IP address, or '' on failure."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except Exception:
        return ''


def _read_wifi_signal() -> int | None:
    try:
        with open('/proc/net/wireless') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[0].rstrip(':').startswith('w'):
                    return int(float(parts[3].rstrip('.')))
    except Exception:
        pass
    return None

