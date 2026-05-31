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

    # -----------------------------------------------------------------------
    # TOP BAR: hostname + time + date
    # -----------------------------------------------------------------------
    y = PAD
    hostname = stats.get('hostname', 'unknown')
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
        ly += BAR_H + SECTION_GAP

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
        ly += ROW_H + SECTION_GAP

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
        ry += ROW_H + SECTION_GAP

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

    # QR code (SSH address) — bottom-right of right column, before dark inversion
    primary_ip = stats.get('primary_ip', '')
    qr_size = 100
    if primary_ip and _HAS_QRCODE and config.get('show_qr_code', True):
        try:
            port = config.get('preview_server_port', 8080)
            qr_url = f'http://{primary_ip}:{port}'
            qr_img = _qrcode.make(qr_url).convert('L')
            qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)
            # Place just above bottom bar in the right column
            qr_x = W - PAD - qr_size
            qr_y = H - PAD - 14 - qr_size - 4
            img.paste(qr_img, (qr_x, qr_y))
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Bottom status bar
    # -----------------------------------------------------------------------
    bar_y = H - PAD - 14
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
        qr_size = 140
        try:
            qr_img = _qrcode.make(qr_url).convert('L')
            qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)
            qr_x = W - PAD - qr_size
            qr_y = PAD
            box_pad = 8
            d = ImageDraw.Draw(img)
            d.rectangle(
                [qr_x - box_pad, qr_y - box_pad,
                 qr_x + qr_size + box_pad, qr_y + qr_size + box_pad + 18],
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
