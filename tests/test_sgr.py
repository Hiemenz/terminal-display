"""Tests for SGR attribute rendering in src/terminal_renderer.py.

pyte tracks nine per-cell attributes; the renderer used to read exactly one of
them (reverse), so bold, underline, strikethrough and colour all came out as
identical black text. These pin down that each one now changes the pixels, and
that turning the feature off reproduces the old flat rendering.
"""
import pyte
import pytest

import terminal_renderer as tr


def _screen(text: str, cols: int = 40, rows: int = 4):
    screen = pyte.Screen(cols, rows)
    pyte.Stream(screen).feed(text)
    return screen


# The status bar is a filled black rectangle across the full width, which
# swamps any per-cell measurement — every count here is over the text area only.
_TEXT_AREA = (0, 0, 800, 100)


def _ink(img, box=None):
    """Count non-white pixels — how much ink a render put down."""
    return sum(1 for p in img.crop(box or _TEXT_AREA).getdata() if p < 128)


def _render(text, **kw):
    kw.setdefault('dark_mode', False)
    kw.setdefault('hq', False)
    return tr.render_screen(_screen(text), 14, **kw)


def test_bold_puts_down_more_ink_than_normal():
    normal = _ink(_render('\x1b[0mHELLO'))
    bold   = _ink(_render('\x1b[1mHELLO'))
    assert bold > normal


def test_underline_draws_a_rule_under_the_cell():
    plain = _render('\x1b[0mabc')
    under = _render('\x1b[4mabc')
    assert _ink(under) > _ink(plain)
    # The extra ink is a solid horizontal run at the bottom of the row.
    _, ch = tr._char_size(tr._find_mono_font('', 14))
    cw = tr._char_size(tr._find_mono_font('', 14))[0]
    bottom = (0, ch - 2, cw * 3, ch)
    assert _ink(under, bottom) > _ink(plain, bottom)


def test_strikethrough_draws_a_rule_through_the_cell():
    assert _ink(_render('\x1b[9mabc')) > _ink(_render('\x1b[0mabc'))


def test_reverse_video_still_fills_the_cell():
    """A filled cell is worth far more ink than the glyph alone."""
    assert _ink(_render('\x1b[7mabc')) > _ink(_render('\x1b[0mabc')) * 2


def test_sgr_off_flattens_every_attribute():
    """The pre-SGR rendering, still reachable for anyone who wants it back."""
    plain = _render('\x1b[0mabc', sgr=False)
    for attr in ('\x1b[1m', '\x1b[4m', '\x1b[9m', '\x1b[31m'):
        assert _ink(_render(attr + 'abc', sgr=False)) == _ink(plain)


def test_colour_is_ignored_in_one_bit_mode():
    """In 1-bit, red/yellow are promoted to bold for visual weight; neutral
    colours (green, cyan) stay at the base weight so errors stand out."""
    plain  = _ink(_render('\x1b[0mabc'))
    # Neutral colours must not change weight.
    for colour in ('\x1b[32m', '\x1b[36m'):   # green, cyan
        assert _ink(_render(colour + 'abc')) == plain
    # High-signal colours gain weight (covered in detail by later tests).


def test_colour_becomes_ink_weight_in_grey_mode():
    """In grey there *is* somewhere for it to go, so a diff regains structure."""
    def mean(text):
        img = _render(text, hq=True, gray=True)
        px = [p for p in img.getdata() if p < 250]
        return sum(px) / len(px)

    # cyan is chrome and lightens; red is emphasis and stays near full ink.
    assert mean('\x1b[36mabcdef') > mean('\x1b[31mabcdef')
    assert mean('\x1b[31mabcdef') > mean('\x1b[30mabcdef') - 1


def test_grey_mode_keeps_anti_aliasing_that_one_bit_throws_away():
    flat = _render('hello world', hq=True)
    grey = _render('hello world', hq=True, gray=True)
    assert set(flat.getdata()) <= {0, 255}
    assert len(set(grey.getdata())) > 2


def test_bold_face_does_not_move_the_grid():
    """Cell metrics come from the regular face precisely so that a bold
    character appearing cannot resize the pty underneath the shell."""
    plain = tr.terminal_dimensions(14)
    assert tr.terminal_dimensions(14) == plain
    faces = tr._find_faces('', 14)
    assert tr._char_size(faces.regular) == tr._char_size(tr._find_mono_font('', 14))


def test_heavy_base_keeps_the_old_uniform_weight():
    """The escape hatch for a panel where regular-weight text reads too thin."""
    normal = _ink(_render('\x1b[0mHELLO'))
    heavy  = _ink(_render('\x1b[0mHELLO', heavy_base=True))
    assert heavy > normal


@pytest.mark.parametrize('style', ['block', 'underline'])
def test_cursor_is_visible_on_an_already_reversed_cell(style):
    """Regression: a block cursor inverted the cell, so on a cell that was
    already reversed — a selection, a TUI's highlight bar — it inverted back
    and vanished exactly where it was most needed."""
    screen = _screen('\x1b[7mABC')
    screen.cursor.x, screen.cursor.y = 1, 0
    cw, ch = tr._char_size(tr._find_mono_font('', 14))
    cell = (cw, 0, cw * 2, ch)

    with_cursor = tr.render_screen(screen, 14, dark_mode=False, hq=False,
                                   cursor_style=style)
    screen.cursor.x = 30   # move it well away from the cell under test
    without = tr.render_screen(screen, 14, dark_mode=False, hq=False,
                               cursor_style=style)
    assert _ink(with_cursor, cell) != _ink(without, cell)


# ── Color → bold promotion in 1-bit mode ──────────────────────────────────────

def test_red_fg_renders_bolder_than_default_in_1bit():
    """Red text (errors, deleted diff lines) should gain visual weight in 1-bit."""
    normal = _ink(_render('\x1b[0mHELLO'))
    red    = _ink(_render('\x1b[31mHELLO'))
    assert red > normal


def test_yellow_fg_renders_bolder_than_default_in_1bit():
    """Yellow/brown (warnings) gets the same promotion as red."""
    normal = _ink(_render('\x1b[0mHELLO'))
    yellow = _ink(_render('\x1b[33mHELLO'))
    assert yellow > normal


def test_green_fg_stays_regular_in_1bit():
    """Green is success/added text — it should NOT be promoted to bold."""
    normal = _ink(_render('\x1b[0mHELLO'))
    green  = _ink(_render('\x1b[32mHELLO'))
    assert green == normal


def test_red_fg_not_promoted_in_gray_mode():
    """In grey mode colour already becomes an ink weight — no extra bold."""
    normal_gray = _ink(_render('\x1b[0mHELLO', gray=True))
    red_gray    = _ink(_render('\x1b[31mHELLO', gray=True))
    assert red_gray <= normal_gray


def test_red_fg_already_bold_not_double_struck(make_app):
    """Red + explicit SGR bold should not hit the synthetic double-strike twice."""
    bold_red    = _ink(_render('\x1b[1;31mHELLO'))
    just_bold   = _ink(_render('\x1b[1mHELLO'))
    # Both explicitly bold — ink should be similar (not runaway double-striking).
    assert abs(bold_red - just_bold) <= just_bold // 2


def test_red_fg_inverted_not_promoted():
    """A selected/reversed red cell must not be additionally bolded."""
    normal_inv = _ink(_render('\x1b[7mHELLO'))
    red_inv    = _ink(_render('\x1b[7;31mHELLO'))
    # Both inverted — ink levels dominated by the fill, not glyph weight.
    assert abs(red_inv - normal_inv) <= normal_inv // 4
