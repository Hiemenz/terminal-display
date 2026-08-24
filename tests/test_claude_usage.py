"""Recent Claude Code activity, read from its local session transcripts.

The real 5-hour/weekly limits are enforced server-side and written nowhere on
disk, so this is explicitly an activity gauge, not a quota reading — the tests
pin the arithmetic and the window boundaries, which is what the panel claims.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from claude_usage import _parse_timestamp, collect_usage, format_tokens, summary_lines  # noqa: E402

HOUR = 3600


def _write_transcript(tmp_path, project, entries, mtime=None):
    project_dir = tmp_path / project
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / 'session.jsonl'
    with open(path, 'w') as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + '\n')
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _message(when_iso, sent=0, generated=0, cached=0, cwd='/home/pi/work'):
    return {
        'timestamp': when_iso,
        'cwd': cwd,
        'message': {'usage': {'input_tokens': sent,
                              'cache_creation_input_tokens': 0,
                              'output_tokens': generated,
                              'cache_read_input_tokens': cached}},
    }


def test_counts_only_messages_inside_the_window(tmp_path):
    now = _parse_timestamp('2026-08-24T12:00:00Z')
    _write_transcript(tmp_path, 'proj', [
        _message('2026-08-24T11:00:00Z', sent=100, generated=10),   # 1 h ago
        _message('2026-08-24T05:00:00Z', sent=999, generated=99),   # 7 h ago
    ], mtime=now)
    usage = collect_usage(str(tmp_path), now=now)
    assert usage['5h']['messages'] == 1
    assert usage['5h']['sent'] == 100
    assert usage['7d']['messages'] == 2
    assert usage['7d']['sent'] == 1099


def test_cache_reads_are_kept_separate(tmp_path):
    """Cache reads dwarf everything else and bill at a fraction of the rate;
    folding them into 'sent' would make the number meaningless."""
    now = _parse_timestamp('2026-08-24T12:00:00Z')
    _write_transcript(tmp_path, 'proj', [
        _message('2026-08-24T11:00:00Z', sent=10, generated=5, cached=500_000),
    ], mtime=now)
    usage = collect_usage(str(tmp_path), now=now)
    assert usage['5h']['sent'] == 10
    assert usage['5h']['cached'] == 500_000


def test_cache_creation_counts_as_sent(tmp_path):
    now = _parse_timestamp('2026-08-24T12:00:00Z')
    entry = _message('2026-08-24T11:30:00Z', sent=7, generated=1)
    entry['message']['usage']['cache_creation_input_tokens'] = 3000
    _write_transcript(tmp_path, 'proj', [entry], mtime=now)
    usage = collect_usage(str(tmp_path), now=now)
    assert usage['5h']['sent'] == 3007


def test_old_files_are_skipped_without_reading(tmp_path):
    """The corpus is ~130 MB; anything last written before the widest window
    can't hold a message inside it and must never be opened."""
    now = _parse_timestamp('2026-08-24T12:00:00Z')
    path = _write_transcript(tmp_path, 'old', [
        _message('2026-08-24T11:00:00Z', sent=100),   # inside the window...
    ], mtime=now - 30 * 24 * HOUR)                    # ...but the file looks ancient
    assert path.exists()
    usage = collect_usage(str(tmp_path), now=now)
    assert usage['7d']['messages'] == 0


def test_busiest_project_uses_the_real_cwd(tmp_path):
    """The transcript directory is a cwd with slashes turned into dashes,
    which can't be split back apart when the project name has one."""
    now = _parse_timestamp('2026-08-24T12:00:00Z')
    _write_transcript(tmp_path, '-home-pi-terminal-display', [
        _message('2026-08-24T11:00:00Z', sent=500, cwd='/home/pi/terminal-display'),
    ], mtime=now)
    _write_transcript(tmp_path, '-home-pi-git-crypto', [
        _message('2026-08-24T11:00:00Z', sent=10, cwd='/home/pi/git/crypto'),
    ], mtime=now)
    usage = collect_usage(str(tmp_path), now=now)
    assert usage['5h']['top_project'] == 'terminal-display'


def test_malformed_lines_do_not_break_the_scan(tmp_path):
    now = _parse_timestamp('2026-08-24T12:00:00Z')
    project_dir = tmp_path / 'proj'
    project_dir.mkdir()
    path = project_dir / 'session.jsonl'
    path.write_text('\n'.join([
        'not json at all "usage"',
        json.dumps({'message': {'usage': 'not a dict'}, 'timestamp': '2026-08-24T11:00:00Z'}),
        json.dumps(_message('2026-08-24T11:00:00Z', sent=42)),
        json.dumps({'message': {'usage': {'input_tokens': 5}}}),   # no timestamp
    ]) + '\n')
    os.utime(path, (now, now))
    usage = collect_usage(str(tmp_path), now=now)
    assert usage['5h']['sent'] == 42
    assert usage['5h']['messages'] == 1


def test_missing_directory_is_not_an_error(tmp_path):
    usage = collect_usage(str(tmp_path / 'nope'), now=0.0)
    assert usage['5h']['messages'] == 0
    assert usage['5h']['top_project'] == ''


def test_token_formatting():
    assert format_tokens(950) == '950'
    assert format_tokens(12_400) == '12k'
    assert format_tokens(3_450_000) == '3.5M'


def test_summary_lines_cover_both_windows(tmp_path):
    now = _parse_timestamp('2026-08-24T12:00:00Z')
    _write_transcript(tmp_path, 'proj', [
        _message('2026-08-24T11:00:00Z', sent=1000, generated=100),
    ], mtime=now)
    lines = summary_lines(collect_usage(str(tmp_path), now=now))
    assert len(lines) == 2
    assert 'last 5 h' in lines[0]
    assert 'last 7 d' in lines[1]
