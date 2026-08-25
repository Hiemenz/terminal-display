"""Render the full command reference as a full-screen cheat sheet.

The Ctrl+/ picker lists the same commands one screen-line at a time, which
answers "run this for me" but not "what can this thing do?" — you can't see
the shape of it, and nothing tells you what happens when you exit your last
terminal. This lays every command out at once, in columns, the way a printed
cheat sheet does.

No EinkTerminal dependency: it takes the sections and returns page images, so
it can be rendered and eyeballed without any hardware (see
tests/test_help_sheet.py).
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from terminal_renderer import _find_mono_font

try:
    import qrcode as _qrcode
    _HAS_QRCODE = True
except ImportError:      # optional dependency, same as in render.py
    _HAS_QRCODE = False

WIDTH, HEIGHT = 800, 480

_MARGIN = 14
_TITLE_SIZE = 15
_BODY_SIZE = 11
_LINE_H = 14
_SECTION_GAP = 6
_KEY_COL_W = 96          # width reserved for the key, per column
_COLUMNS = 2


def _column_rows(sections: list) -> list:
    """Flatten sections into drawable rows: ('head', title) / ('item', key, label)."""
    rows: list = []
    for title, items in sections:
        rows.append(('head', title))
        for label, keys in items:
            rows.append(('item', keys, label))
        rows.append(('gap',))
    while rows and rows[-1][0] == 'gap':
        rows.pop()
    return rows


def _row_height(row) -> int:
    """How much vertical space a row actually takes — a section gap is not a
    full line, and counting it as one is how a column ends up with dead space
    at the bottom."""
    return _SECTION_GAP if row[0] == 'gap' else _LINE_H


def _paginate(rows: list, budgets) -> list:
    """Split rows into pages of _COLUMNS columns.

    `budgets` is how many *lines* of room each column has — an int for a
    uniform grid, or one entry per column when something else on the page
    (the QR block) has taken part of a column's height. Rows are then packed
    by real height, so the gaps between sections cost 6px rather than a whole
    line each.

    A section header stranded as the last row of a column is pushed to the
    next one — a heading with nothing under it reads as a mistake.
    """
    if isinstance(budgets, int):
        budgets = [budgets] * _COLUMNS
    limits = [b * _LINE_H for b in budgets]
    columns: list = []
    current: list = []
    used = 0
    limit = limits[0]

    def _wrap_column():
        nonlocal current, used, limit
        columns.append(current)
        current = []
        used = 0
        limit = limits[len(columns) % len(limits)]

    for row in rows:
        height = _row_height(row)
        # A heading needs room for itself and at least one item under it.
        needed = height + (_LINE_H if row[0] == 'head' else 0)
        if current and used + needed > limit:
            _wrap_column()
        # A gap at the top of a fresh column would just be a blank line.
        if row[0] == 'gap' and not current:
            continue
        current.append(row)
        used += height
    if current:
        columns.append(current)
    return [columns[i:i + _COLUMNS] for i in range(0, len(columns), _COLUMNS)] or [[]]


def _qr_image(url: str) -> Image.Image | None:
    """The settings URL as a 1-bit QR, or None if it can't be made.

    box_size 3 matches the dashboard's QR — small enough to leave the command
    columns their space, big enough that a phone gets it off the panel.
    """
    if not url or not _HAS_QRCODE:
        return None
    try:
        qr = _qrcode.QRCode(
            error_correction=_qrcode.constants.ERROR_CORRECT_L,
            box_size=3, border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        return qr.make_image(fill_color='black', back_color='white').get_image().convert('1')
    except Exception:
        return None


def render_help_pages(sections: list, footer: str = '', dark_mode: bool = False,
                      font_path: str = '', title: str = 'Terminal Commands',
                      qr_url: str = '') -> list:
    """Render `sections` as one or more 800x480 cheat-sheet pages.

    `qr_url` adds a QR block in the bottom-right corner pointing at the web
    settings page. The keys on this sheet are the ones you press on the
    device; everything you'd rather *type* — the config itself — lives in
    that browser page, and a QR is the only way to hand a phone a LAN URL
    from an e-ink panel.
    """
    fg = 255 if dark_mode else 0
    bg = 0 if dark_mode else 255
    title_font = _find_mono_font(font_path, _TITLE_SIZE)
    body_font = _find_mono_font(font_path, _BODY_SIZE)

    probe = ImageDraw.Draw(Image.new('1', (1, 1)))
    footer_lines = _wrap(probe, footer, body_font, WIDTH - 2 * _MARGIN) if footer else []

    top = _MARGIN + _TITLE_SIZE + 8
    bottom = HEIGHT - _MARGIN - (len(footer_lines) * _LINE_H + 8 if footer_lines else 0)
    rows_per_col = max(1, (bottom - top) // _LINE_H)

    # The QR eats the bottom of the last column only, so the first column
    # keeps its full run of commands.
    qr_img = _qr_image(qr_url)
    budgets = [rows_per_col] * _COLUMNS
    if qr_img is not None:
        qr_rows = -(-(qr_img.height + 6) // _LINE_H)
        budgets[-1] = max(1, rows_per_col - qr_rows)

    pages_of_columns = _paginate(_column_rows(sections), budgets)
    col_w = (WIDTH - 2 * _MARGIN) // _COLUMNS

    pages = []
    for page_idx, columns in enumerate(pages_of_columns):
        img = Image.new('1', (WIDTH, HEIGHT), bg)
        draw = ImageDraw.Draw(img)

        heading = title
        if len(pages_of_columns) > 1:
            heading += '  (%d/%d)' % (page_idx + 1, len(pages_of_columns))
        draw.text((_MARGIN, _MARGIN), heading, font=title_font, fill=fg)
        rule_y = _MARGIN + _TITLE_SIZE + 3
        draw.line([(_MARGIN, rule_y), (WIDTH - _MARGIN, rule_y)], fill=fg)

        for col_idx, column in enumerate(columns):
            x = _MARGIN + col_idx * col_w
            y = top
            for row in column:
                if row[0] == 'gap':
                    y += _SECTION_GAP
                    continue
                if row[0] == 'head':
                    draw.text((x, y), row[1].upper(), font=body_font, fill=fg)
                    width = int(draw.textlength(row[1].upper(), font=body_font))
                    draw.line([(x, y + _LINE_H - 3), (x + width, y + _LINE_H - 3)], fill=fg)
                else:
                    _, keys, label = row
                    draw.text((x, y), keys, font=body_font, fill=fg)
                    draw.text((x + _KEY_COL_W, y), label, font=body_font, fill=fg)
                y += _LINE_H

        if qr_img is not None:
            qr_x = WIDTH - _MARGIN - qr_img.width
            qr_y = bottom - qr_img.height
            img.paste(qr_img if not dark_mode else _invert(qr_img), (qr_x, qr_y))
            # Label to the left of the code, not above it: the block then
            # costs the last column only the QR's own height.
            label_y = qr_y + (qr_img.height - 2 * _LINE_H) // 2
            for i, line in enumerate(('Settings & config:', qr_url)):
                text_w = int(draw.textlength(line, font=body_font))
                draw.text((qr_x - 10 - text_w, label_y + i * _LINE_H),
                          line, font=body_font, fill=fg)

        if footer_lines:
            y = HEIGHT - _MARGIN - len(footer_lines) * _LINE_H
            draw.line([(_MARGIN, y - 6), (WIDTH - _MARGIN, y - 6)], fill=fg)
            for line in footer_lines:
                draw.text((_MARGIN, y), line, font=body_font, fill=fg)
                y += _LINE_H

        pages.append(img)
    return pages


def _invert(img: Image.Image) -> Image.Image:
    """Dark mode inverts the page, so the QR has to invert with it — a QR
    drawn light-on-dark still scans, but one left light-on-light does not."""
    return img.point(lambda p: 255 - p)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list:
    lines: list = []
    for paragraph in text.split('\n'):
        words = paragraph.split()
        if not words:
            lines.append('')
            continue
        line = words[0]
        for word in words[1:]:
            candidate = line + ' ' + word
            if draw.textlength(candidate, font=font) <= max_width:
                line = candidate
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines
