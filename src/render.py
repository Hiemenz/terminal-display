"""
Render system stats to an 800x480 PIL image.

Entry point: render(stats, config) -> PIL.Image
"""
import os
import re
from PIL import Image, ImageDraw, ImageFont

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\x1b.')

try:
    import qrcode as _qrcode
    _HAS_QRCODE = True
except ImportError:
    _HAS_QRCODE = False

# Display dimensions
W, H = 800, 480

# Palette: will be inverted at draw time when dark_mode=True
_WHITE = 255
_BLACK = 0

# Layout constants
PAD = 14          # outer padding
COL_GAP = 16      # gap between left and right columns
COL_W = (W - PAD * 2 - COL_GAP) // 2   # ~377 px each column
ROW_H = 28        # base row height
SECTION_GAP = 10  # gap between sections
BAR_H = 16        # progress bar height
BAR_RADIUS = 3    # bar corner radius


def _find_font(path: str, size: int) -> ImageFont.ImageFont:
    """Try provided path, then common monospace fonts, then PIL default."""
    candidates = []
    if path:
        candidates.append((path, size))
    candidates += [
        ('/System/Library/Fonts/Menlo.ttc', size),
        ('/System/Library/Fonts/Supplemental/Andale Mono.ttf', size),
        ('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf', size),
        ('/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf', size),
        ('/System/Library/Fonts/Supplemental/Courier New.ttf', size),
        ('/Library/Fonts/Courier New.ttf', size),
    ]
    for fp, sz in candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, sz)
            except Exception:
                pass
    return ImageFont.load_default()


def _bar(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
         pct: float, fg: int, bg: int, outline: int):
    """Draw a filled progress bar. pct in [0, 100]."""
    # Background
    draw.rectangle([x, y, x + w, y + h], fill=bg, outline=outline)
    # Fill
    fill_w = max(0, int(w * min(pct, 100) / 100))
    if fill_w > 0:
        draw.rectangle([x, y, x + fill_w, y + h], fill=fg)


def _section_header(draw: ImageDraw.ImageDraw, x: int, y: int, label: str,
                    font: ImageFont.ImageFont, color: int, width: int) -> int:
    """Draw a section header with underline. Returns new y."""
    draw.text((x, y), label, font=font, fill=color)
    lh = font.getbbox(label)[3] + 2
    draw.line([(x, y + lh), (x + width, y + lh)], fill=color, width=1)
    return y + lh + 4


def _fmt_rate(bps: float) -> str:
    """Human-readable bytes/sec."""
    v = float(bps)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if v < 1024 or unit == 'GB':
            return f"{v:.0f}{unit}/s" if unit == 'B' else f"{v:.1f}{unit}/s"
        v /= 1024


def _fmt_rate_short(bps: float) -> str:
    """Compact bytes/sec for badges: 66KB/s → 66K, 1.2MB/s → 1.2M."""
    v = float(bps)
    for unit in ('B', 'K', 'M', 'G'):
        if v < 1024 or unit == 'G':
            return f"{v:.0f}{unit}" if unit in ('B', 'K') else f"{v:.1f}{unit}"
        v /= 1024


def _sparkline(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
               vals: list, fg: int, fixed_max: float = None):
    """Plot a polyline of `vals` inside the box (x,y)-(x+w,y+h)."""
    # Bottom axis rule for a visual baseline.
    draw.line([(x, y + h), (x + w, y + h)], fill=fg, width=1)
    if len(vals) < 2:
        return
    vmin = 0.0
    vmax = fixed_max if fixed_max is not None else max(vals)
    vmax = max(vmax * 1.15, 1e-6) if fixed_max is None else vmax
    span = (vmax - vmin) or 1e-6
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        px = x + round(i * (w - 1) / (n - 1))
        frac = min(max((v - vmin) / span, 0.0), 1.0)
        py = y + round((1.0 - frac) * (h - 1))
        pts.append((px, py))
    draw.line(pts, fill=fg, width=1)


