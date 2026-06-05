"""Shared test fixtures.

The app modules live under src/, so make that importable. Importing
eink_terminal_app pulls in the Waveshare driver, which logs a harmless
'GPIO busy' warning off a Pi — the import itself is guarded and succeeds.
"""
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))


def _default_config():
    return {
        'terminal_use_tmux': False,
        'terminal_tmux_session': 'eink',
        'terminal_cursor_style': 'block',
        'terminal_prompt_custom': False,
        'terminal_prompt_show_user': True,
        'terminal_prompt_show_host': True,
        'terminal_prompt_show_cwd': True,
        'terminal_prompt_show_git': True,
        'terminal_prompt_symbol': '$',
        'terminal_start_dir': 'home',
        'terminal_show_qr': True,
        'terminal_dark_mode': False,
        'terminal_font_size': 14,
    }


@pytest.fixture
def make_app():
    """Build a bare EinkTerminal without running __init__ (no hardware), with
    just the attributes the prompt / start-dir / settings logic reads, mirroring
    how __init__ pulls them from config."""
    from eink_terminal_app import EinkTerminal

    def _make(**overrides):
        cfg = _default_config()
        cfg.update(overrides)
        app = EinkTerminal.__new__(EinkTerminal)
        app._config = cfg
        app._use_tmux = cfg['terminal_use_tmux']
        app._tmux_session = cfg['terminal_tmux_session']
        app._cursor_style = cfg['terminal_cursor_style']
        app._dark_mode = cfg['terminal_dark_mode']
        app._font_size = cfg['terminal_font_size']
        app._font_path = ''
        app._split_view = cfg.get('terminal_split_view', False)
        app._pty_master = None
        app._prompt_custom = cfg['terminal_prompt_custom']
        app._prompt_show_user = cfg['terminal_prompt_show_user']
        app._prompt_show_host = cfg['terminal_prompt_show_host']
        app._prompt_show_cwd = cfg['terminal_prompt_show_cwd']
        app._prompt_show_git = cfg['terminal_prompt_show_git']
        app._prompt_symbol = cfg['terminal_prompt_symbol']
        app._start_dir_pref = cfg['terminal_start_dir']
        app._child_pid = None
        app._settings_pending = {}
        app._settings_idx = 0
        app._running = True
        app._big_text_active = False
        app._big_text_prev_font = 0
        app._snippets_active = False
        app._show_refresh_hud = False
        app._beam_url = ''
        app._beam_until_mono = 0.0
        app._needs_periodic_flash = True
        app._img_cache = None
        # Overlay code calls _render after every change; make it a no-op.
        app._render = lambda *a, **k: None
        return app

    return _make
