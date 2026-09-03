"""
Render system stats to an 800x480 PIL image.

Entry point: render(stats, config) -> PIL.Image
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Union

from PIL import Image, ImageDraw, ImageFont

# PIL's FreeTypeFont and (bitmap-fallback) ImageFont aren't related by
# inheritance in typeshed, but our font helpers can return either.
_Font = Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]

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
ROW_H = 24        # base row height
SECTION_GAP = 12  # gap between cards
BAR_H = 12        # progress bar height
CHIP_H = 22       # card title chip height
CARD_RADIUS = 10  # card corner radius
CARD_INSET = 14   # horizontal content inset inside a card


def _find_font(path: str, size: int) -> _Font:
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


def _find_sans(path: str, size: int, bold: bool = False) -> _Font:
    """Sans-serif UI font for the dashboard chrome (headings, metrics, clock).
    Falls back to the mono stack, then the PIL default."""
    if bold:
        candidates = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
            '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
            '/System/Library/Fonts/HelveticaNeue.ttc',
        ]
    else:
        candidates = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
            '/System/Library/Fonts/Supplemental/Arial.ttf',
            '/System/Library/Fonts/Helvetica.ttc',
        ]
    for fp in candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return _find_font(path, size)


def _bar(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
         pct: float, fg: int, bg: int, outline: int):
    """Draw a rounded progress bar. pct in [0, 100]."""
    r = h // 2
    draw.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=bg,
                           outline=outline, width=1)
    fill_w = max(0, int(w * min(pct, 100) / 100))
    if fill_w >= h:
        draw.rounded_rectangle([x, y, x + fill_w, y + h], radius=r, fill=fg)
    elif fill_w > 0:
        # Too narrow for rounded corners — draw a leading dot.
        draw.ellipse([x, y, x + h, y + h], fill=fg)


def _card_frame(draw: ImageDraw.ImageDraw, x: int, y0: int, w: int, y_end: int,
                title: str, font: _Font, fg: int, bg: int):
    """Card outline + filled title chip (fieldset-legend style).

    Content is drawn first, between y0 + CHIP_H and y_end; the frame and chip
    are painted afterwards so the chip sits over the frame's top edge."""
    top = y0 + CHIP_H // 2
    draw.rounded_rectangle([x, top, x + w, y_end], radius=CARD_RADIUS,
                           outline=fg, width=1)
    label = title.upper()
    tw = int(draw.textlength(label, font=font))
    cx0 = x + CARD_INSET
    draw.rounded_rectangle([cx0, y0, cx0 + tw + 18, y0 + CHIP_H],
                           radius=CHIP_H // 2, fill=fg)
    draw.text((cx0 + 9, y0 + CHIP_H // 2 + 1), label, font=font, fill=bg,
              anchor='lm')


def _fmt_rate(bps: float) -> str:
    """Human-readable bytes/sec."""
    v = float(bps)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if v < 1024 or unit == 'GB':
            return f"{v:.0f}{unit}/s" if unit == 'B' else f"{v:.1f}{unit}/s"
        v /= 1024
    raise AssertionError('unreachable: loop always returns on GB')


def _fmt_rate_short(bps: float) -> str:
    """Compact bytes/sec for badges: 66KB/s → 66K, 1.2MB/s → 1.2M."""
    v = float(bps)
    for unit in ('B', 'K', 'M', 'G'):
        if v < 1024 or unit == 'G':
            return f"{v:.0f}{unit}" if unit in ('B', 'K') else f"{v:.1f}{unit}"
        v /= 1024
    raise AssertionError('unreachable: loop always returns on G')


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
               fg: int, font: _Font, fmt, win_min: int,
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

    # Sans-serif for the chrome (clock, chips, metrics); mono for the table.
    f_time     = _find_sans(font_path, 54, bold=True)
    f_date     = _find_sans(font_path, 17)
    f_chip     = _find_sans(font_path, 12, bold=True)
    f_metric   = _find_sans(font_path, 32, bold=True)
    f_metric_s = _find_sans(font_path, 22, bold=True)
    f_body     = _find_sans(font_path, 15)
    f_body_bold = _find_sans(font_path, 15, bold=True)
    f_small    = _find_sans(font_path, 12)
    f_mono     = _find_font(font_path, 13)

    img = Image.new('L', (W, H), color=_WHITE)
    d = ImageDraw.Draw(img)

    fg = _BLACK  # drawn in black, inverted at the end for dark mode

    # Sparkline history (populated by main before render; empty on first runs).
    show_spark = config.get('sparklines_enabled', True)
    hist = stats.get('history', {}) if show_spark else {}
    hist_min = hist.get('window_minutes', 60)

    # -----------------------------------------------------------------------
    # TOP BAR: centred clock + date, host/platform left, uptime/IP right
    # -----------------------------------------------------------------------
    y = PAD - 4
    device_label = config.get('device_label', '').strip()
    hostname = device_label if device_label else stats.get('hostname', 'unknown')
    time_str = stats.get('time', '--:--:--')
    date_str = stats.get('date', '')
    uptime = stats.get('uptime', '')
    primary_ip = stats.get('primary_ip', '')
    platform_str = stats.get('platform', '')

    d.text((W // 2, y), time_str, font=f_time, fill=fg, anchor='ma')
    time_h = int(f_time.getbbox(time_str)[3])
    d.text((W // 2, y + time_h + 6), date_str, font=f_date, fill=fg, anchor='ma')

    # Left block: hostname + platform; right block: uptime + IP.
    d.text((PAD, y + 10), hostname, font=f_body, fill=fg)
    d.text((PAD, y + 32), platform_str, font=f_small, fill=fg)
    d.text((W - PAD, y + 10), f"up {uptime}", font=f_body, fill=fg, anchor='ra')
    if primary_ip:
        d.text((W - PAD, y + 32), primary_ip, font=f_small, fill=fg, anchor='ra')

    # Pending apt updates — only shown when there's something to act on, so a
    # headless Pi you rarely SSH into surfaces it without adding daily clutter.
    updates = stats.get('pending_updates')
    if config.get('show_updates', True) and updates:
        label = '1 update' if updates == 1 else f'{updates} updates'
        d.text((W - PAD, y + 52), label, font=f_small, fill=fg, anchor='ra')

    # CI build status — only shown when the latest run didn't succeed, same
    # "only surface what needs attention" rule as the updates badge above.
    ci_status = stats.get('ci_status')
    if config.get('show_ci_status', True) and ci_status and ci_status != 'success':
        d.text((W - PAD, y + 72), f"CI {ci_status.replace('_', ' ')}",
               font=f_small, fill=fg, anchor='ra')

    y += time_h + 6 + int(f_date.getbbox('Mg')[3]) + 8
    d.line([(PAD, y), (W - PAD, y)], fill=fg, width=1)
    y += SECTION_GAP

    top_y = y  # both columns start here

    # -----------------------------------------------------------------------
    # LEFT COLUMN
    # -----------------------------------------------------------------------
    lx = PAD
    ly = top_y
    load = stats.get('load')
    show_load = config.get('show_load', True) and load

    # --- CPU (load average folded in) ---
    if config.get('show_cpu', True):
        y0 = ly
        cx = lx + CARD_INSET
        cw = COL_W - CARD_INSET * 2
        cy = y0 + CHIP_H + 6
        cpu_pct = stats.get('cpu_percent', 0)
        parts = [f"{stats.get('cpu_count', 0)} cores"]
        freq = stats.get('cpu_freq_mhz')
        if freq:
            parts.append(f"{freq / 1000:.1f} GHz" if freq >= 1000 else f"{freq:.0f} MHz")
        temp = stats.get('cpu_temp_c')
        if temp is not None:
            parts.append(f"{temp:.0f}°C")
        d.text((lx + COL_W - CARD_INSET, cy - 6), f"{cpu_pct:.0f}%",
               font=f_metric, fill=fg, anchor='ra')
        d.text((cx, cy + 4), '  ·  '.join(parts), font=f_body, fill=fg)
        cy += 34
        _bar(d, cx, cy, cw, BAR_H, cpu_pct, fg, _WHITE, fg)
        cy += BAR_H + 8
        if show_spark and 'cpu' in hist:
            cy = _trend_row(d, cx, cy, cw, hist['cpu'], fg, f_small,
                            lambda v: f"{v:.0f}%", hist_min, fixed_max=100)
        if show_load:
            assert load is not None
            d.text((cx, cy), f"load  {load[0]:.2f}  ·  {load[1]:.2f}  ·  {load[2]:.2f}",
                   font=f_small, fill=fg)
            cy += 18
        y_end = cy + 6
        _card_frame(d, lx, y0, COL_W, y_end, 'CPU', f_chip, fg, _WHITE)
        ly = y_end + SECTION_GAP
    elif show_load:
        # CPU panel hidden — show load in its own small card.
        y0 = ly
        cx = lx + CARD_INSET
        cy = y0 + CHIP_H + 6
        assert load is not None
        d.text((cx, cy), f"1m {load[0]:.2f}   5m {load[1]:.2f}   15m {load[2]:.2f}",
               font=f_body, fill=fg)
        cy += ROW_H
        if show_spark and hist.get('load'):
            cy = _trend_row(d, cx, cy, COL_W - CARD_INSET * 2, hist['load'],
                            fg, f_small, lambda v: f"{v:.2f}", hist_min)
        y_end = cy + 6
        _card_frame(d, lx, y0, COL_W, y_end, 'LOAD', f_chip, fg, _WHITE)
        ly = y_end + SECTION_GAP

    # --- Memory / Disk: same compact card pattern ---
    def _usage_card(y0: int, title: str, detail: str, pct: float) -> int:
        cx = lx + CARD_INSET
        cw = COL_W - CARD_INSET * 2
        cy = y0 + CHIP_H + 6
        d.text((lx + COL_W - CARD_INSET, cy - 2), f"{pct:.0f}%",
               font=f_metric_s, fill=fg, anchor='ra')
        d.text((cx, cy + 2), detail, font=f_body, fill=fg)
        cy += 28
        _bar(d, cx, cy, cw, BAR_H, pct, fg, _WHITE, fg)
        y_end = cy + BAR_H + 8
        _card_frame(d, lx, y0, COL_W, y_end, title, f_chip, fg, _WHITE)
        return y_end + SECTION_GAP

    if config.get('show_memory', True):
        mem = stats.get('memory', {})
        ly = _usage_card(ly, 'Memory',
                         f"{mem.get('used_str', '?')} / {mem.get('total_str', '?')}",
                         mem.get('percent', 0))

    if config.get('show_disk', True):
        disk = stats.get('disk', {})
        ly = _usage_card(ly, 'Disk',
                         f"{disk.get('path', '/')}   {disk.get('used_str', '?')} / {disk.get('total_str', '?')}",
                         disk.get('percent', 0))

    # -----------------------------------------------------------------------
    # RIGHT COLUMN
    # -----------------------------------------------------------------------
    rx = PAD + COL_W + COL_GAP
    ry = top_y

    # --- Network (QR for the web UI lives in its right half) ---
    if config.get('show_network', True):
        y0 = ry
        cx = rx + CARD_INSET
        cw = COL_W - CARD_INSET * 2
        cy = y0 + CHIP_H + 6
        net = stats.get('network', {})

        qr_size = 0
        if primary_ip and _HAS_QRCODE and config.get('show_qr_code', True):
            try:
                port = config.get('preview_server_port', 8080)
                qr = _qrcode.QRCode(
                    error_correction=_qrcode.constants.ERROR_CORRECT_L,
                    box_size=3, border=2,
                )
                qr.add_data(f'http://{primary_ip}:{port}/config')
                qr.make(fit=True)
                qr_img = qr.make_image(fill_color='black', back_color='white').get_image().convert('L')
                qr_size = qr_img.width
                img.paste(qr_img, (rx + COL_W - CARD_INSET - qr_size, cy))
            except Exception:
                qr_size = 0

        d.text((cx, cy), net.get('interface', '?'), font=f_metric_s, fill=fg)
        cy += 30
        up_val = f"↑ {net.get('bytes_sent_str', '?')}"
        d.text((cx, cy), up_val, font=f_body_bold, fill=fg)
        d.text((cx + int(d.textlength(up_val, font=f_body_bold)), cy), " sent", font=f_body, fill=fg)
        cy += ROW_H
        dn_val = f"↓ {net.get('bytes_recv_str', '?')}"
        d.text((cx, cy), dn_val, font=f_body_bold, fill=fg)
        d.text((cx + int(d.textlength(dn_val, font=f_body_bold)), cy), " received", font=f_body, fill=fg)
        cy += ROW_H
        if qr_size:
            cy = max(cy, y0 + CHIP_H + 6 + qr_size + 4)
        if show_spark and 'net' in hist:
            cy = _trend_row(d, cx, cy, cw, hist['net'], fg, f_small,
                            _fmt_rate_short, hist_min)
        y_end = cy + 6
        _card_frame(d, rx, y0, COL_W, y_end, 'Network', f_chip, fg, _WHITE)
        ry = y_end + SECTION_GAP

    # --- Top Processes ---
    if config.get('show_top_processes', True):
        y0 = ry
        cx = rx + CARD_INSET
        cy = y0 + CHIP_H + 6
        d.text((cx, cy), f"{'PID':>6}  {'CPU%':>5}  {'MEM%':>5}  NAME",
               font=f_mono, fill=fg)
        cy += 20
        for proc in stats.get('top_processes', []):
            pid = proc.get('pid', '?')
            name = (proc.get('name') or '?')[:20]
            cpu = proc.get('cpu_percent') or 0
            mem = proc.get('memory_percent') or 0
            d.text((cx, cy), f"{pid:>6}  {cpu:>5.1f}  {mem:>5.1f}  {name}",
                   font=f_mono, fill=fg)
            cy += 19
        y_end = min(cy + 6, H - PAD)
        _card_frame(d, rx, y0, COL_W, y_end, 'Processes', f_chip, fg, _WHITE)

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
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _weekly_percent(config: dict) -> float | None:
    """Percentage of the current week elapsed, in [0, 100].

    The week resets to 0% every Tuesday at 23:00 in `config['timezone']`
    (defaults to America/Chicago / Central time) and climbs back to ~100%
    just before the following Tuesday 23:00. Returns None if the
    configured timezone can't be resolved (e.g. missing tzdata).
    """
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(config.get('timezone') or 'America/Chicago')
        now = datetime.now(tz)

        anchor_weekday = 1  # Monday=0 ... Tuesday=1
        days_since = (now.weekday() - anchor_weekday) % 7
        anchor = (now - timedelta(days=days_since)).replace(
            hour=23, minute=0, second=0, microsecond=0)
        if anchor > now:
            anchor -= timedelta(days=7)

        elapsed = (now - anchor).total_seconds()
        pct = elapsed / (7 * 24 * 3600) * 100
        return max(0.0, min(100.0, pct))
    except Exception:
        return None


# The tile sits on top of the screensaver photo, so it is sized to stay a
# corner note rather than the subject: small type, tight leading, short bars.
_TILE_FONT = 11
_TILE_LINE_H = 14
_DAILY_BAR_H = 16
# How much of each day's slot the bar fills. Below ~0.4 the thinnest bars
# start dropping out on the panel's 1-bit dither; at 1.0 they touch and the
# chart reads as one solid mass instead of a day count.
_DAILY_BAR_FILL = 0.5


def _daily_bars(d, x: int, y: int, width: int, totals: list) -> None:
    """Tokens per day as a bar chart, oldest to newest.

    Four weekly totals say how much; this says what shape it had — a steady
    fortnight and one enormous Tuesday have the same average. Scaled to the
    tallest day in the window, because the question is which days were heavy
    relative to each other, not against any absolute number.

    The last bar is today, still in progress, so it is drawn hollow: a short
    solid bar would read as a quiet day rather than an early hour.
    """
    if not totals:
        return
    peak = max(totals) or 1
    # Each day gets an equal slot and the bar sits centred in it, so the
    # chart still spans the full width however skinny the bars are.
    slot = width / len(totals)
    bar_w = max(2, int(slot * _DAILY_BAR_FILL))
    baseline = y + _DAILY_BAR_H
    d.line([(x, baseline), (x + width, baseline)], fill=_BLACK)
    for i, total in enumerate(totals):
        bx = int(round(x + i * slot + (slot - bar_w) / 2))
        height = int(round(_DAILY_BAR_H * total / peak))
        if total > 0:
            height = max(1, height)
        if height <= 0:
            continue
        box = [bx, baseline - height, bx + bar_w - 1, baseline - 1]
        if i == len(totals) - 1:
            d.rectangle(box, fill=_WHITE, outline=_BLACK, width=1)
        else:
            d.rectangle(box, fill=_BLACK)


def _draw_claude_usage(d, font_path: str, usage: dict,
                       reserved_bottom: int = 0,
                       week_pct: float = None) -> None:
    """Panel of recent Claude Code activity, bottom-right of the lock screen.

    `reserved_bottom` is how much of that corner the wake QR already occupies:
    with the QR shown the panel stacks above it rather than over it.

    `week_pct` folds the week-progress bar into the same tile. The week runs
    Tuesday 23:00 to Tuesday 23:00, so it reads as "how far through the week
    this usage happened" — which is the question the two tiles were being
    compared to answer anyway.

    Deliberately labelled an estimate: Claude Code's real 5-hour and weekly
    limits are enforced server-side and written nowhere on disk, so this is
    what the local transcripts show going through, not a quota reading.
    """
    from claude_usage import format_tokens, session_line

    rows = []
    for label, key in (('5h', '5h'), ('7d', '7d')):
        bucket = usage.get(key) or {}
        rows.append('%-3s %4d msg %6s in %6s out'
                    % (label, bucket.get('messages', 0),
                       format_tokens(bucket.get('sent', 0)),
                       format_tokens(bucket.get('generated', 0))))
    # Per-week history, newest first, so a heavy week is visible as a trend
    # rather than as a single percentage with nothing to compare it to.
    totals = usage.get('week_totals') or []
    if totals:
        rows.append('4wk %s av %s'
                    % (' '.join(format_tokens(t) for t in totals),
                       format_tokens(usage.get('week_avg', 0))))
    daily = [t for t in (usage.get('daily') or [])]
    used_pct, used_of = usage.get('week_pct'), usage.get('week_pct_of', '')
    if used_pct is not None:
        # The 7 d row splits in/out; the weekly history is combined totals.
        # Repeat this week as one number so it can be read against them
        # without adding two figures in your head.
        week = usage.get('7d') or {}
        this_week = week.get('sent', 0) + week.get('generated', 0)
        rows.append('wk %s = %.0f%% of %s'
                    % (format_tokens(this_week), used_pct,
                       'usual' if used_of == 'usual' else 'budget'))

    # The bars' own row, drawn under them: what the tallest bar is worth, so
    # the chart carries a scale instead of being pure shape.
    trend = ('%dd peak %s (local est.)'
             % (len(daily), format_tokens(max(daily)))) if daily else ''

    # One line per live session: a project per tmux session is the normal
    # shape here, and showing only the newest hid the other one entirely.
    sessions = usage.get('sessions')
    if sessions is None:
        sessions = [usage.get('session') or ('', '', 0.0)]
    live = [line for line in (session_line(s) for s in sessions) if line]

    f_row = _find_font(font_path, _TILE_FONT)
    measured = rows + live + ([trend] if trend else [])
    box_w = max(int(d.textlength(r, font=f_row)) for r in measured) + 14
    box_h = 10 + len(rows) * _TILE_LINE_H
    if live:
        box_h += len(live) * _TILE_LINE_H + 2
    if daily:
        box_h += _DAILY_BAR_H + 6 + _TILE_LINE_H
    if week_pct is not None:
        box_h += 24
    box_x = W - PAD - box_w
    box_y = H - PAD - box_h - reserved_bottom
    d.rounded_rectangle([box_x, box_y, box_x + box_w, box_y + box_h],
                        radius=8, fill=_WHITE, outline=_BLACK, width=1)
    pad = 7
    y = box_y + 5
    if live:
        # Whose turn it is, on top: every other row is history, these are the
        # only ones on the tile you might act on.
        for line in live:
            d.text((box_x + pad, y), line, font=f_row, fill=_BLACK)
            y += _TILE_LINE_H
        y += 2
    for row in rows:
        d.text((box_x + pad, y), row, font=f_row, fill=_BLACK)
        y += _TILE_LINE_H
    if daily:
        _daily_bars(d, box_x + pad, y + 3, box_w - 2 * pad, daily)
        y += _DAILY_BAR_H + 6
        d.text((box_x + pad, y), trend, font=f_row, fill=_BLACK)
        y += _TILE_LINE_H
    if week_pct is not None:
        y += 3
        d.line([(box_x + pad, y), (box_x + box_w - pad, y)], fill=_BLACK)
        y += 5
        label = 'Week %.0f%%' % week_pct
        d.text((box_x + pad, y), label, font=f_row, fill=_BLACK)
        label_w = int(d.textlength(label, font=f_row)) + 6
        _bar(d, box_x + pad + label_w, y + 2, box_w - 2 * pad - label_w, 7,
             week_pct, _BLACK, _WHITE, _BLACK)
    return box_y


def _draw_speedtest(d: ImageDraw.ImageDraw, font_path: str,
                    history: list, x: int, y: int, width: int,
                    height: int = 81) -> None:
    """Draw a labelled up/download trend chart at (x, y) with the given width/height.

    Layout — sidebar on left, chart using full height on right:
      Left sidebar: title + ↓last + avg + ↑last + avg (Mbps)
      Right panel:  chart lines + Y-axis, full box height

    `history` is a list of {'ts': epoch, 'down': Mbps, 'up': Mbps} dicts,
    oldest first.  Gaps wider than 20 min leave a break in the line.
    Nothing is drawn when the list is empty.
    """
    if not history:
        return

    import math as _math
    import time as _time
    WINDOW     = 5 * 3600
    GAP_SECS   = 20 * 60
    BOX_PAD    = 6
    FONT_SZ    = _TILE_FONT        # 11
    LINE_H     = FONT_SZ + 2      # 13
    SIDEBAR_W  = 48                # left text panel width
    Y_LBL_W    = 22                # Y-axis label column

    box_h = height
    box_w = width

    now    = _time.time()
    cutoff = now - WINDOW
    pts    = [e for e in history if e.get('ts', 0) >= cutoff]
    if not pts:
        return

    f      = _find_font(font_path, FONT_SZ)
    f_bold = _find_sans(font_path, FONT_SZ, bold=True)

    # ── Box ───────────────────────────────────────────────────────────────────
    d.rounded_rectangle([x, y, x + box_w, y + box_h],
                        radius=8, fill=_WHITE, outline=_BLACK, width=1)

    # ── Stats ─────────────────────────────────────────────────────────────────
    last     = pts[-1]
    avg_down = sum(e['down'] for e in pts) / len(pts)
    avg_up   = sum(e['up']   for e in pts) / len(pts)

    # ── Left sidebar — stacked labels ─────────────────────────────────────────
    sx  = x + BOX_PAD
    sy  = y + BOX_PAD
    d.text((sx, sy), f'↓{last["down"]:.0f}', font=f_bold, fill=_BLACK)
    sy += LINE_H
    d.text((sx, sy), f'avg {avg_down:.0f}', font=f, fill=_BLACK)
    sy += LINE_H
    d.text((sx, sy), f'↑{last["up"]:.0f}', font=f_bold, fill=_BLACK)
    sy += LINE_H
    d.text((sx, sy), f'avg {avg_up:.0f}', font=f, fill=_BLACK)
    sy += LINE_H
    d.text((sx, sy), 'Mbps', font=f, fill=_BLACK)

    # Vertical divider between sidebar and chart
    div_x = x + BOX_PAD + SIDEBAR_W
    d.line([(div_x, y + BOX_PAD), (div_x, y + box_h - BOX_PAD)], fill=180, width=1)

    # ── Chart geometry — full box height ──────────────────────────────────────
    cx      = div_x + Y_LBL_W     # left edge of plot area
    cy      = y + BOX_PAD         # top of plot
    cw      = x + box_w - BOX_PAD - cx   # plot width
    cb      = y + box_h - BOX_PAD        # baseline
    CHART_H = cb - cy

    all_vals = [e['down'] for e in pts] + [e['up'] for e in pts]
    peak     = max(all_vals) or 1.0
    mag        = 10 ** max(0, int(_math.log10(peak)) - 1)
    peak_label = int(_math.ceil(peak / mag) * mag)

    def _tx(ts: float) -> int:
        return cx + int(cw * max(0.0, min(1.0, (ts - cutoff) / WINDOW)))

    def _vy(v: float) -> int:
        return cb - int(CHART_H * min(1.0, v / peak_label))

    # ── Y axis ────────────────────────────────────────────────────────────────
    d.line([(cx, cy), (cx, cb)], fill=_BLACK, width=1)
    d.line([(cx - 2, cy), (cx, cy)], fill=_BLACK, width=1)
    d.text((cx - 3, cy - 1), f'{peak_label}', font=f, fill=_BLACK, anchor='rm')
    mid_val = peak_label // 2
    mid_y   = _vy(mid_val)
    d.line([(cx - 2, mid_y), (cx, mid_y)], fill=_BLACK, width=1)
    d.text((cx - 3, mid_y - 1), f'{mid_val}', font=f, fill=_BLACK, anchor='rm')
    d.line([(cx - 2, cb), (cx, cb)], fill=_BLACK, width=1)
    d.text((cx - 3, cb - 1), '0', font=f, fill=_BLACK, anchor='rm')

    # Faint grid
    for gy in (cy, mid_y):
        for gx in range(cx + 2, cx + cw, 4):
            d.point((gx, gy), fill=180)

    # Baseline
    d.line([(cx, cb), (cx + cw, cb)], fill=_BLACK, width=1)

    # X-axis ticks
    for tx in (cx, cx + cw // 2, cx + cw):
        d.line([(tx, cb), (tx, cb + 3)], fill=_BLACK, width=1)

    # Average reference lines (down = dense, up = sparse)
    for avg_val, step in ((avg_down, 2), (avg_up, 5)):
        ay = _vy(avg_val)
        for gx in range(cx, cx + cw, step):
            d.point((gx, ay), fill=120)

    # ── Series lines ──────────────────────────────────────────────────────────
    for key, dashed in (('down', False), ('up', True)):
        prev = None
        for i, pt in enumerate(pts):
            cur_x = _tx(pt['ts'])
            cur_y = _vy(pt[key])
            if prev is not None and pt['ts'] - pts[i - 1]['ts'] <= GAP_SECS:
                px, py = prev
                if dashed:
                    dx, dy = cur_x - px, cur_y - py
                    length = max(1, int((dx ** 2 + dy ** 2) ** 0.5))
                    steps  = max(1, length // 7)
                    for s in range(steps):
                        t0 = s / steps; t1 = (s + 0.5) / steps
                        d.line([(int(px + dx * t0), int(py + dy * t0)),
                                (int(px + dx * t1), int(py + dy * t1))],
                               fill=_BLACK, width=1)
                else:
                    d.line([(px, py), (cur_x, cur_y)], fill=_BLACK, width=1)
            prev = (cur_x, cur_y)


def render_screensaver(image_path: str, qr_url: str, config: dict,
                       claude_usage: dict = None,
                       speedtest_history: list = None) -> Image.Image:
    """Render the idle screensaver: background image + QR code + week-progress
    overlay, and optionally a Claude Code activity panel and speedtest chart."""
    font_path = config.get('font_path', '')

    img = Image.new('L', (W, H), color=_BLACK)

    if image_path and os.path.exists(image_path):
        try:
            bg = Image.open(image_path).convert('L')
            bg = _crop_to_fit(bg, W, H)
            img.paste(bg, (0, 0))
        except Exception:
            pass

    d = ImageDraw.Draw(img)

    pct = _weekly_percent(config)
    # Folded into the activity tile when there is one, so the lock screen
    # carries a single combined panel instead of two boxes saying related
    # things in opposite corners.
    if pct is not None and not claude_usage:
        try:
            box_w, box_h = 160, 42
            box_x, box_y = PAD, PAD
            d.rounded_rectangle(
                [box_x, box_y, box_x + box_w, box_y + box_h],
                radius=8, fill=_WHITE, outline=_BLACK, width=1,
            )
            f_week = _find_font(font_path, 16)
            d.text((box_x + 10, box_y + 6), f'Week {pct:.0f}%', font=f_week, fill=_BLACK)
            _bar(d, box_x + 10, box_y + 28, box_w - 20, 8, pct, _BLACK, _WHITE, _BLACK)
        except Exception:
            pass

    reserved_bottom = 0
    if qr_url and _HAS_QRCODE and config.get('screensaver_show_qr', True):
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
            # Tell the activity panel how much of this corner is spoken for.
            reserved_bottom = H - (qr_y - box_pad) + 6
        except Exception:
            pass

    _ST_BOX_H = 81
    _ST_GAP = 4
    _has_speedtest = bool(speedtest_history and config.get('screensaver_speedtest', True))
    chart_w = 260

    # Draw Claude usage tile at the very bottom-right; get its top y.
    claude_top = None
    if claude_usage:
        try:
            claude_top = _draw_claude_usage(d, font_path, claude_usage,
                                            reserved_bottom, week_pct=pct)
        except Exception:
            pass

    # Speedtest chart stacks directly above the Claude tile.
    if _has_speedtest:
        try:
            chart_x = W - PAD - chart_w
            if claude_top is not None:
                chart_y = claude_top - _ST_GAP - _ST_BOX_H
            else:
                chart_y = H - PAD - _ST_BOX_H
            _draw_speedtest(d, font_path, speedtest_history,
                            chart_x, chart_y, chart_w, _ST_BOX_H)
        except Exception:
            pass

    return img


# ── Tile screensaver ──────────────────────────────────────────────────────────

_TILE_COLS  = 3
_TILE_ROWS  = 2
_TILE_GAP   = 10
_TILE_RADIUS = 8
_TILE_CHIP_H = 20   # title chip height
_TILE_INSET  = 10   # content inset inside tile


def _tile_frame(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                title: str, f_chip: _Font, fg: int, bg: int) -> int:
    """Draw a rounded-rect tile with a filled title chip. Returns y of content area."""
    d.rounded_rectangle([x, y, x + w, y + h], radius=_TILE_RADIUS,
                        fill=bg, outline=fg, width=1)
    chip_x2 = min(x + _find_text_width(d, title, f_chip) + _TILE_INSET * 2, x + w)
    d.rounded_rectangle([x, y, chip_x2, y + _TILE_CHIP_H],
                        radius=_TILE_RADIUS, fill=fg)
    # Re-square bottom corners of chip so it merges with the tile body
    d.rectangle([x, y + _TILE_CHIP_H // 2, chip_x2, y + _TILE_CHIP_H], fill=fg)
    d.text((x + _TILE_INSET, y + (_TILE_CHIP_H - _find_text_height(f_chip)) // 2),
           title, font=f_chip, fill=bg)
    return y + _TILE_CHIP_H + 6


def _find_text_width(d: ImageDraw.ImageDraw, text: str, font: _Font) -> int:
    if hasattr(d, 'textlength'):
        return int(d.textlength(text, font=font))
    return font.getbbox(text)[2]


def _find_text_height(font: _Font) -> int:
    try:
        return font.getbbox('Ay')[3]
    except Exception:
        return 14


def _draw_weather_tile(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                       font_path: str, weather: dict | None, fg: int, bg: int) -> None:
    f_chip  = _find_font(font_path, 11)
    cy = _tile_frame(d, x, y, w, h, 'WEATHER', f_chip, fg, bg)
    if not weather:
        f = _find_font(font_path, 13)
        d.text((x + _TILE_INSET, cy + 10), 'Unavailable', font=f, fill=fg)
        return
    cx = x + _TILE_INSET
    temp  = weather['temp_f']
    hi    = weather['high_f']
    lo    = weather['low_f']
    unit  = '°F'

    f_big  = _find_font(font_path, 44)
    f_body = _find_font(font_path, 12)
    f_sm   = _find_font(font_path, 11)

    # Big temperature centred
    temp_str = f'{temp}{unit}'
    tw = _find_text_width(d, temp_str, f_big)
    d.text((x + (w - tw) // 2, cy + 4), temp_str, font=f_big, fill=fg)
    cy += 54

    # Condition
    desc = weather.get('desc', '')
    d.text((cx, cy), desc, font=f_body, fill=fg)
    cy += 16

    # H / L / humidity
    detail = f"H:{hi}{unit}  L:{lo}{unit}  \U0001f4a7{weather.get('humidity', 0)}%"
    d.text((cx, cy), detail, font=f_sm, fill=fg)
    cy += 14

    # Feels like
    fl_str = f"Feels like {weather.get('feels_like_f', temp)}{unit}"
    d.text((cx, cy), fl_str, font=f_sm, fill=fg)


def _draw_cpu_temp_tile(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                        font_path: str, cpu_temp: float | None, fg: int, bg: int) -> None:
    f_chip = _find_font(font_path, 11)
    cy = _tile_frame(d, x, y, w, h, 'CPU TEMP', f_chip, fg, bg)
    cx = x + _TILE_INSET
    cw = w - _TILE_INSET * 2

    f_big  = _find_font(font_path, 44)
    f_body = _find_font(font_path, 12)

    if cpu_temp is None:
        d.text((cx, cy + 10), 'Unavailable', font=f_body, fill=fg)
        return

    temp_str = f'{cpu_temp:.0f}°C'
    tw = _find_text_width(d, temp_str, f_big)
    d.text((x + (w - tw) // 2, cy + 4), temp_str, font=f_big, fill=fg)
    cy += 54

    # Progress bar — scale 0..100°C
    pct = min(100.0, max(0.0, cpu_temp))
    _bar(d, cx, cy, cw, BAR_H, pct, fg, _WHITE, fg)
    cy += BAR_H + 6

    # Status
    if cpu_temp >= 80:
        status = 'HOT — throttling likely'
    elif cpu_temp >= 70:
        status = 'Warm'
    else:
        status = 'Normal'
    d.text((cx, cy), status, font=f_body, fill=fg)


def _draw_mlb_tile(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                   font_path: str, mlb: dict | None, fg: int, bg: int) -> None:
    team_abbr = (mlb or {}).get('team_abbr', 'NYY')
    f_chip = _find_font(font_path, 11)
    cy = _tile_frame(d, x, y, w, h, team_abbr, f_chip, fg, bg)
    cx = x + _TILE_INSET

    f_big   = _find_font(font_path, 38)
    f_mid   = _find_font(font_path, 14)
    f_body  = _find_font(font_path, 12)
    f_sm    = _find_font(font_path, 11)

    if not mlb:
        d.text((cx, cy + 10), 'Unavailable', font=f_body, fill=fg)
        return

    status = mlb.get('status', '')
    home   = mlb.get('home', '?')
    away   = mlb.get('away', '?')

    if status == 'no_game':
        d.text((cx, cy + 8),  'No game today', font=f_body, fill=fg)
        return

    # Matchup header
    matchup = f'{away} @ {home}'
    d.text((cx, cy), matchup, font=f_mid, fill=fg)
    cy += 20

    if status in ('In Progress', 'Warmup', 'Pre-Game'):
        # Live score
        hs = mlb.get('home_score') or 0
        as_ = mlb.get('away_score') or 0
        score_str = f'{as_}–{hs}'
        sw = _find_text_width(d, score_str, f_big)
        d.text((x + (w - sw) // 2, cy), score_str, font=f_big, fill=fg)
        cy += 48
        inning = mlb.get('inning', '')
        half   = 'Top' if mlb.get('top', True) else 'Bot'
        half_str = f'{half} {inning}' if inning else status
        d.text((cx, cy), half_str, font=f_sm, fill=fg)
    elif status == 'Final':
        hs  = mlb.get('home_score') or 0
        as_ = mlb.get('away_score') or 0
        score_str = f'{as_}–{hs}'
        sw = _find_text_width(d, score_str, f_big)
        d.text((x + (w - sw) // 2, cy), score_str, font=f_big, fill=fg)
        cy += 48
        d.text((cx, cy), 'Final', font=f_sm, fill=fg)
    else:
        # Scheduled
        start = mlb.get('start_str', '')
        gdate = mlb.get('game_date', '')
        if gdate:
            try:
                dt = datetime.strptime(gdate, '%Y-%m-%d')
                day_label = dt.strftime('%a %b %-d')
            except Exception:
                day_label = gdate
        else:
            day_label = ''
        d.text((cx, cy + 6),  day_label,       font=f_body, fill=fg)
        cy += 20
        d.text((cx, cy + 2),  start,            font=f_mid,  fill=fg)
        cy += 22
        venue = mlb.get('venue', '')
        if venue:
            d.text((cx, cy), venue, font=f_sm, fill=fg)


def _draw_git_tile(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                   font_path: str, git_data: list | None, fg: int, bg: int) -> None:
    f_chip = _find_font(font_path, 11)
    cy = _tile_frame(d, x, y, w, h, 'GIT', f_chip, fg, bg)
    cx = x + _TILE_INSET
    cw = w - _TILE_INSET * 2

    f_big  = _find_font(font_path, 36)
    f_body = _find_font(font_path, 12)
    f_sm   = _find_font(font_path, 11)

    if git_data is None:
        d.text((cx, cy + 8), 'Not configured', font=f_body, fill=fg)
        return

    total = sum(r.get('commits_today', 0) for r in git_data)
    label = f'{total} commit{"s" if total != 1 else ""} today'
    tw = _find_text_width(d, str(total), f_big)
    d.text((x + (w - tw) // 2, cy + 2), str(total), font=f_big, fill=fg)
    cy += 44

    d.text((cx, cy), label, font=f_sm, fill=fg)
    cy += 16

    # Divider
    d.line([(cx, cy), (x + w - _TILE_INSET, cy)], fill=fg, width=1)
    cy += 5

    # Per-repo rows (as many as fit)
    bottom_limit = y + h - 6
    for repo in git_data:
        if cy + 26 > bottom_limit:
            break
        name    = repo.get('name', '?')
        commits = repo.get('commits_today', 0)
        rel     = repo.get('last_relative', '')
        subj    = repo.get('last_subject', '')

        # Repo name + today's commit count
        row1 = f'{name}  {commits}✦' if commits else name
        d.text((cx, cy), row1, font=f_body, fill=fg)
        cy += 14

        # Last commit relative time + truncated subject
        detail = f'{rel} · {subj}' if rel and subj else (rel or subj)
        if detail:
            full = detail
            while detail and _find_text_width(d, detail, f_sm) > cw:
                detail = detail[:-1]
            suffix = '…' if detail != full else ''
            d.text((cx, cy), detail + suffix, font=f_sm, fill=fg)
            cy += 13


def _draw_speedtest_tile(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                         font_path: str, speedtest_history: list | None,
                         fg: int, bg: int) -> None:
    f_chip = _find_font(font_path, 11)
    cy = _tile_frame(d, x, y, w, h, 'SPEEDTEST', f_chip, fg, bg)
    if speedtest_history:
        chart_h = h - (cy - y) - 6
        _draw_speedtest(d, font_path, speedtest_history, x, cy, w, chart_h)
    else:
        f_body = _find_font(font_path, 12)
        d.text((x + _TILE_INSET, cy + 8), 'No data yet', font=f_body, fill=fg)


def _draw_claude_tile(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                      font_path: str, claude_usage: dict | None, fg: int, bg: int) -> None:
    f_chip = _find_font(font_path, 11)
    cy = _tile_frame(d, x, y, w, h, 'CLAUDE', f_chip, fg, bg)
    if claude_usage:
        # Reuse the existing renderer clipped to this tile — pass reserved_bottom=0
        # and let _draw_claude_usage place itself; we clip by drawing it at the
        # tile's own coordinate space by temporarily shifting origin via a crop.
        # Simpler: inline a trimmed version.
        _draw_claude_usage_compact(d, font_path, claude_usage,
                                   x, cy, w, h - (cy - y), fg, bg)
    else:
        f_body = _find_font(font_path, 12)
        d.text((x + _TILE_INSET, cy + 8), 'No data', font=f_body, fill=fg)


def _draw_claude_usage_compact(d: ImageDraw.ImageDraw, font_path: str,
                                usage: dict, x: int, y: int, w: int, h: int,
                                fg: int, bg: int) -> None:
    """Compact Claude usage block for the tile grid."""
    from claude_usage import daily_totals, session_states, weekly_totals

    f_body = _find_font(font_path, 12)
    f_sm   = _find_font(font_path, 11)

    cx = x + _TILE_INSET
    cy = y
    bottom = y + h - 6

    # Active sessions
    try:
        sessions = session_states(idle_minutes=30)
        for sess in sessions[:2]:
            if cy + 14 > bottom:
                break
            txt = sess.get('label', '')
            if not txt:
                continue
            d.text((cx, cy), txt, font=f_sm, fill=fg)
            cy += 13
        if sessions and cy < bottom:
            d.line([(cx, cy), (x + w - _TILE_INSET, cy)], fill=fg, width=1)
            cy += 5
    except Exception:
        pass

    # This-week tokens
    try:
        weeks, baseline = weekly_totals()
        if weeks:
            this_w = weeks[-1]
            sent   = this_w.get('sent', 0) + this_w.get('cache_write', 0)
            recv   = this_w.get('recv', 0)
            cache  = this_w.get('cache_read', 0)

            def _fmt(n):
                if n >= 1_000_000:
                    return f'{n/1_000_000:.1f}M'
                if n >= 1_000:
                    return f'{n/1_000:.0f}k'
                return str(n)

            if cy + 16 < bottom:
                d.text((cx, cy), f'↑{_fmt(sent)}  ↓{_fmt(recv)}', font=f_body, fill=fg)
                cy += 15
            if cache and cy + 14 < bottom:
                d.text((cx, cy), f'cache {_fmt(cache)}', font=f_sm, fill=fg)
                cy += 13
    except Exception:
        pass

    # Daily bars
    try:
        days = daily_totals()
        if days and cy + _DAILY_BAR_H + 6 <= bottom:
            bar_w = w - _TILE_INSET * 2
            _daily_bars(d, cx, cy, bar_w, days)
    except Exception:
        pass


def _draw_video_tile(img: Image.Image, d: ImageDraw.ImageDraw,
                     x: int, y: int, w: int, h: int,
                     font_path: str, frame_path: str | None, fg: int, bg: int) -> None:
    f_chip = _find_font(font_path, 11)
    cy = _tile_frame(d, x, y, w, h, 'MOVIE', f_chip, fg, bg)

    if not frame_path or not os.path.exists(frame_path):
        f_body = _find_font(font_path, 12)
        d.text((x + _TILE_INSET, cy + 8), 'No video configured', font=f_body, fill=fg)
        return

    try:
        frame = Image.open(frame_path).convert('L')
        avail_w = w - 2
        avail_h = h - (cy - y) - 2
        frame.thumbnail((avail_w, avail_h), Image.LANCZOS)
        px = x + (w - frame.width) // 2
        py = cy + (avail_h - frame.height) // 2
        img.paste(frame, (px, py))
    except Exception:
        f_body = _find_font(font_path, 11)
        d.text((x + _TILE_INSET, cy + 8), 'Frame error', font=f_body, fill=fg)


def _draw_idea_tile(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                    font_path: str, idea: dict | None, fg: int, bg: int) -> None:
    f_chip = _find_font(font_path, 11)
    cy = _tile_frame(d, x, y, w, h, 'IDEA', f_chip, fg, bg)
    cx = x + _TILE_INSET
    cw = w - _TILE_INSET * 2
    bottom = y + h - 6

    f_title = _find_font(font_path, 13)
    f_body  = _find_font(font_path, 11)
    f_sm    = _find_font(font_path, 10)

    if not idea:
        d.text((cx, cy + 8), 'No idea repo configured', font=f_body, fill=fg)
        return

    # Title — wrap if needed
    title = idea.get('title', '')
    words = title.split()
    line, lines = '', []
    for w_word in words:
        test = (line + ' ' + w_word).strip()
        if _find_text_width(d, test, f_title) <= cw:
            line = test
        else:
            if line:
                lines.append(line)
            line = w_word
    if line:
        lines.append(line)
    for tl in lines[:2]:
        if cy + 15 > bottom:
            break
        d.text((cx, cy), tl, font=f_title, fill=fg)
        cy += 15
    cy += 3

    # Divider
    if cy + 2 < bottom:
        d.line([(cx, cy), (x + w - _TILE_INSET, cy)], fill=fg, width=1)
        cy += 5

    # Body — word-wrap
    body = idea.get('body', '')
    if body and cy < bottom:
        words = body.split()
        line = ''
        for w_word in words:
            test = (line + ' ' + w_word).strip()
            if _find_text_width(d, test, f_body) <= cw:
                line = test
            else:
                if cy + 13 > bottom - 14:
                    # No room for more body lines — leave room for mono
                    break
                if line:
                    d.text((cx, cy), line, font=f_body, fill=fg)
                    cy += 13
                line = w_word
        if line and cy + 13 <= bottom - 14:
            d.text((cx, cy), line, font=f_body, fill=fg)
            cy += 13

    # Monetization line at the bottom of the tile
    mono = idea.get('mono', '')
    if mono:
        # Truncate to fit
        full_mono = mono
        while mono and _find_text_width(d, mono, f_sm) > cw:
            mono = mono[:-1]
        suffix = '…' if mono != full_mono else ''
        d.text((cx, y + h - 14), mono + suffix, font=f_sm, fill=fg)


def render_tile_screensaver(config: dict,
                            tile_data: dict = None,
                            speedtest_history: list = None,
                            claude_usage: dict = None) -> Image.Image:
    """Render an 800×480 tile-grid screensaver — no photo background.

    tile_data keys (all optional / gracefully absent):
        'weather'          : dict from TileFetcher
        'git'              : list of repo dicts
        'mlb'              : dict from TileFetcher
        'cpu_temp'         : float | None
        'video_frame_path' : str | None  → enables 4×2 layout
        'idea'             : dict | None → enables 4×2 layout
    """
    font_path = config.get('font_path', '')
    dark      = config.get('dark_mode', True)
    fg        = _WHITE if dark else _BLACK
    bg        = _BLACK if dark else _WHITE

    img = Image.new('L', (W, H), color=bg)
    d   = ImageDraw.Draw(img)

    tile_data = tile_data or {}

    # If video or idea tiles are present, use a 4-column layout.
    has_extra = ('video_frame_path' in tile_data or 'idea' in tile_data
                 or config.get('screensaver_tiles_video_path')
                 or config.get('screensaver_tiles_idea_repo'))
    cols = 4 if has_extra else _TILE_COLS

    usable_w = W - 2 * PAD
    usable_h = H - 2 * PAD
    tw = (usable_w - (cols - 1) * _TILE_GAP) // cols
    th = (usable_h - (_TILE_ROWS - 1) * _TILE_GAP) // _TILE_ROWS

    def tile_xy(col: int, row: int):
        return PAD + col * (tw + _TILE_GAP), PAD + row * (th + _TILE_GAP)

    # ── Row 0 ─────────────────────────────────────────────────────────────────
    try:
        tx, ty = tile_xy(0, 0)
        _draw_weather_tile(d, tx, ty, tw, th, font_path, tile_data.get('weather'), fg, bg)
    except Exception:
        pass

    try:
        tx, ty = tile_xy(1, 0)
        _draw_cpu_temp_tile(d, tx, ty, tw, th, font_path, tile_data.get('cpu_temp'), fg, bg)
    except Exception:
        pass

    try:
        tx, ty = tile_xy(2, 0)
        _draw_mlb_tile(d, tx, ty, tw, th, font_path, tile_data.get('mlb'), fg, bg)
    except Exception:
        pass

    if has_extra:
        try:
            tx, ty = tile_xy(3, 0)
            _draw_video_tile(img, d, tx, ty, tw, th, font_path,
                             tile_data.get('video_frame_path'), fg, bg)
        except Exception:
            pass

    # ── Row 1 ─────────────────────────────────────────────────────────────────
    try:
        tx, ty = tile_xy(0, 1)
        _draw_git_tile(d, tx, ty, tw, th, font_path, tile_data.get('git'), fg, bg)
    except Exception:
        pass

    try:
        tx, ty = tile_xy(1, 1)
        _draw_speedtest_tile(d, tx, ty, tw, th, font_path, speedtest_history, fg, bg)
    except Exception:
        pass

    try:
        tx, ty = tile_xy(2, 1)
        _draw_claude_tile(d, tx, ty, tw, th, font_path, claude_usage, fg, bg)
    except Exception:
        pass

    if has_extra:
        try:
            tx, ty = tile_xy(3, 1)
            _draw_idea_tile(d, tx, ty, tw, th, font_path,
                            tile_data.get('idea'), fg, bg)
        except Exception:
            pass

    # Updated-at timestamp — small text bottom-center
    try:
        f_ts = _find_font(font_path, 10)
        ts   = datetime.now().strftime('%-I:%M %p')
        tsw  = _find_text_width(d, ts, f_ts)
        d.text(((W - tsw) // 2, H - 11), ts, font=f_ts, fill=fg)
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
        lh = int(f_label.getbbox(label)[3]) + 4
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
