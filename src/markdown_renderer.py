"""Lightweight Markdown -> PIL image renderer for the e-ink display.

Not a full CommonMark implementation — just the subset that shows up in
plain notes: headers (#..######), **bold**/__bold__, *italic*/_italic_,
`inline code`, fenced ```code blocks```, bullet/numbered lists, > blockquotes,
--- horizontal rules, and plain paragraphs.

Entry point: render_markdown_pages(text, label, config) -> list[Image.Image]
Paginated (each page is a full 800x480 frame) since notes can run long and a
single e-ink frame can't scroll — see markdown_viewer_mixin.py for the
PgUp/PgDn paging and Esc-to-close key handling that flips between pages.
"""
from __future__ import annotations

import re

from PIL import Image, ImageDraw

from render import _BLACK, _WHITE, PAD, _find_font, _find_sans

W, H = 800, 480

_HR_RE = re.compile(r'^(-{3,}|\*{3,}|_{3,})$')
_HEADER_RE = re.compile(r'^(#{1,6})\s+(.*)$')
_QUOTE_RE = re.compile(r'^>\s?(.*)$')
_BULLET_RE = re.compile(r'^[-*+]\s+(.*)$')
_NUMBERED_RE = re.compile(r'^(\d+)\.\s+(.*)$')
_INLINE_RE = re.compile(r'(\*\*.+?\*\*|__.+?__|`.+?`|\*.+?\*|_.+?_)')


def _parse_blocks(text: str) -> list:
    """Split raw markdown into a flat list of block tuples: ('h1'..'h6', text),
    ('p', text), ('li', text, ordered, number), ('code', [lines]),
    ('quote', text), ('hr',), or ('blank',)."""
    blocks: list = []
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith('```'):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code_lines.append(lines[i])
                i += 1
            i += 1   # skip the closing fence (or run off the end — fine either way)
            blocks.append(('code', code_lines))
            continue
        if not stripped:
            blocks.append(('blank',))
            i += 1
            continue
        if _HR_RE.match(stripped):
            blocks.append(('hr',))
            i += 1
            continue
        m = _HEADER_RE.match(stripped)
        if m:
            blocks.append((f'h{len(m.group(1))}', m.group(2)))
            i += 1
            continue
        m = _QUOTE_RE.match(stripped)
        if m:
            blocks.append(('quote', m.group(1)))
            i += 1
            continue
        m = _BULLET_RE.match(stripped)
        if m:
            blocks.append(('li', m.group(1), False, None))
            i += 1
            continue
        m = _NUMBERED_RE.match(stripped)
        if m:
            blocks.append(('li', m.group(2), True, int(m.group(1))))
            i += 1
            continue
        blocks.append(('p', stripped))
        i += 1
    return blocks


def _parse_inline(text: str) -> list:
    """Split inline text into (word_text, style_set) spans — style_set is a
    subset of {'bold', 'italic', 'code'}."""
    spans = []
    for part in _INLINE_RE.split(text):
        if not part:
            continue
        if (part.startswith('**') and part.endswith('**') and len(part) >= 4):
            spans.append((part[2:-2], {'bold'}))
        elif part.startswith('__') and part.endswith('__') and len(part) >= 4:
            spans.append((part[2:-2], {'bold'}))
        elif part.startswith('`') and part.endswith('`') and len(part) >= 2:
            spans.append((part[1:-1], {'code'}))
        elif part.startswith('*') and part.endswith('*') and len(part) >= 2:
            spans.append((part[1:-1], {'italic'}))
        elif part.startswith('_') and part.endswith('_') and len(part) >= 2:
            spans.append((part[1:-1], {'italic'}))
        else:
            spans.append((part, set()))
    return spans


def _tokenize_words(spans: list) -> list:
    """Flatten (text, style) spans into (word, style) tokens, splitting each
    span on whitespace so word-wrap can break inside a styled run."""
    tokens = []
    for text, style in spans:
        for word in text.split():
            tokens.append((word, style))
    return tokens


