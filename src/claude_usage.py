"""Summarize recent Claude Code activity from its local session transcripts.

Claude Code's real 5-hour and weekly limits are enforced server-side and are
not written anywhere on disk — `/usage` fetches them live. What *is* on disk
is every session transcript, with a timestamp and a token count per message.
So this reports activity, not quota: how much work has gone through in the
last few hours and the last week. The panel labels it as an estimate for
exactly that reason.

Reading is kept cheap enough to run on a Pi behind a cache: files whose mtime
predates the window are skipped without opening, and lines are filtered by
substring before any JSON parsing.
"""
from __future__ import annotations

import glob
import json
import os
import time
from datetime import datetime, timezone

FIVE_HOURS = 5 * 3600
ONE_WEEK = 7 * 24 * 3600
BASELINE_WEEKS = 4

DEFAULT_PROJECTS_DIR = os.path.expanduser('~/.claude/projects')


def _parse_timestamp(value: str) -> float:
    """ISO-8601 (with a trailing Z) → epoch seconds. 0.0 when unparseable."""
    if not value:
        return 0.0
    try:
        text = value.replace('Z', '+00:00')
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() \
            if datetime.fromisoformat(text).tzinfo is None \
            else datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _iter_messages(projects_dir: str, since: float):
    """Yield (epoch, usage dict, project name) for messages newer than `since`."""
    pattern = os.path.join(projects_dir, '*', '*.jsonl')
    for path in glob.glob(pattern):
        try:
            # A transcript last written before the window can't hold a message
            # inside it, so it never gets opened. That skips most of the corpus.
            if os.path.getmtime(path) < since:
                continue
        except OSError:
            continue
        fallback = os.path.basename(os.path.dirname(path))
        try:
            with open(path, errors='replace') as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    usage = (entry.get('message') or {}).get('usage')
                    if not isinstance(usage, dict):
                        continue
                    when = _parse_timestamp(entry.get('timestamp', ''))
                    if when >= since:
                        # The directory name is the cwd with slashes turned
                        # into dashes, which can't be split back apart when
                        # the project name itself has one ("terminal-display").
                        # Each entry carries the real cwd, so use that.
                        cwd = entry.get('cwd') or ''
                        yield (when, usage,
                               os.path.basename(cwd.rstrip('/')) or fallback)
        except OSError:
            continue


def _blank_window() -> dict:
    return {'messages': 0, 'sent': 0, 'generated': 0, 'cached': 0, 'projects': {}}


def collect_usage(projects_dir: str = None, now: float = None,
                  windows: dict = None) -> dict:
    """Token activity per time window.

    'sent' is input + cache-creation — the tokens actually written up to the
    model. 'cached' (cache reads) is counted separately because it dwarfs
    everything else and is billed at a fraction of the rate, so folding it in
    would make the numbers meaningless.
    """
    projects_dir = projects_dir or DEFAULT_PROJECTS_DIR
    now = time.time() if now is None else now
    windows = windows or {'5h': FIVE_HOURS, '7d': ONE_WEEK}

    result = {name: _blank_window() for name in windows}
    oldest = now - max(windows.values()) if windows else now

    for when, usage, project in _iter_messages(projects_dir, oldest):
        sent = (int(usage.get('input_tokens') or 0)
                + int(usage.get('cache_creation_input_tokens') or 0))
        generated = int(usage.get('output_tokens') or 0)
        cached = int(usage.get('cache_read_input_tokens') or 0)
        for name, span in windows.items():
            if when < now - span:
                continue
            bucket = result[name]
            bucket['messages'] += 1
            bucket['sent'] += sent
            bucket['generated'] += generated
            bucket['cached'] += cached
            bucket['projects'][project] = bucket['projects'].get(project, 0) + sent

    for bucket in result.values():
        bucket['top_project'] = _busiest_project(bucket['projects'])
    return result


def _busiest_project(projects: dict) -> str:
    if not projects:
        return ''
    return max(projects.items(), key=lambda kv: kv[1])[0]


def format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return '%.1fM' % (count / 1_000_000)
    if count >= 1_000:
        return '%.0fk' % (count / 1_000)
    return str(int(count))


def summary_lines(usage: dict) -> list:
    """Two compact lines for the lock screen, plus a header."""
    lines = []
    for label, key in (('last 5 h', '5h'), ('last 7 d', '7d')):
        bucket = usage.get(key)
        if not bucket:
            continue
        lines.append('%-9s %5d msgs   %6s sent   %6s out'
                     % (label, bucket['messages'],
                        format_tokens(bucket['sent']),
                        format_tokens(bucket['generated'])))
    return lines


def weekly_percent(usage: dict, budget: int = 0, baseline: int = 0) -> tuple:
    """(percent, what it is a percent OF) for the week's token use, or (None, '').

    There is no local source for the real weekly limit — it lives server-side
    — so a percentage has to be measured against something we can actually
    know. Either the budget the user set, or, failing that, what their own
    recent weeks looked like. The label says which, because "78%" means very
    different things in those two cases.
    """
    week = (usage or {}).get('7d') or {}
    used = week.get('sent', 0) + week.get('generated', 0)
    if budget > 0:
        return (100.0 * used / budget, 'budget')
    if baseline > 0:
        return (100.0 * used / baseline, 'usual')
    return (None, '')


def weekly_baseline(projects_dir: str = None, now: float = None,
                    weeks: int = BASELINE_WEEKS) -> int:
    """Average tokens per week over the preceding `weeks` weeks, excluding the
    current one — the yardstick for "is this a heavy week for me?".
    """
    projects_dir = projects_dir or DEFAULT_PROJECTS_DIR
    now = time.time() if now is None else now
    totals = [0] * weeks
    for when, usage, _project in _iter_messages(projects_dir, now - (weeks + 1) * ONE_WEEK):
        age = now - when
        if age < ONE_WEEK:
            continue            # the week in progress isn't a full week yet
        index = int(age // ONE_WEEK) - 1
        if 0 <= index < weeks:
            totals[index] += (int(usage.get('input_tokens') or 0)
                              + int(usage.get('cache_creation_input_tokens') or 0)
                              + int(usage.get('output_tokens') or 0))
    counted = [t for t in totals if t > 0]
    return int(sum(counted) / len(counted)) if counted else 0
