"""Getting text off the panel.

Copy mode yanks a selection into the on-device clipboard and a QR. Two gaps
that left: the clipboard can only paste back into this app, and reading a
long error off a QR is miserable. So a yank also lands in tmux's paste
buffer, and /screen serves what's on the panel right now as selectable text.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import preview_server  # noqa: E402
from preview_server import _GATED_HTML_GET, _render_beam_page  # noqa: E402


def test_screen_endpoints_are_pin_gated():
    """Same treatment as /beam and /notes — screen contents are as sensitive
    as anything else on the device."""
    assert '/screen' in _GATED_HTML_GET
    assert '/screen.txt' in _GATED_HTML_GET


def test_published_screen_text_is_served():
    server = preview_server.PreviewServer.__new__(preview_server.PreviewServer)
    server._screen_ref = ['']
    server.set_screen_text('build failed: missing symbol')
    assert server._screen_ref[0] == 'build failed: missing symbol'


def test_set_screen_text_tolerates_none():
    server = preview_server.PreviewServer.__new__(preview_server.PreviewServer)
    server._screen_ref = ['old']
    server.set_screen_text(None)
    assert server._screen_ref[0] == ''


def test_screen_page_escapes_terminal_content():
    """The screen can contain anything a program printed, including markup."""
    page = _render_beam_page('<script>alert(1)</script>', title='Screen text')
    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;' in page
    assert 'Screen text' in page


def test_copy_mode_fills_the_tmux_buffer(monkeypatch):
    from text_actions_mixin import TextActionsMixin

    calls = []
    monkeypatch.setattr('text_actions_mixin.subprocess.run',
                        lambda cmd, **kw: calls.append(cmd))

    app = TextActionsMixin()
    app._use_tmux = True
    app._copy_to_tmux_buffer('some selected text')
    assert calls and calls[0][:4] == ['tmux', 'set-buffer', '-b', 'eink']
    # `--` so a selection starting with a dash isn't read as an option.
    assert '--' in calls[0]
    assert calls[0][-1] == 'some selected text'


def test_no_tmux_buffer_without_tmux(monkeypatch):
    from text_actions_mixin import TextActionsMixin

    calls = []
    monkeypatch.setattr('text_actions_mixin.subprocess.run',
                        lambda cmd, **kw: calls.append(cmd))
    app = TextActionsMixin()
    app._use_tmux = False
    app._copy_to_tmux_buffer('text')
    assert calls == []


def test_empty_selection_is_not_copied(monkeypatch):
    from text_actions_mixin import TextActionsMixin

    calls = []
    monkeypatch.setattr('text_actions_mixin.subprocess.run',
                        lambda cmd, **kw: calls.append(cmd))
    app = TextActionsMixin()
    app._use_tmux = True
    app._copy_to_tmux_buffer('')
    assert calls == []
