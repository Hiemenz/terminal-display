"""Tests for the /notes preview-server endpoint — raw view of the notes file
(reuses the /beam copy-button page), gated by the same PIN as /beam and
/clipboard. See _get_notes_path / _read_notes / _render_beam_page."""
import os

import yaml

from preview_server import _GATED_HTML_GET, _get_notes_path, _read_notes, _render_beam_page


def test_notes_is_gated():
    assert '/notes' in _GATED_HTML_GET


def test_notes_page_title_and_escaping():
    html = _render_beam_page('hello <b>world</b>', title='Notes')
    assert '<title>Notes</title>' in html
    assert '<h1>Notes</h1>' in html
    assert '&lt;b&gt;' in html


def test_get_notes_path_defaults_without_config(tmp_path):
    path = _get_notes_path('')
    assert path.endswith(os.path.join('data', 'notes.txt'))


def test_get_notes_path_reads_relative_config_value(tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump({'terminal_notes_file': 'data/mynotes.txt'}))

    path = _get_notes_path(str(config_path))

    assert path.endswith(os.path.join('data', 'mynotes.txt'))
    assert os.path.isabs(path)


def test_get_notes_path_honors_absolute_config_value(tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump({'terminal_notes_file': '/tmp/abs-notes.txt'}))

    assert _get_notes_path(str(config_path)) == '/tmp/abs-notes.txt'


def test_read_notes_returns_file_contents(tmp_path):
    notes_file = tmp_path / 'notes.txt'
    notes_file.write_text('remember the milk\n')
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump({'terminal_notes_file': str(notes_file)}))

    assert _read_notes(str(config_path)) == 'remember the milk\n'


def test_read_notes_missing_file_returns_empty(tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump({'terminal_notes_file': str(tmp_path / 'nope.txt')}))

    assert _read_notes(str(config_path)) == ''
