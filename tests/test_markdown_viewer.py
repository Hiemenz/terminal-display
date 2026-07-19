"""Tests for markdown_viewer_mixin.py: opening the viewer, PgUp/PgDn paging,
and closing on any other key. See src/markdown_viewer_mixin.py."""
from terminal_state import _MARKDOWN_VIEW, _PALETTE_ACTIONS, _PGDN, _PGUP


class _FakeDriver:
    def __init__(self):
        self.pushed = []

    def full_refresh(self, img):
        self.pushed.append(img)


def _md_app(make_app):
    app = make_app()
    app._driver = _FakeDriver()
    app._markdown_active = False
    app._markdown_pages = []
    app._markdown_page_idx = 0
    return app


def test_markdown_view_in_palette_actions():
    assert _MARKDOWN_VIEW in _PALETTE_ACTIONS


def test_show_markdown_activates_viewer_and_pushes_first_page(make_app):
    app = _md_app(make_app)

    app._show_markdown('# Hello\n\nWorld', 'test.md')

    assert app._markdown_active is True
    assert app._markdown_page_idx == 0
    assert len(app._markdown_pages) >= 1
    assert len(app._driver.pushed) == 1


def test_open_markdown_notes_reads_notes_file(make_app, tmp_path):
    app = _md_app(make_app)
    notes_file = tmp_path / 'notes.txt'
    notes_file.write_text('# My Notes\n\nSome text.')
    app._config = dict(app._config)
    app._config['terminal_notes_file'] = str(notes_file)

    app._open_markdown_notes()

    assert app._markdown_active is True


def test_open_markdown_notes_missing_file_shows_placeholder(make_app, tmp_path):
    app = _md_app(make_app)
    app._config = dict(app._config)
    app._config['terminal_notes_file'] = str(tmp_path / 'nope.txt')

    app._open_markdown_notes()   # must not raise

    assert app._markdown_active is True


def test_handle_markdown_key_passthrough_when_inactive(make_app):
    app = _md_app(make_app)

    result = app._handle_markdown_key(b'hello')

    assert result == b'hello'


def test_handle_markdown_key_pgdn_advances_page(make_app):
    app = _md_app(make_app)
    app._markdown_active = True
    app._markdown_pages = ['page0', 'page1', 'page2']
    app._markdown_page_idx = 0

    result = app._handle_markdown_key(_PGDN)

    assert app._markdown_page_idx == 1
    assert app._driver.pushed == ['page1']
    assert result == b''


def test_handle_markdown_key_pgdn_stops_at_last_page(make_app):
    app = _md_app(make_app)
    app._markdown_active = True
    app._markdown_pages = ['page0', 'page1']
    app._markdown_page_idx = 1

    app._handle_markdown_key(_PGDN)

    assert app._markdown_page_idx == 1
    assert app._driver.pushed == []


def test_handle_markdown_key_pgup_goes_back(make_app):
    app = _md_app(make_app)
    app._markdown_active = True
    app._markdown_pages = ['page0', 'page1', 'page2']
    app._markdown_page_idx = 2

    app._handle_markdown_key(_PGUP)

    assert app._markdown_page_idx == 1
    assert app._driver.pushed == ['page1']


def test_handle_markdown_key_pgup_stops_at_first_page(make_app):
    app = _md_app(make_app)
    app._markdown_active = True
    app._markdown_pages = ['page0', 'page1']
    app._markdown_page_idx = 0

    app._handle_markdown_key(_PGUP)

    assert app._markdown_page_idx == 0
    assert app._driver.pushed == []


def test_handle_markdown_key_other_key_closes_viewer(make_app):
    app = _md_app(make_app)
    app._markdown_active = True
    app._markdown_pages = ['page0']
    app._markdown_page_idx = 0

    result = app._handle_markdown_key(b'q')

    assert app._markdown_active is False
    assert app._markdown_pages == []
    assert result == b''


def test_close_markdown_resets_state(make_app):
    app = _md_app(make_app)
    app._markdown_active = True
    app._markdown_pages = ['page0']
    app._markdown_page_idx = 3

    app._close_markdown()

    assert app._markdown_active is False
    assert app._markdown_pages == []
    assert app._markdown_page_idx == 0
