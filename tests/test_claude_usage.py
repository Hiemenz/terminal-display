"""Recent Claude Code activity, read from its local session transcripts.

The real 5-hour/weekly limits are enforced server-side and written nowhere on
disk, so this is explicitly an activity gauge, not a quota reading — the tests
pin the arithmetic and the window boundaries, which is what the panel claims.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from claude_usage import (  # noqa: E402
    _parse_timestamp,
    collect_usage,
    format_tokens,
    summary_lines,
    weekly_baseline,
    weekly_percent,
    weekly_totals,
)

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


# ── "used N%" for the week ────────────────────────────────────────────────────

def test_percent_of_a_configured_budget():
    usage = {'7d': {'sent': 400_000, 'generated': 100_000}}
    assert weekly_percent(usage, budget=1_000_000) == (50.0, 'budget')


def test_percent_of_your_own_usual_week_when_no_budget():
    """No budget set, so the yardstick is what recent weeks looked like."""
    usage = {'7d': {'sent': 900_000, 'generated': 100_000}}
    pct, of_what = weekly_percent(usage, budget=0, baseline=500_000)
    assert (round(pct), of_what) == (200, 'usual')


def test_budget_wins_over_baseline():
    usage = {'7d': {'sent': 100, 'generated': 0}}
    assert weekly_percent(usage, budget=1000, baseline=50)[1] == 'budget'


def test_no_yardstick_means_no_percentage():
    """Better no number than a number measured against nothing."""
    assert weekly_percent({'7d': {'sent': 5, 'generated': 5}}, 0, 0) == (None, '')


def test_baseline_ignores_the_week_in_progress(tmp_path):
    """A partial week would drag the average down and inflate today's figure."""
    now = _parse_timestamp('2026-08-24T12:00:00Z')
    entries = [
        _message('2026-08-24T11:00:00Z', sent=999_999),          # this week
        {'timestamp': '2026-08-14T12:00:00Z', 'cwd': '/w',       # ~1.5 weeks ago
         'message': {'usage': {'input_tokens': 1000, 'output_tokens': 0}}},
    ]
    path = _write_transcript(tmp_path, 'proj', entries, mtime=now)
    assert path.exists()
    assert weekly_baseline(str(tmp_path), now=now, weeks=4) == 1000


def test_baseline_averages_only_weeks_with_activity(tmp_path):
    """Idle weeks are not evidence of a light workload; averaging zeros in
    would make any active week look enormous."""
    now = _parse_timestamp('2026-08-24T12:00:00Z')
    entries = [
        {'timestamp': '2026-08-14T12:00:00Z', 'cwd': '/w',
         'message': {'usage': {'input_tokens': 2000, 'output_tokens': 0}}},
        {'timestamp': '2026-08-07T12:00:00Z', 'cwd': '/w',
         'message': {'usage': {'input_tokens': 4000, 'output_tokens': 0}}},
    ]
    _write_transcript(tmp_path, 'proj', entries, mtime=now)
    assert weekly_baseline(str(tmp_path), now=now, weeks=4) == 3000


def test_percentage_row_reaches_the_tile():
    from render import render_screensaver

    usage = dict(_usage_fixture(), week_pct=175.0, week_pct_of='usual')
    with_pct = render_screensaver('', '', NO_WEEK, claude_usage=usage)
    without = render_screensaver('', '', NO_WEEK, claude_usage=_usage_fixture())
    assert _changed_pixels(with_pct, without), 'the used% row was not drawn'


# ── the 4-week history row ────────────────────────────────────────────────────

def test_weekly_totals_are_newest_first(tmp_path):
    now = _parse_timestamp('2026-08-24T12:00:00Z')
    day = 24 * HOUR

    def at(days_ago, tokens):
        when = now - days_ago * day
        import datetime
        stamp = datetime.datetime.fromtimestamp(
            when, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        return {'timestamp': stamp, 'cwd': '/w',
                'message': {'usage': {'input_tokens': tokens, 'output_tokens': 0}}}

    _write_transcript(tmp_path, 'proj', [
        at(3, 500),       # this week — excluded
        at(9, 1000),      # 1 week ago
        at(16, 2000),     # 2 weeks ago
        at(23, 3000),     # 3 weeks ago
        at(30, 4000),     # 4 weeks ago
    ], mtime=now)

    assert weekly_totals(str(tmp_path), now=now, weeks=4) == [1000, 2000, 3000, 4000]


def test_baseline_can_reuse_totals_without_rescanning(tmp_path):
    """The panel needs both numbers; scanning ~130 MB twice for them is waste."""
    assert weekly_baseline(totals=[1000, 3000, 0, 0]) == 2000


def test_history_row_reaches_the_tile():
    from render import render_screensaver

    usage = dict(_usage_fixture(), week_totals=[16_000_000, 20_700_000,
                                                23_600_000, 12_900_000],
                 week_avg=18_300_000)
    with_history = render_screensaver('', '', NO_WEEK, claude_usage=usage)
    without = render_screensaver('', '', NO_WEEK, claude_usage=_usage_fixture())
    assert _changed_pixels(with_history, without), 'the 4-week row was not drawn'


def test_this_week_is_shown_as_one_comparable_number():
    """The 7 d row splits in/out while the history is combined totals — the
    percentage line repeats this week as a single figure so the comparison
    doesn't require mental arithmetic."""
    from render import render_screensaver

    usage = dict(_usage_fixture(), week_pct=173.0, week_pct_of='usual')
    drawn = render_screensaver('', '', NO_WEEK, claude_usage=usage)
    plain = render_screensaver('', '', NO_WEEK,
                               claude_usage=dict(_usage_fixture()))
    assert _changed_pixels(drawn, plain)
