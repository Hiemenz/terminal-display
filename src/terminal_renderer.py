"""
Renders a pyte terminal screen buffer to an 800×480 PIL Image for the e-ink display.

Supports:
  - Configurable terminal width (600px in split-view mode, 800px otherwise)
  - Two-line status bar: info line (time/cwd/branch) + hotkey line
  - Alert overlay in the info line when alerts are active
  - Mini stats sidebar rendered into the right 200px in split-view mode
"""
import os
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
import pyte

W, H = 800, 480
SPLIT_TERMINAL_W = 600   # terminal area width in split-view mode
SPLIT_SIDEBAR_W  = W - SPLIT_TERMINAL_W  # 200px stats sidebar
STATUS_H = 34            # two-line status bar height in pixels
TERMINAL_H = H - STATUS_H  # pixel height available for terminal text (446)

_font_cache: dict = {}


# ── Font helpers ──────────────────────────────────────────────────────────────

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
        bbox = font.getbbox('Mgjpq|')
        ch = (bbox[3] - min(bbox[1], 0)) + 2
    except Exception:
        ch = int(cw * 2)
    return max(cw, 4), max(ch, 8)


# ── Public API ────────────────────────────────────────────────────────────────

def terminal_dimensions(
    font_size: int,
    font_path: str = '',
    terminal_width: int = W,
) -> tuple:
    """Return (cols, rows, char_w, char_h) for the given font size and width."""
    font = _find_mono_font(font_path, font_size)
    cw, ch = _char_size(font)
    cols = terminal_width // cw
    rows = TERMINAL_H // ch
    return cols, rows, cw, ch


def render_screen(
    screen: pyte.Screen,
    font_size: int,
    dark_mode: bool = True,
    font_path: str = '',
    terminal_width: int = W,
    status_info: tuple = None,   # (time_str, cwd, git_branch) or None
    alerts: list = None,         # list of alert message strings
) -> Image.Image:
    """
    Render pyte.Screen to an 800×480 grayscale PIL Image.

    terminal_width: 800 for full-screen terminal, 600 for split-view.
    status_info:    (time_str, cwd, git_branch) shown in the info line.
    alerts:         alert messages; first one replaces the info line.
    """
    bg = 0 if dark_mode else 255
    fg = 255 if dark_mode else 0

    font = _find_mono_font(font_path, font_size)
    cw, ch = _char_size(font)

    img = Image.new('L', (W, H), bg)
    draw = ImageDraw.Draw(img)

    # ── Terminal cell grid ────────────────────────────────────────────────────
    for row_idx in range(screen.lines):
        y = row_idx * ch
        if y >= TERMINAL_H:
            break
        row = screen.buffer[row_idx]
        for col_idx in range(screen.columns):
            x = col_idx * cw
            if x >= terminal_width:
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

    # ── Two-line status bar ───────────────────────────────────────────────────
    _draw_status_bar(
        draw, font_size, fg, bg, terminal_width,
        status_info=status_info,
        alerts=alerts,
    )

    return img


