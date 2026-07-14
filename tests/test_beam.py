"""Tests for beam-to-phone (preview server side)."""
from preview_server import PreviewServer, _render_beam_page


def test_beam_page_escapes_and_includes_text():
    html = _render_beam_page('echo <hi> & "go"')
    assert '&lt;hi&gt;' in html       # escaped
    assert '<script' not in html.lower()
    assert 'Copy' in html             # copy button present


def test_beam_page_empty():
    html = _render_beam_page('')
    assert '<pre id="t"></pre>' in html


def test_set_beam_text_roundtrip():
    srv = PreviewServer(0, '/tmp/x.bmp', '/tmp')
    srv.set_beam_text('hello world')
    assert srv._beam_ref[0] == 'hello world'
    srv.set_beam_text(None)
    assert srv._beam_ref[0] == ''
