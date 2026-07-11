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

try:
    import qrcode as _qrcode
    _HAS_QRCODE = True
except ImportError:
    _HAS_QRCODE = False

W, H = 800, 480
SPLIT_TERMINAL_W = 600
SPLIT_SIDEBAR_W  = W - SPLIT_TERMINAL_W  # 200
STATUS_H         = 17    # single-line status bar
TAB_BAR_H        = 0     # tab bar removed; tab indicator shown in status bar
TERMINAL_H       = H - STATUS_H - TAB_BAR_H  # 463

_font_cache: dict = {}
_qr_cache:   dict = {}  # url -> PIL Image, generated once per URL


# ── Font helpers ──────────────────────────────────────────────────────────────

def _find_mono_font(font_path: str, size: int) -> ImageFont.ImageFont:
    key = (font_path, size)
    if key in _font_cache:
        return _font_cache[key]
    candidates = []
    if font_path:
        candidates.append(font_path)

    home = os.path.expanduser('~')
    candidates += [
        # JetBrains Mono Medium — best stroke weight for e-ink 1-bit rendering
        f'{home}/Library/Fonts/JetBrainsMonoNerdFontMono-Medium.ttf',   # macOS (Nerd Font)
        f'{home}/Library/Fonts/JetBrainsMonoNLNerdFontMono-Medium.ttf', # macOS (NL variant)
        '/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Medium.ttf',   # Pi (apt)
        '/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Regular.ttf',  # Pi (apt fallback)
        f'{home}/Library/Fonts/JetBrainsMonoNerdFontMono-Regular.ttf',  # macOS fallback
        # System monospace fallbacks
        '/System/Library/Fonts/Menlo.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf',  # bold for e-ink
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


def _in_select_range(row_idx: int, col_idx: int, select: tuple) -> bool:
    """select = (r1, c1, r2, c2), reading-order-normalized (copy mode)."""
    r1, c1, r2, c2 = select
    if row_idx < r1 or row_idx > r2:
        return False
    if r1 == r2:
        return c1 <= col_idx <= c2
    if row_idx == r1:
        return col_idx >= c1
    if row_idx == r2:
        return col_idx <= c2
    return True


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
    terminal_height: int = TERMINAL_H,
) -> tuple:
    """Return (cols, rows, char_w, char_h) using the 1× font (logical dimensions)."""
    font = _find_mono_font(font_path, font_size)
    cw, ch = _char_size(font)
    return terminal_width // cw, terminal_height // ch, cw, ch


