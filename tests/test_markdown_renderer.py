"""Tests for markdown_renderer.py's parser and pagination — the part of the
Markdown viewer (F6 > "View notes as Markdown") that doesn't need a live
EinkTerminal instance or hardware to exercise."""
from PIL import Image

from markdown_renderer import (
    _parse_blocks,
    _parse_inline,
    _tokenize_words,
    _wrap_tokens,
    render_markdown_pages,
)


def test_parse_blocks_headers():
    blocks = _parse_blocks('# Title\n## Subtitle\n###### Deep')
    assert blocks == [('h1', 'Title'), ('h2', 'Subtitle'), ('h6', 'Deep')]


def test_parse_blocks_paragraph_and_blank():
    blocks = _parse_blocks('Hello world\n\nSecond paragraph')
    assert blocks == [('p', 'Hello world'), ('blank',), ('p', 'Second paragraph')]


def test_parse_blocks_bullet_list():
    blocks = _parse_blocks('- one\n* two\n+ three')
    assert blocks == [('li', 'one', False, None), ('li', 'two', False, None),
                       ('li', 'three', False, None)]


def test_parse_blocks_numbered_list():
    blocks = _parse_blocks('1. first\n2. second')
    assert blocks == [('li', 'first', True, 1), ('li', 'second', True, 2)]


def test_parse_blocks_blockquote():
    assert _parse_blocks('> a wise quote') == [('quote', 'a wise quote')]


def test_parse_blocks_horizontal_rule():
    for hr in ('---', '***', '___', '-----'):
        assert _parse_blocks(hr) == [('hr',)]


def test_parse_blocks_fenced_code():
    blocks = _parse_blocks('```\nline one\nline two\n```')
    assert blocks == [('code', ['line one', 'line two'])]


def test_parse_blocks_unterminated_code_fence_does_not_hang():
    blocks = _parse_blocks('```\nline one')
    assert blocks == [('code', ['line one'])]


def test_parse_inline_bold():
    assert _parse_inline('a **bold** word') == [
        ('a ', set()), ('bold', {'bold'}), (' word', set()),
    ]


def test_parse_inline_italic():
    assert _parse_inline('a *italic* word') == [
        ('a ', set()), ('italic', {'italic'}), (' word', set()),
    ]


def test_parse_inline_code():
    assert _parse_inline('a `code` word') == [
        ('a ', set()), ('code', {'code'}), (' word', set()),
    ]


def test_parse_inline_plain_text_unstyled():
    assert _parse_inline('plain text') == [('plain text', set())]


def test_tokenize_words_splits_on_whitespace():
    tokens = _tokenize_words([('hello world', {'bold'})])
    assert tokens == [('hello', {'bold'}), ('world', {'bold'})]


def test_wrap_tokens_respects_max_width():
    img = Image.new('L', (100, 100))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    from render import _find_sans
    font_map = {frozenset(): _find_sans('', 18)}
    tokens = [('word' + str(i), set()) for i in range(30)]
    lines = _wrap_tokens(draw, tokens, font_map, max_width=100)
    assert len(lines) > 1
    for line in lines:
        width = sum(draw.textlength(w, font=font_map[frozenset()]) for w, _ in line)
        assert width <= 100 + 50   # slack for inter-word spacing not counted here


def test_wrap_tokens_empty_input():
    img = Image.new('L', (100, 100))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    from render import _find_sans
    font_map = {frozenset(): _find_sans('', 18)}
    assert _wrap_tokens(draw, [], font_map, 100) == []


# ── render_markdown_pages: end-to-end pagination ─────────────────────────────

def test_render_markdown_pages_returns_at_least_one_page():
    pages = render_markdown_pages('', 'empty.md', {'dark_mode': True})
    assert len(pages) == 1
    assert pages[0].size == (800, 480)


def test_render_markdown_pages_paginates_long_content():
    body = '\n\n'.join(f'Paragraph {i} ' * 20 for i in range(60))
    pages = render_markdown_pages(body, 'long.md', {'dark_mode': True})
    assert len(pages) > 1
    assert all(p.size == (800, 480) for p in pages)


def test_render_markdown_pages_handles_every_block_type_without_error():
    sample = (
        '# Title\n\n'
        'Some **bold**, *italic*, and `code` text.\n\n'
        '- bullet\n\n'
        '1. numbered\n\n'
        '> a quote\n\n'
        '---\n\n'
        '```\ncode block\n```\n'
    )
    pages = render_markdown_pages(sample, 'notes.txt', {'dark_mode': False})
    assert len(pages) == 1
    assert pages[0].mode == 'L'
