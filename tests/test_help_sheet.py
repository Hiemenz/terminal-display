"""The full-screen command sheet (Ctrl+/, or `commands` at a prompt).

A scrolling list answers "run this for me"; the sheet answers "what can this
thing do?" — so the thing that matters is that every command is actually on
the panel, not just the first screenful.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from help_sheet import HEIGHT, WIDTH, _column_rows, _paginate, render_help_pages  # noqa: E402
from terminal_state import _HELP_FOOTER, _HELP_ITEMS, _HELP_SECTIONS  # noqa: E402


def test_every_command_fits_on_the_sheet():
    """Nothing may be silently dropped: the row count has to cover every item
    plus its section heading."""
    rows = _column_rows(_HELP_SECTIONS)
    items = [r for r in rows if r[0] == 'item']
    heads = [r for r in rows if r[0] == 'head']
    assert len(items) == len(_HELP_ITEMS)
    assert len(heads) == len(_HELP_SECTIONS)


def test_the_whole_reference_is_one_page():
    """It's a cheat sheet — needing to page through it defeats the point.
    If this fails, something has to be cut or the layout tightened."""
    pages = render_help_pages(_HELP_SECTIONS, _HELP_FOOTER)
    assert len(pages) == 1


def test_pages_are_panel_sized():
    for page in render_help_pages(_HELP_SECTIONS, _HELP_FOOTER):
        assert page.size == (WIDTH, HEIGHT)


def test_dark_mode_inverts_the_background():
    light = render_help_pages(_HELP_SECTIONS, _HELP_FOOTER, dark_mode=False)[0]
    dark = render_help_pages(_HELP_SECTIONS, _HELP_FOOTER, dark_mode=True)[0]
    assert light.getpixel((2, 2)) != dark.getpixel((2, 2))


def test_sections_and_items_reach_the_flat_picker_list():
    """The picker and the sheet must show the same commands — _HELP_ITEMS is
    derived from _HELP_SECTIONS so they cannot drift apart."""
    flat = [item for _title, items in _HELP_SECTIONS for item in items]
    assert flat == _HELP_ITEMS


def test_tab_lifecycle_is_documented():
    labels = [label for label, _keys in _HELP_ITEMS]
    for expected in ('New Tab', 'Close Tab', 'Next Tab', 'Prev Tab'):
        assert expected in labels
    # The question a key list can't answer.
    assert 'last tab' in _HELP_FOOTER
    assert 'Shell exited' in _HELP_FOOTER


def test_typed_commands_are_listed():
    keys = [keys for _label, keys in _HELP_ITEMS]
    for command in ('settings', 'clear-eink', 'notes', 'llmchat', 'terminal'):
        assert command in keys


def test_overflow_paginates_instead_of_truncating():
    big = [('Section %d' % i,
            [('Command %d-%d' % (i, j), 'F%d' % j) for j in range(9)])
           for i in range(12)]
    pages = render_help_pages(big, '')
    assert len(pages) > 1


def test_a_heading_never_ends_a_column_alone():
    rows = [('head', 'A'), ('item', 'k', 'v'), ('head', 'B'), ('item', 'k', 'v')]
    for page in _paginate(rows, 3):
        for column in page:
            assert not (column and column[-1][0] == 'head')


def test_the_sheet_carries_the_settings_qr():
    """The sheet lists what you press; the config you'd rather type lives in
    the web page, and a QR is the only way to hand a phone a LAN URL off an
    e-ink panel."""
    url = 'http://192.168.1.145:8080/config'
    with_qr = render_help_pages(_HELP_SECTIONS, _HELP_FOOTER, qr_url=url)[0]
    without = render_help_pages(_HELP_SECTIONS, _HELP_FOOTER)[0]
    assert list(with_qr.getdata()) != list(without.getdata())
    # And it must not have cost a command its place on the single page.
    assert len(render_help_pages(_HELP_SECTIONS, _HELP_FOOTER, qr_url=url)) == 1


def test_qr_block_never_overlaps_the_commands():
    """The QR eats the bottom of the last column, so the columns have to give
    it that room rather than draw through it."""
    url = 'http://192.168.1.145:8080/config'
    rows = _column_rows(_HELP_SECTIONS)
    plain = _paginate(rows, 27)
    assert sum(len(c) for c in plain[0]) == len(rows)
    pages = render_help_pages(_HELP_SECTIONS, _HELP_FOOTER, qr_url=url)
    assert len(pages) == 1


def test_copy_and_paste_are_findable_by_name():
    """Both were on the sheet under names that didn't say 'copy' or 'paste'."""
    labels = ' '.join(label for label, _keys in _HELP_ITEMS).lower()
    assert 'copy' in labels
    assert 'paste' in labels