def _trend_row(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, vals: list,
               fg: int, font: ImageFont.ImageFont, fmt, win_min: int,
               fixed_max: float = None) -> int:
    """Sparkline on the left + min/avg/max badge on the right. Returns new y."""
    spark_h = 16
    spark_w = int(w * 0.42)
    _sparkline(draw, x, y, spark_w, spark_h, vals, fg, fixed_max=fixed_max)

    badge_x = x + spark_w + 8
    if len(vals) >= 1:
        avg = sum(vals) / len(vals)
        badge = f"avg {fmt(avg)} · pk {fmt(max(vals))} · lo {fmt(min(vals))}"
    else:
        badge = f"{win_min}m collecting…"
    # Vertically centre the badge text against the sparkline box.
    th = font.getbbox('Mg')[3]
    draw.text((badge_x, y + (spark_h - th) // 2), badge, font=font, fill=fg)
    return y + spark_h + 4


def render(stats: dict, config: dict) -> Image.Image:
    """
    Build and return an 800x480 grayscale PIL image from stats.
    Applies dark_mode inversion at the end.
    """
    dark = config.get('dark_mode', True)
    font_path = config.get('font_path', '')

    # Fonts (all monospace)
    f_time = _find_font(font_path, 52)   # big clock
    f_date = _find_font(font_path, 20)
    f_head = _find_font(font_path, 18)   # section headers
    f_body = _find_font(font_path, 16)   # body text
    f_small = _find_font(font_path, 14)  # small labels

    img = Image.new('L', (W, H), color=_WHITE)
    d = ImageDraw.Draw(img)

    fg = _BLACK  # drawn in black, inverted at the end for dark mode

    # Sparkline history (populated by main before render; empty on first runs).
    show_spark = config.get('sparklines_enabled', True)
    hist = stats.get('history', {}) if show_spark else {}
    hist_min = hist.get('window_minutes', 60)

    # -----------------------------------------------------------------------
    # TOP BAR: hostname + time + date
    # -----------------------------------------------------------------------
    y = PAD
    device_label = config.get('device_label', '').strip()
    hostname = device_label if device_label else stats.get('hostname', 'unknown')
    time_str = stats.get('time', '--:--:--')
    date_str = stats.get('date', '')
    uptime = stats.get('uptime', '')

    # Time (big, centred)
    tw = d.textlength(time_str, font=f_time) if hasattr(d, 'textlength') else f_time.getlength(time_str)
    d.text(((W - tw) // 2, y), time_str, font=f_time, fill=fg)
    time_h = f_time.getbbox(time_str)[3]
    y += time_h + 2

    # Date centred below
    dw = d.textlength(date_str, font=f_date) if hasattr(d, 'textlength') else f_date.getlength(date_str)
    d.text(((W - dw) // 2, y), date_str, font=f_date, fill=fg)
    date_h = f_date.getbbox(date_str)[3]
    y += date_h + 2

    # Hostname left, uptime right
    d.text((PAD, y), hostname, font=f_small, fill=fg)
    up_label = f"up {uptime}"
    upw = d.textlength(up_label, font=f_small) if hasattr(d, 'textlength') else f_small.getlength(up_label)
    d.text((W - PAD - upw, y), up_label, font=f_small, fill=fg)
    y += f_small.getbbox(up_label)[3] + 4

    # Divider line below top bar
    d.line([(PAD, y), (W - PAD, y)], fill=fg, width=1)
    y += SECTION_GAP

    top_y = y  # save for right column

    # -----------------------------------------------------------------------
    # LEFT COLUMN
    # -----------------------------------------------------------------------
    lx = PAD
    ly = top_y

    # --- CPU ---
    if config.get('show_cpu', True):
        ly = _section_header(d, lx, ly, '[ CPU ]', f_head, fg, COL_W)
        cpu_pct = stats.get('cpu_percent', 0)
        cpu_count = stats.get('cpu_count', 0)
        freq = stats.get('cpu_freq_mhz')
        freq_str = f"  {freq:.0f}MHz" if freq else ''
        d.text((lx, ly), f"Usage: {cpu_pct:.1f}%  ({cpu_count} cores){freq_str}", font=f_body, fill=fg)
        ly += ROW_H
        _bar(d, lx, ly, COL_W, BAR_H, cpu_pct, fg, _WHITE, fg)
        ly += BAR_H + 4
        if show_spark and 'cpu' in hist:
            ly = _trend_row(d, lx, ly, COL_W, hist['cpu'], fg, f_small,
                            lambda v: f"{v:.0f}%", hist_min, fixed_max=100)
        ly += SECTION_GAP

    # --- Memory ---
    if config.get('show_memory', True):
        ly = _section_header(d, lx, ly, '[ Memory ]', f_head, fg, COL_W)
        mem = stats.get('memory', {})
        mem_pct = mem.get('percent', 0)
        d.text((lx, ly), f"Used: {mem.get('used_str','?')} / {mem.get('total_str','?')}  ({mem_pct:.1f}%)", font=f_body, fill=fg)
        ly += ROW_H
        _bar(d, lx, ly, COL_W, BAR_H, mem_pct, fg, _WHITE, fg)
        ly += BAR_H + SECTION_GAP

    # --- Disk ---
    if config.get('show_disk', True):
        ly = _section_header(d, lx, ly, '[ Disk ]', f_head, fg, COL_W)
        disk = stats.get('disk', {})
        disk_pct = disk.get('percent', 0)
        d.text((lx, ly), f"{disk.get('path','/')}  {disk.get('used_str','?')} / {disk.get('total_str','?')}  ({disk_pct:.1f}%)", font=f_body, fill=fg)
        ly += ROW_H
        _bar(d, lx, ly, COL_W, BAR_H, disk_pct, fg, _WHITE, fg)
        ly += BAR_H + SECTION_GAP

    # --- Load ---
    if config.get('show_load', True) and stats.get('load'):
        load = stats['load']
        ly = _section_header(d, lx, ly, '[ Load Average ]', f_head, fg, COL_W)
        d.text((lx, ly), f"1m: {load[0]:.2f}   5m: {load[1]:.2f}   15m: {load[2]:.2f}", font=f_body, fill=fg)
        ly += ROW_H
        if show_spark and hist.get('load'):
            ly = _trend_row(d, lx, ly, COL_W, hist['load'], fg, f_small,
                            lambda v: f"{v:.2f}", hist_min)
        ly += SECTION_GAP

    # -----------------------------------------------------------------------
    # RIGHT COLUMN
    # -----------------------------------------------------------------------
    rx = PAD + COL_W + COL_GAP
    ry = top_y

    # --- Network ---
    if config.get('show_network', True):
        ry = _section_header(d, rx, ry, '[ Network ]', f_head, fg, COL_W)
        net = stats.get('network', {})
        d.text((rx, ry), f"Interface: {net.get('interface','?')}", font=f_body, fill=fg)
        ry += ROW_H
        d.text((rx, ry), f"↑ Sent:  {net.get('bytes_sent_str','?')}", font=f_body, fill=fg)
        ry += ROW_H
        d.text((rx, ry), f"↓ Recv:  {net.get('bytes_recv_str','?')}", font=f_body, fill=fg)
        ry += ROW_H
        if show_spark and 'net' in hist:
            ry = _trend_row(d, rx, ry, COL_W, hist['net'], fg, f_small,
                            _fmt_rate_short, hist_min)
        ry += SECTION_GAP

    # --- Top Processes ---
    if config.get('show_top_processes', True):
        ry = _section_header(d, rx, ry, '[ Top Processes ]', f_head, fg, COL_W)
        # header row
        d.text((rx, ry), f"{'PID':>6}  {'CPU%':>5}  {'MEM%':>5}  NAME", font=f_small, fill=fg)
        ry += ROW_H - 4
        for proc in stats.get('top_processes', []):
            pid = proc.get('pid', '?')
            name = (proc.get('name') or '?')[:18]
            cpu = proc.get('cpu_percent') or 0
            mem = proc.get('memory_percent') or 0
            line = f"{pid:>6}  {cpu:>5.1f}  {mem:>5.1f}  {name}"
            d.text((rx, ry), line, font=f_small, fill=fg)
            ry += ROW_H - 4

    # QR code (config URL) — bottom-right of right column, before dark inversion
    primary_ip = stats.get('primary_ip', '')
    if primary_ip and _HAS_QRCODE and config.get('show_qr_code', True):
        try:
            port = config.get('preview_server_port', 8080)
            qr_url = f'http://{primary_ip}:{port}/config'
            qr = _qrcode.QRCode(
                error_correction=_qrcode.constants.ERROR_CORRECT_L,
                box_size=4, border=2,
            )
            qr.add_data(qr_url)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color='black', back_color='white').get_image().convert('L')
            sz = qr_img.width
            qr_x = W - PAD - sz
            qr_y = H - 18 - sz - 4
            img.paste(qr_img, (qr_x, qr_y))
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Bottom status bar
    # -----------------------------------------------------------------------
    bar_y = H - 18
    d.line([(PAD, bar_y), (W - PAD, bar_y)], fill=fg, width=1)
    bar_y += 4
    platform_str = stats.get('platform', '')
    ip_label = f'  |  {primary_ip}' if primary_ip else ''
    d.text((PAD, bar_y), f"platform: {platform_str}{ip_label}", font=f_small, fill=fg)

    # -----------------------------------------------------------------------
    # Dark mode inversion
    # -----------------------------------------------------------------------
    if dark:
        img = img.point(lambda p: 255 - p)

    return img


def _crop_to_fit(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Scale then center-crop to exact dimensions — no warping."""
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = round(src_w * scale)
    new_h = round(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def render_screensaver(image_path: str, qr_url: str, config: dict) -> Image.Image:
    """Render the idle screensaver: background image + QR code overlay."""
    font_path = config.get('font_path', '')

    img = Image.new('L', (W, H), color=_BLACK)

    if image_path and os.path.exists(image_path):
        try:
            bg = Image.open(image_path).convert('L')
            bg = _crop_to_fit(bg, W, H)
            img.paste(bg, (0, 0))
        except Exception:
            pass

    if qr_url and _HAS_QRCODE:
        try:
            qr = _qrcode.QRCode(
                error_correction=_qrcode.constants.ERROR_CORRECT_L,
                box_size=5, border=2,
            )
            qr.add_data(qr_url)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color='black', back_color='white').get_image().convert('L')
            qr_size = qr_img.width
            box_pad = 4
            label_h = 16
            # Bottom-right corner, leaving room for the label below the QR.
            qr_x = W - PAD - qr_size
            qr_y = H - PAD - qr_size - box_pad - label_h
            d = ImageDraw.Draw(img)
            d.rectangle(
                [qr_x - box_pad, qr_y - box_pad,
                 qr_x + qr_size + box_pad, qr_y + qr_size + box_pad + label_h],
                fill=_WHITE,
            )
            img.paste(qr_img, (qr_x, qr_y))
            f_small = _find_font(font_path, 13)
            label = 'Scan to wake'
            lw = int(d.textlength(label, font=f_small)) if hasattr(d, 'textlength') else f_small.getbbox(label)[2]
            d.text((qr_x + (qr_size - lw) // 2, qr_y + qr_size + 4), label, font=f_small, fill=_BLACK)
        except Exception:
            pass

    return img


def render_text_message(text: str, label: str, config: dict) -> Image.Image:
    """Render a full-screen custom text message (for 'send to display' web feature)."""
    dark = config.get('dark_mode', True)
    font_path = config.get('font_path', '')

    bg = _BLACK if dark else _WHITE
    fg = _WHITE if dark else _BLACK

    f_label = _find_font(font_path, 18)
    f_text  = _find_font(font_path, 36)
    f_hint  = _find_font(font_path, 13)

    img = Image.new('L', (W, H), color=bg)
    d = ImageDraw.Draw(img)

    y = PAD
    if label:
        d.text((PAD, y), label, font=f_label, fill=fg)
        lh = f_label.getbbox(label)[3] + 4
        y += lh
        d.line([(PAD, y), (W - PAD, y)], fill=fg, width=1)
        y += 8

    # Word-wrap text to fit width
    max_px = W - PAD * 2
    words = text.split()
    lines = []
    current = ''
    for word in words:
        test = (current + ' ' + word).strip()
        try:
            tw = int(d.textlength(test, font=f_text)) if hasattr(d, 'textlength') else f_text.getbbox(test)[2]
        except Exception:
            tw = len(test) * 20
        if tw <= max_px:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    line_h = f_text.getbbox('Mg')[3] + 8
    total_h = len(lines) * line_h
    body_h  = H - y - PAD
    y_start = y + max(0, (body_h - total_h) // 2)

    for line in lines:
        if y_start + line_h > H - PAD:
            break
        try:
            lw = int(d.textlength(line, font=f_text)) if hasattr(d, 'textlength') else f_text.getbbox(line)[2]
        except Exception:
            lw = len(line) * 20
        d.text(((W - lw) // 2, y_start), line, font=f_text, fill=fg)
        y_start += line_h

    # Subtle hint at bottom
    hint = 'Press any key to return'
    d.text((PAD, H - PAD - f_hint.getbbox(hint)[3]), hint, font=f_hint, fill=fg)

    return img


def _wrap_lines(d, text, font, max_px):
    """Greedy word-wrap `text` to lines no wider than max_px."""
    out = []
    for para in text.split('\n'):
        words = para.split()
        if not words:
            out.append('')
            continue
        cur = ''
        for w in words:
            test = (cur + ' ' + w).strip()
            try:
                tw = int(d.textlength(test, font=font)) if hasattr(d, 'textlength') else font.getbbox(test)[2]
            except Exception:
                tw = len(test) * 12
            if tw <= max_px or not cur:
                cur = test
            else:
                out.append(cur)
                cur = w
        out.append(cur)
    return out


def render_card(card: dict, config: dict) -> Image.Image:
    """Render a 'pushed card' to the panel: note / countdown / todo / qr.

    `card` is the dict from the web /card endpoint. Dismissed by any key, so a
    'Press any key to return' hint is drawn at the bottom (like text messages).
    """
    from datetime import datetime

    dark = config.get('dark_mode', False)
    font_path = config.get('font_path', '')
    bg = _BLACK if dark else _WHITE
    fg = _WHITE if dark else _BLACK
    kind = card.get('kind', 'note')

    img = Image.new('L', (W, H), color=bg)
    d = ImageDraw.Draw(img)
    max_px = W - PAD * 2

    def draw_title(title, y):
        if not title:
            return y
        f = _find_font(font_path, 30)
        d.text((PAD, y), title, font=f, fill=fg)
        y += f.getbbox('Mg')[3] + 6
        d.line([(PAD, y), (W - PAD, y)], fill=fg, width=1)
        return y + 12

    if kind == 'countdown':
        title = card.get('title', '') or 'Countdown'
        y = draw_title(title, PAD)
        target = card.get('target', '')
        big = _find_font(font_path, 76)
        sub = _find_font(font_path, 20)
        try:
            tgt = datetime.fromisoformat(target)
            delta = tgt - datetime.now()
            secs = int(delta.total_seconds())
            if secs < 0:
                main_txt, sub_txt = 'Done', tgt.strftime('%a %b %d, %H:%M')
            else:
                dd, rem = divmod(secs, 86400)
                hh, rem = divmod(rem, 3600)
                mm, _ = divmod(rem, 60)
                main_txt = (f'{dd}d {hh}h {mm}m' if dd else
                            (f'{hh}h {mm}m' if hh else f'{mm}m'))
                sub_txt = 'until ' + tgt.strftime('%a %b %d, %H:%M')
        except Exception:
            main_txt, sub_txt = '—', 'set a valid date/time'
        bb = big.getbbox(main_txt)
        cy = y + max(0, (H - y - PAD - 80) // 2)
        d.text(((W - (bb[2] - bb[0])) // 2, cy), main_txt, font=big, fill=fg)
        sw = int(d.textlength(sub_txt, font=sub)) if hasattr(d, 'textlength') else sub.getbbox(sub_txt)[2]
        d.text(((W - sw) // 2, cy + (bb[3] - bb[1]) + 18), sub_txt, font=sub, fill=fg)

    elif kind == 'todo':
        y = draw_title(card.get('title', '') or 'To-do', PAD)
        f = _find_font(font_path, 26)
        lh = f.getbbox('Mg')[3] + 14
        for item in card.get('items', [])[:12]:
            if y + lh > H - PAD - 22:
                break
            d.rectangle([PAD, y + 2, PAD + 20, y + 22], outline=fg, width=2)
            for ln in _wrap_lines(d, str(item), f, max_px - 36)[:1]:
                d.text((PAD + 32, y), ln, font=f, fill=fg)
            y += lh

    elif kind == 'qr':
        url = card.get('url', '')
        caption = card.get('caption', '')
        if url and _HAS_QRCODE:
            try:
                qr = _qrcode.QRCode(error_correction=_qrcode.constants.ERROR_CORRECT_M,
                                    box_size=10, border=2)
                qr.add_data(url)
                qr.make(fit=True)
                qr_img = qr.make_image(fill_color='black', back_color='white').get_image().convert('L')
                side = min(300, H - PAD * 2 - 60)
                qr_img = qr_img.resize((side, side))
                qx = (W - side) // 2
                img.paste(qr_img, (qx, PAD + 10))
                cap = caption or url
                f = _find_font(font_path, 20)
                for i, ln in enumerate(_wrap_lines(d, cap, f, max_px)[:2]):
                    lw = int(d.textlength(ln, font=f)) if hasattr(d, 'textlength') else f.getbbox(ln)[2]
                    d.text(((W - lw) // 2, PAD + 20 + side + i * 26), ln, font=f, fill=fg)
            except Exception:
                pass
        else:
            d.text((PAD, PAD), 'No URL / QR unavailable', font=_find_font(font_path, 24), fill=fg)

    else:  # note
        y = draw_title(card.get('title', ''), PAD)
        f = _find_font(font_path, 34)
        lh = f.getbbox('Mg')[3] + 8
        lines = _wrap_lines(d, card.get('text', ''), f, max_px)
        total = len(lines) * lh
        y = y + max(0, (H - y - PAD - total) // 2)
        for ln in lines:
            if y + lh > H - PAD - 22:
                break
            d.text((PAD, y), ln, font=f, fill=fg)
            y += lh

    hint = 'Press any key to return'
    fh = _find_font(font_path, 13)
    d.text((PAD, H - PAD - fh.getbbox(hint)[3]), hint, font=fh, fill=fg)
    return img


def render_output(cmd: str, output_lines: list, exit_code: int, config: dict) -> Image.Image:
    """Render shell command output as a full-screen image."""
    dark = config.get('dark_mode', True)
    font_path = config.get('font_path', '')

    bg = _BLACK if dark else _WHITE
    fg = _WHITE if dark else _BLACK

    f_hdr  = _find_font(font_path, 16)
    f_body = _find_font(font_path, 14)
    f_foot = _find_font(font_path, 12)

    img = Image.new('L', (W, H), color=bg)
    d = ImageDraw.Draw(img)

    # Header bar: inverted "$ command"
    hdr_h = 28
    d.rectangle([0, 0, W, hdr_h], fill=fg)
    d.text((PAD, 5), f'$ {cmd}'[:110], font=f_hdr, fill=bg)

    # Output lines
    y = hdr_h + 6
    line_h = 18
    max_y = H - 24
    truncated = False
    for raw_line in output_lines:
        line = _ANSI_RE.sub('', raw_line).replace('\t', '    ')
        # wrap at 100 chars per visual row
        for i in range(0, max(1, len(line)), 100):
            if y > max_y:
                truncated = True
                break
            d.text((PAD, y), line[i:i + 100], font=f_body, fill=fg)
            y += line_h
        if truncated:
            d.text((PAD, y - line_h + 2), '… (truncated)', font=f_foot, fill=fg)
            break

    if not output_lines:
        d.text((PAD, y), '(no output)', font=f_body, fill=fg)

    # Footer bar
    from datetime import datetime as _dt
    footer_y = H - 20
    d.line([(0, footer_y), (W, footer_y)], fill=fg, width=1)
    status = 'OK' if exit_code == 0 else f'exit {exit_code}'
    ts = _dt.now().strftime('%H:%M:%S')
    d.text((PAD, footer_y + 2), status, font=f_foot, fill=fg)
    ts_w = int(d.textlength(ts, font=f_foot)) if hasattr(d, 'textlength') else f_foot.getbbox(ts)[2]
    d.text((W - PAD - ts_w, footer_y + 2), ts, font=f_foot, fill=fg)

    return img