def _wrap_tokens(draw: ImageDraw.ImageDraw, tokens: list, font_map: dict,
                 max_width: int) -> list:
    """Greedy word-wrap of styled tokens into lines, each a list of
    (word, style) that together fit max_width pixels."""
    if not tokens:
        return []
    space_w = draw.textlength(' ', font=font_map[frozenset()])
    lines: list = []
    current: list = []
    cur_width = 0.0
    for word, style in tokens:
        font = font_map[frozenset(style)]
        w = draw.textlength(word, font=font)
        extra = (space_w if current else 0) + w
        if current and cur_width + extra > max_width:
            lines.append(current)
            current = [(word, style)]
            cur_width = w
        else:
            current.append((word, style))
            cur_width += extra
    if current:
        lines.append(current)
    return lines


def _font_size(font, fallback: int) -> int:
    """FreeTypeFont has .size; PIL's bitmap-default ImageFont fallback doesn't."""
    return getattr(font, 'size', fallback)


def _draw_tokens(draw: ImageDraw.ImageDraw, x: int, y: int, tokens: list,
                 font_map: dict, fg: int) -> None:
    """Draw one wrapped line of (word, style) tokens left-to-right, honoring
    per-word bold/code fonts and an underline for italic (no reliable italic
    ttf across macOS dev / Pi, so underline stands in for it)."""
    space_w = draw.textlength(' ', font=font_map[frozenset()])
    cx: float = x
    for i, (word, style) in enumerate(tokens):
        font = font_map[frozenset(style)]
        if i > 0:
            cx += space_w
        draw.text((cx, y), word, font=font, fill=fg)
        w = draw.textlength(word, font=font)
        if 'italic' in style:
            fh = y + _font_size(font, 18)
            draw.line([(cx, fh), (cx + w, fh)], fill=fg, width=1)
        cx += w