def render_screen(
    screen: pyte.Screen,
    font_size: int,
    dark_mode: bool = True,
    font_path: str = '',
    terminal_width: int = W,
    status_info: tuple = None,
    alerts: list = None,
    hq: bool = True,
    url_qr: str = None,
    net_stats: dict = None,
    overlay: tuple = None,
    tab_bar: list = None,
    bar_config: dict = None,
    cursor_style: str = 'block',
    select: tuple = None,
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

    # Layout uses 1× font metrics so cell positions are identical to
    # render_screen_partial. The scaled font is used only for glyph quality;
    # after HQ downsample (÷scale) column i lands at exactly i*cw — matching the
    # 1× grid — so glyphs don't accumulate sub-pixel drift across the row
    # (previously 2× metrics drifted ~0.5px/col → ~50px gap over 100 cols).
    font_layout = _find_mono_font(font_path, font_size)
    cw, ch = _char_size(font_layout)
    font = _find_mono_font(font_path, font_size * scale) if scale > 1 else font_layout
    s_cw, s_ch = cw * scale, ch * scale   # scaled cell dimensions for drawing

    img = Image.new('L', (W_s, H_s), bg)
    draw = ImageDraw.Draw(img)

    # ── Terminal cell grid (offset by tab bar) ───────────────────────────────
    tab_offset = TAB_BAR_H * scale

    visible_rows = max(1, TERMINAL_H // ch)

    # Auto-scroll the viewport so the cursor row is always on screen. If the
    # cursor sits below the visible window, start drawing further down the
    # buffer so the newest output (where the user is typing) stays in view.
    start_row = 0
    if screen.cursor.y >= visible_rows:
        start_row = screen.cursor.y - visible_rows + 1

    for draw_i, row_idx in enumerate(range(start_row, screen.lines)):
        if draw_i >= visible_rows:
            break
        y = draw_i * s_ch + tab_offset
        row = screen.buffer[row_idx]
        for col_idx in range(screen.columns):
            x = col_idx * s_cw
            if x >= tw_s:
                break
            char = row[col_idx]
            is_cursor    = (row_idx == screen.cursor.y and col_idx == screen.cursor.x)
            cell_inverted = bool(char.reverse)
            if select is not None and _in_select_range(row_idx, col_idx, select):
                cell_inverted = not cell_inverted
            # A block cursor inverts the whole cell (glyph shown in bg over an
            # fg block); an underline cursor keeps the cell as-is and adds a bar.
            if is_cursor and cursor_style == 'block':
                cell_inverted = not cell_inverted
            cell_fg = bg if cell_inverted else fg
            cell_bg = fg if cell_inverted else bg
            if cell_bg != bg:
                draw.rectangle([x, y, x + s_cw - 1, y + s_ch - 1], fill=cell_bg)
            glyph = char.data
            if glyph and glyph != ' ':
                draw.text((x, y), glyph, font=font, fill=cell_fg)
            if is_cursor and cursor_style != 'block':
                bar_h = max(2, s_ch // 6)
                draw.rectangle([x, y + s_ch - bar_h, x + s_cw - 1, y + s_ch - 1], fill=fg)

    # ── Status bar ───────────────────────────────────────────────────────────
    _draw_status_bar(
        draw, font_size, fg, bg, tw_s,
        status_info=status_info,
        alerts=alerts,
        scale=scale,
        net_stats=net_stats,
        bar_config=bar_config,
    )

    # ── HQ downsample + hard threshold ───────────────────────────────────────
    if hq:
        img = img.resize((W, H), Image.LANCZOS)
        # Hard threshold: push anti-aliased gray edges to clean black or white.
        # 128 is neutral; nudge slightly toward white to preserve thin strokes.
        img = img.point(lambda p: 255 if p > 112 else 0)

    # ── URL QR overlay (after HQ downsample — drawn at 1× for crispness) ─────
    if url_qr:
        _draw_url_qr(img, url_qr, terminal_width)

    if overlay is not None:
        items, idx, title = overlay
        if items:
            _draw_palette(ImageDraw.Draw(img), img, items, idx, title, 1, bg, fg)

    if tab_bar:
        render_tab_bar(img, tab_bar, dark_mode)

    return img


def render_tab_bar(img: Image.Image, tabs_info: list, dark_mode: bool = True):
    """Draw a tab bar strip at y=0..TAB_BAR_H on the given image."""
    if TAB_BAR_H == 0:
        return
    bg = 0 if dark_mode else 255
    fg = 255 if dark_mode else 0
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W - 1, TAB_BAR_H - 1], fill=fg)
    font  = _find_mono_font('', 10)
    pad_y = 2
    x     = 0
    for i, (title, is_active) in enumerate(tabs_info):
        label = f'  {i+1} {title}  ' if title else f'  {i+1}  '
        max_w = W // max(len(tabs_info), 1)
        while len(label) > 4:
            try:    lw = int(font.getlength(label))
            except: lw = len(label) * 7
            if lw <= max_w: break
            label = label[:-3] + '  '
        try:    chip_w = int(font.getlength(label))
        except: chip_w = len(label) * 7
        if is_active:
            draw.rectangle([x, 0, x + chip_w - 1, TAB_BAR_H - 1], fill=fg)
            draw.rectangle([x + 1, 1, x + chip_w - 2, TAB_BAR_H - 2], fill=bg)
            draw.text((x, pad_y), label, font=font, fill=fg)
        else:
            draw.text((x, pad_y), label, font=font, fill=160 if dark_mode else 96)
        x += chip_w
        if i < len(tabs_info) - 1:
            draw.text((x, pad_y), '│', font=font, fill=fg)
            try:    x += int(font.getlength('│'))
            except: x += 7


def _draw_url_qr(img: Image.Image, url: str, terminal_width: int = W):
    """Overlay a QR code for *url* in the bottom-right of the terminal area."""
    if not _HAS_QRCODE:
        return
    try:
        if url not in _qr_cache:
            qr = _qrcode.QRCode(
                error_correction=_qrcode.constants.ERROR_CORRECT_L,
                box_size=4, border=2,
            )
            qr.add_data(url)
            qr.make(fit=True)
            _qr_cache[url] = qr.make_image(
                fill_color='black', back_color='white'
            ).get_image().convert('L')
        qr_img = _qr_cache[url]
        pad = 2
        sz  = qr_img.width
        x0  = terminal_width - sz - pad
        y0  = TAB_BAR_H + TERMINAL_H - sz - pad
        draw = ImageDraw.Draw(img)
        draw.rectangle([x0 - pad, y0 - pad, x0 + sz + pad, y0 + sz + pad], fill=255)
        img.paste(qr_img, (x0, y0))
    except Exception:
        pass


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


def _draw_palette(draw, img, items, idx, title, scale, bg, fg):
    """Draw command palette / clipboard picker overlay on the terminal image."""
    if not items:
        return
    PAL_H = 160 * scale
    PAL_Y = (TAB_BAR_H + TERMINAL_H - PAL_H) * scale
    PAL_X = 4 * scale
    PAL_W = (W - 8) * scale
    font  = _find_mono_font('', 11 * scale)
    lh    = 16 * scale
    draw.rectangle([PAL_X, PAL_Y, PAL_X + PAL_W, PAL_Y + PAL_H], fill=fg)
    draw.text((PAL_X + 4 * scale, PAL_Y + 2 * scale), f'  {title}', font=font, fill=bg)
    visible = 8
    start = max(0, min(idx - visible // 2, len(items) - visible))
    y = PAL_Y + lh + 2 * scale
    for i, item in enumerate(items[start:start + visible]):
        row_idx  = start + i
        is_sel   = (row_idx == idx)
        row_bg   = bg if is_sel else fg
        row_fg   = fg if is_sel else bg
        draw.rectangle([PAL_X, y, PAL_X + PAL_W, y + lh], fill=row_bg)
        draw.text((PAL_X + 4 * scale, y + 1 * scale),
                  ('> ' if is_sel else '  ') + item[:95], font=font, fill=row_fg)
        y += lh


def render_screen_partial(
    screen: pyte.Screen,
    cached_img: Image.Image,
    dirty_rows: set,
    prev_cursor_row,
    start_row: int,
    font_size: int,
    dark_mode: bool = True,
    font_path: str = '',
    terminal_width: int = W,
    status_info: tuple = None,
    alerts: list = None,
    net_stats: dict = None,
    url_qr: str = None,
    bar_config: dict = None,
    draw_status: bool = True,
    cursor_style: str = 'block',
) -> Image.Image:
    """Redraw only changed rows onto cached_img — much faster than a full render.

    Repaints dirty pyte rows plus the old and new cursor rows (so the cursor
    block appears/disappears cleanly). The status bar is only repainted when
    draw_status is set; otherwise its cached pixels are left untouched so it
    doesn't trigger a partial refresh of its own (it's deprioritized/throttled).
    Mutates and returns cached_img directly; no allocation."""
    font = _find_mono_font(font_path, font_size)
    cw, ch = _char_size(font)
    bg = 0 if dark_mode else 255
    fg = 255 if dark_mode else 0
    visible_rows = max(1, TERMINAL_H // ch)
    draw = ImageDraw.Draw(cached_img)

    # Rows to repaint: dirty pyte rows + old and new cursor rows.
    repaint = set()
    for r in dirty_rows:
        di = r - start_row
        if 0 <= di < visible_rows:
            repaint.add(r)
    for r in (prev_cursor_row, screen.cursor.y):
        if r is not None:
            di = r - start_row
            if 0 <= di < visible_rows:
                repaint.add(r)

    for row_idx in repaint:
        draw_i = row_idx - start_row
        y = draw_i * ch + TAB_BAR_H
        draw.rectangle([0, y, terminal_width - 1, y + ch - 1], fill=bg)
        row = screen.buffer[row_idx]
        for col_idx in range(screen.columns):
            x = col_idx * cw
            if x >= terminal_width:
                break
            char = row[col_idx]
            is_cursor    = (row_idx == screen.cursor.y and col_idx == screen.cursor.x)
            cell_inverted = bool(char.reverse)
            if is_cursor and cursor_style == 'block':
                cell_inverted = not cell_inverted
            cell_fg = bg if cell_inverted else fg
            cell_bg = fg if cell_inverted else bg
            if cell_bg != bg:
                draw.rectangle([x, y, x + cw - 1, y + ch - 1], fill=cell_bg)
            glyph = char.data
            if glyph and glyph != ' ':
                draw.text((x, y), glyph, font=font, fill=cell_fg)
            if is_cursor and cursor_style != 'block':
                bar_h = max(2, ch // 6)
                draw.rectangle([x, y + ch - bar_h, x + cw - 1, y + ch - 1], fill=fg)

    if draw_status:
        _draw_status_bar(draw, font_size, fg, bg, terminal_width,
                         status_info=status_info, alerts=alerts, scale=1,
                         net_stats=net_stats, bar_config=bar_config)

    if url_qr:
        _draw_url_qr(cached_img, url_qr, terminal_width)

    return cached_img


# ── Split pane rendering ──────────────────────────────────────────────────────

def render_pane(
    screen: pyte.Screen,
    pane_w: int,
    pane_h: int,
    font_size: int,
    dark_mode: bool = True,
    font_path: str = '',
    focused: bool = True,
    cursor_style: str = 'block',
) -> 'Image.Image':
    """Render a pyte screen into a (pane_w × pane_h) PIL image for split-pane display."""
    font = _find_mono_font(font_path, font_size)
    cw, ch = _char_size(font)
    bg = 0 if dark_mode else 255
    fg = 255 if dark_mode else 0
    img = Image.new('L', (pane_w, pane_h), bg)
    draw = ImageDraw.Draw(img)
    visible_rows = max(1, pane_h // ch)
    start_row = 0
    if screen.cursor.y >= visible_rows:
        start_row = screen.cursor.y - visible_rows + 1
    for draw_i, row_idx in enumerate(range(start_row, screen.lines)):
        if draw_i >= visible_rows:
            break
        y = draw_i * ch
        row = screen.buffer[row_idx]
        for col_idx in range(screen.columns):
            x = col_idx * cw
            if x >= pane_w:
                break
            char = row[col_idx]
            is_cursor = (focused and row_idx == screen.cursor.y and col_idx == screen.cursor.x)
            cell_inverted = bool(char.reverse)
            if is_cursor and cursor_style == 'block':
                cell_inverted = not cell_inverted
            cell_fg = bg if cell_inverted else fg
            cell_bg = fg if cell_inverted else bg
            if cell_bg != bg:
                draw.rectangle([x, y, x + cw - 1, y + ch - 1], fill=cell_bg)
            glyph = char.data
            if glyph and glyph != ' ':
                draw.text((x, y), glyph, font=font, fill=cell_fg)
            if is_cursor and cursor_style != 'block':
                bar_h = max(2, ch // 6)
                draw.rectangle([x, y + ch - bar_h, x + cw - 1, y + ch - 1], fill=fg)
    return img


SPLIT_DIVIDER_W = 2

def render_split_lr(
    pane0: pyte.Screen,
    pane1: pyte.Screen,
    focus: int,
    font_size: int,
    dark_mode: bool = True,
    font_path: str = '',
    status_info: tuple = None,
    alerts: list = None,
    bar_config: dict = None,
    cursor_style: str = 'block',
) -> 'Image.Image':
    """Render two panes side-by-side (left/right) into an 800×480 image."""
    bg = 0 if dark_mode else 255
    fg = 255 if dark_mode else 0
    img = Image.new('L', (W, H), bg)
    half_w = (W - SPLIT_DIVIDER_W) // 2
    pane_h = TERMINAL_H

    p0 = render_pane(pane0, half_w, pane_h, font_size, dark_mode, font_path,
                     focused=(focus == 0), cursor_style=cursor_style)
    p1 = render_pane(pane1, half_w, pane_h, font_size, dark_mode, font_path,
                     focused=(focus == 1), cursor_style=cursor_style)

    img.paste(p0, (0, 0))
    img.paste(p1, (half_w + SPLIT_DIVIDER_W, 0))

    draw = ImageDraw.Draw(img)
    draw.rectangle([half_w, 0, half_w + SPLIT_DIVIDER_W - 1, pane_h - 1], fill=fg)

    # Focus indicator: thin inner border on the active pane
    bx = 0 if focus == 0 else half_w + SPLIT_DIVIDER_W
    draw.rectangle([bx, 0, bx + half_w - 1, pane_h - 1], outline=fg)

    _draw_status_bar(draw, font_size, fg, bg, W,
                     status_info=status_info, alerts=alerts, scale=1,
                     bar_config=bar_config)
    return img


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
    net_stats: dict = None,
    bar_config: dict = None,
):
    """Single-line status bar: time · CWD:branch · IP/WiFi · net speeds (or alert)."""
    y0    = (TAB_BAR_H + TERMINAL_H) * scale
    H_s   = H * scale
    sfont = _find_mono_font('', 10 * scale)
    pad   = 2 * scale
    x_pad = 4 * scale
    bc    = bar_config or {}

    draw.rectangle([0, y0, terminal_width, H_s], fill=fg)

    cwd      = status_info[1] if status_info and len(status_info) > 1 else ''
    branch   = status_info[2] if status_info and len(status_info) > 2 else ''
    tab_str  = status_info[3] if status_info and len(status_info) > 3 else ''
    uptime   = status_info[4] if status_info and len(status_info) > 4 else ''
    raw_time = status_info[0] if status_info else datetime.now().strftime('%H:%M')
    time_str = (tab_str + ' ' if tab_str else '') + raw_time

    active = alerts or []
    if active:
        draw.text((x_pad, y0 + pad), f'⚠ {active[0]}', font=sfont, fill=bg)
    else:
        parts = []
        if bc.get('show_time', True):
            parts.append(time_str)
        host = bc.get('host', '')
        if bc.get('show_host', True) and host:
            parts.append(host)
        if bc.get('show_cwd', True) and (cwd or branch):
            parts.append((cwd + ':' + branch) if cwd and branch else (cwd or branch))
        if net_stats:
            ip       = net_stats.get('ip', '')
            up, dn   = net_stats.get('up', ''), net_stats.get('down', '')
            wifi_sig = net_stats.get('wifi_signal')
            if bc.get('show_ip', True) and ip:
                ip_str = ip + (f' {wifi_sig:+d}dBm' if wifi_sig is not None else '')
                parts.append(ip_str)
            if bc.get('show_speed', True) and (up or dn):
                parts.append((f'↑{up}' if up else '') + (f'  ↓{dn}' if dn else ''))
        if bc.get('show_uptime', True) and uptime:
            parts.append(f'up {uptime}')
        if not parts:
            parts = [time_str]  # always show something
        draw.text((x_pad, y0 + pad), '  '.join(parts), font=sfont, fill=bg)
