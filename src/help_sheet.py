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


def _paginate(rows: list, rows_per_col: int) -> list:
    """Split rows into pages of _COLUMNS columns.

    A section header stranded as the last row of a column is pushed to the
    next one — a heading with nothing under it reads as a mistake.
    """
    columns: list = []
    current: list = []
    for row in rows:
        if len(current) >= rows_per_col:
            columns.append(current)
            current = []
        if (row[0] == 'head' and len(current) == rows_per_col - 1):
            current.append(('gap',))
            columns.append(current)
            current = []
        # A gap at the top of a fresh column would just be a blank line.
        if row[0] == 'gap' and not current:
            continue
        current.append(row)
    if current:
        columns.append(current)
    return [columns[i:i + _COLUMNS] for i in range(0, len(columns), _COLUMNS)] or [[]]


def render_help_pages(sections: list, footer: str = '', dark_mode: bool = False,
                      font_path: str = '', title: str = 'Terminal Commands') -> list:
    """Render `sections` as one or more 800x480 cheat-sheet pages."""
    fg = 255 if dark_mode else 0
    bg = 0 if dark_mode else 255
    title_font = _find_mono_font(font_path, _TITLE_SIZE)
    body_font = _find_mono_font(font_path, _BODY_SIZE)

    probe = ImageDraw.Draw(Image.new('1', (1, 1)))
    footer_lines = _wrap(probe, footer, body_font, WIDTH - 2 * _MARGIN) if footer else []

    top = _MARGIN + _TITLE_SIZE + 8
    bottom = HEIGHT - _MARGIN - (len(footer_lines) * _LINE_H + 8 if footer_lines else 0)
    rows_per_col = max(1, (bottom - top) // _LINE_H)

    pages_of_columns = _paginate(_column_rows(sections), rows_per_col)
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

        if footer_lines:
            y = HEIGHT - _MARGIN - len(footer_lines) * _LINE_H
            draw.line([(_MARGIN, y - 6), (WIDTH - _MARGIN, y - 6)], fill=fg)
            for line in footer_lines:
                draw.text((_MARGIN, y), line, font=body_font, fill=fg)
                y += _LINE_H

        pages.append(img)
    return pages


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