def render_markdown_pages(text: str, label: str, config: dict) -> list:
    """Render markdown `text` into a list of full 800x480 PIL images, one per
    page. Word-wraps and paginates so it always fits the fixed-size panel —
    see the module docstring for the supported syntax subset."""
    dark = config.get('dark_mode', True)
    font_path = config.get('font_path', '')
    bg = _BLACK if dark else _WHITE
    fg = _WHITE if dark else _BLACK

    f_title = _find_sans(font_path, 18, bold=True)
    f_footer = _find_font(font_path, 13)
    headers = {n: _find_sans(font_path, size, bold=True)
               for n, size in ((1, 30), (2, 26), (3, 22), (4, 19), (5, 19), (6, 19))}
    body_font_map = {
        frozenset(): _find_sans(font_path, 18),
        frozenset({'bold'}): _find_sans(font_path, 18, bold=True),
        frozenset({'italic'}): _find_sans(font_path, 18),
        frozenset({'code'}): _find_font(font_path, 17),
    }
    code_font = _find_font(font_path, 15)

    header_h = 40
    footer_h = 24
    content_top = header_h + 8
    content_bottom = H - footer_h - 8
    content_w = W - PAD * 2

    blocks = _parse_blocks(text)

    # Probe frame just to get textlength()/size measurements before we know
    # the real page count (page numbers in the footer don't affect layout).
    probe = ImageDraw.Draw(Image.new('L', (W, H)))

    pages: list = []
    lines: list = []   # list of ('text', font, x_offset, tokens_or_None, fg_override)
    y = content_top

    def flush_page():
        nonlocal lines, y
        pages.append(lines)
        lines = []
        y = content_top

    def add_line(x_offset: int, height: int, draw_fn) -> None:
        """draw_fn(draw, x, y) draws one line; height is its line-height."""
        nonlocal y
        if y + height > content_bottom:
            flush_page()
        lines.append((x_offset, y, height, draw_fn))
        y += height

    for block in blocks:
        kind = block[0]
        if kind == 'blank':
            y += 10
            continue
        if kind == 'hr':
            # Must use the `yy` add_line hands the closure, not a value
            # computed from the outer `y` now — a page flush can happen
            # inside add_line, which would leave a precomputed y stale.
            add_line(0, 18, lambda d, x, yy: d.line(
                [(PAD, yy + 8), (W - PAD, yy + 8)], fill=fg, width=1))
            continue
        if kind.startswith('h'):
            level = int(kind[1])
            font = headers[level]
            lh = _font_size(font, 20) + 10
            add_line(0, lh, (lambda d, x, yy, _t=block[1], _f=font: d.text(
                (PAD, yy), _t, font=_f, fill=fg)))
            continue
        if kind == 'code':
            code_lines = block[1] or ['']
            lh = _font_size(code_font, 15) + 6
            box_pad = 6
            box_h = lh * len(code_lines) + box_pad * 2
            if y + box_h > content_bottom and lines:
                flush_page()

            def _draw_code(d, x, yy, _lines=code_lines, _h=box_h, _lh=lh):
                d.rectangle([PAD, yy, W - PAD, yy + _h], outline=fg, width=1)
                cy = yy + box_pad
                for cl in _lines:
                    d.text((PAD + box_pad, cy), cl, font=code_font, fill=fg)
                    cy += _lh

            add_line(0, box_h, _draw_code)
            continue
        if kind == 'quote':
            spans = _parse_inline(block[1])
            tokens = _tokenize_words(spans)
            wrapped = _wrap_tokens(probe, tokens, body_font_map, content_w - 24)
            lh = 24
            for wline in wrapped:
                def _draw_quote(d, x, yy, _wl=wline):
                    d.line([(PAD, yy), (PAD, yy + lh - 4)], fill=fg, width=3)
                    _draw_tokens(d, PAD + 14, yy, _wl, body_font_map, fg)
                add_line(0, lh, _draw_quote)
            continue
        if kind == 'li':
            _text, ordered, number = block[1], block[2], block[3]
            prefix = f'{number}.' if ordered else '•'
            indent = 28
            spans = _parse_inline(_text)
            tokens = _tokenize_words(spans)
            wrapped = _wrap_tokens(probe, tokens, body_font_map, content_w - indent)
            lh = 24
            for j, wline in enumerate(wrapped):
                lead = prefix if j == 0 else ''

                def _draw_li(d, x, yy, _wl=wline, _lead=lead):
                    if _lead:
                        d.text((PAD, yy), _lead, font=body_font_map[frozenset()], fill=fg)
                    _draw_tokens(d, PAD + indent, yy, _wl, body_font_map, fg)
                add_line(0, lh, _draw_li)
            continue
        # Plain paragraph
        spans = _parse_inline(block[1])
        tokens = _tokenize_words(spans)
        wrapped = _wrap_tokens(probe, tokens, body_font_map, content_w)
        lh = 24
        for wline in wrapped:
            def _draw_p(d, x, yy, _wl=wline):
                _draw_tokens(d, PAD, yy, _wl, body_font_map, fg)
            add_line(0, lh, _draw_p)

    if lines or not pages:
        flush_page()

    total = len(pages)
    images = []
    for idx, page_lines in enumerate(pages, start=1):
        img = Image.new('L', (W, H), color=bg)
        d = ImageDraw.Draw(img)
        d.text((PAD, 10), label or 'Markdown', font=f_title, fill=fg)
        d.line([(PAD, header_h), (W - PAD, header_h)], fill=fg, width=1)
        for _x, yy, _h, draw_fn in page_lines:
            draw_fn(d, PAD, yy)
        footer = f'Page {idx}/{total}   PgUp/PgDn page · Esc close'
        fw = d.textlength(footer, font=f_footer)
        d.text((W - PAD - fw, H - footer_h + 2), footer, font=f_footer, fill=fg)
        images.append(img)
    return images
