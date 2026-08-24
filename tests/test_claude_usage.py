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


# ── where the panel lands on the lock screen ──────────────────────────────────

def _usage_fixture():
    return {'5h': {'messages': 12, 'sent': 1000, 'generated': 100,
                   'cached': 5000, 'top_project': 'terminal-display'},
            '7d': {'messages': 300, 'sent': 90000, 'generated': 9000,
                   'cached': 500000, 'top_project': 'crypto'}}


# A config whose timezone can't be resolved yields no week percentage, so the
# baseline frame carries neither the week box nor the week bar — leaving the
# diff to show only what the activity panel itself drew.
NO_WEEK = {'font_path': '', 'timezone': 'Not/AZone'}


def _blank_like(img):
    """A blank frame the same size — for "was anything drawn here at all"."""
    from PIL import Image
    return Image.new(img.mode, img.size, color=0)


def _changed_pixels(with_panel, without_panel):
    width, height = with_panel.size
    return [(x, y) for y in range(height) for x in range(width)
            if with_panel.getpixel((x, y)) != without_panel.getpixel((x, y))]


def test_panel_is_drawn_in_the_bottom_right():
    """Bottom-right, clear of the week-progress box in the top-left."""
    from render import H, W, render_screensaver

    config = dict(NO_WEEK, screensaver_show_qr=False)
    plain = render_screensaver('', '', config)
    panelled = render_screensaver('', '', config, claude_usage=_usage_fixture())
    changed = _changed_pixels(panelled, plain)

    assert changed, 'the panel drew nothing'
    assert min(x for x, _y in changed) > W // 2
    assert min(y for _x, y in changed) > H // 2


def test_qr_can_be_hidden():
    from render import render_screensaver

    url = 'http://192.168.1.2:8080/config'
    shown = render_screensaver('', url, dict(NO_WEEK, screensaver_show_qr=True))
    hidden = render_screensaver('', url, dict(NO_WEEK, screensaver_show_qr=False))
    assert _changed_pixels(shown, hidden), 'screensaver_show_qr changed nothing'
    assert not _changed_pixels(hidden, render_screensaver('', '', NO_WEEK))


def test_panel_stacks_above_the_qr_when_both_are_shown():
    """Both want the same corner. With the QR on, the panel sits above it
    rather than over it."""
    from render import H, W, render_screensaver

    config = dict(NO_WEEK, screensaver_show_qr=True)
    url = 'http://192.168.1.2:8080/config'
    bare = render_screensaver('', '', config)
    with_qr = render_screensaver('', url, config)
    both = render_screensaver('', url, config, claude_usage=_usage_fixture())

    changed = _changed_pixels(both, with_qr)
    assert changed, 'the panel drew nothing'
    qr_top = min(y for _x, y in _changed_pixels(with_qr, bare))
    assert max(y for _x, y in changed) < qr_top, 'panel overlaps the wake QR'
    assert min(x for x, _y in changed) > W // 2
    assert min(y for _x, y in changed) > H // 4


def test_no_usage_means_no_panel():
    from render import render_screensaver

    assert not _changed_pixels(render_screensaver('', '', NO_WEEK, claude_usage={}),
                               render_screensaver('', '', NO_WEEK))


def test_week_bar_is_folded_into_the_panel():
    """One combined tile: with the panel up, the week bar rides inside it and
    the old top-left box is gone."""
    from render import H, W, render_screensaver

    config = {'font_path': '', 'screensaver_show_qr': False}   # week resolves
    with_panel = render_screensaver('', '', config, claude_usage=_usage_fixture())
    no_week = render_screensaver('', '', dict(config, timezone='Not/AZone'),
                                 claude_usage=_usage_fixture())

    # Nothing is drawn in the top-left corner any more...
    corner = [(x, y) for x, y in _changed_pixels(with_panel,
                                                 _blank_like(with_panel))
              if x < W // 3 and y < H // 3]
    assert not corner, 'the week box is still drawn top-left'
    # ...and the week bar shows up inside the bottom-right tile.
    added = _changed_pixels(with_panel, no_week)
    assert added, 'the week bar was not drawn at all'
    assert min(x for x, _y in added) > W // 2
    assert min(y for _x, y in added) > H // 2


def test_week_box_survives_when_the_panel_is_off():
    """Turning the activity panel off must not take the week bar with it."""
    from render import H, W, render_screensaver

    config = {'font_path': '', 'screensaver_show_qr': False}
    without_panel = render_screensaver('', '', config)
    changed = _changed_pixels(without_panel,
                              render_screensaver('', '', dict(config, timezone='Not/AZone')))
    assert changed, 'no week box drawn'
    assert max(x for x, _y in changed) < W // 2
    assert max(y for _x, y in changed) < H // 2
