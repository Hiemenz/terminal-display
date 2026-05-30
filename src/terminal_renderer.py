"""
Renders a pyte terminal screen buffer to an 800×480 PIL Image for the e-ink display.
"""
import os
from PIL import Image, ImageDraw, ImageFont
import pyte

W, H = 800, 480
STATUS_H = 18          # reserved for the hotkey status bar at the bottom
TERMINAL_H = H - STATUS_H  # pixel height available for terminal text

_font_cache: dict = {}


def _find_mono_font(font_path: str, size: int) -> ImageFont.ImageFont:
    key = (font_path, size)
    if key in _font_cache:
        return _font_cache[key]

    candidates = []
    if font_path:
        candidates.append(font_path)
    candidates += [
        '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf',
        '/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf',
        '/usr/share/fonts/truetype/freefont/FreeMono.ttf',
        '/System/Library/Fonts/Supplemental/Courier New.ttf',
        '/Library/Fonts/Courier New.ttf',
    ]
    for fp in candidates:
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, size)
                _font_cache[key] = font
                return font
            except Exception:
                pass

    font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _char_size(font: ImageFont.ImageFont) -> tuple:
    """Return (char_width, char_height) for a monospace font."""
    try:
        cw = int(font.getlength('M'))
    except AttributeError:
        try:
            cw = font.getbbox('M')[2]
        except Exception:
            cw = 8

    try:
        # Use a string with descenders to get full line height
        bbox = font.getbbox('Mgjpq|')
        ch = (bbox[3] - min(bbox[1], 0)) + 2  # +2 px line spacing
    except Exception:
        ch = int(cw * 2)

    return max(cw, 4), max(ch, 8)


def terminal_dimensions(font_size: int, font_path: str = '') -> tuple:
    """Return (cols, rows, char_w, char_h) for the given font size."""
    font = _find_mono_font(font_path, font_size)
    cw, ch = _char_size(font)
    cols = W // cw
    rows = TERMINAL_H // ch
    return cols, rows, cw, ch


def render_screen(
    screen: pyte.Screen,
    font_size: int,
    dark_mode: bool = True,
    font_path: str = '',
) -> Image.Image:
    """Render pyte.Screen to an 800×480 grayscale PIL Image."""
    bg = 0 if dark_mode else 255
    fg = 255 if dark_mode else 0

    font = _find_mono_font(font_path, font_size)
    cw, ch = _char_size(font)

    img = Image.new('L', (W, H), bg)
    draw = ImageDraw.Draw(img)

    for row_idx in range(screen.lines):
        y = row_idx * ch
        if y >= TERMINAL_H:
            break
        row = screen.buffer[row_idx]
        for col_idx in range(screen.columns):
            x = col_idx * cw
            if x >= W:
                break

            char = row[col_idx]
            is_cursor = (row_idx == screen.cursor.y and col_idx == screen.cursor.x)
            cell_inverted = bool(char.reverse) or is_cursor

            cell_fg = bg if cell_inverted else fg
            cell_bg = fg if cell_inverted else bg

            if cell_bg != bg:
                draw.rectangle([x, y, x + cw - 1, y + ch - 1], fill=cell_bg)

            glyph = char.data
            if glyph and glyph != ' ':
                draw.text((x, y), glyph, font=font, fill=cell_fg)

    _draw_status_bar(draw, font_size, fg, bg)
    return img


def _draw_status_bar(draw: ImageDraw.ImageDraw, font_size: int, fg: int, bg: int):
    y = TERMINAL_H
    draw.rectangle([0, y, W, H], fill=fg)
    sfont = _find_mono_font('', 10)
    text = (
        f"F9:Font-({font_size}pt)  F12:Font+  "
        "F10:FullRefresh  F11:Stats  Ctrl+C:Kill"
    )
    draw.text((4, y + 2), text, font=sfont, fill=bg)
