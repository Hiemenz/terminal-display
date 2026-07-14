"""Tests for the new in-app features: tabs, big text, snippets, fonts, beam, HUD."""
import pyte
from PIL import Image


class _StubTab:
    def __init__(self, title='', child_pid=0, activity=False):
        self.title = title
        self.child_pid = child_pid
        self.activity = activity


# ── Per-tab cwd indicator ─────────────────────────────────────────────────────

def test_tab_indicator_hidden_for_single_tab(make_app):
    app = make_app()
    app._tabs = [_StubTab('only')]
    app._active_tab = 0
    assert app._tab_indicator() == ''


def test_tab_indicator_shows_active_name(make_app):
    app = make_app()
    app._tabs = [_StubTab('one'), _StubTab('proj')]
    app._active_tab = 1
    assert app._tab_indicator() == '[2/2 proj]'


# ── Beam: screen text extraction ──────────────────────────────────────────────

def test_screen_text_trims_blanks(make_app):
    app = make_app()
    screen = pyte.Screen(20, 4)
    pyte.Stream(screen).feed('hello\r\nworld')
    app._screen = screen
    assert app._screen_text() == 'hello\nworld'


# ── Snippets ──────────────────────────────────────────────────────────────────

def test_load_snippets(make_app, tmp_path, monkeypatch):
    import palette_help_mixin as m
    monkeypatch.setattr(m, '_REPO_ROOT', str(tmp_path))
    cfg = tmp_path / 'config'
    cfg.mkdir()
    (cfg / 'saved_commands.txt').write_text('# header\nls -la\n\ngit status\nls -la\n')
    app = make_app()
    assert app._load_snippets() == ['ls -la', 'git status']   # deduped, no comments


# ── Font picker ───────────────────────────────────────────────────────────────

def test_available_fonts_starts_with_auto(make_app):
    fonts = make_app()._available_fonts()
    assert isinstance(fonts, list) and fonts[0] == ''


def test_settings_options_resolves_fonts(make_app):
    app = make_app()
    assert app._settings_options('__FONTS__') == app._available_fonts()
    assert app._settings_options(['a', 'b']) == ['a', 'b']


def test_font_display_value(make_app):
    app = make_app()
    assert app._settings_display_value('terminal_font_path', '') == 'auto'
    assert app._settings_display_value(
        'terminal_font_path', '/x/JetBrainsMono-Medium.ttf') == 'JetBrainsMono-Medium'


# ── Big text (read mode) ──────────────────────────────────────────────────────

def test_big_text_enter_and_exit_restore_font(make_app):
    app = make_app(terminal_font_size=14)
    app._font_size = 14
    app._enter_big_text()
    assert app._big_text_active is True
    assert app._font_size >= 24
    app._exit_big_text()
    assert app._big_text_active is False
    assert app._font_size == 14


# ── Refresh HUD ───────────────────────────────────────────────────────────────

class _FakeDriver:
    def stats(self):
        return {'partial': 5, 'region': 2, 'full': 1, 'bytes': 123,
                'du_frames': 20, 'last_flash_mono': 0.0, 'last_flash_age': 12.0}


def test_refresh_hud_draws_without_error(make_app):
    app = make_app()
    app._driver = _FakeDriver()
    app._dark_mode = False
    app._font_size = 14
    img = Image.new('L', (800, 480), 255)
    app._draw_refresh_hud(img)   # must not raise
