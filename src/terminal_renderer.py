"""
Renders a pyte terminal screen buffer to an 800×480 PIL Image for the e-ink display.

HQ rendering (hq=True, default):
  Draws at 2× resolution then downsamples with LANCZOS + hard threshold.
  The supersampling step gives sub-pixel precision; the threshold step converts
  the soft anti-aliased result to clean 1-bit-ready black/white.  On e-ink
  (which is inherently 1-bit) this produces significantly sharper text than
  drawing directly at 800×480, especially at small font sizes.
"""
import os
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
import pyte

W, H = 800, 480
SPLIT_TERMINAL_W = 600
SPLIT_SIDEBAR_W  = W - SPLIT_TERMINAL_W  # 200
STATUS_H         = 34    # two-line status bar (17px per line)
TERMINAL_H       = H - STATUS_H  # 446

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
    """Return (cols, rows, char_w, char_h) using the 1× font (logical dimensions)."""
    font = _find_mono_font(font_path, font_size)
    cw, ch = _char_size(font)
    return terminal_width // cw, TERMINAL_H // ch, cw, ch


def render_screen(
    screen: pyte.Screen,
    font_size: int,
    dark_mode: bool = True,
    font_path: str = '',
    terminal_width: int = W,
    status_info: tuple = None,
    alerts: list = None,
    hq: bool = True,
) -> Image.Image:
    """
    Render pyte.Screen to an 800×480 grayscale PIL Image.

    hq=True (default): render at 2× then downsample — crisper text on e-ink.
    hq=False: render directly at 800×480 (faster, slightly softer edges).
    """
    scale = 2 if hq else 1
    W_s  = W * scale
    H_s  = H * scale
    TH_s = TERMINAL_H * scale
    tw_s = terminal_width * scale

    bg = 0 if dark_mode else 255
    fg = 255 if dark_mode else 0

    # Draw at scaled font size so glyphs are 2× larger
    font = _find_mono_font(font_path, font_size * scale)
    cw, ch = _char_size(font)

    img = Image.new('L', (W_s, H_s), bg)
    draw = ImageDraw.Draw(img)

    # ── Terminal cell grid ────────────────────────────────────────────────────
    for row_idx in range(screen.lines):
        y = row_idx * ch
        if y >= TH_s:
            break
        row = screen.buffer[row_idx]
        for col_idx in range(screen.columns):
            x = col_idx * cw
            if x >= tw_s:
                break
            char = row[col_idx]
            is_cursor    = (row_idx == screen.cursor.y and col_idx == screen.cursor.x)
            cell_inverted = bool(char.reverse) or is_cursor
            cell_fg = bg if cell_inverted else fg
            cell_bg = fg if cell_inverted else bg
            if cell_bg != bg:
                draw.rectangle([x, y, x + cw - 1, y + ch - 1], fill=cell_bg)
            glyph = char.data
            if glyph and glyph != ' ':
                draw.text((x, y), glyph, font=font, fill=cell_fg)

    # ── Status bar ───────────────────────────────────────────────────────────
    _draw_status_bar(
        draw, font_size, fg, bg, tw_s,
        status_info=status_info,
        alerts=alerts,
        scale=scale,
    )

    # ── HQ downsample + hard threshold ───────────────────────────────────────
    if hq:
        img = img.resize((W, H), Image.LANCZOS)
        # Hard threshold: push anti-aliased gray edges to clean black or white.
        # 128 is neutral; nudge slightly toward white to preserve thin strokes.
        img = img.point(lambda p: 255 if p > 112 else 0)

    return img


def render_mini_stats(img: Image.Image, stats: dict, dark_mode: bool = True):
    """Render a compact stats sidebar into the right 200px of an 800×480 image."""
    if stats is None:
        return

    bg = 0 if dark_mode else 255
    fg = 255 if dark_mode else 0
    x0 = SPLIT_TERMINAL_W

    draw = ImageDraw.Draw(img)
    draw.rectangle([x0, 0, W, H], fill=bg)
    draw.line([(x0, 0), (x0, H)], fill=fg, width=1)

    pad = 6
    x   = x0 + pad
    y   = pad

    f_time  = _find_mono_font('', 20)
    f_body  = _find_mono_font('', 11)
    f_small = _find_mono_font('', 10)

    def lh(font): return font.getbbox('0')[3] + 4

    draw.text((x, y), stats.get('time', '--:--'), font=f_time, fill=fg)
    y += lh(f_time)

    draw.text((x, y), stats.get('date', ''), font=f_small, fill=fg)
    y += lh(f_small) + 2

    draw.line([(x, y), (W - pad, y)], fill=fg, width=1)
    y += 4

    bar_w = SPLIT_SIDEBAR_W - pad * 2 - 2

    for label, pct in [
        ('CPU',  stats.get('cpu_percent', 0)),
        ('RAM',  stats.get('memory', {}).get('percent', 0)),
        ('Disk', stats.get('disk', {}).get('percent', 0)),
    ]:
        draw.text((x, y), f'{label} {pct:.0f}%', font=f_body, fill=fg)
        y += lh(f_body) - 2
        _mini_bar(draw, x, y, bar_w, 8, pct, fg, bg)
        y += 12

    ip = stats.get('primary_ip', '')
    if ip:
        draw.line([(x, y), (W - pad, y)], fill=fg, width=1)
        y += 4
        draw.text((x, y), ip, font=f_small, fill=fg)
        y += lh(f_small)

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
    terminal_width: int,  # already scaled
    status_info: tuple = None,
    alerts: list = None,
    scale: int = 1,
):
    """Two-line status bar. All positions and fonts are scale-aware."""
    y0      = TERMINAL_H * scale
    H_s     = H * scale
    line_h  = (STATUS_H // 2) * scale
    sfont   = _find_mono_font('', 10 * scale)
    pad     = 2 * scale
    x_pad   = 4 * scale

    draw.rectangle([0, y0, terminal_width, H_s], fill=fg)

    # ── Line 1: info or active alert ─────────────────────────────────────────
    y1 = y0 + pad
    active = alerts or []
    if active:
        # Alert: draw on a "un-inverted" background to stand out
        draw.rectangle([0, y0, terminal_width, y0 + line_h], fill=bg)
        draw.text((x_pad, y1), f'⚠ {active[0]}', font=sfont, fill=fg)
    elif status_info:
        time_str, cwd, branch = status_info
        parts = [p for p in (time_str, cwd, f'⎯ {branch}' if branch else '') if p]
        draw.text((x_pad, y1), '  '.join(parts), font=sfont, fill=bg)
    else:
        draw.text((x_pad, y1), datetime.now().strftime('%H:%M'), font=sfont, fill=bg)

    # ── Line 2: hotkeys ───────────────────────────────────────────────────────
    y2 = y0 + line_h + pad
    keys = (
        f'F7:Dark/Light  F8:Paste  F9:Font-({font_size}pt)  F12:Font+  '
        'F10:Refresh  F11:Stats  PgUp/Dn:Scroll'
    )
    draw.text((x_pad, y2), keys, font=sfont, fill=bg)
