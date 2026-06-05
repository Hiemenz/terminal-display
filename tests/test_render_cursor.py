"""Tests for cursor-style rendering (block vs underline)."""
import pyte

from terminal_renderer import render_screen, render_screen_partial, terminal_dimensions


def _screen(text='ls'):
    cols, rows, _, _ = terminal_dimensions(14, '', 800)
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    stream.feed(text)
    return screen


def test_block_and_underline_differ():
    screen = _screen()
    block = render_screen(screen, 14, cursor_style='block')
    underline = render_screen(screen, 14, cursor_style='underline')
    assert block.size == (800, 480)
    assert block.tobytes() != underline.tobytes()


def test_block_fills_cursor_cell_more_than_underline():
    """In light mode the cursor cell is foreground (0=black). A block cursor
    inverts the whole empty cell; an underline only paints a thin bar — so the
    block image has strictly more black pixels in that region."""
    screen = _screen('')          # cursor at (0,0) over an empty cell
    block = render_screen(screen, 14, dark_mode=False, cursor_style='block',
                          hq=False).convert('L')
    underline = render_screen(screen, 14, dark_mode=False, cursor_style='underline',
                              hq=False).convert('L')

    cw, ch = 8, 16  # generous cell crop around the top-left cursor
    def black_in_cell(img):
        px = img.load()
        return sum(1 for y in range(ch) for x in range(cw) if px[x, y] < 128)

    assert black_in_cell(block) > black_in_cell(underline)


def test_partial_render_accepts_cursor_style():
    screen = _screen('echo hi')
    base = render_screen(screen, 14, cursor_style='block')
    out = render_screen_partial(screen, base.copy(), set(screen.dirty), 0, 0, 14,
                                cursor_style='block')
    assert out.size == (800, 480)