def render_mini_stats(img: Image.Image, stats: dict, dark_mode: bool = True):
    """
    Render a compact stats sidebar into the right 200px of an existing 800×480 image.
    Draws in-place. Call after render_screen() so the sidebar overlays the right side.
    """
    if stats is None:
        return

    bg = 0 if dark_mode else 255
    fg = 255 if dark_mode else 0
    x0 = SPLIT_TERMINAL_W

    draw = ImageDraw.Draw(img)

    # Sidebar background
    draw.rectangle([x0, 0, W, H], fill=bg)
    # Left border separator
    draw.line([(x0, 0), (x0, H)], fill=fg, width=1)

    pad = 6
    x = x0 + pad
    y = pad

    f_time = _find_mono_font('', 20)
    f_body = _find_mono_font('', 11)
    f_small = _find_mono_font('', 10)

    # Time
    time_str = stats.get('time', '--:--')
    draw.text((x, y), time_str, font=f_time, fill=fg)
    y += _find_mono_font('', 20).getbbox('0')[3] + 4

    # Date (small)
    date_str = stats.get('date', '')
    draw.text((x, y), date_str, font=f_small, fill=fg)
    y += _find_mono_font('', 10).getbbox('0')[3] + 6

    # Divider
    draw.line([(x, y), (W - pad, y)], fill=fg, width=1)
    y += 4

    bar_w = SPLIT_SIDEBAR_W - pad * 2 - 2

    # CPU
    cpu_pct = stats.get('cpu_percent', 0)
    draw.text((x, y), f'CPU {cpu_pct:.0f}%', font=f_body, fill=fg)
    y += _find_mono_font('', 11).getbbox('0')[3] + 2
    _mini_bar(draw, x, y, bar_w, 8, cpu_pct, fg, bg)
    y += 12

    # RAM
    mem = stats.get('memory', {})
    mem_pct = mem.get('percent', 0)
    draw.text((x, y), f'RAM {mem_pct:.0f}%', font=f_body, fill=fg)
    y += _find_mono_font('', 11).getbbox('0')[3] + 2
    _mini_bar(draw, x, y, bar_w, 8, mem_pct, fg, bg)
    y += 12

    # Disk
    disk = stats.get('disk', {})
    disk_pct = disk.get('percent', 0)
    draw.text((x, y), f'Disk {disk_pct:.0f}%', font=f_body, fill=fg)
    y += _find_mono_font('', 11).getbbox('0')[3] + 2
    _mini_bar(draw, x, y, bar_w, 8, disk_pct, fg, bg)
    y += 14

    # IP address
    ip = stats.get('primary_ip', '')
    if ip:
        draw.line([(x, y), (W - pad, y)], fill=fg, width=1)
        y += 4
        draw.text((x, y), ip, font=f_small, fill=fg)
        y += _find_mono_font('', 10).getbbox('0')[3] + 4

    # Uptime
    uptime = stats.get('uptime', '')
    if uptime:
        draw.text((x, y), f'up {uptime}', font=f_small, fill=fg)


def _mini_bar(draw, x, y, w, h, pct, fg, bg):
    draw.rectangle([x, y, x + w, y + h], fill=bg, outline=fg)
    fill_w = max(0, int(w * min(pct, 100) / 100))
    if fill_w > 0:
        draw.rectangle([x, y, x + fill_w, y + h], fill=fg)


# ── Status bar ────────────────────────────────────────────────────────────────

def _draw_status_bar(
    draw: ImageDraw.ImageDraw,
    font_size: int,
    fg: int,
    bg: int,
    terminal_width: int,
    status_info: tuple = None,
    alerts: list = None,
):
    """Two-line status bar at the bottom of the terminal area."""
    sfont = _find_mono_font('', 10)
    line_h = STATUS_H // 2  # 17px per line

    # Full bar background (inverted)
    draw.rectangle([0, TERMINAL_H, terminal_width, H], fill=fg)

    # ── Line 1: info (or alert) ───────────────────────────────────────────────
    y1 = TERMINAL_H + 2
    active_alerts = alerts or []

    if active_alerts:
        # Show first alert with extra emphasis (double-inverted = normal bg)
        draw.rectangle([0, TERMINAL_H, terminal_width, TERMINAL_H + line_h], fill=bg)
        draw.text((4, y1), f'⚠ {active_alerts[0]}', font=sfont, fill=fg)
    elif status_info:
        time_str, cwd, branch = status_info
        parts = []
        if time_str:
            parts.append(time_str)
        if cwd:
            parts.append(cwd)
        if branch:
            parts.append(f'⎇ {branch}')
        draw.text((4, y1), '  '.join(parts), font=sfont, fill=bg)
    else:
        draw.text((4, y1), datetime.now().strftime('%H:%M'), font=sfont, fill=bg)

    # ── Line 2: hotkeys ───────────────────────────────────────────────────────
    y2 = TERMINAL_H + line_h + 2
    keys = (
        f'F9:Font-({font_size}pt)  F12:Font+  F10:Refresh  '
        'F11:Stats  PgUp/Dn:Scroll'
    )
    draw.text((4, y2), keys, font=sfont, fill=bg)
